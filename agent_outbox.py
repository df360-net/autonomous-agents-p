"""agent_outbox.py — where answers go. SMTP today; something else later.

The mirror of agent_inbox. `deliver()` takes a TaskEnvelope and a body and puts the reply on
the wire, threading it under whatever conversation the envelope names. The worker composes the
message; this module knows how to send one.

`send_mail` stays a public primitive because a SECOND identity uses it: the reviewer signs off
from its own mailbox, so the sign-off visibly is not the worker grading its own homework.
"""

import email.utils
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage

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


def log(msg):
    print(f"[outbox] {msg}", flush=True)


def tls_context():
    # See the note in agent_inbox: each transport configures its own.
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def send_mail(to, subject, body, from_name, from_addr, in_reply_to=None, references=None,
              attachments=None):
    """Put one message on the wire and return its Message-ID.

    The Message-ID comes back so the reviewer's note can be threaded underneath the worker's
    reply rather than floating loose in the inbox.
    """
    reply = EmailMessage()
    reply["Subject"] = subject
    reply["From"] = f"{from_name} <{from_addr}>"
    reply["To"] = to
    reply["Date"] = email.utils.formatdate(localtime=True)
    mid = email.utils.make_msgid(domain=from_addr.split("@")[-1])
    reply["Message-ID"] = mid
    if in_reply_to:
        reply["In-Reply-To"] = in_reply_to
        reply["References"] = " ".join(filter(None, [references, in_reply_to]))
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
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(reply)
    log(f"sent {from_addr} -> {to}: {subject!r}")
    return mid


def _re(subject):
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def deliver(envelope, body, attachments=None):
    """The worker's answer, threaded under the request that asked for it."""
    return send_mail(
        to=envelope.reply_to or envelope.requester,
        subject=_re(envelope.subject or "(no subject)"),
        body=body,
        from_name=AGENT_NAME,
        from_addr=AGENT_ADDRESS,
        in_reply_to=envelope.message_id,
        references=envelope.references,
        attachments=attachments,
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
