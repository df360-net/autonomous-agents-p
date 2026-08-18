"""Does the thing the agent made actually reach the human?

Both halves of task 0030 are replayed here. The agent wrote a 746-line book, then ended its
turn on "Now I'll compose the email with the full book embedded... Let me compose it." The
harness mailed that sentence. The book stayed in the workspace, and at 35,554 characters it
would have been cut off by MAX_REPLY_CHARS even if the agent had pasted it.

No API calls and no mail server: call_llm and smtplib are both stubbed.
"""
import os, sys, types, email

os.environ.update({"WORKSPACE_ROOT": os.path.join(os.path.dirname(__file__), "ws3")})
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
import agent_brain, agent_outbox, agent_validator, agent_worker

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def replies(*answers):
    """Stub call_llm: hand back these final answers in order, never any tool calls."""
    seq = list(answers)
    seen = []

    def fake(messages, role="worker", **_):
        seen.append(list(messages))
        return {"role": "assistant", "content": seq.pop(0)}
    return fake, seen


# ---- 1. The marker nudge ----------------------------------------------------
print("\n--- the ---EMAIL--- contract ---")

# The real text agent1 sent on 2026-08-08, verbatim from the container log.
NARRATION = (
    "I have the full book content. Now I'll compose the email with the full book embedded. "
    "The book is plain text and works as-is in the email. I'll write a friendly opening and "
    "then include the entire book.\n\nLet me finalize my response.\n\nLet me compose it."
)
REAL = "---EMAIL---\nHi Jianmin,\n\nHere is the book you asked for.\n\nCHAPTER 1 ..."

agent_brain.call_llm, seen = replies(NARRATION, REAL)
r = agent_brain.agent_loop("write a book", tag="t", require_marker=True)
check("task 0030 replay: narration is not accepted as the email", r["answer"] == REAL,
      f"got {r['answer'][:60]!r}")
check("the nudge was actually put to the model",
      any(m.get("content") == agent_brain.MISSING_MARKER_NUDGE for m in seen[-1]))
check("the corrective turn is counted as a step", r["steps"] == 2, f"steps={r['steps']}")

# Fail open on the second miss: one correction, then send whatever we have. A withheld reply
# is worse than an ugly one, which is the same trade strip_preamble makes.
agent_brain.call_llm, _ = replies(NARRATION, "still no marker, sorry")
r = agent_brain.agent_loop("write a book", tag="t", require_marker=True)
check("nudged once and only once", r["answer"] == "still no marker, sorry")
check("  ...and it still stops rather than looping", r["stopped"] == "final")

agent_brain.call_llm, seen = replies(REAL)
r = agent_brain.agent_loop("write a book", tag="t", require_marker=True)
check("a marked answer is returned untouched, no extra turn", r["steps"] == 1 and r["answer"] == REAL)

# The reviewer answers "VERDICT: PASS", never a marker. Nudging it would be a bug.
agent_brain.call_llm, seen = replies("VERDICT: PASS\nchecked the sums myself")
r = agent_brain.agent_loop("review this", tag="rev", system_prompt=agent_validator.VALIDATOR_PROMPT)
check("the reviewer is never nudged for a marker it does not owe",
      r["steps"] == 1 and r["answer"].startswith("VERDICT: PASS"))


# ---- 2. Collecting what the task produced -----------------------------------
print("\n--- what gets attached ---")
import shutil, time

WS = os.path.join(os.path.dirname(__file__), "ws3", "task-0030")
shutil.rmtree(WS, ignore_errors=True)
os.makedirs(WS)

BOOK = ("A PRACTICAL GUIDE TO OPTION TRADING\n" + ("x" * 80 + "\n") * 440)   # ~35 KB, as shipped
old = time.time() - 3600
# newline="" so Windows does not rewrite \n as \r\n underneath the fixture — the container
# this actually runs in is Linux, and the comparison should be against the same bytes there.
with open(os.path.join(WS, "option-trading-book.txt"), "w", encoding="utf-8", newline="") as fh:
    fh.write(BOOK)
with open(os.path.join(WS, "test.txt"), "w", encoding="utf-8") as fh:
    fh.write("test write\n")
# Pre-existing junk from an earlier task, and build noise that is never a deliverable.
stale = os.path.join(WS, "old-notes.txt")
with open(stale, "w", encoding="utf-8") as fh:
    fh.write("from last week")
