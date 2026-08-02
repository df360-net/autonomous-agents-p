"""
agent_notes.py — the agent's long-term memory across tasks.

Every task used to start from nothing. The container persists, the workspace persists, the
apps keep running — but the agent woke up with no idea any of it existed. It rediscovered
its own booking app by running `ls /workspace` and guessing from the folder names, then
copied it into the new task folder rather than fixing it where it lived. It worked, but only
because the folder happened to be named after the email subject.

So the agent keeps three files at the root of the workspace, in the same spirit as the
CLAUDE.md a human engineer leaves for the next session. They are split by how they get USED,
not by subject — you reach for a different one at a different moment:

    AGENT.md         how this machine works and how it likes to work here   (read while planning)
    AGENT-ASSETS.md  what it has built, where it lives, how to start it     (read while orienting)
    AGENT-AVOID.md   what has burned it before, and what to do instead      (scanned before acting)

THE AGENT OWNS ALL THREE. The harness never writes them, never seeds them, never parses them
for meaning and never imposes a format — they are notes to itself, and the moment we start
depending on their structure they stop being notes and become a schema it has to serve. The
harness does exactly two things:

  1. Pastes them into every task, so reading them is not something it can forget to do.
  2. Says which one is missing, so it knows to start it.

`free_port` is here for the same reason but from the other side: what a port is FOR is in
the agent's notes, but whether a port is BUSY is a fact about the machine, so we ask the
machine. Ground truth needs no format contract.
"""

import os
import socket

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
NOTES_NAME = "AGENT.md"
ASSETS_NAME = "AGENT-ASSETS.md"
AVOID_NAME = "AGENT-AVOID.md"
# Enough for a substantial working file; a cap only so a runaway file cannot crowd the task
# itself out of the context window. It is told when it has been cut, and where to read the rest.
MAX_INJECT_CHARS = int(os.environ.get("NOTES_MAX_CHARS", "8000"))

# (filename, what it is for, what to do about it being absent, what to put in it today).
# One table, so adding a fourth file is one row rather than an edit in four places — and so the
# description the agent reads when the file is EMPTY is the same one it reads when deciding
# whether today's task belongs in it.
FILES = (
    (
        NOTES_NAME,
        "how this machine works and how you work in it",
        "start it when you learn something about this machine worth knowing again",
        "the shape of this environment and the conventions you have settled on — what is "
        "installed and what is not, where things live, how you like to lay out a project, "
        "anything you would otherwise have to re-derive by poking around. Facts about the "
        "machine, not a diary of your tasks.",
    ),
    (
        ASSETS_NAME,
        "what you have built and how to run it again",
        "start it the first time you build something that outlives the task",
        "anything durable you built, changed, deployed or retired. Enough to pick it up cold: "
        "where it lives, what it does, what port and URL it serves on if any, how to start it, "
        "how to run its tests, and any decision a future you would otherwise have to re-derive "
        "from the code. Correct the existing entry rather than adding a second one for the same "
        "thing, and delete entries for things that no longer exist.",
    ),
    (
        AVOID_NAME,
        "your lessons learned: what does not work here, and what to do instead",
        "start it the first time something bites you",
        "anything that wasted your time or went wrong — a command that hung, a library that "
        "would not install, an approach that looked right and was not, a mistake you made and "
        "had to undo. Write the symptom you would actually recognise it by, then the cause, "
        "then what to do instead: a future you will be scanning this to see whether the thing "
        "you are about to try has already failed. One line each is fine. An entry that only "
        "says 'be careful with X' is worth nothing — say what happened and what works.",
    ),
)


def path_of(name):
    return os.path.join(WORKSPACE_ROOT, name)


def notes_path():
    return path_of(NOTES_NAME)


def assets_path():
    return path_of(ASSETS_NAME)


def avoid_path():
    return path_of(AVOID_NAME)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _section(name, purpose, missing_hint):
    """One file, rendered for pasting into the task. Missing is a normal state, not an error:
    on a fresh container all three are absent and the first task is the one that starts them."""
    path = path_of(name)
    head = f"=== {name} — {purpose} ({path}) ==="
    text = _read(path)
    if text is None:
        return f"{head}\n(does not exist yet — {missing_hint})"
    text = text.strip()
    if not text:
        return f"{head}\n(exists but is empty — {missing_hint})"
    if len(text) > MAX_INJECT_CHARS:
        text = (text[:MAX_INJECT_CHARS]
                + f"\n...[cut after {MAX_INJECT_CHARS} chars — read {path} yourself for the rest]")
    return f"{head}\n{text}"


def read_assets():
    """The assets file as text, or "" if it does not exist yet. Public because delivery needs
    to know which app slots are already claimed (see agent_delivery.suggest_index)."""
    return _read(assets_path()) or ""


def context_block():
    """All three files, verbatim, ready to append to a task. Injected rather than requested: an
    instruction to go and read a file is one the model can quietly skip, and this is exactly
    the information it does not know it is missing."""
    return "\n\n".join(_section(name, purpose, hint) for name, purpose, hint, _ in FILES)


def upkeep_note():
    """The closing obligation, built from the same table the files are described by."""
    lines = [
        "These files are YOURS. Nothing but you writes them, they are not graded, and there is "
        "no required format — organise them however is actually useful to you when you read them "
        "back with no memory of today. Before you finish this task, update whichever of them "
        "this task changed:",
    ]
    lines += [f"- {name}: {contents}" for name, _, _, contents in FILES]
    lines.append(
        "If this task built nothing and taught you nothing, change none of them. An honest empty "
        "update beats padding. But do not let a lesson go unwritten because it feels obvious now "
        "— it will not be obvious to you next time, because next time you will not remember it."
    )
    return "\n".join(lines)


UPKEEP_NOTE = upkeep_note()


def digest():
    """Cheap before/after fingerprint of every file, so the worker can log whether a task
    actually left anything behind. Diagnostics only — nothing branches on it."""
    return tuple(len(_read(path_of(name)) or "") for name, _, _, _ in FILES)


def describe_digest(before, after):
    """One log line: which files grew, which did not."""
    parts = [f"{name} {b}->{a}" for (name, _, _, _), b, a in zip(FILES, before, after)]
    return ("UPDATED" if before != after else "unchanged") + ": " + ", ".join(parts) + " chars"


def port_is_free(port, host="127.0.0.1"):
    """True if nothing is listening. Connect rather than bind: the servers the agent leaves
    running are in this same container and bound to 0.0.0.0, so a bind test would report a
    port free right up until the moment we tried to steal it from a live app."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) != 0


def free_port(ports, fallback_index=0):
    """First port in `ports` with nothing listening, and whether we had to evict something.

    The old rule was `base + seq % count`, which reclaimed the port with `kill -9` before
    handing it out. That is fine while the agent builds throwaways and fatal once it maintains
    them: task 22 would have shot the booking app from task 12 in the head to make room.
    Every app it leaves up now keeps its port until the range genuinely runs out.
    """
    for port in ports:
        if port_is_free(port):
            return port, False
    return ports[fallback_index % len(ports)], True
