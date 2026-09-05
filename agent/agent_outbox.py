"""agent_outbox.py — where answers go. SMTP today; something else later.

The mirror of agent_inbox. `deliver()` takes a TaskEnvelope and a body and puts the reply on
the wire, threading it under whatever conversation the envelope names. The worker composes the
message; this module knows how to send one.

`send_mail` stays a public primitive because a SECOND identity uses it: the reviewer signs off
from its own mailbox, so the sign-off visibly is not the worker grading its own homework.

EVERY message this fleet sends copies BOSS_ADDRESS. The rule is enforced here, in the one
function all of them pass through, rather than at the three call sites that exist today —
because the fourth one somebody adds would be the one that silently isn't copied, and nobody
would notice until they went looking for a conversation that was never in their inbox.
"""

import email.utils
import mimetypes
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

import fleet_identity

SMTP_HOST = os.environ.get("SMTP_HOST", "mailserver")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SMTP_SSL = os.environ.get("SMTP_SSL", "false").lower() == "true"
# Sending is a separate credential from reading: inside the compose bridge the mail server
# relays for trusted containers with no auth at all (port 25), so SMTP_USER stays empty.
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# Defaults OFF: the LAN mail server has no real certificate. See the note in agent_inbox.
TLS_VERIFY = os.environ.get("TLS_VERIFY", "false").lower() == "true"

AGENT_NAME = fleet_identity.NAME
AGENT_ADDRESS = fleet_identity.AGENT_ADDRESS
VALIDATOR_NAME = fleet_identity.VALIDATOR_NAME
VALIDATOR_ADDRESS = fleet_identity.VALIDATOR_ADDRESS


def smtp_identity_warning(user=None, validator=None):
    """The one way this agent's mail credential can be wrong, said at startup instead of at send.

    validatorN@ is a send-as ALIAS for agentN's login — there is no validator account and no
    validator password. Configuring SMTP_USER as a validator address therefore cannot work, and
    with SPOOF_PROTECTION=1 on the server it fails at LOGIN, which is the worst moment: the
    task is finished, the reply is composed, and the only symptom is an auth error that reads
    like a wrong password rather than a wrong username.

    Returns a warning line or "". A warning, not an exception — a fleet that is merely
    misconfigured should still start and say so, the same way ALLOWED_SENDERS=* does.
    """
    user = (SMTP_USER if user is None else user).strip()
    validator = (VALIDATOR_ADDRESS if validator is None else validator).strip()
    if user and validator and user.lower() == validator.lower():
        return (f"WARNING: SMTP_USER is {user}, but that is a send-as alias, not an account. "
                f"There is no mailbox or password for it. Authenticate as "
                f"{AGENT_ADDRESS} and use {validator} only in the From header.")
    return ""

# COPIED ON EVERY EMAIL THIS FLEET SENDS. Not just agent-to-agent: task replies, reviewer
# sign-offs, everything. It lives HERE, at the one function every outbound message passes
# through, rather than at each call site — a call site can be forgotten, and the next one
# added would silently not be copied. There is no argument that disables it.
BOSS_ADDRESS = os.environ.get("BOSS_ADDRESS", "boss@agents.local").strip()


def log(msg):
    print(f"[outbox] {msg}", flush=True)


def tls_context():
    # See the note in agent_inbox: each transport configures its own.
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _one_line(value):
    """Any header value, collapsed to a single line.

    Belt to agent_inbox's braces. A linefeed in a header value raises when the message is
    built, which happens AFTER the agent has done the work and spent the money — so the whole
    reply is lost to a formatting detail. Every value that reaches a header goes through here,
    because the next one to arrive folded will be one nobody thought about.
    """
    return " ".join((value or "").split()) or None


def _with_boss(to, cc):
    """The Cc line, with the boss on it. Always.

    Skipped only when he is already a recipient, because two copies of the same message in one
    inbox is worse oversight than one — the duplicate teaches you to skim. Matching is on the
    bare address so `Boss <boss@agents.local>` and `boss@agents.local` count as the same
    person, which is exactly how they arrive from different senders.
    """
    if not BOSS_ADDRESS:
        return cc
    already = {parseaddr(a)[1].lower() or a.strip().lower()
               for a in (to or "").split(",") + (cc or "").split(",") if a.strip()}
    if parseaddr(BOSS_ADDRESS)[1].lower() in already:
        return cc
    return f"{cc}, {BOSS_ADDRESS}" if cc else BOSS_ADDRESS


#: An address only the sender can open: loopback by name or by number, and the container's own
#: hostname. `0.0.0.0` is in here because a server started on it gets reported as reachable there.
_UNREACHABLE_HOST = re.compile(
    r"""\bhttps?://(?P<host>localhost|127(?:\.\d+){3}|0\.0\.0\.0|\[::1\]|::1)"""
    r"""(?P<port>:\d+)?(?:/\S*)?""",
    re.IGNORECASE)

