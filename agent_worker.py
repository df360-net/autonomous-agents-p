"""
agent_worker.py — the I/O adapter that turns the brain into an employee.

    agent.py  NOW:   task = sys.argv[1]       -> agent_loop() -> print(answer)
    this file:       task = next unread email -> agent_loop() -> reply email

Poll IMAP for unread mail addressed to this agent, run each message as a task in its own
workspace, then reply over SMTP with what it built, how it verified it, and a transcript
the human can actually read (the "verify, don't trust" beat stays in the loop).

Run:  python agent_worker.py            # loops forever
      python agent_worker.py --once     # one poll cycle, then exit (handy while debugging)
"""

import mimetypes
import os
import sys
import time
import traceback

import agent_brain
import agent_budget
import agent_delivery
import agent_inbox
import agent_memory
import agent_notes
import agent_outbox
import agent_peer
import agent_principal
import agent_validator
import fleet_identity

# ---- Config -----------------------------------------------------------------
# The transports own their own configuration now: IMAP and the \Seen/dedupe machinery live in
# agent_inbox, SMTP in agent_outbox. What is left here is the work, not the plumbing.
#
# Identity is derived from TENANT and AGENT_NAME in one place — see fleet_identity for why
# four independent environment variables for one identity was a bug waiting to be typed.
AGENT_ADDRESS = fleet_identity.AGENT_ADDRESS
AGENT_NAME = fleet_identity.NAME
# The reviewer signs off from its own mailbox, so the sign-off visibly is not the worker
# grading its own homework. It only ever sends — it has no inbox to poll and no password.
VALIDATOR_ADDRESS = fleet_identity.VALIDATOR_ADDRESS
VALIDATOR_NAME = fleet_identity.VALIDATOR_NAME

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
# How many times a reply may be sent back for review before we give up and mail it anyway,
# flagged. 0 disables the gate entirely. Never silently drop a task: a human asked for it.
VALIDATION_ROUNDS = int(os.environ.get("VALIDATION_ROUNDS", "3"))
# Anything the agent builds that serves HTTP has to land on a port published by compose, or you
# cannot open it. One port per task, cycling through the published range.
# APP_HOST is gone. It named the machine a human would open a preview on, which stopped being a
# single answerable thing once an agent became a pod that can run on either box — and stopped
# being answerable at all while nothing publishes a pod's ports. The preview note now says
# `localhost` and says plainly that a browser cannot reach it, which is true from inside the
# container where the agent does its verifying. Deployed apps get their address from the fleet.
APP_PORT_BASE = int(os.environ.get("APP_PORT_BASE", "3000"))
APP_PORT_COUNT = int(os.environ.get("APP_PORT_COUNT", "10"))
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "40000"))


def log(msg):
    print(f"[worker] {msg}", flush=True)


# ---- Reporting --------------------------------------------------------------
strip_preamble = agent_brain.strip_preamble   # lives in the brain: it defines the marker


def flatten(command, limit=200):
    """One command, one readable line. A multi-line `python3 -c "..."` used to be recorded as
    just `python3 -c "` — its first line — which told the reader exactly nothing on the one
    email where the maths was wrong. Collapse the whitespace instead of cutting at the newline."""
    line = " ".join((command or "").split())
    return line[:limit] + ("..." if len(line) > limit else "") or "(empty)"


def build_report(result, workspace, review=None, attach_note=""):
    """The reply body. If the reviewer rejected it, that goes ON TOP — you should not have to
    scroll past a confident-sounding answer to find out it failed review. Then the agent's own
    answer, then the evidence: what it actually ran. You read this instead of trusting it."""
    verdict = {
        "final": "completed",
        "max_steps": "STOPPED — hit the step limit before finishing",
        "error": "FAILED — the run aborted",
    }.get(result["stopped"], result["stopped"])

    # Files carry their author too — /tmp/draft.txt looked like the worker's until you noticed
    # the reviewer had created it to have something to run `wc -w` against.
    files = dict.fromkeys(
        f"{c['args'].get('path', '')}   [{who_ran(c)}]"
        for c in result["transcript"] if c["tool"] == "write_file"
    )

    parts = []
    if review and not review["passed"]:
        parts += [
            "!! THIS DID NOT PASS REVIEW — sending it anyway so you can judge. "
            f"({review['rounds']} attempt(s))",
            "The reviewer's remaining objections:",
            "",
            review["notes"] or "(none given)",
            "",
            "=" * 60,
            "",
        ]
    parts += [
        strip_preamble(result["answer"]),
        "",
        "-" * 60,
        # `steps` is None when the crash happened outside the step loop and the count is
        # genuinely unknown. Say so rather than printing a number that is not true.
        (f"run: {verdict} in {result['steps']} steps, {len(result['transcript'])} tool calls"
         if result.get("steps") is not None
         else f"run: {verdict} — step count unknown (crashed outside the step loop)"),
        # On the same footer as the steps and the review verdict, because the price of a task
        # is evidence about the task, and evidence belongs where the reader already looks.
        agent_budget.task_summary(),
    ]
    if review:
        parts.append(
            f"review: {'PASSED' if review['passed'] else 'NOT PASSED'} "
            f"after {review['rounds']} round(s)"
        )
    # High up, directly under the run line: the reader must know an artefact came with this
    # email before they start scrolling through the evidence block looking for one.
    if attach_note:
        parts += ["", attach_note]
    # Only append the evidence block when there is evidence. A question answered in one step
    # shouldn't arrive with an empty "FILES WRITTEN" section stapled to it.
    if result["transcript"]:
        parts += [f"workspace: {workspace}", ""]
        if files:
            parts += ["FILES WRITTEN", "\n".join(f"  {f}" for f in files), ""]
        parts += [
            "EVERYTHING THAT WAS RUN (in order, by whom)",
            render_calls(result["transcript"]),
        ]
    body = "\n".join(parts)
    if len(body) > MAX_REPLY_CHARS:
        body = body[:MAX_REPLY_CHARS] + "\n...[report truncated]"
    return body


