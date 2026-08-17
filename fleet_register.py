"""fleet_register.py — an agent's client to the fleet control plane.

HANDOFF for the autonomous-agents project: drop this into the repo and have `ship_app`
call `register(...)` where it used to lean on the Harness deploy. It is **harness-called**,
not a model-facing tool — the agent's harness fills the fields; the model never invokes it
with arbitrary arguments.

Reads (nothing else):
  FLEET_CONTROL_URL  the fleet service base, e.g. http://hp-tiger:8091 (FLEET_URL also accepted)
  FLEET_TOKEN        THIS agent's scoped token — the actor and home box are derived from it
                     server-side, so they can't be spoofed from the payload.
  FLEET_PAUSE_TTL    seconds paused() trusts its last good answer on error (default 60)

The token is least-privilege: it can register/update this agent's OWN apps, read this
agent's own app + spend total, read the kill switch, and append this agent's OWN spend.
It cannot touch another agent's app, any infrastructure app, settings, or tokens. (So
`printenv FLEET_TOKEN` + curl buys the model nothing it couldn't already do as itself.)
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request

FLEET_URL   = (os.environ.get("FLEET_CONTROL_URL") or
               os.environ.get("FLEET_URL", "http://hp-tiger:8091")).rstrip("/")
FLEET_TOKEN = os.environ.get("FLEET_TOKEN", "")
PAUSE_TTL   = int(os.environ.get("FLEET_PAUSE_TTL", "60"))


class FleetError(RuntimeError):
    pass


def _call(method, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FLEET_URL + path, data=data, method=method, headers={
        "Authorization": f"Bearer {FLEET_TOKEN}", "Content-Type": "application/json",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def register(app, image, port=None, replicas=1, thread=None):
    """Register (or update) THIS agent's app. The control plane assigns the target box
    (this agent's home box), the nodePort, and the url — do NOT pass them; they're ignored.

    thread: {requester, subject, message_id, references, agent_id} — carried so governance's
    'it's live' email threads onto the agent's own reply. Pass it; it's how the loop closes.

    Returns {app, target, nodePort, url, status}. Raises FleetError on failure."""
    spec = {"app": app, "image": image, "replicas": replicas}
    if port is not None:
        spec["port"] = port
    if thread is not None:
        spec["thread"] = thread
    try:
        return _call("POST", "/agent/apps", spec)
    except urllib.error.HTTPError as e:
        raise FleetError(f"register {app}: HTTP {e.code}: {e.read().decode()[:200]}")
    except (urllib.error.URLError, OSError) as e:
        raise FleetError(f"register {app}: fleet unreachable: {e}")


def app_status(app):
    """This agent's app as the control plane sees it: {status, url, ...} or None if unknown.
    Poll this to learn when it went `deployed` and what url it resolved to."""
    try:
        return _call("GET", f"/apps/{app}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise FleetError(f"status {app}: HTTP {e.code}")


_pause = {"val": None, "ts": 0.0}


def paused(ttl=None):
    """The fleet kill switch, FAIL CLOSED. Returns True (= stop working) on any error once
    the last good answer is older than `ttl` seconds (default FLEET_PAUSE_TTL). A reachable
    control plane answers fresh on every call, so an explicit pause is seen on the very next
    check; a control-plane outage keeps the last good answer for up to `ttl`, then stops the
    agent. The failure direction is always toward 'stop'."""
    if ttl is None:
        ttl = PAUSE_TTL
    now = time.monotonic()
    try:
        v = bool(_call("GET", "/fleet/pause", timeout=5).get("paused"))
        _pause["val"], _pause["ts"] = v, now
        return v
    except Exception:
        if _pause["ts"] and now - _pause["ts"] < ttl:
            return _pause["val"]        # grace: trust the last good answer briefly
        return True                     # stale or never reached -> paused


def record_spend(amount, detail=""):
    """Append THIS agent's spend and return the authoritative total {agent,total,ceiling,over}.
    FAIL CLOSED: raises if it cannot record, so the caller stops rather than spending
    unrecorded (an unrecorded spend is an unbounded one). The `agent` is forced server-side
    to this token's actor — it cannot be spoofed, so an agent can never charge a colleague."""
    try:
        return _call("POST", "/fleet/spend", {"amount": amount, "detail": detail})
    except Exception as e:
        raise FleetError(f"spend not recorded (stop): {e}")
