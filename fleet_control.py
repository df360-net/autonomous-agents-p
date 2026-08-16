"""fleet_control.py — the money controls, when they live on another machine.

The kill switch and the fleet spend ceiling used to be files on a bind mount every agent could
see. That works exactly as long as every agent is on one box. Once agents are PODs that can
land on either box, a shared directory is not shareable and both controls quietly become
per-agent — which is worse than having none, because the number still looks right. They move
behind the fleet control plane's HTTP API; this module is the client.

    GET  /fleet/pause                    -> {"paused": bool}
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
# agents polling every 20s do not turn a settings read into a load test. Agreed with the
# control plane's owner: an explicit pause propagates within one poll (~20s), an outage stops
# everyone within the TTL.
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
    with _lock:
        _pause_cache.update(at=now, paused=is_paused)
    _log_state("paused" if is_paused else "running",
               "the fleet control plane says PAUSED" if is_paused
               else "fleet control plane reachable, not paused")
    return (True, "the fleet control plane says the fleet is paused") if is_paused else (False, "")


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
    """Append one spend record. Returns the plane's view: {total, ceiling, over, ...}.

    Raises OSError if the write did not land. The caller must NOT let that kill the task in
    progress — see the module docstring — but it must remember it, because an unrecorded spend
    is an unbounded one.
    """
    if not enabled():
        return None
    payload = {"agent": agent_id, "amount": round(float(usd), 6)}
    if detail:
        payload["detail"] = detail
    return _request("POST", "/fleet/spend", payload)


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
