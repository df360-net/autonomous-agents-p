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

import email
import email.utils
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
import traceback
from email.header import decode_header, make_header
from email.message import EmailMessage
from imaplib import IMAP4, IMAP4_SSL

import agent_brain
import agent_delivery
import agent_notes
import agent_validator

# ---- Config -----------------------------------------------------------------
IMAP_HOST = os.environ.get("IMAP_HOST", "mailserver")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "143"))
IMAP_SSL = os.environ.get("IMAP_SSL", "false").lower() == "true"
SMTP_HOST = os.environ.get("SMTP_HOST", "mailserver")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SMTP_SSL = os.environ.get("SMTP_SSL", "false").lower() == "true"

AGENT_ADDRESS = os.environ.get("AGENT_ADDRESS", "agent1@agents.local")
AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "agent1")
# The reviewer signs off from its own mailbox, so the sign-off visibly is not the worker
# grading its own homework. It only ever sends — it has no inbox to poll and no password.
VALIDATOR_ADDRESS = os.environ.get("VALIDATOR_ADDRESS", "validator1@agents.local")
VALIDATOR_NAME = os.environ.get("VALIDATOR_NAME", "validator1")
# Sending is a separate credential from reading: inside the compose bridge the mail server
# relays for trusted containers with no auth at all (port 25), so SMTP_USER stays empty.
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# LAN-only MVP: the mail server has no real certificate. Opportunistic STARTTLS is still
# worth having, but it must not fail the send over an unverifiable self-signed cert.
TLS_VERIFY = os.environ.get("TLS_VERIFY", "false").lower() == "true"


def tls_context():
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
# How many times a reply may be sent back for review before we give up and mail it anyway,
# flagged. 0 disables the gate entirely. Never silently drop a task: a human asked for it.
VALIDATION_ROUNDS = int(os.environ.get("VALIDATION_ROUNDS", "3"))
# Anything the agent builds that serves HTTP has to land on a port published by compose, or you
# cannot open it. One port per task, cycling through the published range.
APP_HOST = os.environ.get("APP_HOST", "192.168.0.21")
APP_PORT_BASE = int(os.environ.get("APP_PORT_BASE", "3000"))
APP_PORT_COUNT = int(os.environ.get("APP_PORT_COUNT", "10"))
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(WORKSPACE_ROOT, ".processed.json"))
MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "40000"))


def log(msg):
    print(f"[worker] {msg}", flush=True)


# ---- Idempotency ------------------------------------------------------------
# Belt and braces. The \Seen flag is set BEFORE the task runs (so a crash mid-build can
# never re-trigger a build), and every Message-ID is also recorded on disk so a restored
# mailbox or a lost flag can't make us do the same job twice.
def load_processed():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_processed(ids):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, STATE_FILE)


