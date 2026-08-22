"""The spend ledger and the ceilings — no API calls, no mail server.

The ledger is the instrument the rest of the fleet plan is calibrated against, so the thing
under test is mostly "does it tell the truth": the right tokens, an honest price when the
provider reports less than we hoped, and a ceiling that actually stops the next call.
"""
import json, os, shutil, sys, tempfile

WS = tempfile.mkdtemp(prefix="budget-ws-")
# These are derived from AGENT_NAME now; a stale one inherited from the real container would
# contradict the AGENT_NAME set below and fleet_identity would refuse to import at all.
for _v in ("AGENT_ADDRESS", "VALIDATOR_NAME", "VALIDATOR_ADDRESS"):
    os.environ.pop(_v, None)
os.environ.update({
    "WORKSPACE_ROOT": WS,
    "TENANT": "dev",
    "AGENT_NAME": "agent-01",
    "AGENT_TASK_USD": "1.00",
    "AGENT_DAILY_USD": "3.00",
    "FLEET_DAILY_USD": "4.00",
    "PRICE_CACHE_HIT_PER_M": "0.07",
    "PRICE_CACHE_MISS_PER_M": "0.27",
    "PRICE_OUTPUT_PER_M": "1.10",
    "BUDGET_RECENT_TTL": "0",          # never memoise, so ceilings are exact under test
})
_SANDBOX = WS
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
import agent_budget as b

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def reset():
    for p in (b.LEDGER, b.PAUSE_FILE, b.BUDGET_FILE):
        if os.path.exists(p):
            os.remove(p)
    b._recent.update({"path": None, "at": 0.0, "usd": 0.0})
    b.start_task("task-0001", "boss@agents.local")


