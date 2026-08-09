"""D5: memory as a git remote, and the cattle test in miniature.

Driven against REAL git with real bare repositories, not a mocked one. The whole claim of D5 is
that a container can be destroyed and come back knowing everything, and that claim is about how
git actually behaves — a fake would be asserting my own beliefs about clone, rebase and merge
drivers back at me. The two that would be untestable any other way are the concurrent push from
a second agent, and the destroy-and-restore.
"""
import os, shutil, stat, subprocess, sys, tempfile


def rmtree(path):
    """Delete a tree containing a git repo. Git writes its objects read-only, and on Windows a
    read-only file cannot be unlinked at all — so the plain rmtree that stands in for `docker
    compose rm -v` fails on the very repo the test is about destroying."""
    def clear_readonly(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=clear_readonly)

ROOT = tempfile.mkdtemp(prefix="mem-")
REMOTES = os.path.join(ROOT, "remotes")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def git(args, cwd=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=60)


def bare(name):
    path = os.path.join(REMOTES, name)
    os.makedirs(path, exist_ok=True)
    git(["init", "-q", "--bare", "-b", "main", path])
    return path


def agent(name, tenant_remote, fleet_remote="", home=None):
    """A fresh import of the whole memory stack as a named agent.

    Reimported per agent because identity is resolved at module import — which is the point of
    D1 — so a second agent genuinely has to be a second process-level environment. Modules are
    evicted rather than reloaded so nothing is carried over by accident.
    """
    home = home or os.path.join(ROOT, name)
    os.environ.update({"TENANT": "dev", "AGENT_NAME": name,
                       "WORKSPACE_ROOT": os.path.join(home, "workspace"),
                       "MEMORY_ROOT": os.path.join(home, "memory"),
                       "MEMORY_TENANT_REMOTE": tenant_remote,
                       "MEMORY_FLEET_REMOTE": fleet_remote})
    os.makedirs(os.environ["WORKSPACE_ROOT"], exist_ok=True)
    for m in ("agent_memory", "agent_notes", "fleet_identity"):
        sys.modules.pop(m, None)
    import agent_memory, agent_notes
    return agent_memory, agent_notes


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


if git(["--version"]).returncode != 0:
    print("SKIP: git is not on PATH")
    sys.exit(0)

TENANT = bare("dev-knowledge.git")
FLEET = bare("fleet-knowledge.git")


# ---- Off by default ----------------------------------------------------------
print("\n--- unconfigured: nothing whatsoever changes ---")
mem, notes = agent("agent1", tenant_remote="")
check("memory reports disabled", not mem.enabled())
check("NOTES STAY EXACTLY WHERE THEY WERE — one env var is the whole switch",
      notes.path_of("AGENT.md") == os.path.join(os.environ["WORKSPACE_ROOT"], "AGENT.md"),
      notes.path_of("AGENT.md"))
check("sync is a no-op that does not touch the network", "local to the workspace" in
      mem.sync(os.environ["WORKSPACE_ROOT"], notes.SCOPES)[0])
check("publish is a no-op", mem.publish("task-0001") == "memory: local only")


# ---- Seeding: the migration that must not lose the only copy -----------------
print("\n--- first run against an empty repo seeds from the workspace ---")
ws1 = os.path.join(ROOT, "agent1", "workspace")
os.makedirs(ws1, exist_ok=True)
write(os.path.join(ws1, "AGENT.md"), "python is python3 here\n")
write(os.path.join(ws1, "AGENT-AVOID.md"), "curl | head strands on EPIPE\n")
write(os.path.join(ws1, "AGENT-ASSETS.md"), "booking app on 3001\n")

mem, notes = agent("agent1", TENANT, FLEET)
mem.sync(ws1, notes.SCOPES)
check("the existing notes were imported, not lost",
      read(notes.path_of("AGENT.md")) == "python is python3 here\n")
check("THE WORKSPACE COPY IS UNTOUCHED until the data is demonstrably on the remote",
      read(os.path.join(ws1, "AGENT.md")) == "python is python3 here\n")
check("tenant-scope files land at the repo root",
      notes.path_of("AGENT-AVOID.md") == os.path.join(mem.TENANT_DIR, "AGENT-AVOID.md"))
check("PERSONAL scope is namespaced by agent — two agents cannot collide",
      notes.path_of("AGENT-ASSETS.md")
      == os.path.join(mem.TENANT_DIR, "agents", "agent1", "AGENT-ASSETS.md"),
      notes.path_of("AGENT-ASSETS.md"))
check("a union merge driver is configured, so two agents appending do not conflict",
      "merge=union" in (read(os.path.join(mem.TENANT_DIR, ".gitattributes")) or ""))