# ---- Mail parsing -----------------------------------------------------------
def decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def plain_body(msg):
    """Best-effort plain text of an email: prefer text/plain, else de-tag the HTML."""
    def payload(part):
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                return payload(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", payload(part))
        return ""
    if msg.get_content_type() == "text/html":
        return re.sub(r"<[^>]+>", " ", payload(msg))
    return payload(msg)


def slug(text, limit=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s[:limit] or "task").strip("-")


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
        f"I reviewed agent1's reply to \"{task_subject}\" before it went out. "
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
ATTACH_SKIP_NAMES = {"AGENT.md", "AGENT-ASSETS.md", "AGENT-AVOID.md", ".processed.json"}
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


def send_mail(to, subject, body, from_name, from_addr, in_reply_to=None, references=None,
              attachments=None):
    """Put one message on the wire and return its Message-ID.

    Split out from send_reply so a SECOND identity can use it: the reviewer signs off from its
    own mailbox, and the Message-ID comes back so its note can be threaded underneath the
    worker's reply rather than floating loose in the inbox.
    """
    reply = EmailMessage()
    reply["Subject"] = subject
    reply["From"] = f"{from_name} <{from_addr}>"
    reply["To"] = to
    reply["Date"] = email.utils.formatdate(localtime=True)
    mid = email.utils.make_msgid(domain=from_addr.split("@")[-1])
    reply["Message-ID"] = mid
    if in_reply_to:
        reply["In-Reply-To"] = in_reply_to
        reply["References"] = " ".join(filter(None, [references, in_reply_to]))
    reply.set_content(body)
    for name, data in attachments or []:
        ctype, _ = mimetypes.guess_type(name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        reply.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    if SMTP_SSL:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60, context=tls_context())
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
    with smtp:
        smtp.ehlo()
        if not SMTP_SSL and smtp.has_extn("starttls"):
            smtp.starttls(context=tls_context())
            smtp.ehlo()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(reply)
    log(f"sent {from_addr} -> {to}: {subject!r}")
    return mid


def send_reply(original, body, attachments=None):
    """The worker's answer, threaded under the email that asked for it."""
    subject = decode(original.get("Subject")) or "(no subject)"
    return send_mail(
        to=original.get("Reply-To") or original.get("From"),
        subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
        body=body,
        from_name=AGENT_NAME,
        from_addr=AGENT_ADDRESS,
        in_reply_to=original.get("Message-ID"),
        references=original.get("References"),
        attachments=attachments,
    )


# ---- One task ---------------------------------------------------------------
def handle_message(raw, seq):
    msg = email.message_from_bytes(raw)
    sender = decode(msg.get("From"))
    subject = decode(msg.get("Subject")) or "(no subject)"

    # Loop guard: never take orders from ourselves (bounces, self-CCs, mailing loops).
    if AGENT_ADDRESS.lower() in email.utils.parseaddr(msg.get("From", ""))[1].lower():
        log(f"ignoring mail from self: {subject!r}")
        return

    workspace = os.path.join(WORKSPACE_ROOT, f"task-{seq:04d}-{slug(subject)}")
    # Hand out a port nothing is listening on. The old rule cycled base + seq%count and killed
    # the incumbent, which is fine for throwaways and fatal once the agent maintains what it
    # built — task 22 would have shot task 12's booking app to make room for a scratch server.
    ports = [APP_PORT_BASE + i for i in range(APP_PORT_COUNT)]
    port, evicted = agent_notes.free_port(ports, fallback_index=seq)
    if evicted:
        log(f"all {APP_PORT_COUNT} app ports are busy — evicting whatever holds {port}")
        log(agent_brain.run_bash(f"lsof -t -i:{port} | xargs -r kill -9 || true").replace("\n", " "))

    task = (
        f"{subject}\n\n{plain_body(msg).strip()}".strip()
        + "\n\n---\n"
        "(Notes about your machine, not part of what was asked. Do not treat these as "
        "requirements and do not quote them back.)\n"
        f"Your workspace for scratch work on this task is {workspace} and it is your current "
        "directory. If this task is about something you have already built, work on it where it "
        "already lives — do not copy it here.\n"
        f"A NEW thing that serves over HTTP must listen on port {port}, which is free right now; "
        f"its URL would be http://{APP_HOST}:{port}. But if you are changing something that is "
        "ALREADY serving on a port, keep that port and redeploy it there — moving it breaks the "
        f"link people already have, and ignore the {port} above. Either way: start it in the "
        "background, curl it to confirm it answers, leave it running, and put the real URL in "
        "your reply so it can be opened in a browser. Only ports "
        f"{APP_PORT_BASE}-{APP_PORT_BASE + APP_PORT_COUNT - 1} are reachable from outside your "
        "container. Say plainly that it stays up only while the container does. If the task "
        "needs no server, ignore all of this.\n"
        "\n"
        "--- YOUR OWN NOTES FROM EARLIER TASKS ---\n"
        "You wrote these. Nobody else has touched them. They are pasted in because you have no "
        "memory of writing them.\n"
        "\n"
        f"{agent_notes.context_block()}\n"
        "\n"
        f"{agent_notes.UPKEEP_NOTE}\n"
        "\n"
        "--- DELIVERY ---\n"
        # Built from the assets file so the suggested app slot accounts for what is already
        # deployed, and suppressed entirely when there is no token — an agent told to ship
        # without credentials reports having shipped.
        f"{agent_delivery.delivery_note(agent_notes.read_assets(), bool(os.environ.get('GITHUB_TOKEN')))}"
    )
    log(f"TASK from {sender}: {subject!r} -> {workspace} (free port {port})")

    before = agent_notes.digest()
    started = time.time()          # anything newer than this in the workspace, the task made
    try:
        result = agent_brain.agent_loop(task, workspace=workspace, tag="worker",
                                        require_marker=True)
        result, review = run_review_gate(task, result, workspace)
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

    try:
        reply_mid = send_reply(msg, build_report(result, workspace, review, attach_note),
                               attachments)
    except Exception:
        log(f"COULD NOT SEND REPLY:\n{traceback.format_exc()}")
        return

    # The reviewer writes separately, from its own address, only when it actually signed the
    # work off. On a failure its objections already ride on top of the worker's reply — a
    # second email would just say the same thing twice. Threaded under the reply it approved.
    if review and review["passed"]:
        try:
            send_mail(
                to=msg.get("Reply-To") or msg.get("From"),
                subject=f"Reviewed: {subject}",
                body=build_review_email(review, subject),
                from_name=VALIDATOR_NAME,
                from_addr=VALIDATOR_ADDRESS,
                in_reply_to=reply_mid,
                references=" ".join(filter(None, [msg.get("References"), msg.get("Message-ID")])),
            )
        except Exception:
            log(f"COULD NOT SEND REVIEW EMAIL:\n{traceback.format_exc()}")


def run_review_gate(task, result, workspace):
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
    for attempt in range(1, VALIDATION_ROUNDS + 1):
        log(f"review round {attempt}/{VALIDATION_ROUNDS}")
        verdict = agent_validator.review(task, result, workspace)
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
def drain_inbox(processed):
    """Take every unread message off the inbox and return the raw ones worth running.

    Deliberately does NOT run the tasks: a build takes minutes to tens of minutes, and
    Dovecot will close an idle IMAP connection out from under us long before a slow one
    finishes. Grab the mail, flag it, hang up — then build.
    """
    if IMAP_SSL:
        imap = IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=tls_context())
    else:
        imap = IMAP4(IMAP_HOST, IMAP_PORT)
    fresh = []
    try:
        if not IMAP_SSL and "STARTTLS" in imap.capabilities:
            imap.starttls(ssl_context=tls_context())
        imap.login(AGENT_ADDRESS, AGENT_PASSWORD)
        # Check SELECT explicitly: imaplib returns ('NO', ...) instead of raising, and the
        # next SEARCH then fails with a baffling "illegal in state AUTH". Dovecot answers NO
        # for the first few seconds after it starts, before the maildir is ready.
        typ, data = imap.select("INBOX")
        if typ != "OK":
            log(f"INBOX not selectable yet ({data}) — will retry")
            return fresh
        typ, data = imap.search(None, "UNSEEN")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        if not uids:
            return fresh
        log(f"{len(uids)} unread message(s)")

        for uid in uids:
            # PEEK so fetching doesn't implicitly consume the message, then mark \Seen
            # ourselves BEFORE running it: at-most-once beats at-least-once when the
            # side effect is "an agent builds software and emails a human".
            typ, data = imap.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                log(f"could not fetch uid {uid!r}")
                continue
            raw = data[0][1]
            imap.store(uid, "+FLAGS", "\\Seen")

            mid = email.message_from_bytes(raw).get("Message-ID", "").strip()
            if mid and mid in processed:
                log(f"already handled {mid} — skipping")
                continue
            if mid:
                processed.add(mid)
                save_processed(processed)
            fresh.append(raw)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return fresh


