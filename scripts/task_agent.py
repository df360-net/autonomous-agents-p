"""
task_agent.py — task the agent from a terminal instead of the Roundcube UI, and wait for
the reply. Same path the browser takes (SMTP in, IMAP out), so a green run here proves the
whole loop, not just the brain.

    python scripts/task_agent.py "Build a tic-tac-toe web app" --body "Two players, one HTML file."

Points at the ports docker-compose publishes on the Zeenie host (1025/1143), so it runs from
this laptop. Override with --host / --smtp-port / --imap-port.
"""

import argparse
import email
import email.utils
import imaplib
import smtplib
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
p.add_argument("--host", default="192.168.0.21")
p.add_argument("--smtp-port", type=int, default=1025)
p.add_argument("--imap-port", type=int, default=1143)
p.add_argument("--boss", default="boss@agents.local")
p.add_argument("--boss-password", required=True)
p.add_argument("--agent", default="agent1@agents.local")
p.add_argument("--wait", type=int, default=900, help="seconds to wait for the reply")
args = p.parse_args()

msg = EmailMessage()
msg["From"] = args.boss
msg["To"] = args.agent
msg["Subject"] = args.subject
msg["Message-ID"] = email.utils.make_msgid(domain="agents.local")
msg["Date"] = email.utils.formatdate(localtime=True)
msg.set_content(args.body or args.subject)
sent_id = msg["Message-ID"]

with smtplib.SMTP(args.host, args.smtp_port, timeout=30) as s:
    s.send_message(msg)
print(f"sent {sent_id} -> {args.agent}: {args.subject!r}")

deadline = time.time() + args.wait
print(f"waiting up to {args.wait}s for the reply...", flush=True)
while time.time() < deadline:
    time.sleep(10)
    try:
        with imaplib.IMAP4(args.host, args.imap_port) as imap:
            imap.login(args.boss, args.boss_password)
            imap.select("INBOX")
            typ, data = imap.search(None, "ALL")
            for uid in reversed((data[0] or b"").split()):
                typ, d = imap.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not isinstance(d[0], tuple):
                    continue
                reply = email.message_from_bytes(d[0][1])
                if reply.get("In-Reply-To", "").strip() == sent_id:
                    body = reply.get_payload(decode=True).decode("utf-8", "replace")
                    print("\n" + "=" * 70)
                    print(f"REPLY from {reply.get('From')}: {reply.get('Subject')}")
                    print("=" * 70 + "\n" + body)
                    sys.exit(0)
    except Exception as e:
        print(f"  (poll error: {e})", flush=True)
    print(f"  ...{int(deadline - time.time())}s left", flush=True)

sys.exit("No reply arrived in time — check `docker compose logs agent-worker`.")