def who_ran(call):
    """agent_loop stamps each call with its tag; turn that into the mailbox name you know."""
    return {"worker": AGENT_NAME, "reviewer": VALIDATOR_NAME}.get(call.get("by"), AGENT_NAME)


def as_command(call):
    return (call["args"].get("command", "") if call["tool"] == "run_bash"
            else f"{call['tool']} {call['args'].get('path', '')}")


def render_calls(transcript, label=True):
    """One numbered list in true execution order, but grouped under [agent1] / [validator1]
    headers wherever the author changes. Keeping a single ordered list preserves the trail;
    the headers stop it reading as if one agent did all of it."""
    lines, current = [], None
    for i, call in enumerate(transcript, 1):
        if label:
            name = who_ran(call)
            if name != current:
                if lines:
                    lines.append("")
                lines.append(f"[{name}]")
                current = name
        lines.append(f"  {i:3}. {flatten(as_command(call))}")
    return "\n".join(lines)


def build_review_email(review, task_subject):
    """The reviewer's own sign-off. Its prose comes first — but the commands it ran are
    appended from the harness's record, so 'here is how I checked' can be checked."""
    parts = [
        # AGENT_NAME, not the literal "agent1". This is validator3 writing about agent3's
        # work, and the hardcoded name made every reviewer in the fleet credit agent1 — in an
        # email whose entire purpose is to show that the reviewer is a different identity from
        # the worker. Invisible with one agent; wrong on three quarters of the fleet.
        f"I reviewed {AGENT_NAME}'s reply to \"{task_subject}\" before it went out. "
        f"It passed on round {review['rounds']}.",
        "",
        review["notes"] or "(no detail given)",
    ]
    if len(review["history"]) > 1:
        parts += ["", "ROUNDS", ""]
        for h in review["history"]:
            if h["passed"]:
                parts.append(f"  round {h['round']}: passed")
            else:
                first = (h["notes"] or "").strip().splitlines()
                parts.append(f"  round {h['round']}: sent back — {first[0] if first else '(no note)'}")
        parts.append("")
        parts.append("The reply you received is the corrected one.")
    if review["transcript"]:
        # No labels here — everything in this list is the reviewer's own.
        parts += ["", "-" * 60, "WHAT I RAN MYSELF (in order)",
                  render_calls(review["transcript"], label=False)]
    parts += ["", "I did not do the work; I only checked it."]
    body = "\n".join(parts)
    return body[:MAX_REPLY_CHARS]


# ---- Attachments -------------------------------------------------------------
# Whether a document reached the human used to depend entirely on the agent retyping it into
# the reply. Task 0030 broke both halves of that at once: the agent narrated ("Now I'll compose
# the email...") instead of pasting, and at 35,554 characters the book could not have survived
# MAX_REPLY_CHARS even if it had pasted it perfectly. The workspace is the ground truth for what
# was produced, so the harness ships the artefacts itself and stops depending on the prose.
ATTACH_MAX_FILES = int(os.environ.get("ATTACH_MAX_FILES", "12"))
ATTACH_MAX_BYTES = int(os.environ.get("ATTACH_MAX_BYTES", str(2 * 1024 * 1024)))
ATTACH_MAX_TOTAL = int(os.environ.get("ATTACH_MAX_TOTAL", str(8 * 1024 * 1024)))
ATTACH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist",
                    "build", ".next", "target", ".cache", "coverage", ".pytest_cache"}
