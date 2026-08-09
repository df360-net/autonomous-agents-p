"""Agents writing to each other, with the boss always copied.

Everything here is a property the MODEL MUST NOT BE ABLE TO BREAK, so the tests are written
from the model's side: they call the tool the way the model calls it, with arguments the model
chooses, and check that the things it does not get to choose came out right anyway. A prompt
instruction would pass none of these.
"""
import os, sys, tempfile
from dataclasses import replace

WS = tempfile.mkdtemp(prefix="peer-ws-")
_SANDBOX = WS
for _v in ("AGENT_ADDRESS", "VALIDATOR_NAME", "VALIDATOR_ADDRESS"):
    os.environ.pop(_v, None)
os.environ.update({
    "WORKSPACE_ROOT": WS, "STATE_FILE": os.path.join(WS, ".processed.json"),
    "TENANT": "dev", "AGENT_NAME": "agent1", "AGENT_DOMAIN": "agents.local",
    "FLEET_PEERS": "agent1,agent2,agent3", "BOSS_ADDRESS": "boss@agents.local",
    "FLEET_HMAC_SECRET": "test-key-not-a-real-one",
    "FLEET_LEDGER": os.path.join(WS, ".spend.jsonl"),
    "SPEND_LEDGER": os.path.join(WS, ".spend.jsonl"),
    "FLEET_PAUSE_FILE": os.path.join(WS, "FLEET-PAUSED"),
    "BUDGET_FILE": os.path.join(WS, "budget.json"),
})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent_envelope, agent_inbox, agent_outbox, agent_peer, agent_principal, fleet_identity

all_ok = True
sent = []


class FakeSMTP:
    """Only smtplib is faked, so agent_outbox.send_mail builds the message for real.

    An earlier version stubbed send_mail itself and captured its kwargs. That hid the bug this
    file exists to catch: the signature covers thread_id, and the recipient derives thread_id
    from the References header that send_mail assembles. Stub the assembly and the two agree
    only because the test made them agree — the round trip passed while the real one would
    have failed to verify.
    """

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def has_extn(self, name): return False
    def login(self, *a): pass
    def send_message(self, msg): sent.append(msg)


agent_outbox.smtplib.SMTP = FakeSMTP


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def task(hops=0, thread="<root@x>", mid="<req@x>", from_agent=""):
    """The inbound task this agent is working on."""
    e = agent_envelope.TaskEnvelope(
        task_id="task-0007-do-a-thing", tenant="dev", agent_id="dev/agent1",
        requester="Boss <boss@agents.local>", reply_to="boss@agents.local",
        subject="Do a thing", thread_id=thread, message_id=mid, hops=hops,
        from_agent_id=from_agent)
    p = (agent_principal.Principal("agent", from_agent, "dev", True) if from_agent
         else agent_principal.Principal("human", "boss@agents.local", "dev"))
    agent_peer.start_task(e, p)
    return e, p


# ---- The boss is copied, and the model cannot prevent it ---------------------
print("\n--- the boss sees everything ---")
task()
out = agent_peer.send(to="agent2", purpose="review-request", subject="Check my recipe box",
                      body="It is at /workspace/task-0000.")
check("the message goes out", out.startswith("Sent to agent2"), out)
k = sent[-1]
check("addressed to the peer's mailbox", k["To"] == "agent2@agents.local", k["To"])
check("THE BOSS IS COPIED", k["Cc"] == "boss@agents.local", str(k.get("Cc")))
check("sent from this agent, not the peer", "agent1@agents.local" in k["From"])
check("the purpose is visible in the subject line a human scans",
      k["Subject"] == "[review-request] Check my recipe box", k["Subject"])
check("the body says who asked and why, for the reader who is not either agent",
      "dev/agent1" in k.get_content() and "dev/agent2" in k.get_content()
      and "task-0007-do-a-thing" in k.get_content())
check("  ...and states that the boss is on every one of these", "copied" in k.get_content())

# There is no argument that removes the CC. This is the whole requirement.
before = len(sent)
for evasion in ({"cc": None}, {"cc": ""}, {"bcc": "x"}, {"boss": None},
                {"headers": {"Cc": ""}}, {"BOSS_ADDRESS": None}):
    try:
        agent_peer.send(to="agent2", purpose="question", subject="s", body="b", **evasion)
        check(f"REJECTED an attempt to pass {list(evasion)[0]!r}", False, "it was accepted")
    except TypeError:
        check(f"REJECTED an attempt to pass {list(evasion)[0]!r} — not a parameter", True)
