"""The transport split: a task is an envelope, and mail is one way to make one.

Two things are worth testing here that could not be tested before D3. The parser is now a pure
function, so what a task actually contains can be asserted without a mail server. And the IMAP
side takes an injected connection, so the \\Seen ordering — the thing at-most-once rests on —
can finally be exercised without Dovecot.
"""
import os, sys, tempfile

WS = tempfile.mkdtemp(prefix="env-ws-")
os.environ.update({"WORKSPACE_ROOT": WS, "STATE_FILE": os.path.join(WS, ".processed.json")})
# IDENTITY IS PINNED, not inherited. These assertions name agent1's mailbox and id, so
# without this the suite passes in agent1's container and fails in agent2's — where "a peer
# called dev/agent2" is the agent running the test, and a self-loop refusal is CORRECT. A unit
# test that varies with the host is testing the host. See the same fix in test_notes.py.
os.environ.update({"TENANT": "dev", "AGENT_NAME": "agent1", "AGENT_DOMAIN": "agents.local"})
for _v in ("AGENT_ADDRESS", "VALIDATOR_NAME", "VALIDATOR_ADDRESS"):
    os.environ.pop(_v, None)
_SANDBOX = WS
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
import agent_envelope, agent_inbox, agent_outbox

# Kept because a later section replaces send_mail with a stub, and the folded-header
# regression has to exercise the REAL one — that bug lives in message assembly.
_REAL_SEND_MAIL = agent_outbox.send_mail

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def mail(subject=b"Do a thing", frm=b"Boss <boss@agents.local>", mid=b"<a1@agents.local>",
         extra=b"", body=b"Please do the thing.\r\n"):
    return (b"From: " + frm + b"\r\nTo: agent1@agents.local\r\nSubject: " + subject +
            b"\r\nMessage-ID: " + mid + b"\r\n" + extra +
            b"Content-Type: text/plain\r\n\r\n" + body)


# ---- Parsing -----------------------------------------------------------------
print("\n--- an email becomes an envelope ---")
e = agent_inbox.envelope_from_bytes(mail(), seq=7)
check("subject", e.subject == "Do a thing", e.subject)
check("body", e.body == "Please do the thing.", repr(e.body))
check("requester", e.requester == "Boss <boss@agents.local>", e.requester)
check("reply goes back to the asker", e.reply_to == "Boss <boss@agents.local>", e.reply_to)
check("task id carries the sequence and a slug",
      e.task_id == "task-0007-do-a-thing", e.task_id)
check("source is named", e.source == "email")
check("tenant and agent are stamped on the task",
      e.tenant == "dev" and e.agent_id == "dev/agent1", f"{e.tenant} {e.agent_id}")
check("state starts submitted (an A2A name)",
      e.state == "submitted" and e.state in agent_envelope.STATES)
check("a fresh message is its own thread root",
      e.thread_id == "<a1@agents.local>", e.thread_id)

# Reply-To wins over From: the asker and the destination are not always the same person.
e2 = agent_inbox.envelope_from_bytes(mail(extra=b"Reply-To: team@agents.local\r\n"), seq=1)
check("Reply-To overrides where the answer goes", e2.reply_to == "team@agents.local", e2.reply_to)
check("  ...but the requester is still who asked",
      e2.requester == "Boss <boss@agents.local>", e2.requester)

# A reply keeps the conversation it belongs to, which is what a depth guard will count.
e3 = agent_inbox.envelope_from_bytes(
    mail(mid=b"<c@agents.local>", extra=b"References: <root@x> <b@x>\r\n"), seq=2)
check("thread id is the ROOT of the conversation, not this message",
      e3.thread_id == "<root@x>", e3.thread_id)

# Multipart: plain text preferred, HTML de-tagged, and the HTML must not leak into the task.
MULTI = (b"From: Boss <boss@agents.local>\r\nTo: agent1@agents.local\r\n"
         b"Subject: Mixed\r\nMessage-ID: <m@x>\r\n"
         b'Content-Type: multipart/alternative; boundary="X"\r\n\r\n'
         b"--X\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nthe real text\r\n"
         b"--X\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>markup</p>\r\n--X--\r\n")