print(mem.publish("task-0001-first"))
check("the seeded state is pushed", git(["log", "--oneline"], cwd=TENANT).stdout.strip() != "")
check("committed as the agent, so git log is the audit trail",
      "dev/agent1" in git(["log", "-1", "--format=%an %s"], cwd=TENANT).stdout,
      git(["log", "-1", "--format=%an %s"], cwd=TENANT).stdout.strip())
check("seeding does not run twice", mem.publish("task-0002") == "memory: unchanged")
# The agent has written to /workspace/AGENT.md for months and its own notes say to. Habit is
# the failure mode here, and an append to an abandoned file is lost silently.
stub = read(os.path.join(ws1, "AGENT.md")) or ""
check("ONLY AFTER THE PUSH does the old path become a signpost", "MOVED" in stub, repr(stub[:60]))
check("  ...which names where the file actually went now",
      notes.path_of("AGENT.md") in stub, repr(stub))
check("  ...and the real memory is untouched by that",
      read(notes.path_of("AGENT.md")) == "python is python3 here\n")


# ---- THE CATTLE TEST ---------------------------------------------------------
print("\n--- destroy the container, bring it back ---")
write(notes.path_of("AGENT-AVOID.md"), "curl | head strands on EPIPE\ndocker pull needs a TTY\n")
write(notes.path_of("AGENT-ASSETS.md"), "booking app on 3001\nshortener on 3002\n")
print(mem.publish("task-0003-learned-things"))

rmtree(os.path.join(ROOT, "agent1"))          # rm -f -v: container AND its volume
mem, notes = agent("agent1", TENANT, FLEET)
mem.sync(os.environ["WORKSPACE_ROOT"], notes.SCOPES)
check("EVERYTHING CAME BACK: lessons",
      read(notes.path_of("AGENT-AVOID.md")) == "curl | head strands on EPIPE\n"
                                               "docker pull needs a TTY\n")
check("EVERYTHING CAME BACK: its own assets",
      read(notes.path_of("AGENT-ASSETS.md")) == "booking app on 3001\nshortener on 3002\n")
check("  ...with an empty workspace, which is now scratch and nothing else",
      not os.path.exists(os.path.join(os.environ["WORKSPACE_ROOT"], "AGENT.md")))
check("and it did NOT re-seed over the top of what it pulled",
      mem.publish("task-0004") == "memory: unchanged")


# ---- Two agents, one tenant repo ---------------------------------------------
print("\n--- a second agent, which is the case flock would have failed ---")
mem2, notes2 = agent("agent2", TENANT, FLEET)
mem2.sync(os.environ["WORKSPACE_ROOT"], notes2.SCOPES)
check("agent2 starts up already knowing what agent1 learned",
      "docker pull needs a TTY" in (read(notes2.path_of("AGENT-AVOID.md")) or ""))
check("  ...but does NOT inherit agent1's inventory as its own",
      read(notes2.path_of("AGENT-ASSETS.md")) is None,
      str(read(notes2.path_of("AGENT-ASSETS.md"))))
check("  ...because personal scope resolves to its own directory",
      "agents" + os.sep + "agent2" in notes2.path_of("AGENT-ASSETS.md"))

# Both append to the SAME shared file without either having seen the other's line, then both
# push. This is the concurrent write that a single-machine lock could not have coordinated.
mem1, notes1 = agent("agent1", TENANT, FLEET, home=os.path.join(ROOT, "agent1"))
write(notes1.path_of("AGENT-AVOID.md"),
      read(notes1.path_of("AGENT-AVOID.md")) + "agent1: npm ci needs a lockfile\n")
write(notes2.path_of("AGENT-AVOID.md"),
      read(notes2.path_of("AGENT-AVOID.md")) + "agent2: pytest -x hides later failures\n")
print(" ", mem1.publish("task-0005-agent1"))
print(" ", mem2.publish("task-0006-agent2"))        # loses the race, must rebase and retry

fresh = os.path.join(ROOT, "verify")
git(["clone", "-q", TENANT, fresh])
merged = read(os.path.join(fresh, "AGENT-AVOID.md")) or ""
check("BOTH agents' lessons survived the race", "agent1: npm ci needs a lockfile" in merged
      and "agent2: pytest -x hides later failures" in merged, repr(merged))
check("  ...neither side left conflict markers in the agent's memory", "<<<<<<<" not in merged)
check("  ...and the earlier shared knowledge is still there",
      "docker pull needs a TTY" in merged)
check("each agent's assets stay separate in the repo",
      os.path.isdir(os.path.join(fresh, "agents", "agent1")))