os.utime(stale, (old, old))
os.makedirs(os.path.join(WS, "node_modules", "left-pad"))
with open(os.path.join(WS, "node_modules", "left-pad", "index.js"), "w") as fh:
    fh.write("module.exports = 1")
with open(os.path.join(WS, "empty.log"), "w") as fh:
    pass

started = time.time() - 60
files, note = agent_worker.collect_attachments(WS, started)
names = [n for n, _ in files]

check("the book is attached", "option-trading-book.txt" in names, f"got {names}")
check("its bytes are complete, not truncated",
      dict(files)["option-trading-book.txt"].decode("utf-8") == BOOK)
check("a file from an earlier task is left alone", "old-notes.txt" not in names, f"got {names}")
check("node_modules is not mailed to anyone",
      not any("left-pad" in n or "index.js" in n for n in names), f"got {names}")
check("empty files are skipped", "empty.log" not in names, f"got {names}")
check("the report names what it attached", "option-trading-book.txt" in note and "ATTACHED" in note)

# A source tree is not a deliverable; an arbitrary dozen out of forty would be noise.
BIG = os.path.join(os.path.dirname(__file__), "ws3", "task-app")
shutil.rmtree(BIG, ignore_errors=True)
os.makedirs(BIG)
for i in range(20):
    with open(os.path.join(BIG, f"src{i}.js"), "w") as fh:
        fh.write("code")
files, note = agent_worker.collect_attachments(BIG, started)
check("a 20-file project attaches nothing", files == [], f"got {[n for n, _ in files]}")
check("  ...and says so plainly", "NOT ATTACHED" in note and "20 files" in note, note)

# Oversize: named in the note rather than dropped in silence.
HUGE = os.path.join(os.path.dirname(__file__), "ws3", "task-huge")
shutil.rmtree(HUGE, ignore_errors=True)
os.makedirs(HUGE)
with open(os.path.join(HUGE, "dump.bin"), "wb") as fh:
    fh.write(b"\0" * (agent_worker.ATTACH_MAX_BYTES + 1))
with open(os.path.join(HUGE, "summary.txt"), "w") as fh:
    fh.write("what I found")
files, note = agent_worker.collect_attachments(HUGE, started)
check("the oversize file is skipped", [n for n, _ in files] == ["summary.txt"],
      f"got {[n for n, _ in files]}")
check("  ...and the reader is told it was skipped", "dump.bin" in note and "NOT ATTACHED" in note)

# A nested file keeps its plain name — that is what the recipient saves it as.
NEST = os.path.join(os.path.dirname(__file__), "ws3", "task-nest")
shutil.rmtree(NEST, ignore_errors=True)
os.makedirs(os.path.join(NEST, "report"))
with open(os.path.join(NEST, "report", "chapter1.md"), "w") as fh:
    fh.write("# one")
files, _ = agent_worker.collect_attachments(NEST, started)
check("a nested file keeps its plain filename", [n for n, _ in files] == ["chapter1.md"],
      f"got {[n for n, _ in files]}")

# ...until two of them would overwrite each other in someone's downloads folder.
os.makedirs(os.path.join(NEST, "appendix"))
with open(os.path.join(NEST, "appendix", "chapter1.md"), "w") as fh:
    fh.write("# also one")
files, _ = agent_worker.collect_attachments(NEST, started)
check("a colliding name is qualified, never silently dropped",
      len(files) == 2 and len({n for n, _ in files}) == 2, f"got {[n for n, _ in files]}")


# ---- The gap the first live run exposed -------------------------------------
print("\n--- work done in an EARLIER task's folder ---")
# The agent is told to work on existing things where they already live. The follow-up that
# corrected the option book ran as task-0031 but edited the file in task-0030's directory.
# Scanning only the current task's folder found nothing to attach, on the one task whose whole
# purpose was to hand back a corrected file. So the scan is rooted at the workspace root.
ROOT = os.path.join(os.path.dirname(__file__), "ws3", "root")
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(os.path.join(ROOT, "task-0030-write-an-option-trading-book"))
os.makedirs(os.path.join(ROOT, "task-0031-two-things-about-the-book"))
corrected = os.path.join(ROOT, "task-0030-write-an-option-trading-book", "book.txt")
with open(corrected, "w", encoding="utf-8", newline="") as fh:
    fh.write("corrected book, chapter 7 now says 2%")
