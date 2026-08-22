"""Exercise the worker's mail path with a fake SMTP server — no mail server, no API calls."""
import email
import os
import socket
import sys
import threading

PORT = 8025
os.environ.update({
    "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(PORT), "SMTP_USER": "",
    "AGENT_ADDRESS": "agent1@agents.local", "AGENT_NAME": "agent1",
    "WORKSPACE_ROOT": os.path.join(os.path.dirname(__file__), "ws"),
})
import tempfile as _tf
_SANDBOX = _tf.mkdtemp(prefix='sandbox-')
# NO TEST MAY TOUCH REAL FLEET STATE. The container sets FLEET_LEDGER and FLEET_PAUSE_FILE to
# a shared host directory, and a test that only overrides WORKSPACE_ROOT inherits them — so
# running this suite inside a container wrote a $4-ceiling trip into the production ledger and
# left FLEET-PAUSED behind, halting both live agents. The cascade was worse than the pause: the
# next suite's failures pointed at mail parsing, because agent_inbox.fetch() checks paused()
# and quietly returned nothing. Redirected, not popped, so a future default cannot leak either.
# FLEET_LEDGER points at the SAME file as SPEND_LEDGER, which is the module's own default
# (one agent => one ledger). The sandbox relocates the defaults; it must not invent different
# ones, or a suite written against single-agent semantics starts tripping a fleet ceiling that
# would never have fired in the configuration it is testing.
for _k, _p in (("SPEND_LEDGER", ".spend.jsonl"), ("FLEET_LEDGER", ".spend.jsonl"),
               ("FLEET_PAUSE_FILE", "FLEET-PAUSED"), ("BUDGET_FILE", "budget.json")):
    os.environ[_k] = os.path.join(_SANDBOX, _p)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent_brain
import agent_inbox
import agent_outbox
import agent_worker

captured = []


def fake_smtp():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(1)
    conn, _ = srv.accept()
    f = conn.makefile("rwb")
    conn.sendall(b"220 fake ESMTP\r\n")
    while True:
        line = f.readline()
        if not line:
            break
        cmd = line.decode().strip()
        if cmd.upper().startswith("EHLO"):
            conn.sendall(b"250-fake\r\n250 SIZE 10240000\r\n")  # note: no STARTTLS offered
        elif cmd.upper().startswith(("MAIL", "RCPT", "HELO")):
            conn.sendall(b"250 ok\r\n")
        elif cmd.upper().startswith("DATA"):
            conn.sendall(b"354 go\r\n")
            body = []
            while True:
                l = f.readline()
                if l in (b".\r\n", b""):
                    break
                body.append(l)
            captured.append(b"".join(body).decode())
            conn.sendall(b"250 queued\r\n")
        elif cmd.upper().startswith("QUIT"):
            conn.sendall(b"221 bye\r\n")
            break
    conn.close()
    srv.close()


# Don't spend money: stub the brain with a canned run, and switch the review gate off — the
# gate has its own test (test_gate.py); this one is about the mail path.
agent_worker.VALIDATION_ROUNDS = 0
agent_brain.agent_loop = lambda task, **kw: {
    "answer": f"WHAT I BUILT\nreceived task: {task!r}",
    "steps": 3,
    "transcript": [
        {"tool": "write_file", "args": {"path": "app.ts"}, "result": "ok"},
        {"tool": "run_bash", "args": {"command": "npm test"}, "result": "(exit 0)\npass"},
    ],
    "stopped": "final",
}

RAW = (
    b"From: Boss <boss@agents.local>\r\n"
    b"To: agent1@agents.local\r\n"
    b"Subject: =?utf-8?q?Build_a_tic-tac-toe_web_app?=\r\n"
    b"Message-ID: <abc123@agents.local>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/alternative; boundary="X"\r\n\r\n'
    b"--X\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Two players, no framework. Reply when done.\r\n"
    b"--X\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>Two players</p>\r\n--X--\r\n"
)

t = threading.Thread(target=fake_smtp, daemon=True)
t.start()
agent_worker.run(agent_inbox.envelope_from_bytes(RAW, seq=7))
t.join(timeout=10)

assert captured, "no mail was sent"
reply = email.message_from_string(captured[0])
body = reply.get_payload(decode=True).decode()
print("--- headers ---")
for h in ("From", "To", "Subject", "In-Reply-To", "References"):
    print(f"{h}: {reply.get(h)}")
print("--- body ---")
print(body)

assert reply["Subject"] == "Re: Build a tic-tac-toe web app", reply["Subject"]
assert reply["To"] == "Boss <boss@agents.local>"
assert reply["In-Reply-To"] == "<abc123@agents.local>"
assert "Two players, no framework." in body, "plain-text part not used as the task"
assert "<p>" not in body, "html leaked into the task"
assert "app.ts" in body and "npm test" in body, "transcript missing from report"
assert "task-0007-build-a-tic-tac-toe-web-app" in body, "workspace name wrong"

# Loop guard: mail from the agent itself must be ignored (no reply sent).
before = len(captured)
agent_worker.run(agent_inbox.envelope_from_bytes(
    RAW.replace(b"Boss <boss@agents.local>", b"agent1@agents.local"), seq=8))
assert len(captured) == before, "worker replied to itself — mail loop!"

# ---- one login, two From addresses -------------------------------------------
# The mail server now runs SPOOF_PROTECTION=1, with validatorN@ as a SEND-AS ALIAS for agentN's
# login rather than an account of its own. So the credential must never depend on the From
# address — the refactor that would have made it depend on one was planned, and dropped.
print("\n--- the send-as contract ---")
ok_cfg = agent_outbox.smtp_identity_warning(user="agent1@agents.local",
                                            validator="validator1@agents.local")
assert ok_cfg == "", "authenticating as the agent is correct configuration and must not warn"
warning = agent_outbox.smtp_identity_warning(user="validator1@agents.local",
                                             validator="validator1@agents.local")
assert warning, "SMTP_USER set to the validator alias must be caught at boot"
assert "send-as alias, not an account" in warning, warning
assert "agent1@agents.local" in warning, "the warning must name the address to use instead"
# An empty SMTP_USER is the no-auth relay case, not a misconfiguration.
assert agent_outbox.smtp_identity_warning(user="", validator="validator1@agents.local") == ""
print("PASS  a validator SMTP_USER is caught at boot, and a correct one is silent")

print("\nALL ASSERTIONS PASSED")
