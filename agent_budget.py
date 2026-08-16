"""agent_budget.py — what a task costs, and the ceiling it must not cross.

Nobody has ever measured what a task costs on this system. That number decides whether the
memory split matters, whether caps matter, whether N agents are affordable at all — so it is
measured before anything is designed around a guess.

TWO JOBS, DELIBERATELY SEPARATE.

  1. The ledger: one append-only line per LLM call, recording TOKENS, which are ground truth.
  2. The ceiling: refuse the next call when the money is gone.

WHY THE LEDGER STORES TOKENS AND NOT JUST DOLLARS. Prices change and published rates get
misread. Tokens are what the provider actually reported; dollars are a derivation. Recording
both means a wrong price is a recomputation over the ledger rather than a quarter of lost data.
The same reasoning applies to the schema: `tenant` and `requester` are in every line from the
first one, because a fleet that grows into teams cannot reconstruct who spent what afterwards.
Both are constants today. That is fine — the point is that the column exists.

CEILINGS START DELIBERATELY LOOSE. Enforcing a limit chosen before the first measurement is how
you break a working system on the day you instrument it. The defaults here are set well above
any plausible single task so that the FLEET ceiling still catches a runaway loop overnight while
normal work is untouched. Tighten them once the ledger says what normal looks like.

Fail-open on bookkeeping, fail-closed on money: a ledger that cannot be written must never cost
you a task, but a ceiling that cannot be evaluated must stop the spend.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import fleet_control
import fleet_identity

# ---- Identity ----------------------------------------------------------------
# One derivation, in one module. TENANT is a constant today and a real dimension later
# (docs/Fleet-Design.md D1/D2); it is recorded from the first row because ledger history
# cannot be back-filled with a tenant column.
TENANT = fleet_identity.TENANT
AGENT_NAME = fleet_identity.NAME
AGENT_ID = fleet_identity.AGENT_ID

# ---- Where the numbers live --------------------------------------------------
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
LEDGER = os.environ.get("SPEND_LEDGER", os.path.join(WORKSPACE_ROOT, ".spend.jsonl"))
# THESE TWO MUST POINT SOMEWHERE EVERY AGENT CAN SEE, or neither of them means what it says.
# Both default into the agent's OWN workspace, which is right for a single agent and quietly
# wrong for a fleet: with a per-agent fleet ledger the $500 "fleet" ceiling is really a second
# per-agent ceiling and true exposure is 500 x N; with a per-agent pause file, the switch that
# announces "All agents are now paused" pauses exactly one. compose points both at a shared
# bind mount, and startup_report() says so out loud when they are not — a money control that
# has silently become per-agent is worse than no control, because the number still looks right.
FLEET_LEDGER = os.environ.get("FLEET_LEDGER", LEDGER)
PAUSE_FILE = os.environ.get("FLEET_PAUSE_FILE", os.path.join(WORKSPACE_ROOT, "FLEET-PAUSED"))

# ---- Ceilings ----------------------------------------------------------------
# Measured on task-0036: a real build (130 steps, an app designed, tested and shipped) cost
# $0.4969, and a trivial question about $0.01. These are set far above that on purpose. The
# goal right now is a capable fleet, not a cheap one, and a ceiling tight enough to interrupt
# real work would teach us nothing except how to raise it. They still bound the thing they
# exist for: a loop burning calls overnight trips the fleet switch long before it matters.
TASK_USD = float(os.environ.get("AGENT_TASK_USD", "20.00"))
DAILY_USD = float(os.environ.get("AGENT_DAILY_USD", "150.00"))
# Must stay ABOVE the per-agent daily figure. With one agent the two ledgers are the same file,
# so a lower fleet ceiling would always trip first — and that one writes a pause file that stops
# every agent, which is a much bigger hammer than stopping one.
FLEET_DAILY_USD = float(os.environ.get("FLEET_DAILY_USD", "500.00"))

# Runtime override, so a ceiling can be changed without a rebuild or even a restart. On Zeenie
# `docker compose up` over SSH is unreliable and `docker restart` keeps the environment the
# container was created with, which would make an env-only ceiling effectively frozen. A JSON
# file in the workspace is editable with one `docker exec` and takes effect on the next call.
BUDGET_FILE = os.environ.get("BUDGET_FILE", os.path.join(WORKSPACE_ROOT, "budget.json"))
_bad_budget_file = None       # remembered so a broken file is reported once, not 130 times

# ---- Prices, per million tokens ----------------------------------------------
# Estimates for deepseek-chat, overridable without a code change. Verify against current
# published pricing before trusting the dollar column; the token columns are unaffected.
PRICE_CACHE_HIT_PER_M = float(os.environ.get("PRICE_CACHE_HIT_PER_M", "0.07"))
PRICE_CACHE_MISS_PER_M = float(os.environ.get("PRICE_CACHE_MISS_PER_M", "0.27"))
PRICE_OUTPUT_PER_M = float(os.environ.get("PRICE_OUTPUT_PER_M", "1.10"))

_lock = threading.Lock()
_task = {"id": None, "requester": None, "usd": 0.0}
# What the control plane last told us, and whether the last write to it landed. `broken` is the
# fail-closed latch: set when a spend could not be recorded, cleared by the next one that can.
_remote = {"broken": None, "over": False, "total": None, "ceiling": None}
_recent = {"path": None, "at": 0.0, "usd": 0.0}   # memoised 24h total, see _spent_24h
RECENT_TTL = float(os.environ.get("BUDGET_RECENT_TTL", "30"))


class BudgetExceeded(RuntimeError):
    """Raised before a call that would cross a ceiling. `agent_brain.call_llm` re-raises it as
    LLMError so the existing failure path — honest answer, gate skipped, human emailed — runs
    unchanged. That path is already written and already tested; this borrows it."""


def _now():
    return datetime.now(timezone.utc)


def usd_for(usage):
    """Price one call. DeepSeek splits prompt tokens into cache hits and misses; when the split
    is absent (another provider, or an older response shape) everything counts as a miss, which
    over-states rather than under-states the bill. Never flatter the number."""
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    miss = int(miss) if miss is not None else max(prompt - hit, 0)
    out = int(usage.get("completion_tokens") or 0)
    return (hit * PRICE_CACHE_HIT_PER_M
            + miss * PRICE_CACHE_MISS_PER_M
            + out * PRICE_OUTPUT_PER_M) / 1_000_000.0


def start_task(task_id, requester=None):
    """Called once per email. Resets the per-task counter; the daily ones come off the ledger,
    so they survive a restart mid-day and this does not."""
    with _lock:
        _task.update({"id": task_id, "requester": requester, "usd": 0.0})


def _append(path, line):
    """One line, atomically. O_APPEND on a single write below PIPE_BUF is atomic on Linux even
    with several writers, which is what makes a shared fleet ledger safe later."""
    data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def record(usage, model=None, role="worker"):
    """Append one call to the ledger. Never raises: a failed write must not cost a task."""
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    miss = int(miss) if miss is not None else max(prompt - hit, 0)
    usd = usd_for(usage)

    with _lock:
        _task["usd"] += usd
        line = {
            "ts": _now().isoformat(timespec="seconds"),
            "tenant": TENANT,
            "agent_id": AGENT_ID,
            "task_id": _task["id"],
            "requester": _task["requester"],
            "role": role,                       # worker | reviewer — is the gate worth its cost?
            "model": model,
            "prompt_tokens": prompt,
            "cached_tokens": hit,
            "miss_tokens": miss,
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "usd": round(usd, 6),
        }
        _recent["at"] = 0.0                     # our own write invalidates the memo
    try:
        _append(LEDGER, line)
        if FLEET_LEDGER != LEDGER:
            _append(FLEET_LEDGER, line)
    except OSError as e:
        print(f"[budget] could not write the ledger ({e}) — the run continues", flush=True)

    # THE LOCAL WRITE HAPPENS FIRST AND IS THE WRITE-AHEAD LOG. If the control plane is down,
    # the spend is still on disk here and can be reconciled later; losing the record entirely
    # because a remote was unreachable would be the worst of both designs.
    #
    # A failure does NOT raise. This runs after the model call, so the money is already spent
    # and refusing now prevents nothing — it would only throw away the answer we paid for.
    # It is remembered instead, and `check()` refuses the NEXT call, which is the first moment
    # refusing actually saves anything.
    if fleet_control.enabled():
        try:
            answer = fleet_control.record_spend(AGENT_ID, usd, detail=_task["id"])
            with _lock:
                _remote["broken"] = None
                _remote["over"] = bool((answer or {}).get("over"))
                _remote["total"] = (answer or {}).get("total")
                _remote["ceiling"] = (answer or {}).get("ceiling")
        except OSError as e:
            with _lock:
                _remote["broken"] = str(e)
            print(f"[budget] could not record ${usd:.4f} with the fleet control plane ({e}) — "
                  f"it is in the local ledger, and the next call will be refused until the "
                  f"control plane answers again", flush=True)
    return usd


def _spent_24h(path):
    """Sum the last 24 hours out of a ledger file.

    Re-read rather than kept in memory, because at N>1 the fleet total includes lines this
    process did not write — an in-memory counter would silently under-count exactly when the
    ceiling matters most. Memoised briefly so a 200-step run does not re-read it 200 times.
    """
    now = time.time()
    if _recent["path"] == path and now - _recent["at"] < RECENT_TTL:
        return _recent["usd"]
    cutoff = now - 86400
    total = 0.0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                    ts = datetime.fromisoformat(row["ts"]).timestamp()
                except (ValueError, KeyError, TypeError):
                    continue                    # a torn line is not a reason to stop counting
                if ts >= cutoff:
                    total += float(row.get("usd") or 0.0)
    except FileNotFoundError:
        total = 0.0
    _recent.update({"path": path, "at": now, "usd": total})
    return total


def limits():
    """The three ceilings in force right now.

    Environment sets them at start-up; BUDGET_FILE overrides at runtime. Example:

        {"task_usd": 40, "daily_usd": 300, "fleet_daily_usd": 1000}

    A missing file, a malformed one, or a value that is not a positive number falls back to
    the configured default rather than to no limit. That direction matters: this is the money
    path, so a typo must never be able to remove a ceiling — only a deliberate, valid number
    can move one.
    """
    global _bad_budget_file
    out = {"task": TASK_USD, "daily": DAILY_USD, "fleet": FLEET_DAILY_USD}
    try:
        with open(BUDGET_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        if _bad_budget_file != str(e):
            _bad_budget_file = str(e)
            print(f"[budget] ignoring {BUDGET_FILE} ({e}) — using the configured ceilings",
                  flush=True)
        return out
    _bad_budget_file = None
    for key, field in (("task", "task_usd"), ("daily", "daily_usd"), ("fleet", "fleet_daily_usd")):
        value = (raw or {}).get(field)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[key] = value
    return out


def paused():
    """The fleet kill switch. Checked before any mail is fetched, so a pause consumes nothing
    and the backlog is intact when it is cleared.

    TWO SOURCES, EITHER OF WHICH CAN STOP THE WORK, and deliberately not one replacing the
    other. The control plane is the fleet-wide switch and the only one that still works when
    agents are PODs on different boxes. The local file stays because it is the switch that
    works when everything else is broken: an operator with a shell can stop this container
    without the network, the API or a token, and `pause()` below still writes it when a
    ceiling trips. A control that needs a healthy distributed system to say "stop" is a
    control that is missing on the day you most want it.

    They fail in opposite directions ON PURPOSE. An unreadable local file reads as "not
    paused", which is right — it means something is wrong with this container, and pausing
    would not fix it. An unreachable control plane reads as "paused", which is also right —
    it means nobody can stop the fleet, and spending on through that is how a runaway night
    happens. See fleet_control for why "unset" is a third case that must not be confused with
    either.
    """
    remote, reason = fleet_control.paused()
    if remote:
        _log_pause_reason(reason)
        return True
    return os.path.exists(PAUSE_FILE)


_pause_reported = {"reason": None}


def _log_pause_reason(reason):
    """Say why once, not every twenty seconds."""
    if reason and _pause_reported["reason"] != reason:
        _pause_reported["reason"] = reason
        print(f"[budget] PAUSED: {reason}", flush=True)


def pause(reason):
    try:
        with open(PAUSE_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"{_now().isoformat(timespec='seconds')}  {AGENT_ID}\n{reason}\n")
    except OSError as e:
        print(f"[budget] COULD NOT WRITE THE PAUSE FILE ({e}) — {reason}", flush=True)


def startup_report():
    """Lines to log once, so a per-agent 'fleet' ceiling cannot hide behind a correct number.

    The failure this exists for has no symptom until the day it matters: everything reports
    healthy, the ceiling reads $500, and the fleet is actually able to spend $500 per agent
    while a kill switch that says it stopped everybody stopped one container.
    """
    cap = limits()
    lines = [f"budget: task ${cap['task']:.0f} | {AGENT_ID} daily ${cap['daily']:.0f} | "
             f"fleet ${cap['fleet']:.0f} | ledger {LEDGER}"]
    lines += fleet_control.startup_report()
    shared = FLEET_LEDGER != LEDGER
    if fleet_control.enabled():
        # The warning below is about a per-agent file pretending to be a fleet control. With
        # the control plane in use that is no longer the operative risk — the plane counts
        # every agent — so saying it anyway would train the reader to skip the block that also
        # carries the real warnings.
        lines.append(f"budget: fleet total and kill switch come from the control plane; "
                     f"{LEDGER} is the local write-ahead log and {PAUSE_FILE} is the "
                     f"break-glass switch that works without a network")
    elif shared:
        lines.append(f"budget: fleet ledger {FLEET_LEDGER}, kill switch {PAUSE_FILE}")
    else:
        lines.append(
            f"WARNING: FLEET_LEDGER is this agent's own ledger, so the ${cap['fleet']:.0f} "
            f"fleet ceiling only counts {AGENT_ID}. With N agents the fleet can spend N times "
            f"that, and {PAUSE_FILE} pauses this container alone. Correct for a single agent; "
            f"set FLEET_LEDGER and FLEET_PAUSE_FILE to a shared path for a fleet.")
    return lines


def check():
    """Called before every LLM request. Raises BudgetExceeded, having tripped the fleet switch
    if that is the ceiling that went."""
    if paused():
        raise BudgetExceeded(
            f"the fleet is paused ({PAUSE_FILE} exists). No work is being consumed until it is "
            f"removed."
        )
    cap = limits()
    with _lock:
        task_usd, task_id = _task["usd"], _task["id"]
    if task_usd >= cap["task"]:
        raise BudgetExceeded(
            f"this task has spent ${task_usd:.2f}, at its ${cap['task']:.2f} ceiling "
            f"(AGENT_TASK_USD). Work already done is in the workspace."
        )
    day = _spent_24h(LEDGER)
    if day >= cap["daily"]:
        raise BudgetExceeded(
            f"{AGENT_ID} has spent ${day:.2f} in the last 24h, at its ${cap['daily']:.2f} "
            f"ceiling (AGENT_DAILY_USD)."
        )
    # THE FLEET CEILING, WHEN THE FLEET IS NOT ON ONE BOX. Two checks, in the order that costs
    # least to be wrong about.
    #
    # First the latch: a spend that could not be recorded is a spend nobody is counting, and
    # continuing past it means the ceiling is being checked against a total that is already
    # known to be short. Refusing here is the fail-closed half of the ledger migration, and
    # this is the first moment in the call path where refusing prevents anything.
    if fleet_control.enabled():
        with _lock:
            broken, over, total, ceiling = (_remote["broken"], _remote["over"],
                                            _remote["total"], _remote["ceiling"])
        if broken:
            raise BudgetExceeded(
                f"the last spend could not be recorded with the fleet control plane "
                f"({broken}). Refusing to start another call while the fleet total is "
                f"unknown — the work already done is in the workspace and the spend is in "
                f"this agent's local ledger."
            )
        # Then the plane's own verdict. `over` is computed there against the authoritative
        # total across every agent, which is the number a per-box file cannot see.
        if over:
            pause(f"the fleet control plane reports {AGENT_ID} over its ceiling "
                  f"(${total} of ${ceiling}), tripped on task {task_id}.")
            raise BudgetExceeded(
                f"the fleet control plane reports ${total} spent against a ${ceiling} "
                f"ceiling. Stopping."
            )

    fleet = _spent_24h(FLEET_LEDGER) if FLEET_LEDGER != LEDGER else day
    if fleet >= cap["fleet"]:
        # The one ceiling that stops the whole fleet, so it leaves a mark a human must clear.
        pause(f"fleet spend ${fleet:.2f} reached the ${cap['fleet']:.2f} ceiling "
              f"(FLEET_DAILY_USD), tripped by {AGENT_ID} on task {task_id}.")
        raise BudgetExceeded(
            f"the fleet has spent ${fleet:.2f} in 24h, at its ${cap['fleet']:.2f} ceiling. "
            f"All agents are now paused; remove {PAUSE_FILE} to resume."
        )


def task_summary():
    """One line for the reply footer. The human should see the cost of the thing they asked for
    without going to look for it."""
    with _lock:
        return f"cost: ${_task['usd']:.4f} this task"
