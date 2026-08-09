"""Drive the review gate with a stubbed brain — no API calls, no mail server."""
import os, sys
os.environ.update({"WORKSPACE_ROOT": os.path.join(os.path.dirname(__file__), "ws2"),
                   "VALIDATION_ROUNDS": "3"})
# IDENTITY IS PINNED, not inherited. These assertions name agent1's mailbox and id, so
# without this the suite passes in agent1's container and fails in agent2's — where "a peer
# called dev/agent2" is the agent running the test, and a self-loop refusal is CORRECT. A unit
# test that varies with the host is testing the host. See the same fix in test_notes.py.
os.environ.update({"TENANT": "dev", "AGENT_NAME": "agent1", "AGENT_DOMAIN": "agents.local"})
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
import agent_brain, agent_inbox, agent_outbox, agent_validator, agent_worker

calls = []


def make_result(answer, transcript=None, stopped="final"):
    return {"answer": answer, "steps": 1, "transcript": list(transcript or []),
            "stopped": stopped, "messages": [{"role": "system", "content": "s"}]}


def scenario(name, verdicts, expect_passed, expect_rounds, expect_answer):
    """verdicts: what the reviewer says on each round."""
    calls.clear()
    seq = list(verdicts)

    def fake_loop(task, workspace=None, on_event=None, system_prompt=None, messages=None,
                  tag="agent", **_):
        if (system_prompt or '').startswith(agent_validator.VALIDATOR_PROMPT):
            calls.append(("review", task))
            return make_result(seq.pop(0), [{"tool": "run_bash", "args": {"command": "recheck"},
                                             "result": "ok"}])
        calls.append(("work", task))
        return make_result(f"fixed answer #{len([c for c in calls if c[0] == 'work'])}")

    agent_brain.agent_loop = fake_loop
    start = make_result("original answer", [{"tool": "run_bash",
                                             "args": {"command": "python3 -c \"\nprint(1)\n\""},
                                             "result": "1"}])
    result, review = agent_worker.run_review_gate("do a thing", start, "/ws")

    ok = (review["passed"] == expect_passed and review["rounds"] == expect_rounds
          and result["answer"] == expect_answer)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: passed={review['passed']} "
          f"rounds={review['rounds']} answer={result['answer']!r}")
    if not ok:
        print(f"      expected passed={expect_passed} rounds={expect_rounds} answer={expect_answer!r}")
    # the reply must carry the reviewer's re-checks as evidence too
    assert any(c["args"].get("command") == "recheck" for c in result["transcript"]), \
        "reviewer's own tool calls missing from the evidence"
    return ok, result, review


all_ok = True
ok, _, _ = scenario("clean pass first time", ["VERDICT: PASS\nrechecked the sums"],
                    True, 1, "original answer"); all_ok &= ok
ok, _, _ = scenario("fails once, fix accepted",
                    ["VERDICT: FAIL\n1. 119 months is $19,980.06, not doubled.",
                     "VERDICT: PASS\nrecomputed, month 120 is correct"],
                    True, 2, "fixed answer #1"); all_ok &= ok
ok, r, v = scenario("never passes -> send anyway",
                    ["VERDICT: FAIL\nstill wrong", "VERDICT: FAIL\nstill wrong",
                     "VERDICT: FAIL\nstill wrong (final)"],
                    False, 3, "fixed answer #2"); all_ok &= ok

# the failure notice must be at the very top of the email, above the confident answer
body = agent_worker.build_report(r, "/ws", v)
first = body.splitlines()[0]
print(("PASS" if first.startswith("!! THIS DID NOT PASS REVIEW") else "FAIL"),
      " failure banner on top:", first[:60])
all_ok &= first.startswith("!! THIS DID NOT PASS REVIEW")
assert "still wrong (final)" in body, "reviewer's objections missing from the reply"

# multi-line commands must be readable in the footer now
flat = [l for l in body.splitlines() if "python3" in l][0]
print(("PASS" if "print(1)" in flat else "FAIL"), " multi-line command flattened:", flat.strip())
all_ok &= "print(1)" in flat

# gate off
agent_worker.VALIDATION_ROUNDS = 0
res, rev = agent_worker.run_review_gate("t", make_result("x"), "/ws")
print(("PASS" if rev is None and res["answer"] == "x" else "FAIL"), " gate disabled -> no review")
all_ok &= rev is None

# unparseable verdict counts as a failure, never a pass
p, n = agent_validator.parse_verdict("I think it's probably fine?")
print(("PASS" if p is False else "FAIL"), " vague reviewer cannot wave work through")
all_ok &= p is False


# ---- the reviewer's own email ------------------------------------------------
agent_worker.VALIDATION_ROUNDS = 3
sent = []
# The transport is agent_outbox now; deliver()/deliver_review() both go through send_mail.
agent_outbox.send_mail = lambda **kw: (sent.append(kw), "<mid-%d@x>" % len(sent))[1]

seq = ["VERDICT: FAIL\n1. 119 months is not doubled.",
       "VERDICT: PASS\nI recomputed the balance at month 120 myself: $20,096.61."]


def fake_loop(task, workspace=None, on_event=None, system_prompt=None, messages=None,
              tag="agent", **_):
    if (system_prompt or '').startswith(agent_validator.VALIDATOR_PROMPT):
        return make_result(seq.pop(0), [{"tool": "run_bash",
                                         "args": {"command": "python3 -c \"\nprint(20096.61)\n\""},
                                         "result": "20096.61"}])
    return make_result("the corrected answer")