check("  ...and none of those reached the wire", len(sent) == before)


# ---- The hop count is the harness's, not the model's -------------------------
print("\n--- the model cannot touch the hop count ---")
task(hops=0)
agent_peer.send(to="agent2", purpose="question", subject="s", body="b")
check("a task from a human goes out at hop 1", sent[-1]["X-Agent-Hops"] == "1")
task(hops=1, from_agent="dev/agent3")
agent_peer.send(to="agent2", purpose="question", subject="s", body="b")
check("A CHAIN INCREMENTS: hop 1 in, hop 2 out",
      sent[-1]["X-Agent-Hops"] == "2", sent[-1]["X-Agent-Hops"])

# The model supplies only to/purpose/subject/body. Anything else is not a parameter at all,
# which is stronger than validating it — there is nothing to validate.
for forged in ({"hops": 0}, {"inbound_hops": 0}, {"from_agent_id": "dev/agent9"},
               {"signature": "v1=deadbeef"}, {"from_addr": "agent9@agents.local"}):
    try:
        agent_peer.send(to="agent2", purpose="question", subject="s", body="b", **forged)
        check(f"REJECTED forged {list(forged)[0]!r}", False, "it was accepted")
    except TypeError:
        check(f"REJECTED forged {list(forged)[0]!r} — the model has no such argument", True)

# At the limit, sending is refused rather than sent-and-dropped at the far end.
task(hops=agent_principal.MAX_HOPS - 1, from_agent="dev/agent3")
out = agent_peer.send(to="agent2", purpose="question", subject="s", body="b")
check("REFUSED at the hop limit", out.startswith("ERROR"), out)
check("  ...and explains it is a loop guard, not a bug", "loop guard" in out)


# ---- Who the model may write to ---------------------------------------------
print("\n--- it can only write to agents, and only real ones ---")
task()
check("an unknown agent is refused",
      agent_peer.send(to="agent99", purpose="question", subject="s", body="b")
      .startswith("ERROR"))
check("ITSELF is not a peer — that is a loop with extra steps",
      agent_peer.send(to="agent1", purpose="question", subject="s", body="b")
      .startswith("ERROR"))
check("a human's address is refused: this tool is not a mail client",
      agent_peer.send(to="boss@agents.local", purpose="question", subject="s", body="b")
      .startswith("ERROR"))
check("an unknown purpose is refused",
      agent_peer.send(to="agent2", purpose="delete-everything", subject="s", body="b")
      .startswith("ERROR"))
check("an empty body is refused", agent_peer.send(to="agent2", purpose="question",
                                                  subject="s", body="  ").startswith("ERROR"))

# Answering the peer who asked is the harness's job, not the tool's. Seen live: agent2 used
# the tool AND the harness sent its task reply, so agent1 got two emails and ran two tasks off
# one question.
task(from_agent="dev/agent2")
out = agent_peer.send(to="agent2", purpose="answer", subject="s", body="b")
check("REFUSED: answering the peer who sent this task — the reply already goes to them",
      out.startswith("ERROR") and "twice" in out, out)
check("  ...same for a review result", agent_peer.send(to="agent2", purpose="review-result",
                                                       subject="s", body="b").startswith("ERROR"))
check("  ...but writing to them about something NEW is fine",
      agent_peer.send(to="agent2", purpose="question", subject="s", body="b").startswith("Sent"))
task(from_agent="dev/agent3")
check("  ...and answering a DIFFERENT agent is fine",
      agent_peer.send(to="agent2", purpose="answer", subject="s", body="b").startswith("Sent"))

# Width, not depth: hops cannot see a fan-out inside one task.
task()
outs = [agent_peer.send(to="agent2", purpose="question", subject=f"s{i}", body="b")
        for i in range(agent_peer.MAX_SENDS_PER_TASK + 3)]
check(f"the first {agent_peer.MAX_SENDS_PER_TASK} are sent",
      all(o.startswith("Sent") for o in outs[:agent_peer.MAX_SENDS_PER_TASK]))
check("THEN IT IS CAPPED — hops bound a chain's depth, never its width",
      all(o.startswith("ERROR") for o in outs[agent_peer.MAX_SENDS_PER_TASK:]))
task()
check("a new task gets a fresh allowance",
      agent_peer.send(to="agent2", purpose="question", subject="s", body="b").startswith("Sent"))