# The agent's own memory, which it rewrites on almost every task, and the harness's state.
# Mailing these back would attach three files nobody asked for to every single reply.
ATTACH_SKIP_NAMES = {"AGENT.md", "AGENT-ASSETS.md", "AGENT-AVOID.md", ".processed.json",
                     # Harness state that lives in the scan root and is written on every run.
                     # The ledger gets a line per LLM call, so without this every single reply
                     # would arrive with the agent's spend history stapled to it.
                     ".spend.jsonl", "FLEET-PAUSED", "budget.json"}
# Live application data, not deliverables. Apps built by earlier tasks keep serving inside this
# container, so a request arriving mid-run touches their database — and an expense tracker's
# sqlite file must never be posted to anyone because the timestamp made it look fresh.
ATTACH_SKIP_EXTS = {".log", ".pid", ".lock", ".sock", ".pyc", ".db", ".db-journal", ".db-wal",
                    ".db-shm", ".sqlite", ".sqlite3"}
# Build scaffolding. Asked for one self-contained Markdown file, the harness sent ten: the book
# plus package.json, package-lock.json, puppeteer-config.json and the scripts that generated it.
# None of these is ever the thing someone asked for, and one more of them would have crossed
# ATTACH_MAX_FILES and attached NOTHING — the loud failure hiding behind the untidy one.
ATTACH_SKIP_SCAFFOLD = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
                        "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "tsconfig.json",
                        ".gitignore", ".dockerignore", ".npmrc", ".editorconfig"}

# How the agent says "this one, not the fifteen scratch files next to it". Writing this file is
# optional and its absence is not an error: no nomination means the heuristics above decide, so
# an agent that never learns about it still delivers.
DELIVERABLES_FILE = "DELIVERABLES"


def _is_scaffold(name):
    low = name.lower()
    return (name in ATTACH_SKIP_SCAFFOLD or low.endswith("-config.json")
            or low.endswith(".config.json") or low.endswith(".config.js")
            or low.endswith(".config.mjs"))


def _read_nomination(path, root):
    """Parse a DELIVERABLES file into (paths, problems).

    One path per line, blank lines and # comments ignored. Relative paths resolve against the
    file's own directory, which is what an agent writing `option-trading-book.md` means. A path
    that escapes the workspace root is refused outright — attaching a file to an outgoing email
    is the one thing in this container that reaches beyond it, so /etc/shadow must not be
    nameable however it got into the list.
    """
    paths, problems = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return [], [f"could not read {DELIVERABLES_FILE}: {e}"]

    base, real_root = os.path.dirname(path), os.path.realpath(root)
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        full = entry if os.path.isabs(entry) else os.path.join(base, entry)
        full = os.path.realpath(full)
        if os.path.commonpath([full, real_root]) != real_root:
            problems.append(f"{entry} (outside the workspace — refused)")
        elif not os.path.isfile(full):
            problems.append(f"{entry} (named in {DELIVERABLES_FILE} but not on disk)")
        elif full not in paths:
            paths.append(full)
    return paths, problems