# The agent rewrites its own memory on nearly every task; those must not ride along.
for n in ("AGENT.md", "AGENT-ASSETS.md", "AGENT-AVOID.md"):
    with open(os.path.join(ROOT, n), "w") as fh:
        fh.write("notes to self")
# An app from an earlier task is still serving, so a request mid-run touches its database.
os.makedirs(os.path.join(ROOT, "task-0022-expense-tracker"))
with open(os.path.join(ROOT, "task-0022-expense-tracker", "expenses.db"), "wb") as fh:
    fh.write(b"SQLite format 3\0" + b"private")
with open(os.path.join(ROOT, "task-0022-expense-tracker", "server.log"), "w") as fh:
    fh.write("GET / 200")

# Harness state written on every run, sitting in the same directory the walk starts from.
for n in (".spend.jsonl", "FLEET-PAUSED", ".processed.json"):
    with open(os.path.join(ROOT, n), "w") as fh:
        fh.write("harness state")

# This one is different, and worse. ship_app writes it INSIDE the task workspace, so it is
# fresh, small and non-empty on precisely the tasks that produce a real deliverable — it would
# have been attached alongside the app somebody asked for, not on quiet runs where nobody looks.
with open(os.path.join(ROOT, "task-0022-expense-tracker", ".fleet-registered"), "w") as fh:
    fh.write("expense-tracker\n")

files, note = agent_worker.collect_attachments(ROOT, started)
names = [n for n, _ in files]
check("a file corrected in an earlier task's folder IS attached", names == ["book.txt"],
      f"got {names}")
check("the spend ledger is never mailed to anyone", ".spend.jsonl" not in names, f"got {names}")
check("nor the pause file or the dedupe state",
      not ({"FLEET-PAUSED", ".processed.json"} & set(names)), f"got {names}")
check("the fleet registration marker is not mailed with the app it belongs to",
      ".fleet-registered" not in names, f"got {names}")
check("the agent's own notes are not mailed back",
      not any(n.startswith("AGENT") for n in names), f"got {names}")
check("a live app's database is never attached", "expenses.db" not in names, f"got {names}")
check("nor its logs", "server.log" not in names, f"got {names}")

check("an empty workspace attaches nothing and says nothing",
      agent_worker.collect_attachments(os.path.join(os.path.dirname(__file__), "ws3", "nope-%d" % 1)
                                       if False else NEST + "-missing", started) == ([], ""))


# ---- 2b. Scaffolding, and the agent naming its own deliverable --------------
print("\n--- one deliverable, not the whole working directory ---")

# Exactly what task 0032 produced: one Markdown book asked for, nine build artefacts beside it.
MD = os.path.join(os.path.dirname(__file__), "ws3", "task-0032")
shutil.rmtree(MD, ignore_errors=True)
os.makedirs(MD)
produced = ["option-trading-book.md", "build.py", "diagrams.py", "payoff_diagrams.py",
            "curve_plots.py", "strategy_flow.mmd", "beginner_path.mmd", "package.json",
            "package-lock.json", "puppeteer-config.json"]
for n in produced:
    with open(os.path.join(MD, n), "w") as fh:
        fh.write("content of " + n)

files, note = agent_worker.collect_attachments(MD, started, MD)
names = [n for n, _ in files]
check("lockfiles and tool config are never a deliverable",
      not ({"package.json", "package-lock.json", "puppeteer-config.json"} & set(names)),
      f"got {names}")

# ...and with the agent saying what it meant, the noise goes entirely.
with open(os.path.join(MD, "DELIVERABLES"), "w") as fh:
    fh.write("# the thing that was asked for\noption-trading-book.md\n\n")
files, note = agent_worker.collect_attachments(MD, started, MD)
check("a nomination reduces it to the one file asked for",
      [n for n, _ in files] == ["option-trading-book.md"], f"got {[n for n, _ in files]}")
check("the DELIVERABLES file does not attach itself", "DELIVERABLES" not in dict(files))

# A nomination is a decision, not a hint: it outranks the skip heuristics.
with open(os.path.join(MD, "DELIVERABLES"), "w") as fh:
    fh.write("package.json\n")
