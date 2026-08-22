"""The agent's memory files and port allocation. No API calls, no mail server."""
import os, socket, sys, tempfile

# This file tests the WORKSPACE-LOCAL layout, so external memory has to be off. Without this
# the tests pass on a laptop with no remote configured and fail inside the container, where
# path_of() correctly resolves to /memory/tenant and "all three files are missing" is answered
# by the agent's real 53KB of notes. Same shape as the AGENT_ADDRESS problem in test_identity:
# a test that reads ambient environment is a test that only checks the machine it ran on.
for _v in ("MEMORY_TENANT_REMOTE", "MEMORY_FLEET_REMOTE", "MEMORY_ROOT"):
    os.environ.pop(_v, None)
_SANDBOX = tempfile.mkdtemp(prefix='notes-sandbox-')
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
_HERE = os.path.dirname(os.path.abspath(__file__))
# BOTH LAYOUTS, and the order matters. The source tree keeps the modules in agent/;
# the image copies them flat into /app beside this tests/ directory (see the Dockerfile).
# A test has to run in either, because the image ships these as its only self-check.
sys.path[:0] = [os.path.join(_HERE, "..", "agent"), os.path.join(_HERE, "..")]
import agent_notes

fails = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


def listener(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    return s


root = tempfile.mkdtemp(prefix="notes-")
agent_notes.WORKSPACE_ROOT = root

N = len(agent_notes.FILES)

print("\nmissing files")
block = agent_notes.context_block()
check(f"all {N} files reported missing, not crashed",
      block.count("does not exist yet") == N)
check("names each file it should create",
      all(agent_notes.path_of(f[0]) in block for f in agent_notes.FILES))
check("digest of nothing is zeroes", agent_notes.digest() == (0,) * N)
check("every file is described in the upkeep note",
      all(f[0] in agent_notes.UPKEEP_NOTE for f in agent_notes.FILES))

print("\nfiles the agent wrote")
NOTES = "- npm install needs --no-audit here, the registry is slow\n"
ASSETS = "booking app: /workspace/task-0012-..., port 3002, node dist/src/index.js\n"
AVOID = "- `cd x && nohup srv > log 2>&1 &` HANGS: & backgrounds the whole list. Use a subshell.\n"
for path, text in ((agent_notes.notes_path(), NOTES),
                   (agent_notes.assets_path(), ASSETS),
                   (agent_notes.avoid_path(), AVOID)):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
block = agent_notes.context_block()
check("notes pasted verbatim", "registry is slow" in block)
check("assets pasted verbatim", "port 3002" in block)
check("lessons pasted verbatim", "backgrounds the whole list" in block)
check("no 'missing' noise once they exist", "does not exist yet" not in block)
check("digest tracks every file",
      agent_notes.digest() == (len(NOTES), len(ASSETS), len(AVOID)))
check("describe_digest says UPDATED when a file grew",
      agent_notes.describe_digest((0,) * N, agent_notes.digest()).startswith("UPDATED"))
check("describe_digest says unchanged when nothing moved",
      "unchanged" in agent_notes.describe_digest(agent_notes.digest(), agent_notes.digest()))

print("\nan oversized file is cut, not dropped")
agent_notes.MAX_INJECT_CHARS = 100
with open(agent_notes.assets_path(), "w", encoding="utf-8") as f:
    f.write("x" * 5000)
block = agent_notes.context_block()
check("kept the head", "x" * 100 in block)
check("did not paste all 5000", "x" * 200 not in block)
check("says where to read the rest", "read " + agent_notes.assets_path() in block)
check("the other files are untouched by the cut",
      "registry is slow" in block and "backgrounds the whole list" in block)
agent_notes.MAX_INJECT_CHARS = 8000

print("\nport allocation")
# A real listener on the first port: this is exactly the app-left-running case.
base = 34100
held = listener(base)
try:
    ports = [base + i for i in range(4)]
    port, evicted = agent_notes.free_port(ports)
    check("skips the port a live app is holding", port == base + 1)
    check("did not report an eviction", evicted is False)
    check("port_is_free says busy for the held one", agent_notes.port_is_free(base) is False)
    check("port_is_free says free for an empty one", agent_notes.port_is_free(base + 3) is True)

    all_held = [listener(p) for p in ports[1:]]
    try:
        port, evicted = agent_notes.free_port(ports, fallback_index=6)
        check("evicts only when every port is busy", evicted is True)
        check("falls back to the rotating choice", port == ports[6 % 4])
    finally:
        for s in all_held:
            s.close()
finally:
    held.close()

print("\nunreadable workspace")
agent_notes.WORKSPACE_ROOT = os.path.join(root, "does", "not", "exist")
check("missing directory reads as missing files, not an exception",
      agent_notes.context_block().count("does not exist yet") == N)

print("\n" + (f"{len(fails)} FAILED" if fails else "ALL PASSED"))
sys.exit(1 if fails else 0)