def collect_attachments(root, since, task_workspace=None):
    """Everything the task produced, ready to hang on the reply.

    Returns (files, note): files is [(filename, bytes)], note is a line for the report — or
    "" when there is nothing to say. Anything skipped is named in the note, because silently
    dropping a deliverable is the failure this function exists to prevent.

    The directory is the evidence, not the transcript. The book in task 0030 was appended with
    six `cat >>` heredocs through run_bash, so a list built from write_file calls would have
    captured only its first 3,722 characters and looked complete while being a third of a book.

    `root` is the whole workspace root, NOT the current task's folder. The agent is told to
    work on existing things where they already live, so the follow-up task that corrected that
    same book edited it in task-0030's directory while running as task-0031 — scoped to the new
    folder this function would have found nothing and attached nothing, on the very task whose
    entire purpose was to hand back a corrected file.
    """
    candidates, nominations = [], []
    for parent, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ATTACH_SKIP_DIRS and not d.startswith(".")]
        for name in names:
            path = os.path.join(parent, name)
            try:
                st = os.stat(path)
            except OSError:
                continue                      # vanished mid-walk; nothing to attach
            # A whole second of slack: the run's own files are minutes newer than `since`, and
            # coarse filesystem timestamps should not lose one that was written immediately.
            fresh = st.st_mtime >= since - 1
            if name == DELIVERABLES_FILE and fresh:
                nominations.append(path)
                continue
            if (name in ATTACH_SKIP_NAMES or _is_scaffold(name)
                    or os.path.splitext(name)[1].lower() in ATTACH_SKIP_EXTS):
                continue
            if not st.st_size or not fresh:
                continue
            candidates.append((path, st.st_size, st.st_mtime))

    # An explicit nomination outranks every heuristic above: the agent knows which file it was
    # asked for, and the harness is only guessing. A nominated file is attached even if the
    # skip lists would have dropped it — someone may genuinely have asked for a package.json.
    problems = []
    if task_workspace:
        nominations.sort(key=lambda p: os.path.dirname(p) != os.path.normpath(task_workspace))
    if nominations:
        chosen, problems = _read_nomination(nominations[0], root)
        picked = []
        for path in chosen:
            try:
                picked.append((path, os.path.getsize(path), 0))
            except OSError as e:
                problems.append(f"{os.path.basename(path)} (unreadable: {e})")
        candidates = picked
    elif len(candidates) > ATTACH_MAX_FILES:
        # A source tree is not a deliverable. Twelve arbitrary files chosen out of forty would
        # be noise pretending to be a delivery, and the code ships through GitHub anyway.
        return [], (f"NOT ATTACHED: the task produced {len(candidates)} files — too many to "
                    f"attach, and it left no {DELIVERABLES_FILE} file saying which ones matter. "
                    f"They are in the workspace, and any app was shipped to GitHub.")

    if not candidates and not problems:
        return [], ""

    files, skipped, total, used = [], problems, 0, set()
    # Nominated order is the agent's own; otherwise newest first.
    order = candidates if nominations else sorted(candidates, key=lambda c: -c[2])
    if len(order) > ATTACH_MAX_FILES:      # only reachable via a long nomination list
        skipped.append(f"{len(order) - ATTACH_MAX_FILES} further nominated file(s), over the "
                       f"{ATTACH_MAX_FILES}-attachment limit")
        order = order[:ATTACH_MAX_FILES]
    for path, size, _ in order:
        # The plain filename, because that is what the recipient wants to save. Only when two
        # files would land on the same name does it grow a qualifier — scanning the whole
        # workspace root means index.html from two different tasks can genuinely collide, and
        # one silently overwriting the other in someone's downloads folder is a real loss.
        rel = os.path.basename(path)
        if rel in used:
            rel = f"{os.path.basename(os.path.dirname(path))}_{rel}"
        n = 2
        while rel in used:
            stem, ext = os.path.splitext(rel)
            rel, n = f"{stem}-{n}{ext}", n + 1
        used.add(rel)
        if size > ATTACH_MAX_BYTES:
            skipped.append(f"{rel} ({size // 1024} KB, over the {ATTACH_MAX_BYTES // 1024} KB "
                           f"per-file limit)")
            continue
        if total + size > ATTACH_MAX_TOTAL:
            skipped.append(f"{rel} (would exceed the {ATTACH_MAX_TOTAL // 1024} KB total)")
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            skipped.append(f"{rel} (unreadable: {e})")
            continue
        files.append((rel, data))
        total += size

    note = ""
    if files:
        note = "ATTACHED TO THIS EMAIL\n" + "\n".join(
            f"  {n}   ({len(d):,} bytes)" for n, d in files)
    if skipped:
        note += ("\n" if note else "") + "NOT ATTACHED (still in the workspace)\n" + "\n".join(
            f"  {s}" for s in skipped)
    return files, note


# ---- One task ---------------------------------------------------------------
def task_seq(task_id):
    """The sequence number out of a task id ("task-0037-build-a-thing" -> 37).

    Only used to spread the fallback port choice deterministically, so any stable integer
    would do; reading it back out of the id keeps the worker from needing a counter of its own.
    """
    try:
        return int(task_id.split("-")[1])
    except (IndexError, ValueError):
        return 0


def export_thread_context(envelope):
    """Put this task's mail identity in the environment, and return the reply's Message-ID.

    WHY THE ENVIRONMENT AND NOT AN ARGUMENT. `ship_app` registers the app with the fleet
    control plane, and the control plane needs to know which conversation to answer in when it
    later emails "it's live". But ship_app is a SEPARATE PROCESS — the agent invokes it through
    the shell — so it cannot see the envelope, agent_peer's task state, or anything else this
    module holds. Environment is what a child process inherits.

    THE MESSAGE-ID IS MINTED HERE, BEFORE ANYTHING IS SENT. Registration happens in the middle
    of the task and the reply does not exist yet, so "the Message-ID of the agent's reply" is
    not a value that can be read — it has to be decided in advance and then honoured. The same
    trick agent_peer already uses for signing, and for the same reason: an id that is generated
    at send time is an id nothing earlier could have referenced. `deliver()` is handed this
    value below, so the message that eventually goes out really does carry it.

    NOTE FOR WHOEVER CONSUMES THIS: the agent's shell can overwrite these variables, because
    the agent's shell can overwrite anything in its own environment. They are a ROUTING HINT,
    not an authorisation — fine for "which thread does this belong to", never a basis for
    deciding who may be told what. Every message the fleet sends is copied to the boss anyway,
    so a misdirected follow-up is visible rather than silent.
    """
    reply_mid = agent_outbox.new_message_id()
    os.environ.update({
        "FLEET_THREAD_REQUESTER": envelope.reply_to or envelope.requester or "",
        "FLEET_THREAD_SUBJECT": agent_outbox.reply_subject(envelope.subject or "(no subject)"),
        "FLEET_THREAD_MESSAGE_ID": reply_mid,
        "FLEET_THREAD_REFERENCES": " ".join(
            filter(None, [envelope.references, envelope.message_id])),
        "FLEET_THREAD_AGENT_ID": fleet_identity.AGENT_ID,
    })
    return reply_mid