files, _ = agent_worker.collect_attachments(MD, started, MD)
check("an explicitly named file beats the skip list",
      [n for n, _ in files] == ["package.json"], f"got {[n for n, _ in files]}")

# Nominating something that isn't there must be visible, not silent.
with open(os.path.join(MD, "DELIVERABLES"), "w") as fh:
    fh.write("option-trading-book.md\nthe-one-i-forgot-to-write.md\n")
files, note = agent_worker.collect_attachments(MD, started, MD)
check("a missing nomination is reported, not swallowed",
      [n for n, _ in files] == ["option-trading-book.md"] and "the-one-i-forgot" in note, note)

# Attaching to outbound email is the one thing here that leaves the container.
with open(os.path.join(MD, "DELIVERABLES"), "w") as fh:
    fh.write("../../../../../../etc/passwd\noption-trading-book.md\n")
files, note = agent_worker.collect_attachments(MD, started, MD)
check("a path escaping the workspace is refused",
      [n for n, _ in files] == ["option-trading-book.md"] and "refused" in note, note)

# The whole point of the nomination: 40 files is normally an outright refusal to attach.
MANY = os.path.join(os.path.dirname(__file__), "ws3", "task-many")
shutil.rmtree(MANY, ignore_errors=True)
os.makedirs(MANY)
for i in range(40):
    with open(os.path.join(MANY, f"f{i}.txt"), "w") as fh:
        fh.write("x")
files, note = agent_worker.collect_attachments(MANY, started, MANY)
check("40 files with no nomination still attaches nothing", files == [])
check("  ...and the refusal now points at the way out", "DELIVERABLES" in note, note)
with open(os.path.join(MANY, "DELIVERABLES"), "w") as fh:
    fh.write("f7.txt\n")
files, _ = agent_worker.collect_attachments(MANY, started, MANY)
check("a nomination rescues the delivery from a crowded workspace",
      [n for n, _ in files] == ["f7.txt"], f"got {[n for n, _ in files]}")


# ---- 3. End to end: does it survive the wire? -------------------------------
print("\n--- on the wire ---")
captured = {}


class FakeSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def has_extn(self, _): return False
    def starttls(self, **kw): pass
    def login(self, *a): pass
    def send_message(self, m): captured["msg"] = m


agent_outbox.smtplib = types.SimpleNamespace(SMTP=FakeSMTP, SMTP_SSL=FakeSMTP)

files, note = agent_worker.collect_attachments(WS, started)
result = {"answer": "---EMAIL---\nHi Jianmin,\n\nThe book is attached.", "steps": 12,
          "stopped": "final", "transcript": [{"tool": "write_file", "by": "worker",
                                              "args": {"path": "option-trading-book.txt"}}]}
body = agent_worker.build_report(result, WS, None, note)
agent_outbox.send_mail("boss@agents.local", "Re: book", body, "agent1", "agent1@agents.local",
                       attachments=files)

sent = captured["msg"]
parts = [p for p in sent.walk() if p.get_filename()]
check("the email carries an attachment", len(parts) == 2, f"got {[p.get_filename() for p in parts]}")
book_part = [p for p in parts if p.get_filename() == "option-trading-book.txt"]
check("the attached filename is the real one", bool(book_part))
if book_part:
    check("the book arrives byte-for-byte intact",
          book_part[0].get_payload(decode=True).decode("utf-8") == BOOK)
check("the body still reads as an email", "The book is attached." in sent.get_body(
    preferencelist=("plain",)).get_content())
check("the body tells the reader an attachment is there",
      "ATTACHED TO THIS EMAIL" in sent.get_body(preferencelist=("plain",)).get_content())

# The failure that started all this: 35 KB of book plus the report exceeded MAX_REPLY_CHARS,
# so pasting it inline could never have worked. Prove the attachment path is not subject to it.
check("the attachment is not bound by MAX_REPLY_CHARS",
      len(BOOK) > agent_worker.MAX_REPLY_CHARS - 10000 and
      len(book_part[0].get_payload(decode=True)) == len(BOOK.encode("utf-8")))

shutil.rmtree(os.path.join(os.path.dirname(__file__), "ws3"), ignore_errors=True)
print("\n" + ("ALL DELIVERABLE TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
