"""fleet_control.py — the money controls, when they live on another machine.

The kill switch and the fleet spend ceiling used to be files on a bind mount every agent could
see. That works exactly as long as every agent is on one box. Once agents are PODs that can
land on either box, a shared directory is not shareable and both controls quietly become
per-agent — which is worse than having none, because the number still looks right. They move
behind the fleet control plane's HTTP API; this module is the client.

    GET  /fleet/pause                    -> {"paused": bool, "inter_agent_thread_cap": int}
    POST /fleet/spend  {agent, amount}   -> {"total", "ceiling", "over", ...}

THE WHOLE POINT OF THIS FILE IS THE DIRECTION IT FAILS IN.

`agent_budget.paused()` is `os.path.exists(PAUSE_FILE)`: a path it cannot read reads as "not
paused". That is fail-OPEN, and on a local mount it is the right answer, because an unreadable
local file means something is broken with THIS container and stopping it changes nothing.
Across a network the same code means a control-plane outage leaves every agent spending with
nothing able to stop them. So here, unreachable means PAUSED. An agent that cannot confirm it
is allowed to spend money does not spend money.

UNCONFIGURED IS NOT UNREACHABLE, AND CONFLATING THEM WOULD HAVE HALTED THE LIVE FLEET. Today's
compose sets no FLEET_CONTROL_URL, so if "no answer" meant "paused" this module would stop
both running agents the moment it shipped. Unset means the control plane is not in use and
`agent_budget` keeps its file-based behaviour unchanged; only once a URL is configured does
silence start meaning stop. Same shape as agent_memory: MEMORY_TENANT_REMOTE unset is
"disabled", not "broken".

WHY THE SPEND CALL DOES NOT REFUSE THE SPEND. `record()` runs AFTER the model call — the money
is gone before this module hears about it, so raising there prevents nothing and loses the
record. Instead a failed write is remembered, and the NEXT pre-call check refuses. Stopping
before the next call is the only place stopping helps, and the local ledger keeps the record
that the remote missed.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("FLEET_CONTROL_URL", "").strip().rstrip("/")
TOKEN = os.environ.get("FLEET_TOKEN", "").strip()
# Short enough that a control-plane outage stops the fleet quickly, long enough that four
# agents polling every 20s do not turn a settings read into a load test.
#
# WHAT THE LATENCY ACTUALLY IS: up to this many seconds, for BOTH cases. An earlier version of
# this comment claimed an explicit pause landed "within one poll (~20s)" and only an outage
# waited for the TTL. That was wrong, and wrong in the direction that matters — the cache is
# consulted BEFORE the request, so an agent holding a fresh "not paused" answer does not ask
# again until it expires, however urgently a human is clicking pause. Both paths are bounded by
# the TTL, not by the poll interval. Lower this if 60s is too long to wait for a stop; do not
# tell anyone it is faster than it is.
PAUSE_TTL = float(os.environ.get("FLEET_PAUSE_TTL", "60"))
# Deliberately short. This call sits in front of every mail fetch, so a slow control plane
# must not become a slow agent — and since a timeout means "paused", waiting longer only
# delays the safe answer.
TIMEOUT = float(os.environ.get("FLEET_HTTP_TIMEOUT", "5"))

_lock = threading.Lock()
# Only SUCCESSFUL answers are cached. A failure is not, so recovery takes one poll rather than
# one TTL — the cache exists to bound request rate in the healthy case, not to remember errors.
_pause_cache = {"at": 0.0, "paused": False}
_last_logged = {"state": None}


def log(msg):
    print(f"[fleet] {msg}", flush=True)


def enabled():
    """Whether the control plane is in use at all. See the docstring: unset is not unreachable."""
    return bool(BASE_URL)


def _request(method, path, payload=None):
    """One HTTP call. Raises on anything that is not a 2xx with a JSON body.

    Errors are re-raised as plain OSError with a short message. Nothing that could contain the
    bearer token is ever put in an exception or a log line — the model reads this container's
    stdout, and a token in a traceback is a token in the transcript.
    """
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            if resp.status < 200 or resp.status >= 300:
                raise OSError(f"{method} {path} -> HTTP {resp.status}")
            return json.loads(body or "{}")
    except urllib.error.HTTPError as e:
        raise OSError(f"{method} {path} -> HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise OSError(f"{method} {path} -> unreachable ({e.reason})") from None
    except (ValueError, TimeoutError) as e:
        raise OSError(f"{method} {path} -> bad response ({e})") from None


# ---- The kill switch ---------------------------------------------------------
def paused():
    """(paused, reason). Fail-closed: anything other than a clear "no" is a yes.

    Returns (False, "") when no control plane is configured, so a fleet that has not migrated
    behaves exactly as it did before this module existed.
    """
    if not enabled():
        return False, ""
    now = time.time()
    with _lock:
        if now - _pause_cache["at"] < PAUSE_TTL:
            if _pause_cache["paused"]:
                return True, "the fleet control plane says the fleet is paused (cached)"
            return False, ""
    try:
        answer = _request("GET", "/fleet/pause")
    except OSError as e:
        # THE IMPORTANT BRANCH. No answer is not "carry on".
        _log_state("unreachable", f"cannot reach the fleet control plane ({e}) — treating the "
                                  f"fleet as PAUSED until it answers")
        return True, f"the fleet control plane is unreachable ({e}), so work is paused"
    is_paused = bool(answer.get("paused"))
    # The thread cap rides on this same response, so read it while we have it. Deliberately
    # tolerant: a plane that has not been upgraded yet simply omits the field, and that must
    # leave the kill switch working rather than turning a governance change into an outage.
    try:
        _cap_from(answer, now)
    except (TypeError, ValueError) as e:
        log(f"ignoring a malformed inter_agent_thread_cap on /fleet/pause ({e})")
    with _lock:
        _pause_cache.update(at=now, paused=is_paused)
    _log_state("paused" if is_paused else "running",
               "the fleet control plane says PAUSED" if is_paused
               else "fleet control plane reachable, not paused")
    return (True, "the fleet control plane says the fleet is paused") if is_paused else (False, "")


# ---- Governance settings -----------------------------------------------------
# The cap used when the plane has not told us otherwise. NOT a hardcoded policy — policy is the
# plane's `inter_agent_thread_cap`. This is what applies while nobody has said, which must be a
# real number rather than "unlimited": the failure it guards against is agents talking to each
# other forever, and that has already happened once.
DEFAULT_THREAD_CAP = int(os.environ.get("FLEET_DEFAULT_THREAD_CAP", "8"))
_settings_cache = {"at": 0.0, "value": None}


def inter_agent_thread_cap():
    """How deep an agent-to-agent thread may get before this agent stops taking part.

    0 means OFF, and ONLY the control plane may say so. An unreachable plane, a malformed
    answer or a missing field all fall back to DEFAULT_THREAD_CAP rather than to 0 — "I could
    not read the policy" must never resolve to "there is no policy", which is the direction
    that let the loop run in the first place.

    Unlike `paused()` this does not fail closed to a stop, because it cannot: refusing all peer
    traffic when the plane blinks would break collaboration on a network hiccup. It fails to
    the DEFAULT instead, which is the same shape of answer, just not the operator's chosen one.

    IT RIDES ON /fleet/pause, which is already fetched before every poll — so this normally
    costs no request at all. `paused()` stores the value on the way past, and the cold path
    below asks the same endpoint rather than a second one.
    """
    if not enabled():
        return DEFAULT_THREAD_CAP
    now = time.time()
    with _lock:
        if now - _settings_cache["at"] < PAUSE_TTL and _settings_cache["value"] is not None:
            return _settings_cache["value"]
    try:
        return _cap_from(_request("GET", "/fleet/pause"), now)
    except (OSError, TypeError, ValueError) as e:
        log(f"cannot read inter_agent_thread_cap ({e}) — using the default of "
            f"{DEFAULT_THREAD_CAP}")
        return DEFAULT_THREAD_CAP


def _cap_from(answer, now=None):
    """Pull the cap out of a /fleet/pause response and remember it.

    A MISSING KEY IS NOT A ZERO, and that is the whole function. `int(None)` raises and
    `or 0` reads an explicit 0 and an absent field as the same thing — but 0 means "governance
    turned the cap off" and absent means "nobody said", and collapsing those is what turns an
    unreadable policy into no policy. Only the plane may say 0.
    """
    raw = (answer or {}).get("inter_agent_thread_cap")
    cap = DEFAULT_THREAD_CAP if raw is None else max(0, int(raw))
    with _lock:
        _settings_cache.update(at=now if now is not None else time.time(), value=cap)
    return cap


def _log_state(state, message):
    """Log transitions, not polls. At a 20s cadence, logging every check would bury the one
    line that matters — the moment the answer changed — under three an hour times forever."""
    with _lock:
        if _last_logged["state"] == state:
            return
        _last_logged["state"] = state
    log(message)


# ---- The spend ledger --------------------------------------------------------
def record_spend(agent_id, usd, detail=None):
    """Append one spend record synchronously. Returns the plane's view: {total, ceiling, over}.

    Raises OSError if the write did not land. Kept for tests and for the drainer; the agent
    does NOT call this on the hot path any more — see `queue_spend`.
    """
    if not enabled():
        return None
    payload = {"agent": agent_id, "amount": round(float(usd), 6)}
    if detail:
        payload["detail"] = detail
    return _request("POST", "/fleet/spend", payload)


# ---- The drainer: why the push moved off the call path ------------------------
# A SINGLE TRANSIENT TIMEOUT USED TO BRICK THE POD, and the mechanism is worth stating
# precisely because the obvious fix does not address it. The push already ran after the model
# call and already did not raise; what it did was latch "unrecorded spend", and the next
# `check()` refused every call while the latch was set. The latch could only be cleared by a
# SUCCESSFUL push, a push only happens after a model call, and no model call was allowed —
# so the latch sealed itself and the agent refused every task until someone restarted it.
# Reproduced with the network stubbed: one failure, then a healthy plane forever, still dead.
#
# So the fix is not "make the write async". It is that A FAILURE MUST BE ABLE TO CLEAR ITSELF
# WITHOUT THE AGENT DOING WORK FIRST. That is what this thread is: it retries in the
# background, so recovery needs a reachable plane and nothing else.
#
# What is deliberately NOT solved here: durability across a pod death. The queue is in memory,
# and the local ledger is the write-ahead log that survives — which is the contract we agreed
# (I3). A pod that dies with entries pending loses them from the REMOTE aggregate only.
_pending = []                       # oldest first; one dict per unpushed spend
_drain = {"thread": None, "error": None, "last_ok": 0.0,
          "view": {"total": None, "ceiling": None, "over": False}}
_wake = threading.Event()
DRAIN_BACKOFF_MAX = float(os.environ.get("FLEET_DRAIN_BACKOFF_MAX", "60"))


def queue_spend(agent_id, usd, detail=None):
    """Hand one spend record to the drainer. Never blocks, never raises, never touches the
    network on this thread. The caller has already written the local ledger."""
    if not enabled():
        return
    with _lock:
        _pending.append({"agent": agent_id, "amount": round(float(usd), 6),
                         "detail": detail, "at": time.time()})
    _ensure_drainer()
    _wake.set()


def _ensure_drainer():
    with _lock:
        if _drain["thread"] is not None and _drain["thread"].is_alive():
            return
        t = threading.Thread(target=_drain_loop, name="fleet-spend-drainer", daemon=True)
        _drain["thread"] = t
    t.start()


def _drain_loop():
    """Push the queue, oldest first, retrying with backoff until the plane takes it.

    ONE AT A TIME AND IN ORDER, because each record carries its own task id: batching would
    either lose that or need a new endpoint, and the volume is a few hundred small POSTs per
    task. A permanent rejection (a 4xx that will never succeed) is not special-cased — it
    backs off like anything else and the caller's grace window eventually refuses work, which
    is the right outcome for a contract that is actually broken.
    """
    backoff = 1.0
    while True:
        with _lock:
            item = _pending[0] if _pending else None
        if item is None:
            _wake.wait(timeout=5.0)
            _wake.clear()
            continue
        try:
            answer = record_spend(item["agent"], item["amount"], item["detail"]) or {}
        except OSError as e:
            with _lock:
                _drain["error"] = str(e)
            time.sleep(backoff)
            backoff = min(backoff * 2, DRAIN_BACKOFF_MAX)
            continue
        backoff = 1.0
        with _lock:
            if _pending and _pending[0] is item:
                _pending.pop(0)
            _drain["error"] = None
            _drain["last_ok"] = time.time()
            _drain["view"] = {"total": answer.get("total"),
                              "ceiling": answer.get("ceiling"),
                              "over": bool(answer.get("over"))}


def spend_view():
    """What the caller needs to decide whether to keep working.

    `unpushed_age` is the age of the OLDEST unpushed record, which is the only honest measure
    of how stale the plane's copy is: a queue that is long but moving is healthy, and a queue
    of one that has not moved for ten minutes is not.
    """
    now = time.time()
    with _lock:
        oldest = _pending[0]["at"] if _pending else None
        return {"pending": len(_pending),
                "pending_usd": round(sum(p["amount"] for p in _pending), 6),
                "unpushed_age": 0.0 if oldest is None else now - oldest,
                "error": _drain["error"],
                "total": _drain["view"]["total"],
                "ceiling": _drain["view"]["ceiling"],
                "over": _drain["view"]["over"]}


def flush_spend(timeout=10.0):
    """Best-effort drain, for shutdown and for tests. Returns True when the queue is empty."""
    _ensure_drainer()
    _wake.set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            if not _pending:
                return True
        time.sleep(0.05)
    with _lock:
        return not _pending


def fleet_total(agent_id=None):
    """The authoritative 24h total. Raises OSError rather than returning a number it made up:
    a ceiling checked against a guess is not a ceiling."""
    if not enabled():
        return None
    path = f"/fleet/spend?agent={agent_id}" if agent_id else "/fleet/spend"
    return _request("GET", path)


def startup_report():
    """One line at boot, so the posture is visible without reading the code."""
    if not enabled():
        return ["fleet control: not configured — kill switch and fleet ledger are local files"]
    return [f"fleet control: {BASE_URL} (pause TTL {PAUSE_TTL:.0f}s, timeout {TIMEOUT:.0f}s, "
            f"token {'present' if TOKEN else 'MISSING'}); unreachable == paused"]