def peer_note():
    """The standing paragraph about colleagues. Empty when this agent is alone in the fleet —
    an agent told it has colleagues it does not have will offer to consult them."""
    peers = agent_peer.peers()
    if not peers:
        return ("\n--- YOUR COLLEAGUES ---\n"
                "You are the only agent in this fleet at the moment, so `message_agent` has "
                "nobody to write to. Do the work yourself.")
    return (
        "\n--- YOUR COLLEAGUES ---\n"
        f"Other agents work alongside you in tenant {fleet_identity.TENANT}: "
        f"{', '.join(peers)}. Each runs in its own container with its own workspace, and "
        "reaches its own mailbox — you cannot see their files and they cannot see yours.\n"
        "You share AGENT.md and AGENT-AVOID.md with them: lessons you write go to all of "
        "them, and some of what you read there was written by one of them on a DIFFERENT "
        "machine. AGENT-ASSETS.md is yours alone, so what you built is not in their inventory "
        "and what they built is not in yours.\n"
        "Use `message_agent` to ask one of them something — most usefully to review work you "
        "are unsure of, or when a task concerns an app THEY built and you did not (you cannot "
        "push to their repositories; ship_app will refuse). It is email, so the answer comes "
        "back long after this task has ended.\n"
        f"Every message between agents is copied to {agent_peer.BOSS_ADDRESS}, always. Write "
        "them as something a human reads over your shoulder, because one does."
    )