# ---- EVERY email, not just the agent-to-agent ones ---------------------------
print()
print("--- the boss is copied on everything the fleet sends ---")
e, _ = task()
# A requester who is NOT the boss — otherwise the dedupe below correctly suppresses the Cc and
# these two checks pass without testing anything. The fixture's default requester is the boss.
outsider = replace(e, reply_to="someone@example.com", requester="Someone <someone@example.com>")
agent_outbox.deliver(outsider, "here is your answer")
check("a TASK REPLY to a human copies the boss", sent[-1]["Cc"] == "boss@agents.local",
      str(sent[-1].get("Cc")))
agent_outbox.deliver_review(outsider, "looks right", under="<r@x>")
check("the REVIEWER'S sign-off copies the boss too", sent[-1]["Cc"] == "boss@agents.local",
      str(sent[-1].get("Cc")))
check("  ...and it is genuinely from the reviewer, not the worker",
      "validator1@agents.local" in sent[-1]["From"], sent[-1]["From"])

# Addressed TO him already: one copy, not two. A duplicate in the inbox teaches you to skim.
boss_env = replace(e, reply_to="boss@agents.local", requester="Boss <boss@agents.local>")
agent_outbox.deliver(boss_env, "answer")
check("NO DUPLICATE when the boss is the recipient", sent[-1]["Cc"] is None,
      str(sent[-1].get("Cc")))
boss_env = replace(e, reply_to="Boss <boss@agents.local>")
agent_outbox.deliver(boss_env, "answer")
check("  ...matched on the address, not the display name", sent[-1]["Cc"] is None,
      str(sent[-1].get("Cc")))
# A caller's own Cc is kept, and the boss is added to it.
agent_outbox.send_mail(to="a@x.com", cc="b@x.com", subject="s", body="b",
                       from_name="agent1", from_addr="agent1@agents.local")
check("an existing Cc is kept AND the boss added",
      sent[-1]["Cc"] == "b@x.com, boss@agents.local", sent[-1]["Cc"])


# ---- The round trip ----------------------------------------------------------
print("\n--- what we send, the other agent accepts ---")
task()
agent_peer.send(to="agent2", purpose="review-request", subject="Check this", body="please")
k = sent[-1]
# EXACTLY the bytes that went on the wire — headers, References and all. Reconstructing them
# by hand is what hid the thread_id mismatch: the signature covers thread_id, and the recipient
# derives it from the References header that send_mail assembles, so a hand-built message makes
# the two agree for reasons the real one would not.
raw = k.as_bytes()

# Verified from the RECIPIENT's side: agent2's identity, agent2's admission.
os.environ["AGENT_NAME"] = "agent2"
for m in ("agent_principal", "agent_inbox", "fleet_identity"):
    sys.modules.pop(m, None)
import agent_principal as p2, agent_inbox as in2
d = p2.admit(in2.envelope_from_bytes(raw, seq=1))
check("AGENT2 ADMITS IT, attested, as an agent",
      d.allowed and d.principal.kind == "agent" and d.principal.attested, d.reason)
check("  ...as agent1 specifically", d.principal.principal_id == "dev/agent1")
check("  ...with the hop count and purpose intact",
      d.envelope.hops == 1 and d.envelope.purpose == "review-request")

# The gap this closes: an UNSIGNED message from a fleet mailbox used to be admitted as a human,
# with hops forced to 0 — so the hop guard never engaged on the return leg of any exchange.
plain = (b"From: agent1 <agent1@agents.local>\r\nTo: agent2@agents.local\r\n"
         b"Subject: hello\r\nMessage-ID: <plain@x>\r\n\r\nhi\r\n")
d = p2.admit(in2.envelope_from_bytes(plain, seq=2))
check("AN UNSIGNED MESSAGE FROM A FLEET MAILBOX IS REFUSED", not d.allowed, d.reason)
check("  ...and is not quietly downgraded to a human request",
      "not signed" in d.reason, d.reason)
# A stranger forging the From gets the same answer, which is the point of denying on it.
forged = plain.replace(b"agent1 <agent1@agents.local>", b"agent1 <agent1@agents.local>")
check("  ...so a forged From: buys nothing either",
      not p2.admit(in2.envelope_from_bytes(forged, seq=3)).allowed)
# Humans are completely unaffected.
human = (b"From: Boss <boss@agents.local>\r\nTo: agent2@agents.local\r\n"
         b"Subject: hello\r\nMessage-ID: <h@x>\r\n\r\nhi\r\n")
check("  ...and a person is untouched by any of this",
      p2.admit(in2.envelope_from_bytes(human, seq=4)).allowed)

import shutil; shutil.rmtree(WS, ignore_errors=True)
print("\n" + ("ALL PEER TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