#: The line on which the model's prose stops and the runtime's record of what ran begins. Line
#: anchored, because these are emitted at column 0 by the report builders and a sentence that
#: merely mentions one should not move the boundary. agent_worker writes them; the constant
#: lives here because this module is what has to know where prose ends.
FILES_HEADER = "FILES WRITTEN"
RUN_HEADER = "EVERYTHING THAT WAS RUN"
REVIEWER_RUN_HEADER = "WHAT I RAN MYSELF"
EVIDENCE_HEADERS = (FILES_HEADER, RUN_HEADER, REVIEWER_RUN_HEADER)
_EVIDENCE_START = re.compile(r"^(?:" + "|".join(EVIDENCE_HEADERS) + ")", re.MULTILINE)

#: What the prose gets: a phrase that reads naturally in a sentence, because in prose the
#: sentence is the point — "I tested on a local preview inside this container" is what the
#: writer meant and what the reader needs.
PROSE_REPLACEMENT = "a local preview inside this container"


def _redaction(match):
    """What EVIDENCE gets: something that reads as an edit, and keeps host:port.

    A REDACTION THAT READS AS THE ORIGINAL TEXT IS A FABRICATION. Substituting the prose phrase
    into a command transcript produced

        curl -s a local preview inside this container | grep -c "Office Coffee Tracker"

    — a line that could never have executed, in the block whose entire purpose is to let a human
    confirm the reviewer ran what it claims. A reader who notices stops trusting the transcript;
    a reader who does not now believes a record of something nobody ran. The brackets are the
    whole fix: they say an edit happened, and the address is still named so the evidence keeps
    its meaning. It is also not clickable, which is why the raw address is not simply left.
    """
    return f"[{match.group('host')}{match.group('port') or ''} redacted]"


def redact_unreachable(text):
    """Machine-generated text with loopback addresses marked as redacted. Idempotent.

    Applied where a command is RECORDED, not where the message is sent — see `flatten` — so it
    runs BEFORE the transcript's per-line truncation. That ordering is the bug it also fixes: a
    command cut at 200 characters left `http://loc...`, which no longer matched, so the same
    email both redacted the address and printed it. A control a line break can defeat is not
    reliably a control.
    """
    return _UNREACHABLE_HOST.sub(_redaction, text or "")


def without_unreachable_urls(body):
    """The PROSE of a message, with any localhost URL replaced by a phrase. Returns (body, found).

    ENFORCED HERE, IN PYTHON, FOR THE REASON THE Cc RULE IS. The prompt tells the agent not to
    put a localhost address in an email, and an instruction is something a model can talk itself
    out of — the reviewer that shipped one wrote "the running preview (http://localhost:3000
    from inside this container...)", which is honest, careful, correctly disclaimed, and still
    an invitation. `localhost` names the READER's machine. Roundcube linkifies it, the operator
    clicks it, and gets a connection failure; the disclaimer beside a link loses to the link.

    PROSE ONLY, AND THAT LIMIT IS THE POINT. The argument for putting this at the chokepoint was
    that a model can talk itself out of an instruction. The appended evidence blocks are not
    written by a model — they are the runtime's own record — so the argument does not reach
    them, and a substitution that is right in a sentence is a different operation with a
    different meaning inside a transcript. Those are handled at capture, by `redact_unreachable`.

    REPLACED, NOT REFUSED. The sentence around it is usually worth keeping and the work is
    already done and paid for by the time a message is sent — bouncing the reply here would
    lose a good answer over a phrase in it. What is left says what the address was for.
    """
    if not body:
        return body, []
    boundary = _EVIDENCE_START.search(body)
    end = boundary.start() if boundary else len(body)
    prose, evidence = body[:end], body[end:]
    found = [m.group(0) for m in _UNREACHABLE_HOST.finditer(prose)]
    if not found:
        return body, []
    return _UNREACHABLE_HOST.sub(PROSE_REPLACEMENT, prose) + evidence, found


def new_message_id(from_addr=None):
    """A Message-ID minted BEFORE the message is built.

    Needed because an agent-to-agent signature covers the id, so the value has to exist before
    `send_mail` is called and be the same one that goes on the wire. Generating it inside
    send_mail would sign one id and send another, and the far end would reject every message
    with a signature that verified against nothing.
    """
    return email.utils.make_msgid(domain=(from_addr or AGENT_ADDRESS).split("@")[-1])