def run(envelope):
    """Do one task, end to end, from an envelope — never from a message.

    Every check below reads an envelope field. That is the whole substance of D3: the loop
    guard used to parse a From header, dedupe used to read Message-ID, threading used to walk
    References. Those checks would all have had to be rewritten the day a task arrived from
    something that is not mail, which is the worst possible code to be rewriting.
    """
    # ADMISSION FIRST, before anything else touches this envelope. Who is asking, whether the
    # machine fields on it are believable, and whether it may run at all — resolved once, in
    # one place, and never re-litigated below. `admit` hands back a SANITISED envelope; using
    # the original after this point would put the sender's own claims back in play, so it is
    # rebound rather than kept alongside.
    decision = agent_principal.admit(envelope)
    if not decision.allowed:
        # Logged and dropped, never answered. A refusal that replies is a refusal that can be
        # aimed: it turns the agent into a way to send mail to a third party, and if the cause
        # was a loop, the reply is another lap.
        log(f"REFUSED {envelope.task_id} from {envelope.requester!r}: {decision.reason}")
        return
    envelope = decision.envelope
    principal = decision.principal

    sender = envelope.requester
    subject = envelope.subject

    # Pull memory BEFORE the notes are read into the prompt, so this task sees what any other
    # agent in the tenant learned since the last one. Failure here is logged, not fatal: the
    # local clone is still there and a task should not die because a git remote is down.
    for line in agent_memory.sync(WORKSPACE_ROOT, agent_notes.SCOPES):
        log(line)

    workspace = os.path.join(WORKSPACE_ROOT, envelope.task_id)
    # Hand out a port nothing is listening on. The old rule cycled base + seq%count and killed
    # the incumbent, which is fine for throwaways and fatal once the agent maintains what it
    # built — task 22 would have shot task 12's booking app to make room for a scratch server.
    ports = [APP_PORT_BASE + i for i in range(APP_PORT_COUNT)]
    port, evicted = agent_notes.free_port(ports, fallback_index=task_seq(envelope.task_id))
    if evicted:
        log(f"all {APP_PORT_COUNT} app ports are busy — evicting whatever holds {port}")
        log(agent_brain.run_bash(f"lsof -t -i:{port} | xargs -r kill -9 || true").replace("\n", " "))

    # STANDING CONTEXT — the same bytes on almost every task: the agent's own notes, the upkeep
    # duty, the delivery rules. It goes in the SYSTEM message, not the user message, for two
    # reasons that pull the same way.
    #
    # Cache: measured on task-0033, the first call of each conversation was only 16-26% cached
    # while every later call was 99%. The prefix is already stable WITHIN a run, so the win is
    # not there — it is that a new conversation re-sends this whole block as novel tokens, and
    # there are two conversations per task because the reviewer starts fresh. Hoisting it into
    # the system message makes it a prefix that is stable ACROSS tasks and later across agents.
    #
    # Reading order: the alternative — putting it at the head of the user message — would bury
    # the actual request under thousands of tokens of notes. In the system message the request
    # stays the last thing the model reads, which is where it should be.
    standing = (
        "\n\n--- YOUR OWN NOTES FROM EARLIER TASKS ---\n"
        "You wrote these. Nobody else has touched them. They are pasted in because you have no "
        "memory of writing them.\n"
        "\n"
        f"{agent_notes.context_block()}\n"
        "\n"
        f"{agent_notes.UPKEEP_NOTE}\n"
        "\n"
        # Who else exists. The agent cannot discover this: its own notes are about its own
        # machine, and nothing else in the prompt names another agent. Kept to the facts —
        # the tool description carries the mechanics, and this is the standing context that
        # tells it there is anybody to use the tool on.
        f"{peer_note()}\n"
        "\n"
        "--- DELIVERY ---\n"
        # Built from the assets file so the suggested app slot accounts for what is already
        # deployed, and suppressed entirely when there is no token — an agent told to ship
        # without credentials reports having shipped.
        f"{agent_delivery.delivery_note(agent_notes.read_assets(), bool(os.environ.get('GITHUB_TOKEN')))}"
    )

    # THE REQUEST — what actually arrived, plus the two facts that are specific to this task.
    task = (
        f"{subject}\n\n{envelope.body}".strip()
        + "\n\n---\n"
        "(Notes about your machine, not part of what was asked. Do not treat these as "
        "requirements and do not quote them back.)\n"
        f"Your workspace for scratch work on this task is {workspace} and it is your current "
        "directory. If this task is about something you have already built, work on it where it "
        "already lives — do not copy it here.\n"
        f"A NEW thing that serves over HTTP must listen on port {port}, which is free right now. "
        f"Verify it with `curl http://localhost:{port}` — from inside this container, which is "
        "the only place it answers. A PREVIEW IS NOT REACHABLE FROM A BROWSER right now: this "
        "agent runs as a pod and nothing publishes its ports outside the cluster yet. So do not "
        "offer a preview address as something a person can open; say you tested it locally, and "
        "let the deployed app be the thing anyone visits. But if you are changing something that "
        "is ALREADY serving on a port, keep that port and redeploy it there — moving it breaks "
        f"the link people already have, and ignore the {port} above. Either way: start it in the "
        "background, curl it to confirm it answers, and leave it running. Only ports "
        f"{APP_PORT_BASE}-{APP_PORT_BASE + APP_PORT_COUNT - 1} are reachable from outside your "
        "container. Say plainly that it stays up only while the container does. If the task "
        "needs no server, ignore all of this."
        # Only ever non-empty when a peer sent this task. Last in the block because it is the
        # one machine fact that changes what the agent should WRITE rather than where it should
        # work, and the closing instruction is the one worth reading nearest the end.
        + agent_principal.conversation_note(
            envelope.hops, principal.principal_id if principal.kind == "agent" else "")
    )
    log(f"TASK from {principal}: {subject!r} -> {workspace} (free port {port})")

    # Scopes every LLM call from here on to this task and this requester, so the ledger can
    # answer "what did that email cost?" rather than only "what did today cost?".
    agent_budget.start_task(envelope.task_id, requester=sender)
    # Binds the hop count and thread of THIS task so `message_agent` can increment from them.
    # The agent never sees these values and cannot reach them from a tool argument, which is
    # the only reason the hop limit means anything.
    agent_peer.start_task(envelope, principal)
    reply_mid = export_thread_context(envelope)

    before = agent_notes.digest()
    started = time.time()          # anything newer than this in the workspace, the task made
    try:
        result = agent_brain.agent_loop(task, workspace=workspace, tag="worker",
                                        require_marker=True,
                                        system_prompt=agent_brain.SYSTEM_PROMPT + standing)
        result, review = run_review_gate(task, result, workspace, standing)
    except Exception:
        tb = traceback.format_exc()
        log(f"worker crashed running the task:\n{tb}")
        # Last resort only — agent_loop now returns a result rather than raising, so reaching
        # here means something outside the step loop broke. Do not claim "0 steps": this
        # message once accompanied a task that had already committed, pushed and deployed a
        # fix, and the contradiction sent the reader looking for a problem that did not exist.
        result = {"answer": f"The run crashed before finishing:\n\n{tb}\n\n"
                            f"Any work completed before the crash is still in {workspace} — "
                            f"check there before assuming the task did nothing.",
                  "steps": None, "transcript": [], "stopped": "error"}
        review = None

    # Diagnostic only — nothing depends on it, but "did it keep its own notes up to date?" is
    # the one question this whole mechanism turns on, and it should be answerable from the log.
    log("notes " + agent_notes.describe_digest(before, agent_notes.digest()))
    # Push whatever it wrote, as the harness rather than as the agent. Durability must not
    # depend on the model remembering a step at the end of a long task — the same reason the
    # budget check lives inside call_llm instead of in the prompt.
    log(agent_memory.publish(envelope.task_id, note=f"task: {subject}\nrequester: {sender}"))

    # Collected after the review gate so a reworked deliverable is the one that ships. Never
    # let a failure here cost the reply itself — an email with no attachment still carries the
    # answer, whereas an exception on the way to the wire loses the whole run.
    try:
        # WORKSPACE_ROOT, not `workspace`: work on an existing thing happens in the folder that
        # thing already lives in, which is a different task's folder than this one.
        attachments, attach_note = collect_attachments(WORKSPACE_ROOT, started, workspace)
        if attachments:
            log(f"attaching {len(attachments)} file(s): "
                + ", ".join(n for n, _ in attachments))
    except Exception:
        log(f"could not collect attachments:\n{traceback.format_exc()}")
        attachments, attach_note = [], ("NOT ATTACHED: the harness could not read the "
                                        "workspace. The files are still there.")

    # When a PEER asked, the answer has to be signed and hop-stamped like the request was —
    # an unsigned reply arriving back at agent1 is refused by its own admission check, so the
    # conversation would die silently on the return leg. The boss is copied on that leg too.
    peer_extras = agent_peer.reply_extras(envelope, principal)
    # The id promised to the control plane at registration time must be the id that actually
    # goes on the wire, or governance's "it's live" email threads onto a message that never
    # existed and starts its own conversation. A peer reply mints its own — the signature
    # covers it — and in that case the promise was never made, because registering an app is
    # not something a peer-to-peer exchange does.
    # A reply that exhausted the rounds still goes out — never drop a task a human asked for —
    # but until now that fact lived only in the body, where nothing but a human reading it can
    # act on it. The subject cannot carry it: it was promised to the control plane at
    # registration time and governance threads its follow-up onto that exact string. A header
    # is the channel that stays machine-readable without breaking a promise already made.
    review_headers = {}
    if review and not review["passed"]:
        review_headers["X-Agent-Review"] = f"NOT-PASSED after {review['rounds']} round(s)"
    try:
        reply_mid = agent_outbox.deliver(
            envelope, build_report(result, workspace, review, attach_note), attachments,
            **{"message_id": reply_mid, **peer_extras,
               "headers": {**peer_extras.get("headers", {}), **review_headers}})
    except Exception:
        log(f"COULD NOT SEND REPLY:\n{traceback.format_exc()}")
        return

    # The reviewer writes separately, from its own address, only when it actually signed the
    # work off. On a failure its objections already ride on top of the worker's reply — a
    # second email would just say the same thing twice. Threaded under the reply it approved.
    if review and review["passed"]:
        try:
            agent_outbox.deliver_review(
                envelope, build_review_email(review, subject), under=reply_mid)
        except Exception:
            log(f"COULD NOT SEND REVIEW EMAIL:\n{traceback.format_exc()}")