def poll_once(processed):
    for raw in drain_inbox(processed):
        handle_message(raw, seq=len(processed))


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
    lines.append(
        f"preview ports {expected} (published {published or 'unknown'}) | "
        f"cluster app slots {agent_delivery.APP_SLOTS} "
        f"(NodePort {agent_delivery.NODE_PORT_BASE}+, browser {agent_delivery.PROXY_PORT_BASE}+)")
    return lines


def main():
    once = "--once" in sys.argv
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)
    processed = load_processed()
    log(f"{AGENT_NAME} <{AGENT_ADDRESS}> watching {IMAP_HOST}:{IMAP_PORT} "
        f"every {POLL_SECONDS}s | model={agent_brain.MODEL} | workspaces={WORKSPACE_ROOT}")
    if not AGENT_PASSWORD:
        log("WARNING: AGENT_PASSWORD is empty — IMAP login will almost certainly fail.")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log("WARNING: DEEPSEEK_API_KEY is not set — every task will abort.")
    for line in port_config_report():
        log(line)

    while True:
        try:
            poll_once(processed)
        except (IMAP4.error, OSError) as e:
            log(f"mail server not reachable ({e}) — retrying in {POLL_SECONDS}s")
        except Exception:
            log(f"unexpected error in poll cycle:\n{traceback.format_exc()}")
        if once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
