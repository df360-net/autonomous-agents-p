"""agent_inbox.py — where tasks come from. IMAP today; something else later.

The seam is `fetch()`: it returns TaskEnvelopes and owns everything mail-shaped — the IMAP
connection, MIME decoding, the \\Seen flag, the Message-ID dedupe. Nothing above this module
sees a header, which is the whole point of D3: a broker can replace this file without touching
a single safety check.

AT-MOST-ONCE IS UNCHANGED AND STILL CORRECT. BODY.PEEK, then \\Seen set BEFORE the task runs,
then the Message-ID recorded on disk. A crash mid-build can never re-trigger the build, and a
restored mailbox or a lost flag cannot make the agent do the same job twice. That ordering is
deliberate and survives the refactor verbatim.

WHAT DOES NOT SURVIVE N CONSUMERS. IMAP's `STORE +FLAGS \\Seen` has no compare-and-swap, so two
processes on one mailbox can both SEARCH UNSEEN and both claim the same message. The guarantee
above holds for exactly one consumer per mailbox. Making it hold for more is the lease work in
D3/phase 3, not something a flag on a maildir can do.
"""

import email
import email.utils
import json
import os
import re
import ssl
from email.header import decode_header, make_header
from imaplib import IMAP4, IMAP4_SSL

import agent_budget
import agent_envelope
import fleet_identity

IMAP_HOST = os.environ.get("IMAP_HOST", "mailserver")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "143"))
IMAP_SSL = os.environ.get("IMAP_SSL", "false").lower() == "true"
# LAN-only MVP: the mail server has no real certificate. Opportunistic STARTTLS is still worth
# having, but it must not fail over an unverifiable self-signed cert. Defaults to OFF, as it
# always has — changing this default would silently break every connection on this box.
TLS_VERIFY = os.environ.get("TLS_VERIFY", "false").lower() == "true"

AGENT_ADDRESS = fleet_identity.AGENT_ADDRESS
AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "")

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(WORKSPACE_ROOT, ".processed.json"))

# Machine-to-machine headers. These are the TRANSPORT ENCODING of envelope fields, never the
# thing a decision is made on. With SPOOF_PROTECTION off and an open relay on the bridge,
# anything on the LAN can write them — which is exactly why they are lifted verbatim here and
# judged in agent_principal.admit(), the single place allowed to decide what any of it means.
HDR_AGENT_ID = "X-Agent-Id"
HDR_HOPS = "X-Agent-Hops"
HDR_PURPOSE = "X-Agent-Purpose"
HDR_SIGNATURE = "X-Agent-Signature"


def log(msg):
    print(f"[inbox] {msg}", flush=True)


def tls_context():
    # Each transport configures its own TLS. IMAP and SMTP are different protocols that happen
    # to read the same switch; sharing one helper across two modules would couple them for the
    # sake of five lines.
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---- Idempotency -------------------------------------------------------------
def load_processed():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_processed(ids):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, STATE_FILE)