def run_review_gate(task, result, workspace, standing=""):
    """Build -> review -> rework, until it passes or we run out of patience.

    Returns (final_result, review) where review is {"passed", "notes", "rounds"} — or
    (result, None) when the gate is switched off. The result is returned rather than mutated
    because each rework produces a NEW result dict; rebinding it locally would silently mail
    the original answer and throw every fix away.

    On the final round we stop reworking and send regardless: a task that vanishes because a
    reviewer would not be satisfied is worse than one that arrives with the objections
    attached. The human stays the last word, which is the whole point.
    """
    if VALIDATION_ROUNDS < 1 or result["stopped"] == "error":
        return result, None

    history, review_calls = [], []
    forced_recheck = False
    for attempt in range(1, VALIDATION_ROUNDS + 1):
        log(f"review round {attempt}/{VALIDATION_ROUNDS}")
        # The reviewer gets the standing context too — it needs the delivery rules to judge a
        # delivery claim, and it used to receive them only because they were glued to the task.
        verdict = agent_validator.review(task, result, workspace, standing=standing)
        history.append({"round": attempt, "passed": verdict["passed"], "notes": verdict["notes"]})
        review_calls.extend(verdict["transcript"])
        # The reviewer's own tool calls belong in the worker's evidence too — it re-ran things,
        # and you should be able to see what it checked. Kept separately as well, because its
        # sign-off email lists only what IT ran.
        result["transcript"].extend(verdict["transcript"])
        result["steps"] += verdict["steps"]
        summary = {"notes": verdict["notes"], "rounds": attempt,
                   "history": history, "transcript": review_calls}
        if verdict["passed"]:
            # A PASS that forgives something it noticed gets looked at once more. Both measured
            # rubber stamps had this shape: the false claim found, called "one small wording
            # note", and passed anyway. Costs one extra review on a shape that has not yet been
            # a correct pass, and nothing at all on a clean one — sound work has no excuse in it.
            #
            # ONCE per task, and NO REWORK in between. Nothing was rejected, so there is nothing
            # for the worker to fix; sending it back would invite it to edit an answer that may
            # well be right. And a reviewer that hedges every time must not be able to burn the
            # whole budget re-reviewing itself.
            # `attempt < VALIDATION_ROUNDS` because a recheck needs a round to happen in. On the
            # last one there is none, and falling out of the loop instead returns None — which
            # the caller reads as "the gate was switched off", turning a hedged pass into an
            # unreviewed one. The stricter-looking branch would have been the weaker outcome.
            if (not forced_recheck and attempt < VALIDATION_ROUNDS
                    and agent_validator.hedged_pass(verdict.get("answer", ""))):
                forced_recheck = True
                log("review passed but hedged — re-reviewing once before accepting")
                history[-1]["forced_recheck"] = True
                continue
            log(f"review PASSED on round {attempt}")
            return result, dict(summary, passed=True)

        log(f"review FAILED on round {attempt}: {verdict['notes'][:200]}")
        if attempt == VALIDATION_ROUNDS:
            return result, dict(summary, passed=False)

        # Hand the objections back to the SAME worker conversation — it keeps everything it
        # already knows and just fixes the problem, like pushing a commit after review.
        rework = agent_brain.agent_loop(
            agent_validator.REWORK_TEMPLATE.format(notes=verdict["notes"]),
            workspace=workspace,
            messages=result["messages"],
            tag="worker",
        )
        rework["transcript"] = result["transcript"] + rework["transcript"]
        rework["steps"] += result["steps"]
        result = rework
        if result["stopped"] == "error":
            return result, dict(summary, passed=False)
    return result, None