# ---- Global scope: the one an agent cannot write -----------------------------
print("\n--- global scope is operator-only, by mechanism ---")
seed = os.path.join(ROOT, "seed-fleet")
git(["clone", "-q", FLEET, seed])
write(os.path.join(seed, "FLEET.md"), "every agent: run `python`, not `python3`\n")
git(["add", "-A"], cwd=seed); git(["-c", "user.name=op", "-c", "user.email=op@x",
                                   "commit", "-qm", "fleet knowledge"], cwd=seed)
git(["push", "-q", "origin", "main"], cwd=seed)

mem3, notes3 = agent("agent1", TENANT, FLEET, home=os.path.join(ROOT, "agent3"))
mem3.sync(os.environ["WORKSPACE_ROOT"], notes3.SCOPES)
check("the agent reads fleet-wide knowledge",
      "run `python`" in (read(notes3.path_of("FLEET.md")) or ""))
check("  ...and it is injected into the prompt", "run `python`" in notes3.context_block())
check("  ...marked read-only where the agent will see it",
      "READ-ONLY" in notes3.context_block())
check("  ...and it is NOT offered as somewhere to write",
      "FLEET.md" not in notes3.UPKEEP_NOTE)
write(notes3.path_of("FLEET.md"), "every agent: I decided this myself\n")
pushed = git(["push", "origin", "main"], cwd=mem3.FLEET_DIR)
check("A PUSH TO GLOBAL SCOPE FAILS — the local clone has no usable push URL",
      pushed.returncode != 0, pushed.stdout + pushed.stderr)
check("  ...and publish() never touches it anyway",
      "fleet" not in mem3.publish("task-0007"))
git(["clone", "-q", FLEET, os.path.join(ROOT, "verify-fleet")])
check("  ...so the fleet repo is unchanged",
      read(os.path.join(ROOT, "verify-fleet", "FLEET.md")) ==
      "every agent: run `python`, not `python3`\n")


# ---- Failure posture ---------------------------------------------------------
print("\n--- what happens when the remote is gone ---")
# A path INSIDE the mounted remotes directory is created on demand: that is how standing up
# agent number thirty stays one command instead of a runbook step somebody skips.
fresh_remote = os.path.join(ROOT, "remotes", "brand-new.git")
mem4, notes4 = agent("agent9", fresh_remote, home=os.path.join(ROOT, "agent9"))
mem4.sync(os.environ["WORKSPACE_ROOT"], notes4.SCOPES)
check("a missing repo under the mounted directory is created, not fatal",
      os.path.isdir(fresh_remote) and mem4.enabled())

# But an unreachable remote is NOT quietly turned into an empty one. The parent directory has
# to exist, which on Zeenie means the bind mount is actually mounted — the difference between
# "first agent in a new tenant" and "the volume did not come up".
unmounted = os.path.join(ROOT, "never-mounted", "dev-knowledge.git")
mem4, notes4 = agent("agent1", unmounted, home=os.path.join(ROOT, "agent4"))
try:
    mem4.sync(os.environ["WORKSPACE_ROOT"], notes4.SCOPES)
    check("REFUSES TO START with no remote and no clone", False, "it started anyway")
except SystemExit as e:
    check("REFUSES TO START with no remote and no clone — amnesia is worse than downtime", True)
    check("  ...and says how to fall back", "MEMORY_TENANT_REMOTE" in str(e))
check("  ...leaving NO half-built clone for the next start to mistake for memory",
      not os.path.isdir(os.path.join(ROOT, "agent4", "memory", "tenant")))

# But an existing clone plus a dead remote is degraded, not fatal.
offline = os.path.join(ROOT, "agent5")
mem5, notes5 = agent("agent1", TENANT, home=offline)
mem5.sync(os.environ["WORKSPACE_ROOT"], notes5.SCOPES)
moved = TENANT + ".moved"
os.rename(TENANT, moved)
lines = mem5.sync(os.environ["WORKSPACE_ROOT"], notes5.SCOPES)
check("an unreachable remote with a local clone keeps working",
      any("OFFLINE" in l for l in lines), str(lines))
check("  ...the agent still reads its memory",
      "docker pull needs a TTY" in (read(notes5.path_of("AGENT-AVOID.md")) or ""))
write(notes5.path_of("AGENT.md"), "written while the remote was down\n")
out = mem5.publish("task-0008-offline")
check("  ...the write is COMMITTED LOCALLY even though the push fails",
      "PUSH FAILED" in out, out)
os.rename(moved, TENANT)
out = mem5.publish("task-0009-back-online")
check("  ...and goes out with the next task once the remote returns",
      "pushed" in out, out)
git(["clone", "-q", TENANT, os.path.join(ROOT, "verify2")])
check("  ...the offline write really did land upstream",
      read(os.path.join(ROOT, "verify2", "AGENT.md")) == "written while the remote was down\n")


try:
    rmtree(ROOT)
except OSError:
    pass
print("\n" + ("ALL MEMORY TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