agent_brain.agent_loop = fake_loop
RAW = (b"From: Boss <boss@agents.local>\r\nTo: agent1@agents.local\r\n"
       b"Subject: Two calculations\r\nMessage-ID: <orig@agents.local>\r\n"
       b"Content-Type: text/plain\r\n\r\nhow long to double?\r\n")
agent_worker.run(agent_inbox.envelope_from_bytes(RAW, seq=9))

print()
ok = len(sent) == 2
print(("PASS" if ok else "FAIL"), f" two emails sent (worker + reviewer): got {len(sent)}")
all_ok &= ok
if ok:
    worker_mail, review_mail = sent
    checks = [
        ("worker mail is from agent1", worker_mail["from_addr"] == "agent1@agents.local"),
        ("reviewer mail is from validator1", review_mail["from_addr"] == "validator1@agents.local"),
        ("reviewer mail goes to the boss", review_mail["to"] == "Boss <boss@agents.local>"),
        ("reviewer subject", review_mail["subject"] == "Reviewed: Two calculations"),
        ("threaded under the worker's reply", review_mail["in_reply_to"] == "<mid-1@x>"),
        ("carries its own explanation", "month 120" in review_mail["body"]),
        ("shows its own commands", "print(20096.61)" in review_mail["body"]),
        ("discloses the rejected round", "sent back" in review_mail["body"]),
        ("says it didn't do the work", "only checked it" in review_mail["body"]),
    ]
    for label, cond in checks:
        print(("PASS" if cond else "FAIL"), " " + label)
        all_ok &= cond

# on a FAILED review no second email — the objections already ride on the worker's reply
sent.clear()
seq2 = ["VERDICT: FAIL\nno", "VERDICT: FAIL\nno", "VERDICT: FAIL\nstill no"]
agent_brain.agent_loop = lambda task, workspace=None, on_event=None, system_prompt=None, \
    messages=None, tag="agent", **_: (
        make_result(seq2.pop(0), []) if (system_prompt or '').startswith(agent_validator.VALIDATOR_PROMPT)
        else make_result("attempted answer"))
agent_worker.run(agent_inbox.envelope_from_bytes(RAW.replace(b"<orig@", b"<orig2@"), seq=10))
ok = len(sent) == 1 and sent[0]["from_addr"] == "agent1@agents.local"
print(("PASS" if ok else "FAIL"), f" failed review sends ONE email, not two: got {len(sent)}")
all_ok &= ok


# ---- the preamble cut: must never lose real content -------------------------
sp = agent_worker.strip_preamble
cases = [
    ("cuts the backstage narration",
     "The numbers check out. Here's the write-up.\n---EMAIL---\nHi Jianmin,\n\n$722.90",
     "Hi Jianmin,\n\n$722.90"),
    ("no marker -> sends everything untouched (fail-open)",
     "Hi Jianmin,\n\nthe answer is 42.", "Hi Jianmin,\n\nthe answer is 42."),
    ("empty after the marker -> keeps the whole answer rather than send nothing",
     "Hi Jianmin, the answer is 42.\n---EMAIL---\n   \n", "Hi Jianmin, the answer is 42.\n---EMAIL---\n   \n"),
    ("marker with nothing above it",
     "---EMAIL---\nHi Jianmin,", "Hi Jianmin,"),
    ("repeated marker -> the LAST one wins (a rework re-marking its final draft)",
     "draft\n---EMAIL---\nold reply\n---EMAIL---\ncorrected reply", "corrected reply"),
    ("empty answer survives", "", ""),
]
print()
for label, raw, expected in cases:
    got = sp(raw)
    print(("PASS" if got == expected else "FAIL"), " " + label)
    if got != expected:
        print(f"      got {got!r} expected {expected!r}")
    all_ok &= got == expected

# ---- the merged trail must say who ran what ---------------------------------
# The real bug this caught: agent1's footer listed the reviewer's commands and its file
# (/tmp/draft.txt) with no marker, so the worker appeared to have done work it never did.
mixed = {
    "answer": "answer", "steps": 3, "stopped": "final", "messages": [],
    "transcript": [
        {"tool": "run_bash", "args": {"command": "python3 -c 'the table'"},
         "result": "", "by": "worker"},
        {"tool": "write_file", "args": {"path": "/tmp/draft.txt"}, "result": "", "by": "reviewer"},
        {"tool": "run_bash", "args": {"command": "wc -w /tmp/draft.txt"},
         "result": "", "by": "reviewer"},
    ],
}
body = agent_worker.build_report(mixed, "/ws", {"passed": True, "notes": "ok", "rounds": 1,
                                                "history": [], "transcript": []})
checks = [
    ("worker's own call is under [agent1]", "[agent1]\n    1. python3 -c 'the table'" in body),
    ("reviewer's calls are under [validator1]", "[validator1]\n    2. write_file" in body),
    ("numbering stays in true execution order", "  3. wc -w /tmp/draft.txt" in body),
    ("the reviewer's file is attributed to it", "/tmp/draft.txt   [validator1]" in body),
    ("worker not credited with the reviewer's file", "/tmp/draft.txt   [agent1]" not in body),
]
print()
for label, cond in checks:
    print(("PASS" if cond else "FAIL"), " " + label)
    all_ok &= cond

print("\n" + ("ALL GATE TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
