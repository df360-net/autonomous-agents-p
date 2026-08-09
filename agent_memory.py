"""agent_memory.py — the agent's memory as a git repository outside the container.

D5 in docs/Fleet-Design.md, and the change almost everything else in phase 2 rests on. Until
now memory was three markdown files in a docker volume, which meant the agent was a *pet*: you
could not delete and recreate its container without deleting everything it had ever learned.
Once memory is a git remote, the container becomes cattle and the memory becomes the pet — and
the pet is a thing you can back up, diff, revert, read in a browser and hand to a second agent.

    docker compose rm -f -v agent1 && docker compose up -d agent1

comes back knowing everything. That is the acceptance test for phase 2, and it is this file.

THREE SCOPES, AND WHO MAY WRITE THEM

    global    fleet-knowledge          operator only. The agent CANNOT push it.
    tenant    <tenant>-knowledge       any agent in the tenant
    personal  <tenant>-knowledge/agents/<name>/    that agent only, by path

Global is what is true for everyone ("run `python` not `python3`"). An agent physically cannot
write it, which is the mechanism — not a rule in a prompt — that stops one tenant's secret
becoming fleet-wide knowledge. Locally that is enforced by pointing the push URL at a scheme
that does not exist, so an attempt fails loudly instead of silently succeeding into a clone
nobody reads; the real enforcement is a deploy key with no write access on the remote, and it
belongs there because that is the only place the agent cannot reach around.

TWO DELIBERATE DEPARTURES FROM THE DESIGN DOC

1. The doc specified a `note_write` TOOL — the agent would call it, and it would flock, commit
   and push. Built instead as: the agent keeps writing plain files exactly as it does today,
   and the HARNESS commits and pushes at the end of every task. Durability must not depend on
   the model remembering to call something. It is the same principle that put the budget check
   inside `call_llm` rather than in the prompt, and it also preserves the property the notes
   mechanism actually runs on — that the agent owns these files and no schema is imposed on
   them. A tool call is a schema.

2. The doc said `flock`. flock is the wrong instrument: it coordinates processes on ONE machine,
   and the concurrency that matters is two agents in different containers pushing to one tenant
   repo. That is solved by pull-rebase-and-retry on push, plus a union merge driver so two
   agents appending to the same notes file merge instead of conflicting. flock would have looked
   correct at N=1 and failed at exactly the moment N=2 made it necessary.

FAILURE POSTURE. Unreachable remote plus an existing local clone: work offline, log loudly, try
again next task — a task email should not fail because GitHub is down. Unreachable remote and NO
local clone: refuse to start. An agent that wakes with amnesia does not sit idle; it rebuilds
apps it already deployed and rewrites notes it already had, and that is worse than being down.
"""

import os
import shutil
import subprocess
import time

import fleet_identity

# Two remotes, both optional. UNSET MEANS OFF: with neither configured this module reports
# disabled and agent_notes keeps every file in the workspace exactly where it is today. That is
# what makes this deployable without a migration window — the switch is thrown by setting an
# environment variable, and it can be thrown back.
FLEET_REMOTE = os.environ.get("MEMORY_FLEET_REMOTE", "").strip()
TENANT_REMOTE = os.environ.get("MEMORY_TENANT_REMOTE", "").strip()

MEMORY_ROOT = os.environ.get("MEMORY_ROOT", "/memory")
FLEET_DIR = os.path.join(MEMORY_ROOT, "fleet")
TENANT_DIR = os.path.join(MEMORY_ROOT, "tenant")

GIT_TIMEOUT = int(os.environ.get("MEMORY_GIT_TIMEOUT", "120"))
PUSH_ATTEMPTS = int(os.environ.get("MEMORY_PUSH_ATTEMPTS", "3"))

# Where a scope's files live once memory is external. Personal is a subdirectory of the tenant
# repo rather than a repo of its own: one clone, one push, one place to look, and "agent2 can
# read what agent1 built" costs nothing when the day comes that it should.
PERSONAL_SUBDIR = os.path.join("agents", fleet_identity.NAME)

# Appended notes from two agents must merge, not conflict. `union` is built into git and keeps
# both sides' lines. It can duplicate an entry when both agents learn the same lesson in the
# same task, which is a cosmetic problem the agent already fixes as part of curating its notes —
# and it is strictly better than a rebase that stops with conflict markers inside the memory of
# a process with no human at the keyboard.
GITATTRIBUTES = "*.md merge=union\n"