e4 = agent_inbox.envelope_from_bytes(MULTI, seq=3)
check("plain text is preferred over html", e4.body == "the real text", repr(e4.body))
check("markup never reaches the task", "<p>" not in e4.body)

# Machine headers are LIFTED into fields — they are transport encoding, not decision inputs.
e5 = agent_inbox.envelope_from_bytes(
    mail(extra=b"X-Agent-Id: agent2\r\nX-Agent-Hops: 2\r\nX-Agent-Purpose: review-request\r\n"),
    seq=4)
check("hops are lifted onto the envelope", e5.hops == 2, str(e5.hops))
check("purpose is lifted onto the envelope", e5.purpose == "review-request", e5.purpose)
check("a machine sender is recorded as such", e5.from_agent is True)
check("a human message has no hops and no purpose",
      e.hops == 0 and e.purpose == "" and e.from_agent is False)
e6 = agent_inbox.envelope_from_bytes(mail(extra=b"X-Agent-Hops: garbage\r\n"), seq=5)
check("an unparseable hop count is 0, not a crash", e6.hops == 0, str(e6.hops))

# A long header is FOLDED across lines by the sender's MTA, and msg.get() hands the newlines
# back. Harmless to read, fatal to re-send. References is the one that grows — a message-id per
# exchange — so it is safe for the first few and breaks exactly when a conversation gets long,
# which is when losing the reply costs the most. It broke a ten-round exchange at round two,
# AFTER the agent had done the work and spent the money.
folded = agent_inbox.envelope_from_bytes(
    mail(mid=b"<f@x>", extra=b"References: <a@x>\r\n <b@x>\r\n\t<c@x>\r\n"), seq=6)
check("A FOLDED References HEADER IS FLATTENED, not carried with its newlines",
      folded.references == "<a@x> <b@x> <c@x>", repr(folded.references))
check("  ...and the thread root still reads correctly out of it",
      folded.thread_id == "<a@x>", folded.thread_id)


# ---- The IMAP side, without an IMAP server -----------------------------------
print("\n--- at-most-once, exercised for the first time ---")


class FakeIMAP:
    """Just enough Dovecot. Records the order of operations, which is the thing under test."""

    def __init__(self, messages, select_ok=True):
        self.messages = messages           # {uid: raw}
        self.select_ok = select_ok
        self.ops = []

    capabilities = ()

    def login(self, u, p): self.ops.append(("login", u))
    def select(self, box): return ("OK" if self.select_ok else "NO"), [b""]
    def search(self, charset, term):
        self.ops.append(("search", term))
        return "OK", [b" ".join(self.messages)]
    def fetch(self, uid, what):
        self.ops.append(("fetch", uid))
        return "OK", [(b"1", self.messages[uid])]
    def store(self, uid, flag, value):
        self.ops.append(("store", uid, value))
        return "OK", [b""]
    def logout(self): self.ops.append(("logout",))


def drain(messages, processed=None, select_ok=True):
    fake = FakeIMAP(messages, select_ok)
    got = agent_inbox.fetch(processed if processed is not None else set(), connect=lambda: fake)
    return got, fake


got, fake = drain({b"1": mail(subject=b"First", mid=b"<m1@x>"),
                   b"2": mail(subject=b"Second", mid=b"<m2@x>")})
check("both messages become envelopes", len(got) == 2, str(len(got)))
check("TWO MESSAGES IN ONE POLL GET DIFFERENT TASK IDS",
      got[0].task_id != got[1].task_id, f"{got[0].task_id} vs {got[1].task_id}")
check("  ...and therefore different workspaces",
      got[0].task_id == "task-0000-first" and got[1].task_id == "task-0001-second",
      f"{got[0].task_id} {got[1].task_id}")

# The ordering at-most-once depends on: flagged BEFORE the caller ever sees it.
fetch_i = fake.ops.index(("fetch", b"1"))
store_i = fake.ops.index(("store", b"1", "\\Seen"))
check("\\Seen is set before the task is handed over", store_i > fetch_i)
check("the body is PEEKed, so fetching does not implicitly consume it",
      all(op[2 if len(op) > 2 else 0] != "(BODY[])" for op in fake.ops))

