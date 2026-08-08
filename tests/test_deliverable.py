"""Does the thing the agent made actually reach the human?

Both halves of task 0030 are replayed here. The agent wrote a 746-line book, then ended its
turn on "Now I'll compose the email with the full book embedded... Let me compose it." The
harness mailed that sentence. The book stayed in the workspace, and at 35,554 characters it
would have been cut off by MAX_REPLY_CHARS even if the agent had pasted it.

No API calls and no mail server: call_llm and smtplib are both stubbed.
"""
import os, sys, types, email

os.environ.update({"WORKSPACE_ROOT": os.path.join(os.path.dirname(__file__), "ws3")})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent_brain, agent_validator, agent_worker

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def replies(*answers):
    """Stub call_llm: hand back these final answers in order, never any tool calls."""
    seq = list(answers)
    seen = []

    def fake(messages):
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

# Nested paths flatten, so two index.js files cannot collide into one attachment.
NEST = os.path.join(os.path.dirname(__file__), "ws3", "task-nest")
shutil.rmtree(NEST, ignore_errors=True)
os.makedirs(os.path.join(NEST, "report"))
with open(os.path.join(NEST, "report", "chapter1.md"), "w") as fh:
    fh.write("# one")
files, _ = agent_worker.collect_attachments(NEST, started)
check("nested files keep a unique, readable name", [n for n, _ in files] == ["report_chapter1.md"],
      f"got {[n for n, _ in files]}")

check("an empty workspace attaches nothing and says nothing",
      agent_worker.collect_attachments(os.path.join(os.path.dirname(__file__), "ws3", "nope-%d" % 1)
                                       if False else NEST + "-missing", started) == ([], ""))


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


agent_worker.smtplib = types.SimpleNamespace(SMTP=FakeSMTP, SMTP_SSL=FakeSMTP)

files, note = agent_worker.collect_attachments(WS, started)
result = {"answer": "---EMAIL---\nHi Jianmin,\n\nThe book is attached.", "steps": 12,
          "stopped": "final", "transcript": [{"tool": "write_file", "by": "worker",
                                              "args": {"path": "option-trading-book.txt"}}]}
body = agent_worker.build_report(result, WS, None, note)
agent_worker.send_mail("boss@agents.local", "Re: book", body, "agent1", "agent1@agents.local",
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