def rows():
    with open(b.LEDGER, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


# ---- Pricing ----------------------------------------------------------------
print("\n--- what a call costs ---")
reset()

# 1M cache hits, 1M misses, 1M output = 0.07 + 0.27 + 1.10
usd = b.usd_for({"prompt_tokens": 2_000_000, "prompt_cache_hit_tokens": 1_000_000,
                 "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 1_000_000})
check("hits, misses and output are priced separately", abs(usd - 1.44) < 1e-9, f"got {usd}")

# The split is what makes caching visible; the same tokens all-miss must cost more.
cheap = b.usd_for({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000,
                   "prompt_cache_miss_tokens": 0, "completion_tokens": 0})
dear = b.usd_for({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 0,
                  "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 0})
check("a cached prompt is cheaper than an uncached one", cheap < dear, f"{cheap} vs {dear}")

# A provider that reports no split must be assumed uncached — over-state, never flatter.
blind = b.usd_for({"prompt_tokens": 1_000_000, "completion_tokens": 0})
check("no cache split reported -> priced as a full miss", abs(blind - dear) < 1e-12,
      f"{blind} vs {dear}")
check("missing usage entirely is free, not a crash", b.usd_for(None) == 0.0)


# ---- The ledger line --------------------------------------------------------
print("\n--- what gets written down ---")
reset()
b.record({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
          "prompt_cache_miss_tokens": 200, "completion_tokens": 50},
         model="deepseek-chat", role="worker")
r = rows()[0]
check("one line per call", len(rows()) == 1)
check("tenant is recorded from the first row", r["tenant"] == "dev", str(r))
check("agent id is tenant-qualified", r["agent_id"] == "dev/agent-01", str(r))
check("the requester is recorded", r["requester"] == "boss@agents.local", str(r))
check("the task is recorded", r["task_id"] == "task-0001", str(r))
check("tokens are stored, not just dollars",
      (r["prompt_tokens"], r["cached_tokens"], r["miss_tokens"], r["completion_tokens"])
      == (1000, 800, 200, 50), str(r))
check("the role distinguishes the gate from the work", r["role"] == "worker", str(r))

b.record({"prompt_tokens": 10, "completion_tokens": 1}, model="deepseek-chat", role="reviewer")
check("the reviewer's calls are billed to the same task",
      rows()[1]["task_id"] == "task-0001" and rows()[1]["role"] == "reviewer")


# ---- Ceilings ---------------------------------------------------------------
print("\n--- the ceiling stops the next call ---")
reset()
b.check()                                     # a fresh task may proceed
big = {"prompt_tokens": 4_000_000, "prompt_cache_hit_tokens": 0,
       "prompt_cache_miss_tokens": 4_000_000, "completion_tokens": 0}   # $1.08 > $1.00 task cap
b.record(big, model="m", role="worker")
try:
    b.check()
    check("the task ceiling is enforced", False, "check() did not raise")
except b.BudgetExceeded as e:
    check("the task ceiling is enforced", "ceiling" in str(e))
    check("  ...and says the work is not lost", "workspace" in str(e), str(e))

# A new task clears the per-task counter but not the day's total.
b.start_task("task-0002", "boss@agents.local")
b.check()
check("a new task starts with a clean per-task counter", True)

# Daily: $3.00 cap. One more $1.08 call and the 24h total is $2.16, still under.
b.record(big, model="m", role="worker")
b.start_task("task-0003", "boss@agents.local")
b.check()
check("under the daily cap it keeps going", True)
b.record(big, model="m", role="worker")       # 24h total now $3.24 > $3.00
b.start_task("task-0004", "boss@agents.local")
try:
    b.check()
    check("the daily ceiling is enforced", False, "check() did not raise")
except b.BudgetExceeded as e:
    check("the daily ceiling is enforced", "24h" in str(e), str(e))


# ---- The fleet kill switch --------------------------------------------------
print("\n--- the fleet switch ---")
reset()
os.environ["AGENT_DAILY_USD"] = "100"         # take the agent cap out of the way
b.DAILY_USD = 100.0
b.record(big, model="m", role="worker")
b.record(big, model="m", role="worker")
b.record(big, model="m", role="worker")
b.record(big, model="m", role="worker")       # $4.32 > $4.00 fleet cap
b.start_task("task-0005", "boss@agents.local")
try:
    b.check()
    check("the fleet ceiling is enforced", False, "check() did not raise")
except b.BudgetExceeded:
    check("the fleet ceiling is enforced", True)
check("tripping it leaves a pause file a human must clear", b.paused())
reason = open(b.PAUSE_FILE, encoding="utf-8").read()
check("the pause file says who tripped it and why",
      "dev/agent-01" in reason and "FLEET_DAILY_USD" in reason, reason)

# Paused is refused before anything else is even evaluated.
b.start_task("task-0006", "boss@agents.local")
try:
    b.check()
    check("a paused fleet refuses new calls", False)
except b.BudgetExceeded as e:
    check("a paused fleet refuses new calls", "paused" in str(e))
os.remove(b.PAUSE_FILE)
b._recent.update({"path": None, "at": 0.0})
check("removing the file resumes the fleet", not b.paused())


# ---- Ceilings are changeable without a restart -------------------------------
print("\n--- the runtime override ---")
reset()
check("with no file, the configured ceilings apply",
      b.limits() == {"task": b.TASK_USD, "daily": b.DAILY_USD, "fleet": b.FLEET_DAILY_USD},
      str(b.limits()))

with open(b.BUDGET_FILE, "w", encoding="utf-8") as fh:
    json.dump({"task_usd": 9.5, "daily_usd": 40}, fh)
lim = b.limits()
check("a value in the file wins", lim["task"] == 9.5 and lim["daily"] == 40.0, str(lim))
check("an unmentioned ceiling keeps its default", lim["fleet"] == b.FLEET_DAILY_USD, str(lim))

# The raised ceiling must actually take effect mid-flight, with no restart.
b.record(big, model="m", role="worker")       # $1.08, over the $1.00 configured task cap
b.check()
check("a raised ceiling takes effect on the very next call", True)

# Garbage must never be able to REMOVE a ceiling — that is the direction that matters.
for bad in ('{"task_usd": "lots"}', '{"task_usd": -5}', '{"task_usd": 0}', 'not json at all'):
    with open(b.BUDGET_FILE, "w", encoding="utf-8") as fh:
        fh.write(bad)
    check(f"garbage falls back to the default, not to no limit: {bad[:22]}",
          b.limits()["task"] == b.TASK_USD, str(b.limits()))
os.remove(b.BUDGET_FILE)
check("removing the file restores the configured ceilings", b.limits()["task"] == b.TASK_USD)


# ---- Fail open on bookkeeping ------------------------------------------------
print("\n--- a broken ledger must not cost a task ---")
reset()
saved = b.LEDGER
b.LEDGER = os.path.join(WS, "no-such-dir", "spend.jsonl")
try:
    b.record({"prompt_tokens": 10, "completion_tokens": 1}, model="m")
    check("an unwritable ledger does not raise", True)
except Exception as e:
    check("an unwritable ledger does not raise", False, f"{type(e).__name__}: {e}")
b.LEDGER = saved

# A torn line (a half-written row, or a crash mid-append) must not stop the count.
reset()
b.record(big, model="m", role="worker")
with open(b.LEDGER, "a", encoding="utf-8") as fh:
    fh.write('{"ts": "not-a-date", "usd": 99999}\n{ this is not json\n')
b.record(big, model="m", role="worker")
total = b._spent_24h(b.LEDGER)
check("torn lines are skipped, not fatal, and do not inflate the total",
      2.0 < total < 2.5, f"got {total}")

# The summary the human reads in the reply footer.
reset()
b.record({"prompt_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
          "completion_tokens": 0}, model="m")
check("the footer reports this task's cost", b.task_summary() == "cost: $0.2700 this task",
      b.task_summary())

# ---- Two agents, one fleet ---------------------------------------------------
# The regression that made this necessary: FLEET_LEDGER and FLEET_PAUSE_FILE both default
# into the agent's OWN workspace. Correct for one agent, silently wrong for two — the "fleet"
# ceiling counts only the agent checking it, and a switch announcing "All agents are now
# paused" pauses one container. Nothing reports it; the number still reads $500.
print()
print("--- the fleet ceiling and kill switch are FLEET-wide ---")
import importlib

FLEET_DIR = tempfile.mkdtemp(prefix="fleet-")
FLEET_LEDGER = os.path.join(FLEET_DIR, "spend.jsonl")
PAUSE = os.path.join(FLEET_DIR, "FLEET-PAUSED")


def agent(name, shared=True):
    """A second agent is a second process-level environment: identity resolves at import."""
    ws = os.path.join(FLEET_DIR, name)
    os.makedirs(ws, exist_ok=True)
    os.environ.update({"WORKSPACE_ROOT": ws, "AGENT_NAME": name,
                       # Its OWN ledger and budget file, as a real container would have. The
                       # sandbox at the top of this file pins SPEND_LEDGER to one path, so
                       # without this both "agents" would share the per-agent ledger and the
                       # separation being tested would not exist.
                       "SPEND_LEDGER": os.path.join(ws, ".spend.jsonl"),
                       "BUDGET_FILE": os.path.join(ws, "budget.json"),
                       "FLEET_LEDGER": FLEET_LEDGER, "FLEET_PAUSE_FILE": PAUSE,
                       "AGENT_DAILY_USD": "3.00", "FLEET_DAILY_USD": "4.00",
                       # High enough to be out of the way. At the suite's default $1 the TASK
                       # ceiling trips first and every assertion below passes for the wrong
                       # reason — which it did, silently, until the kill switch failed to
                       # appear and gave it away.
                       "AGENT_TASK_USD": "100.00"})
    if not shared:                       # the misconfiguration: no shared fleet paths at all
        os.environ.pop("FLEET_LEDGER", None)
        os.environ.pop("FLEET_PAUSE_FILE", None)
    for m in ("agent_budget", "fleet_identity"):
        sys.modules.pop(m, None)
    return importlib.import_module("agent_budget")


def spend(mod, usd):
    """usd of pure cache-miss prompt tokens."""
    mod.record({"prompt_tokens": int(usd / 0.27 * 1_000_000),
                "prompt_cache_miss_tokens": int(usd / 0.27 * 1_000_000),
                "completion_tokens": 0}, model="m")


a1 = agent("agent-01"); a1.start_task("t1")
spend(a1, 2.0)
a2 = agent("agent-02"); a2.start_task("t1")
check("each agent still has its OWN ledger", a2.LEDGER != a1.LEDGER)
check("  ...so agent-02's DAILY total does not include agent-01's spend",
      a2._spent_24h(a2.LEDGER) < 0.01, str(a2._spent_24h(a2.LEDGER)))
check("BUT THE FLEET TOTAL DOES — otherwise the fleet ceiling is 4 dollars PER AGENT",
      1.9 < a2._spent_24h(a2.FLEET_LEDGER) < 2.1, str(a2._spent_24h(a2.FLEET_LEDGER)))

# agent-02 spends enough that the pair crosses the $4 fleet ceiling while neither has
# crossed its own $3 daily one. Only a shared ledger can see this.
spend(a2, 2.5)
check("neither agent is over its own daily ceiling", a2._spent_24h(a2.LEDGER) < 3.0)
try:
    a2.check()
    check("REFUSED at the fleet ceiling", False, "it was allowed")
except a2.BudgetExceeded as e:
    # Assert WHICH ceiling. "Something refused" is satisfied by the task ceiling, the daily
    # ceiling, or a typo in the test.
    check("REFUSED at the fleet ceiling, which only a shared ledger can see",
          "fleet has spent" in str(e), str(e))
    check("  ...and the trip wrote the kill switch", os.path.exists(PAUSE))

check("THE OTHER AGENT IS PAUSED TOO — the whole point of a fleet switch", a1.paused())
try:
    a1.check()
    check("  ...and refuses to spend", False, "it kept going")
except a1.BudgetExceeded:
    check("  ...and refuses to spend", True)

os.remove(PAUSE)
check("removing the file resumes everybody", not a1.paused() and not a2.paused())

# The misconfiguration itself has to be visible, because it has no other symptom.
check("a shared fleet ledger is reported plainly",
      any("fleet ledger" in l for l in a1.startup_report()), str(a1.startup_report()))
solo = agent("agent-01", shared=False)
check("A PER-AGENT FLEET LEDGER WARNS LOUDLY — it looks healthy otherwise",
      any("WARNING" in l and "fleet ceiling only counts" in l for l in solo.startup_report()),
      str(solo.startup_report()))

shutil.rmtree(FLEET_DIR, ignore_errors=True)

import shutil; shutil.rmtree(WS, ignore_errors=True)
print("\n" + ("ALL BUDGET TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
