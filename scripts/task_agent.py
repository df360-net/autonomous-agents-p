"""
task_agent.py — task an agent from a terminal instead of the Roundcube UI, and wait for the
reply. It takes the same path the browser takes (submission in, IMAP out), so a green run here
proves the whole loop end to end and not just the brain.

    export BOSS_PASSWORD=...        # or --boss-password, but see the note below
    python scripts/task_agent.py "Build a tic-tac-toe web app" --body "Two players, one file."

Defaults point at the fleet mail server on hp-tiger. It used to point at 192.168.0.105:1025/1143
— ports the retired Compose stack published on zeenie, which no longer exists — and it sent
without authenticating, which the server now refuses outright.
"""

import argparse
import email
import email.utils
import imaplib
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage

# The agent writes arrows and box characters into its reports; a Windows console defaults to
# cp1252 and raises UnicodeEncodeError on them, losing the reply you just waited 10 min for.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

p = argparse.ArgumentParser()
p.add_argument("subject")
p.add_argument("--body", default="")
p.add_argument("--host", default="hp-tiger")
# 587 is SUBMISSION and requires credentials. Port 25 is inter-server only and will not relay
# for you: PERMIT_DOCKER was removed precisely so an unauthenticated sender cannot task an
# agent and spend its budget. 143 is IMAP with STARTTLS.
p.add_argument("--smtp-port", type=int, default=587)
p.add_argument("--imap-port", type=int, default=143)
p.add_argument("--boss", default="boss@agents.local")
# Prefer the environment. A password in argv is visible to every other process on the machine
# via the process list, and lands in shell history — for a mailbox that can task four agents
# with a real spend ceiling, that is not a footnote.
p.add_argument("--boss-password", default=os.environ.get("BOSS_PASSWORD", ""))
p.add_argument("--agent", default="agent1@agents.local")
p.add_argument("--wait", type=int, default=900, help="seconds to wait for the reply")
args = p.parse_args()

if not args.boss_password:
    sys.exit("No password. Set BOSS_PASSWORD in the environment, or pass --boss-password.")


def tls():
    """Unverified TLS, deliberately — the same choice the agents make.

    `agents.local` exists in no public DNS and the server's certificate is self-signed, so
    there is nothing to chain to. The value here is that the mailbox password does not cross
    the LAN in clear, not that the server's identity is proven. agent_inbox and agent_outbox
    both default TLS_VERIFY to false for exactly this reason.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


msg = EmailMessage()
msg["From"] = args.boss
msg["To"] = args.agent
msg["Subject"] = args.subject
msg["Message-ID"] = email.utils.make_msgid(domain="agents.local")
msg["Date"] = email.utils.formatdate(localtime=True)
msg.set_content(args.body or args.subject)
sent_id = msg["Message-ID"]

with smtplib.SMTP(args.host, args.smtp_port, timeout=30) as s:
    s.ehlo()
    s.starttls(context=tls())
    s.ehlo()
    # SPOOF_PROTECTION is on: the server rejects a MAIL FROM this login is not allowed to use,
    # so --boss and the credentials have to belong to each other. A 553 here means they do not.
    s.login(args.boss, args.boss_password)
    s.send_message(msg)
print(f"sent {sent_id} -> {args.agent}: {args.subject!r}")


def plain_text(m):
    """The readable part of a reply.

    Replies are usually MULTIPART, because the agent attaches whatever the task produced —
    calling get_payload(decode=True) on one of those returns None and this used to crash on
    exactly the runs that succeeded. Walk for the text part, and list what came with it.
    """
    body, attached = "", []
    if m.is_multipart():
        for part in m.walk():
            disp = str(part.get("Content-Disposition", ""))
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                if not body:
                    body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            elif part.get_filename():
                attached.append(part.get_filename())
    else:
        body = (m.get_payload(decode=True) or b"").decode("utf-8", "replace")
    return body, attached


deadline = time.time() + args.wait
print(f"waiting up to {args.wait}s for the reply...", flush=True)
while time.time() < deadline:
    time.sleep(10)
    try:
        with imaplib.IMAP4(args.host, args.imap_port) as imap:
            imap.starttls(ssl_context=tls())
            imap.login(args.boss, args.boss_password)
            imap.select("INBOX")
            # ASK THE SERVER, rather than fetching the mailbox and checking each one here. The
            # boss mailbox holds every message this fleet has ever sent — the old version
            # downloaded all of them every ten seconds to find one reply.
            typ, data = imap.search(None, "HEADER", "In-Reply-To", sent_id)
            uids = (data[0] or b"").split() if typ == "OK" else []
            for uid in reversed(uids):
                typ, d = imap.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not d or not isinstance(d[0], tuple):
                    continue
                reply = email.message_from_bytes(d[0][1])
                body, attached = plain_text(reply)
                print("\n" + "=" * 70)
                print(f"REPLY from {reply.get('From')}: {reply.get('Subject')}")
                if attached:
                    print(f"ATTACHED: {', '.join(attached)}")
                print("=" * 70 + "\n" + body)
                sys.exit(0)
    except Exception as e:
        print(f"  (poll error: {e})", flush=True)
    print(f"  ...{int(deadline - time.time())}s left", flush=True)

sys.exit(f"No reply arrived in {args.wait}s. The task may still be running — check the agent "
         f"with: kubectl logs -n fleet deploy/{args.agent.split('@')[0]} --tail 50")