_state = {"synced": False, "online": False, "reason": "", "seeded": []}

# Left in the workspace where the notes used to be, once they are safely pushed. The agent has
# been writing to /workspace/AGENT.md for months and its OWN NOTES say so, so habit is a real
# risk here — and a note appended to an abandoned file is lost in silence, which is the worst
# way to lose one. This turns the old path into something that answers back.
MOVED_STUB = """MOVED — this file is no longer your memory and nothing reads it.

Your notes now live in a git repository outside this container, so they survive it being
rebuilt. The real file is at:

    {new_path}

Edit that path instead. Every task shows you the current location in the notes block; trust
that over anything an older note of yours says about /workspace. Anything written HERE is
discarded and will not be there next time.
"""


def log(msg):
    print(f"[memory] {msg}", flush=True)


def enabled():
    """False keeps every path in the workspace, which is exactly today's behaviour."""
    return bool(TENANT_REMOTE)


# ---- git ---------------------------------------------------------------------
def _git(args, cwd=None, check=True):
    """One git invocation. Identity is passed per-command rather than configured globally so a
    commit is attributed to this agent even in a container whose global config says otherwise —
    the attribution is the audit trail, and it should not depend on image build order."""
    # No merge driver is configured here on purpose. An earlier version passed
    # `-c merge.union.driver=true`, which does NOT enable git's union merge — `union` is a
    # built-in, and defining `merge.union.driver` REPLACES it with a custom driver that runs
    # `true`, exits 0, and leaves the file at whichever side was already there. It looked like
    # belt-and-braces and it silently discarded the second agent's notes on every concurrent
    # write; the test caught it because it compares the merged file rather than the exit code.
    cmd = ["git",
           "-c", f"user.name={fleet_identity.AGENT_ID}",
           "-c", f"user.email={fleet_identity.AGENT_ADDRESS}"] + args
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({p.returncode}): "
                           f"{(p.stderr or p.stdout).strip()[:400]}")
    return p


def _ensure_local_remote(remote):
    """Create a bare repo for a LOCAL path remote that does not exist yet.

    Only for filesystem paths, never for a URL, and only when the parent directory is already
    there — which on Zeenie means the bind mount is present. That parent check is the whole
    safety of this: an unmounted volume fails loudly instead of quietly manufacturing an empty
    memory, and the only thing that gets created automatically is the first-ever repo inside a
    directory a human deliberately mounted.

    Without this, standing up an agent is "ssh in, git init --bare, come back" — and a
    prerequisite that lives only in a runbook is one that gets skipped when it is agent number
    thirty rather than agent number two.
    """
    if "://" in remote or remote.startswith("git@") or os.path.exists(remote):
        return False
    parent = os.path.dirname(remote.rstrip("/\\"))
    if not parent or not os.path.isdir(parent):
        return False
    log(f"CREATING a new empty bare repo at {remote} — it did not exist. If this agent should "
        f"already have memory, stop and check the path before it seeds a fresh one.")
    os.makedirs(remote, exist_ok=True)
    _git(["init", "-q", "--bare", "-b", "main", remote])
    return True


def _is_clone(path):
    return os.path.isdir(os.path.join(path, ".git"))


def _has_commits(path):
    return _git(["rev-parse", "--verify", "HEAD"], cwd=path, check=False).returncode == 0


def _clone(remote, path, writable):
    """Clone into a scratch directory and rename it into place only once it has worked.

    ATOMIC ON PURPOSE. The first version built the repo directly at `path`, so a `git init` that
    succeeded followed by a `fetch` that failed left a valid-looking but EMPTY clone behind —
    and the next start found it, decided memory was present, and ran the agent with no memory at
    all. That is exactly the amnesia this module exists to refuse, arrived at by the failure
    path instead of the happy one. Either the whole clone lands or nothing does.
    """
    staging = path + ".incoming"
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    log(f"cloning {remote} -> {path}")
    try:
        # An empty remote clones fine but leaves no branch; `git init` + `remote add` reaches
        # the same state for both cases and does not need the empty one special-cased.
        _git(["init", "-q", "-b", "main", staging])
        _git(["remote", "add", "origin", remote], cwd=staging)
        if not writable:
            # Read-only, made executable rather than merely intended. A push here fails at once
            # with an unknown-transport error naming this file, instead of quietly working into
            # a clone that nothing else ever reads.
            _git(["remote", "set-url", "--push", "origin",
                  "readonly-do-not-push://global-scope-is-operator-only"], cwd=staging)
        _git(["fetch", "origin"], cwd=staging)
        if _git(["rev-parse", "--verify", "origin/main"], cwd=staging,
                check=False).returncode == 0:
            _git(["reset", "--hard", "origin/main"], cwd=staging)
        _git(["branch", "--set-upstream-to=origin/main", "main"], cwd=staging, check=False)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.rename(staging, path)