# ---- Mail parsing ------------------------------------------------------------
def decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def plain_body(msg):
    """Best-effort plain text of an email: prefer text/plain, else de-tag the HTML."""
    def payload(part):
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                return payload(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", payload(part))
        return ""
    if msg.get_content_type() == "text/html":
        return re.sub(r"<[^>]+>", " ", payload(msg))
    return payload(msg)


def _one_line(value):
    """A header value collapsed to a single line.

    RFC 5322 lets a long header be FOLDED across several lines, and `msg.get()` hands the value
    back with those newlines still in it. That is fine to read and fatal to re-send: setting a
    header containing a linefeed raises, so the reply is lost after the work is already paid
    for. It bites on `References`, because that header grows by one message-id per exchange and
    is short enough to be safe right up until a conversation gets long — which is exactly when
    losing the reply costs the most. Found mid-way through a ten-round exchange between two
    agents, in a thread that had been fine for the first four.
    """
    return " ".join((value or "").split())


def slug(text, limit=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s[:limit] or "task").strip("-")


def task_id_for(seq, subject):
    """The task's name, which is also its workspace directory."""
    return f"task-{seq:04d}-{slug(subject)}"


def envelope_from_bytes(raw, seq):
    """Parse one RFC822 message into a TaskEnvelope. No network, so it is testable alone.

    This function is the entire mail-shaped surface of a task. Everything downstream reads the
    envelope, so anything that needs to be known about a request has to be lifted into a field
    here — which is a useful forcing function: a check that cannot be expressed on the envelope
    is a check that would not survive a change of transport.
    """
    msg = email.message_from_bytes(raw)
    subject = decode(msg.get("Subject")) or "(no subject)"
    sender = decode(msg.get("From"))
    try:
        hops = int((msg.get(HDR_HOPS) or "0").strip() or 0)
    except ValueError:
        hops = 0
    references = _one_line(msg.get("References"))
    message_id = _one_line(msg.get("Message-ID"))
    return agent_envelope.TaskEnvelope(
        task_id=task_id_for(seq, subject),
        tenant=fleet_identity.TENANT,
        agent_id=fleet_identity.AGENT_ID,
        source="email",
        requester=sender,
        reply_to=msg.get("Reply-To") or msg.get("From") or "",
        subject=subject,
        body=plain_body(msg).strip(),
        # The first id in References is the root of the conversation; with none, this message
        # starts one. That is the conversation identity a thread-depth guard will count on.
        thread_id=(references.split() or [message_id])[0],
        message_id=message_id,
        references=references,
        hops=hops,
        purpose=(msg.get(HDR_PURPOSE) or "").strip(),
        from_agent_id=(msg.get(HDR_AGENT_ID) or "").strip(),
        signature=(msg.get(HDR_SIGNATURE) or "").strip(),
        state="submitted",
    )


# ---- The seam ----------------------------------------------------------------
def _connect():
    if IMAP_SSL:
        return IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=tls_context())
    return IMAP4(IMAP_HOST, IMAP_PORT)


def fetch(processed, connect=_connect):
    """See below. `connect` is injectable purely so this can be tested without a mail server —
    the \\Seen ordering and the batch numbering are exactly the things worth testing, and until
    now neither could be exercised without Dovecot running."""
    return _fetch(processed, connect)


def _fetch(processed, connect):
    """Take every unread message off the inbox and return the envelopes worth running.

    Deliberately does NOT run them: a build takes minutes to tens of minutes, and Dovecot will
    close an idle IMAP connection out from under us long before a slow one finishes. Grab the
    mail, flag it, hang up — then build.
    """
    # The kill switch is checked here, before the connection, precisely so that a paused fleet
    # consumes nothing: no fetch, no \Seen, no dedupe entry. The backlog is untouched and
    # arrives in order once a human removes the file.
    if agent_budget.paused():
        log(f"PAUSED: {agent_budget.PAUSE_FILE} exists — not fetching mail. Remove it to resume.")
        return []
    imap = connect()
    fresh = []
    # Numbered from the history, then once per accepted message. It used to be read as
    # len(processed) AFTER the whole batch had been recorded, so two emails arriving in one
    # poll were both handed the same sequence number — and therefore the same workspace
    # directory, each overwriting the other's files.
    seq = len(processed)
    try:
        if not IMAP_SSL and "STARTTLS" in imap.capabilities:
            imap.starttls(ssl_context=tls_context())
        imap.login(AGENT_ADDRESS, AGENT_PASSWORD)
        # Check SELECT explicitly: imaplib returns ('NO', ...) instead of raising, and the
        # next SEARCH then fails with a baffling "illegal in state AUTH". Dovecot answers NO
        # for the first few seconds after it starts, before the maildir is ready.
        typ, data = imap.select("INBOX")
        if typ != "OK":
            log(f"INBOX not selectable yet ({data}) — will retry")
            return fresh
        typ, data = imap.search(None, "UNSEEN")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        if not uids:
            return fresh
        log(f"{len(uids)} unread message(s)")

        for uid in uids:
            # PEEK so fetching doesn't implicitly consume the message, then mark \Seen
            # ourselves BEFORE running it: at-most-once beats at-least-once when the
            # side effect is "an agent builds software and emails a human".
            typ, data = imap.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                log(f"could not fetch uid {uid!r}")
                continue
            raw = data[0][1]
            imap.store(uid, "+FLAGS", "\\Seen")

            mid = email.message_from_bytes(raw).get("Message-ID", "").strip()
            if mid and mid in processed:
                log(f"already handled {mid} — skipping")
                continue
            if mid:
                processed.add(mid)
                save_processed(processed)
            fresh.append(envelope_from_bytes(raw, seq))
            seq += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return fresh