def send_mail(to, subject, body, from_name, from_addr, in_reply_to=None, references=None,
              attachments=None, cc=None, headers=None, message_id=None):
    """Put one message on the wire and return its Message-ID.

    The Message-ID comes back so the reviewer's note can be threaded underneath the worker's
    reply rather than floating loose in the inbox.

    `cc` and `headers` exist for agent-to-agent mail: the attestation headers ride along, and
    a caller may add further recipients. Both are set by agent_peer, never by anything the
    model can reach. BOSS_ADDRESS is added to every message regardless of either.
    """
    reply = EmailMessage()
    reply["Subject"] = _one_line(subject) or "(no subject)"
    reply["From"] = f"{from_name} <{from_addr}>"
    reply["To"] = to
    cc = _with_boss(to, cc)
    if cc:
        reply["Cc"] = cc
    reply["Date"] = email.utils.formatdate(localtime=True)
    mid = message_id or email.utils.make_msgid(domain=from_addr.split("@")[-1])
    reply["Message-ID"] = mid
    for key, value in (headers or {}).items():
        reply[key] = _one_line(value)
    in_reply_to, references = _one_line(in_reply_to), _one_line(references)
    if in_reply_to:
        reply["In-Reply-To"] = in_reply_to
        reply["References"] = " ".join(filter(None, [references, in_reply_to]))
    # THE LAST PLACE A LOCALHOST ADDRESS CAN BE STOPPED, and the same argument as the boss Cc
    # three lines up: every message the fleet sends passes through here, so the rule is applied
    # once rather than at the three composition sites that exist today.
    body, unreachable = without_unreachable_urls(body)
    if unreachable:
        log(f"removed {len(unreachable)} address(es) the recipient could not open: "
            + ", ".join(sorted(set(unreachable))))
    reply.set_content(body)
    for name, data in attachments or []:
        ctype, _ = mimetypes.guess_type(name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        reply.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    if SMTP_SSL:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60, context=tls_context())
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
    with smtp:
        smtp.ehlo()
        if not SMTP_SSL and smtp.has_extn("starttls"):
            smtp.starttls(context=tls_context())
            smtp.ehlo()
        if SMTP_USER:
            # ONE LOGIN, TWO FROM ADDRESSES — and that is now enforced by the mail server.
            #
            # This agent authenticates as agentN and sends as either agentN@ or validatorN@.
            # validatorN@ is a SEND-AS ALIAS for this same login, not an account: there is no
            # validator mailbox and no validator password. The server runs SPOOF_PROTECTION=1,
            # so it rejects a MAIL FROM the authenticated login does not own — sending as
            # another agent is refused (553), and logging IN as validatorN@ would fail outright
            # because no such account exists.
            #
            # SO DO NOT MAKE THE CREDENTIAL DEPEND ON from_addr. That refactor was actually
            # planned — "select credentials by From address", to allow per-validator accounts —
            # and it is dead work now that the alias does the same job with no new secrets. A
            # version that looked up a credential per sender would break every reviewer email
            # the moment it shipped, and the failure would arrive at SEND time, after the work
            # was finished and paid for.
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(reply)
    log(f"sent {from_addr} -> {to}: {subject!r}")
    return mid


def reply_subject(subject):
    """The subject a reply to `subject` will carry.

    Public because something OTHER than the reply now needs to predict it: the fleet control
    plane is told, at registration time, what subject the agent's eventual reply will have, so
    governance's "it's live" email can thread onto it. Computing that string in two places is
    how the follow-up ends up in its own conversation.
    """
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


_re = reply_subject          # the old private name, kept so existing callers do not move


def deliver(envelope, body, attachments=None, cc=None, headers=None, message_id=None):
    """The worker's answer, threaded under the request that asked for it.

    When the request came from a peer agent the caller passes the attestation headers and the
    boss's address, so the return leg is signed and witnessed exactly like the outbound one.
    An unsigned answer would be refused by the peer's own admission check.
    """
    return send_mail(
        to=envelope.reply_to or envelope.requester,
        subject=_re(envelope.subject or "(no subject)"),
        body=body,
        from_name=AGENT_NAME,
        from_addr=AGENT_ADDRESS,
        in_reply_to=envelope.message_id,
        references=envelope.references,
        attachments=attachments,
        cc=cc,
        headers=headers,
        message_id=message_id,
    )


def deliver_review(envelope, body, under):
    """The reviewer's own sign-off, from its own mailbox, threaded under the reply it approved.

    `under` is the worker reply's Message-ID rather than the original request's, so the two
    arrive nested in the order they happened.
    """
    return send_mail(
        to=envelope.reply_to or envelope.requester,
        subject=f"Reviewed: {envelope.subject}",
        body=body,
        from_name=VALIDATOR_NAME,
        from_addr=VALIDATOR_ADDRESS,
        in_reply_to=under,
        references=" ".join(filter(None, [envelope.references, envelope.message_id])),
    )