# Dedupe: a Message-ID already recorded is not run again, even if the flag was lost.
processed = {"<m1@x>"}
got, _ = drain({b"1": mail(subject=b"First", mid=b"<m1@x>"),
                b"2": mail(subject=b"Second", mid=b"<m2@x>")}, processed)
check("an already-handled message is skipped", [g.subject for g in got] == ["Second"],
      str([g.subject for g in got]))
check("  ...and the new one is recorded", "<m2@x>" in processed)

got, _ = drain({}, set())
check("an empty inbox yields nothing", got == [])
got, fake = drain({b"1": mail()}, set(), select_ok=False)
check("an unselectable INBOX yields nothing and consumes nothing",
      got == [] and not any(o[0] == "store" for o in fake.ops))


# ---- Sending, driven by the envelope -----------------------------------------
print("\n--- the reply is threaded from the envelope ---")
sent = []
agent_outbox.send_mail = lambda **kw: (sent.append(kw), "<reply-1@x>")[1]

env = agent_inbox.envelope_from_bytes(
    mail(subject=b"Build a thing", mid=b"<orig@x>", extra=b"References: <root@x>\r\n"), seq=9)
mid = agent_outbox.deliver(env, "here you go")
k = sent[-1]
check("subject gets one Re:", k["subject"] == "Re: Build a thing", k["subject"])
check("addressed to the reply-to", k["to"] == "Boss <boss@agents.local>", k["to"])
check("threaded under the request", k["in_reply_to"] == "<orig@x>", str(k["in_reply_to"]))
check("carries the conversation forward", k["references"] == "<root@x>", str(k["references"]))
check("sent as the agent", k["from_addr"] == "agent1@agents.local", k["from_addr"])

agent_outbox.deliver_review(env, "looks right to me", under=mid)
k = sent[-1]
check("the reviewer signs from its own mailbox",
      k["from_addr"] == "validator1@agents.local", k["from_addr"])
check("its note nests under the reply, not the request",
      k["in_reply_to"] == "<reply-1@x>", str(k["in_reply_to"]))
check("reviewer subject", k["subject"] == "Reviewed: Build a thing", k["subject"])

# An already-Re: subject must not grow a second one.
env2 = agent_inbox.envelope_from_bytes(mail(subject=b"Re: Build a thing"), seq=10)
agent_outbox.deliver(env2, "x")
check("Re: is not doubled", sent[-1]["subject"] == "Re: Build a thing", sent[-1]["subject"])

# The crash was on the SEND side, so prove a reply to a deep thread actually goes out.
print()
print("--- a reply to a long thread still sends ---")
CRLF = bytes([13, 10])
FOLD = bytes([13, 10, 32])          # CRLF + a space: an RFC 5322 folded header
probe = []


class _ProbeSMTP:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def has_extn(self, n): return False
    def login(self, *a): pass
    def send_message(self, m): probe.append(m)


_real_smtp = agent_outbox.smtplib.SMTP
agent_outbox.smtplib.SMTP = _ProbeSMTP
agent_outbox.send_mail = _REAL_SEND_MAIL      # undo the stub from the section above
deep = agent_inbox.envelope_from_bytes(
    mail(mid=b"<deep@x>", subject=b"Round 7",
         extra=b"References: " + FOLD.join(b"<r%d@x>" % i for i in range(40)) + CRLF),
    seq=7)
agent_outbox.deliver(deep, "my answer")
check("the reply to a 40-deep thread SENDS instead of raising", len(probe) == 1)
check("  ...with References on one line",
      chr(10) not in (probe[0]["References"] or ""), repr(probe[0]["References"])[:80])
check("  ...and the thread intact", "<r0@x>" in probe[0]["References"])
agent_outbox.smtplib.SMTP = _real_smtp

import shutil; shutil.rmtree(WS, ignore_errors=True)
print("\n" + ("ALL ENVELOPE TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
