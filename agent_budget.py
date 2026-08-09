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

# ---- Identity ----------------------------------------------------------------
# TENANT is a constant today and a real dimension later (see docs/Fleet-Design.md, D1/D2).
# It is recorded now because ledger history cannot be back-filled with it.
TENANT = os.environ.get("TENANT", "dev")
AGENT_NAME = os.environ.get("AGENT_NAME", "agent1")
AGENT_ID = f"{TENANT}/{AGENT_NAME}"

# ---- Where the numbers live --------------------------------------------------
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
LEDGER = os.environ.get("SPEND_LEDGER", os.path.join(WORKSPACE_ROOT, ".spend.jsonl"))
# Separate path once agents share a volume; the same file while there is one agent.
FLEET_LEDGER = os.environ.get("FLEET_LEDGER", LEDGER)
PAUSE_FILE = os.environ.get("FLEET_PAUSE_FILE", os.path.join(WORKSPACE_ROOT, "FLEET-PAUSED"))

# ---- Ceilings (loose until measured) -----------------------------------------
TASK_USD = float(os.environ.get("AGENT_TASK_USD", "5.00"))
DAILY_USD = float(os.environ.get("AGENT_DAILY_USD", "20.00"))
FLEET_DAILY_USD = float(os.environ.get("FLEET_DAILY_USD", "50.00"))

# ---- Prices, per million tokens ----------------------------------------------
# Estimates for deepseek-chat, overridable without a code change. Verify against current
# published pricing before trusting the dollar column; the token columns are unaffected.
PRICE_CACHE_HIT_PER_M = float(os.environ.get("PRICE_CACHE_HIT_PER_M", "0.07"))
PRICE_CACHE_MISS_PER_M = float(os.environ.get("PRICE_CACHE_MISS_PER_M", "0.27"))
PRICE_OUTPUT_PER_M = float(os.environ.get("PRICE_OUTPUT_PER_M", "1.10"))

_lock = threading.Lock()
_task = {"id": None, "requester": None, "usd": 0.0}
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


def paused():
    """The fleet kill switch. Checked before any mail is fetched, so a pause consumes nothing
    and the backlog is intact when it is cleared."""
    return os.path.exists(PAUSE_FILE)


def pause(reason):
    try:
        with open(PAUSE_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"{_now().isoformat(timespec='seconds')}  {AGENT_ID}\n{reason}\n")
    except OSError as e:
        print(f"[budget] COULD NOT WRITE THE PAUSE FILE ({e}) — {reason}", flush=True)


def check():
    """Called before every LLM request. Raises BudgetExceeded, having tripped the fleet switch
    if that is the ceiling that went."""
    if paused():
        raise BudgetExceeded(
            f"the fleet is paused ({PAUSE_FILE} exists). No work is being consumed until it is "
            f"removed."
        )
    with _lock:
        task_usd, task_id = _task["usd"], _task["id"]
    if task_usd >= TASK_USD:
        raise BudgetExceeded(
            f"this task has spent ${task_usd:.2f}, at its ${TASK_USD:.2f} ceiling "
            f"(AGENT_TASK_USD). Work already done is in the workspace."
        )
    day = _spent_24h(LEDGER)
    if day >= DAILY_USD:
        raise BudgetExceeded(
            f"{AGENT_ID} has spent ${day:.2f} in the last 24h, at its ${DAILY_USD:.2f} ceiling "
            f"(AGENT_DAILY_USD)."
        )
    fleet = _spent_24h(FLEET_LEDGER) if FLEET_LEDGER != LEDGER else day
    if fleet >= FLEET_DAILY_USD:
        # The one ceiling that stops the whole fleet, so it leaves a mark a human must clear.
        pause(f"fleet spend ${fleet:.2f} reached the ${FLEET_DAILY_USD:.2f} ceiling "
              f"(FLEET_DAILY_USD), tripped by {AGENT_ID} on task {task_id}.")
        raise BudgetExceeded(
            f"the fleet has spent ${fleet:.2f} in 24h, at its ${FLEET_DAILY_USD:.2f} ceiling. "
            f"All agents are now paused; remove {PAUSE_FILE} to resume."
        )


def task_summary():
    """One line for the reply footer. The human should see the cost of the thing they asked for
    without going to look for it."""
    with _lock:
        return f"cost: ${_task['usd']:.4f} this task"