def _unpushed(path):
    """Are there local commits the remote has not got? True when in doubt.

    Guessing "yes" costs a redundant push that git turns into a no-op. Guessing "no" is how a
    committed lesson stays on a disk that the whole point of D5 is not to depend on.
    """
    if _git(["rev-parse", "--verify", "origin/main"], cwd=path, check=False).returncode != 0:
        return _has_commits(path)
    p = _git(["rev-list", "--count", "origin/main..main"], cwd=path, check=False)
    return p.returncode != 0 or p.stdout.strip() not in ("0", "")


def _pull(path):
    _git(["fetch", "origin"], cwd=path)
    if _git(["rev-parse", "--verify", "origin/main"], cwd=path, check=False).returncode != 0:
        return                                     # remote is still empty; nothing to pull
    _git(["pull", "--rebase", "--autostash", "origin", "main"], cwd=path)


# ---- Seeding from what already exists ----------------------------------------
def _seed(workspace_root, scopes):
    """First run against an empty tenant repo: import the notes the agent already has.

    Only ever runs when the repo has NO commits, so it cannot overwrite anything — the only
    copy of the data moves to the place that will now hold it. Skipping this would be worse than
    a lost file: the agent would start its next task believing it had built nothing, and go and
    rebuild it.
    """
    moved = []
    for name, scope in scopes.items():
        src = os.path.join(workspace_root, name)
        if not os.path.isfile(src):
            continue
        dst = path_for(scope, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        moved.append(f"{name} -> {scope} ({os.path.getsize(src)} bytes)")
    attrs = os.path.join(TENANT_DIR, ".gitattributes")
    if not os.path.exists(attrs):
        with open(attrs, "w", encoding="utf-8", newline="\n") as f:
            f.write(GITATTRIBUTES)
    if moved:
        log("SEEDED the empty tenant repo from this workspace: " + "; ".join(moved))
        log("  the workspace copies stay untouched until the first push succeeds")
        _state["seeded"] = [(name, os.path.join(workspace_root, name), path_for(scope, name))
                            for name, scope in scopes.items()
                            if os.path.isfile(os.path.join(workspace_root, name))]
    return moved


def _retire_workspace_copies():
    """Replace the old workspace notes with a signpost — but only after a successful push.

    Ordering matters more than it looks. Doing this at seed time would mean a crash between the
    copy and the push destroys the only surviving copy of everything the agent has ever learned.
    So the originals stay verbatim until the data is demonstrably on the remote, and only then
    does the old location start saying where the new one is.
    """
    for name, old, new in _state.pop("seeded", []) or []:
        try:
            with open(old, "w", encoding="utf-8", newline="\n") as f:
                f.write(MOVED_STUB.format(new_path=new))
        except OSError as e:
            log(f"could not leave a forwarding note at {old}: {e}")
    return True


# ---- The public surface ------------------------------------------------------
def path_for(scope, name):
    """Where one memory file lives. The single place that knows the layout."""
    if scope == "global":
        return os.path.join(FLEET_DIR, name)
    if scope == "personal":
        return os.path.join(TENANT_DIR, PERSONAL_SUBDIR, name)
    return os.path.join(TENANT_DIR, name)


def sync(workspace_root=None, scopes=None):
    """Bring the local clones up to date. Safe to call before every task.

    Returns a list of lines to log. Never raises for a network problem — see the failure
    posture in the module docstring — but DOES exit the process if there is no local memory at
    all, because that is amnesia rather than degradation.
    """
    if not enabled():
        _state.update(synced=True, online=False, reason="not configured")
        return ["memory: local to the workspace (MEMORY_TENANT_REMOTE unset)"]

    lines = []
    for remote, path, writable in ((TENANT_REMOTE, TENANT_DIR, True),
                                   (FLEET_REMOTE, FLEET_DIR, False)):
        if not remote:
            continue
        # Read BEFORE attempting anything: a failed clone must not be able to answer this
        # question with a directory it created on the way down.
        had_clone = _is_clone(path)
        try:
            if not had_clone:
                _ensure_local_remote(remote)
                _clone(remote, path, writable)
            else:
                _pull(path)
            _state["online"] = True
            lines.append(f"memory: {path} <- {remote} ok")
        except Exception as e:
            reason = str(e).splitlines()[0][:200]
            _state["reason"] = reason
            if not had_clone:
                # No clone and no remote: the agent would come up with no idea what it has
                # built or learned. Refuse, loudly, rather than run a task from nothing.
                raise SystemExit(
                    f"agent_memory: refusing to start — cannot reach {remote} and there is no "
                    f"local clone at {path}.\n  {reason}\n\n"
                    f"An agent with no memory does not do less work, it does the WRONG work: it "
                    f"redeploys apps it already runs and rewrites lessons it already learned. "
                    f"Fix the remote, or unset MEMORY_TENANT_REMOTE to fall back to "
                    f"workspace-local notes.")
            lines.append(f"memory: WARNING {path} is OFFLINE ({reason}) — working from the "
                         f"local clone; nothing will be pushed until it recovers")

    if workspace_root and scopes and _is_clone(TENANT_DIR) and not _has_commits(TENANT_DIR):
        if _seed(workspace_root, scopes):
            lines.append("memory: seeded from the existing workspace notes (one time)")
    _state["synced"] = True
    return lines


def publish(task_id="", note=""):
    """Commit and push whatever the task changed. Called by the harness, not by the agent.

    Returns a one-line summary for the log. Swallows every failure: a memory push that fails
    must not cost the reply the human is waiting for, and the next task pushes the same commit
    anyway because it is already committed locally.
    """
    if not enabled() or not _is_clone(TENANT_DIR):
        return "memory: local only"
    try:
        _git(["add", "-A"], cwd=TENANT_DIR)
        staged = bool(_git(["diff", "--cached", "--quiet"], cwd=TENANT_DIR,
                           check=False).returncode)
        if staged:
            subject = f"{fleet_identity.AGENT_ID}: {task_id or 'update'}"
            _git(["commit", "-q", "-m", subject + (f"\n\n{note}" if note else "")],
                 cwd=TENANT_DIR)
        elif not _unpushed(TENANT_DIR):
            return "memory: unchanged"
        # Falling through with nothing staged is deliberate: a task that changed nothing still
        # has to push if an EARLIER task committed while the remote was down. The first version
        # returned "unchanged" here, which meant an offline commit sat in the local clone until
        # the agent happened to write a note again — a durability guarantee that quietly
        # depended on the next task being productive.
    except Exception as e:
        return f"memory: COULD NOT COMMIT ({str(e).splitlines()[0][:160]})"

    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            _git(["push", "origin", "main"], cwd=TENANT_DIR)
            _retire_workspace_copies()
            return f"memory: pushed {task_id}" + (f" (attempt {attempt})" if attempt > 1 else "")
        except Exception as e:
            last = str(e).splitlines()[0][:160]
            if attempt == PUSH_ATTEMPTS:
                return (f"memory: committed locally but PUSH FAILED after {attempt} attempts "
                        f"({last}) — it will go out with the next task")
            # Somebody else pushed first, which at N>1 is ordinary rather than exceptional.
            # Rebase onto them and try again; the union merge driver keeps both sets of notes.
            try:
                _pull(TENANT_DIR)
            except Exception:
                time.sleep(1)
    return "memory: push failed"


def status():
    """One line describing where memory lives and whether it is reachable."""
    if not enabled():
        return "memory: workspace-local (no remote configured)"
    where = f"tenant={TENANT_REMOTE}" + (f" fleet={FLEET_REMOTE}" if FLEET_REMOTE else "")
    if _state["online"]:
        return f"memory: external git, {where}"
    return f"memory: external git OFFLINE ({_state['reason'] or 'not synced yet'}), {where}"