# ---- The inbox loop ---------------------------------------------------------
def poll_once(processed):
    # One line, and it is the seam: swap agent_inbox for a broker and nothing below moves.
    for envelope in agent_inbox.fetch(processed):
        run(envelope)


def port_config_report(published=None):
    """Lines to log at startup about the two port ranges, warning first if they disagree.

    Compose publishes a literal RANGE ("3000-3009") while this worker hands ports out from
    APP_PORT_BASE + APP_PORT_COUNT, and nothing keeps the two in step — Compose cannot compute
    one from the other. Raise the count alone and the extra ports are simply unreachable from
    outside the container, which presents as a broken app rather than a misconfiguration.

    Returns lines instead of logging directly so the mismatch can actually be tested; the
    first version of this was inline in main() and could only be checked by running the whole
    worker against a live mail server.
    """
    if published is None:
        published = os.environ.get("APP_PORT_RANGE")
    expected = f"{APP_PORT_BASE}-{APP_PORT_BASE + APP_PORT_COUNT - 1}"
    lines = []
    if published and published != expected:
        lines.append(
            f"WARNING: APP_PORT_RANGE={published} but APP_PORT_BASE/APP_PORT_COUNT imply "
            f"{expected}. Ports outside the published range are NOT reachable from outside "
            f"this container — fix one of them in .env and recreate the container.")
    # No browser port any more: agent_delivery.PROXY_PORT_BASE is gone, because the control
    # plane assigns addresses and nothing here computes one. Referencing it would now be an
    # AttributeError in the startup banner — the loudest possible place to leave a stale
    # reference, and the reason a grep for the removed name has to include the log lines.
    lines.append(
        f"preview ports {expected} (published {published or 'unknown'}) | "
        f"cluster app slots {agent_delivery.APP_SLOTS} "
        f"(NodePort {agent_delivery.NODE_PORT_BASE}+ suggested; the fleet allocates)")
    return lines


def main():
    once = "--once" in sys.argv
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)
    processed = agent_inbox.load_processed()
    log(f"{AGENT_NAME} <{AGENT_ADDRESS}> watching {agent_inbox.IMAP_HOST}:{agent_inbox.IMAP_PORT} "
        f"every {POLL_SECONDS}s | model={agent_brain.MODEL} | workspaces={WORKSPACE_ROOT}")
    if not agent_inbox.AGENT_PASSWORD:
        log("WARNING: AGENT_PASSWORD is empty — IMAP login will almost certainly fail.")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log("WARNING: DEEPSEEK_API_KEY is not set — every task will abort.")
    # Memory first, and NOT inside the poll loop's exception handler: sync() exits the process
    # when there is neither a remote nor a local clone, and that exit has to happen here where
    # it is visible in `docker logs`, not be swallowed as a bad poll cycle.
    for line in agent_memory.sync(WORKSPACE_ROOT, agent_notes.SCOPES):
        log(line)
    for line in (port_config_report() + agent_principal.startup_report()
                 + agent_budget.startup_report() + [agent_memory.status()]):
        log(line)

    while True:
        try:
            poll_once(processed)
        except (agent_inbox.IMAP4.error, OSError) as e:
            log(f"mail server not reachable ({e}) — retrying in {POLL_SECONDS}s")
        except Exception:
            log(f"unexpected error in poll cycle:\n{traceback.format_exc()}")
        if once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
