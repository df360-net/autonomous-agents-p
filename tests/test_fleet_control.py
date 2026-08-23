"""The money controls, once they live on another machine.

Every test here is about the DIRECTION OF FAILURE, because that is the only interesting
property. A client that returns "not paused" when it can reach the control plane is easy and
proves nothing; the whole reason this module exists is what it answers when it CANNOT reach it,
and the answer has to be the opposite of what the file-based version does.

Run against a real HTTP server on a real socket rather than a stubbed opener. A fake that
returns whatever the test wants agrees with the implementation by construction — it cannot tell
you that a 500 raises HTTPError while a dead port raises URLError, which are different branches
that both have to end in "paused".
"""
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

WS = tempfile.mkdtemp(prefix="fleetctl-ws-")
os.environ.update({"WORKSPACE_ROOT": WS, "TENANT": "dev", "AGENT_NAME": "agent1",
                   "AGENT_DOMAIN": "agents.local"})
# NO TEST MAY TOUCH REAL FLEET STATE — see the same block in test_principal.py. This suite
# writes a pause file on purpose, and a pause file on the shared mount halts the live fleet.
for _k, _p in (("SPEND_LEDGER", ".spend.jsonl"), ("FLEET_LEDGER", ".spend.jsonl"),
               ("FLEET_PAUSE_FILE", "FLEET-PAUSED"), ("BUDGET_FILE", "budget.json")):
    os.environ[_k] = os.path.join(WS, _p)
os.environ.pop("FLEET_CONTROL_URL", None)
os.environ.pop("FLEET_TOKEN", None)
_HERE = os.path.dirname(os.path.abspath(__file__))
# BOTH LAYOUTS, and the order matters. The source tree keeps the modules in agent/;
# the image copies them flat into /app beside this tests/ directory (see the Dockerfile).
# A test has to run in either, because the image ships these as its only self-check.
sys.path[:0] = [os.path.join(_HERE, "..", "agent"), os.path.join(_HERE, "..")]
import agent_budget, fleet_control                                          # noqa: E402

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


# ---- A control plane we can break on purpose ---------------------------------
class Plane:
    """Serves /fleet/pause and /fleet/spend, and can be told to misbehave."""

    def __init__(self):
        self.paused = False
        self.over = False
        self.status = 200
        self.spend_status = None                       # set to 503 to break POST /fleet/spend
        self.hits = {"pause": 0, "spend": 0, "settings": 0, "status": 0}
        self.settings = {"inter_agent_thread_cap": 8}
        self.has_status = True                         # False = a plane that predates it
        self.budget = {"spend_ceiling": 10.0, "your_total_24h": 0.0, "over": False}
        self.last_body = None
        self.last_auth = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass                                   # keep the test output readable

            def _reply(self, obj, status=None):
                body = json.dumps(obj).encode()
                self.send_response(status or outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.last_auth = self.headers.get("Authorization")
                if self.path.startswith("/fleet/status"):
                    # An OLDER plane does not have this endpoint. The catch-all below used to
                    # answer it 200 with an unrelated body, which is worse than a 404 in a
                    # test: the client cannot tell a missing endpoint from a working one, and
                    # every assertion about the kill switch passed while reading a body that
                    # never mentioned `paused`.
                    if not outer.has_status:
                        return self._reply({"error": "not found"}, 404)
                    outer.hits["status"] += 1
                    outer.hits["pause"] += 1          # it is the poll, whatever it is called
                    return self._reply({"paused": outer.paused, **outer.settings,
                                        **outer.budget})
                if self.path.startswith("/fleet/pause"):
                    outer.hits["pause"] += 1
                    return self._reply({"paused": outer.paused, **outer.settings})
                if self.path.startswith("/settings"):
                    outer.hits["settings"] += 1
                    return self._reply(outer.settings)
                if self.path.startswith("/fleet/spend"):
                    return self._reply({"agent": "dev/agent1", "total": 0})
                return self._reply({"error": "not found"}, 404)

            def do_POST(self):
                outer.last_auth = self.headers.get("Authorization")
                n = int(self.headers.get("Content-Length") or 0)
                outer.last_body = json.loads(self.rfile.read(n) or b"{}")
                outer.hits["spend"] += 1
                # Breakable INDEPENDENTLY of GET. The failure being reproduced took out the
                # spend write while the kill switch kept answering, and a toggle that broke
                # both would make the agent pause instead — a different branch entirely, and
                # the one that already worked.
                if outer.spend_status:
                    return self._reply({"error": "nope"}, outer.spend_status)
                return self._reply({"agent": outer.last_body.get("agent"), "total": 12.5,
                                    "count": 3, "ceiling": 500.0, "over": outer.over})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


def use(url, token=""):
    """Point the client at a plane and clear everything it remembers."""
    fleet_control.BASE_URL = url.rstrip("/")
    fleet_control.TOKEN = token
    fleet_control._pause_cache.update(at=0.0, paused=False)
    fleet_control._settings_cache.update(at=0.0, value=None)
    fleet_control._last_logged["state"] = None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


plane = Plane()

# ---- Unconfigured is not unreachable -----------------------------------------
# The bug this guards against would have stopped both live agents the moment the module
# shipped: today's compose sets no FLEET_CONTROL_URL, and "no URL" must not read as "no answer".
print("\n--- unconfigured: behave exactly as before ---")
use("")
check("no URL configured means the control plane is not in use", not fleet_control.enabled())
check("  ...and paused() says no, rather than failing closed on a fleet that never opted in",
      fleet_control.paused() == (False, ""))
check("  ...so agent_budget falls through to the local file (absent = running)",
      not agent_budget.paused())
open(os.environ["FLEET_PAUSE_FILE"], "w").close()
check("  ...and the local file still stops it, unchanged", agent_budget.paused())
os.remove(os.environ["FLEET_PAUSE_FILE"])

# ---- Configured and healthy --------------------------------------------------
print("\n--- configured and answering ---")
use(plane.url)
plane.paused = False
check("a clear 'not paused' is the only thing that lets work through",
      fleet_control.paused() == (False, ""))
use(plane.url)
plane.paused = True
p, why = fleet_control.paused()
check("an explicit pause stops it", p and "paused" in why, why)
check("  ...and agent_budget agrees without a local file present", agent_budget.paused())

# ---- The branches that matter ------------------------------------------------
print("\n--- unreachable means PAUSED (the whole point) ---")
use(plane.url)
plane.status = 500
p, why = fleet_control.paused()
check("HTTP 500 -> paused", p, why)
check("  ...and says so as a reason a human can act on", "unreachable" in why or "500" in why, why)
plane.status = 200

use(f"http://127.0.0.1:{free_port()}")
p, why = fleet_control.paused()
check("nothing listening -> paused (a different urllib branch from 500)", p, why)

use("http://127.0.0.1:1/nope")
p, why = fleet_control.paused()
check("connection refused -> paused", p, why)

# A control plane that answers with garbage is not a control plane that said "run".
garbage = Plane()
garbage.httpd.RequestHandlerClass.do_GET = lambda self: (
    self.send_response(200), self.send_header("Content-Length", "7"), self.end_headers(),
    self.wfile.write(b"not{json"[:7]))
use(garbage.url)
p, why = fleet_control.paused()
check("a 200 with an unparseable body -> paused, not 'falsy so not paused'", p, why)
garbage.stop()

# ---- Caching: bound the request rate, never the recovery ---------------------
print("\n--- the 60s cache ---")
use(plane.url)
plane.paused = False
plane.hits["pause"] = 0
for _ in range(5):
    fleet_control.paused()
check("five checks inside the TTL cost one request", plane.hits["pause"] == 1,
      plane.hits["pause"])

use(plane.url)
plane.status = 500
plane.hits["pause"] = 0
fleet_control.paused()
fleet_control.paused()
check("a FAILURE is not cached — it retries, so recovery takes one poll not one TTL",
      plane.hits["pause"] == 2, plane.hits["pause"])
plane.status = 200

# The TTL's honest cost, asserted rather than glossed: there IS a window where a warm cache
# keeps work running after the plane has died. That is the agreed trade for bounding the
# request rate, and it is only acceptable because the window is bounded — so both halves are
# tested, the tolerated one and the guarantee.
use(plane.url)
plane.paused = False
fleet_control.paused()                                     # warm the cache with "running"
fleet_control.BASE_URL = f"http://127.0.0.1:{free_port()}"  # plane vanishes; cache untouched
check("inside the TTL a warm 'running' cache still answers — the accepted window",
      fleet_control.paused() == (False, ""))
fleet_control.PAUSE_TTL = 0.0                              # ...and the moment it goes stale
check("the instant the cache is stale, unreachable means PAUSED",
      fleet_control.paused()[0])
fleet_control.PAUSE_TTL = 60.0

# ---- Spend -------------------------------------------------------------------
print("\n--- spend ---")
use(plane.url, token="test-token-not-a-real-one")
answer = fleet_control.record_spend("dev/agent1", 0.1234, detail="task-0001")
check("posts the agent, the amount and the detail",
      plane.last_body == {"agent": "dev/agent1", "amount": 0.1234, "detail": "task-0001"},
      plane.last_body)
check("sends the bearer token", plane.last_auth == "Bearer test-token-not-a-real-one")
check("returns the plane's authoritative view", answer["total"] == 12.5 and not answer["over"])

# A token in an exception is a token in the agent's transcript — the model reads stdout.
use(f"http://127.0.0.1:{free_port()}", token="super-secret-value")
try:
    fleet_control.record_spend("dev/agent1", 1.0)
    check("an unreachable plane raises rather than pretending the write landed", False)
except OSError as e:
    check("an unreachable plane raises rather than pretending the write landed", True)
    check("  ...and the token never appears in the error", "super-secret-value" not in str(e),
          str(e))

# ---- Recording a spend must never cost the agent the answer it just paid for ----------------
# record() runs AFTER the model call, so it cannot refuse a spend — the money is already gone.
# An implementation that "fails closed" by raising here would look right, throw away the answer
# just paid for, and still not prevent a single call.
#
# THE ASSERTION THAT USED TO LIVE HERE WAS TRUE AND USELESS: "a later successful write clears
# the latch — this recovers, it does not wedge". It does clear it. What the test never asked is
# whether a later write could ever HAPPEN, and it could not: the latch refused the model call
# that a write comes after. A test can pin a property and still miss the deadlock around it.
print("\n--- recording a spend ---")
USAGE = {"prompt_tokens": 100, "completion_tokens": 50}
agent_budget.start_task("task-0001-test", requester="boss@agents.local")

use(plane.url)
plane.over = False
agent_budget.record(USAGE)
fleet_control.flush_spend(timeout=10)
agent_budget.check()
check("a spend the plane accepted leaves the agent working", True)

fleet_control.BASE_URL = f"http://127.0.0.1:{free_port()}"      # plane dies mid-task
usd = agent_budget.record(USAGE)
check("record() still returns the cost — the answer paid for is not thrown away", usd > 0)
check("  ...and the spend is in the LOCAL ledger regardless",
      os.path.exists(os.environ["SPEND_LEDGER"]))
check("  ...and record() does not raise when the plane is gone", True)
try:
    agent_budget.check()
    check("ONE failed write does not stop the agent — it is retried, not latched", True)
except agent_budget.BudgetExceeded as e:
    check("ONE failed write does not stop the agent — it is retried, not latched",
          False, str(e))

use(plane.url)
check("and the spend owed from the outage is still pushed once the plane returns",
      fleet_control.flush_spend(timeout=10), str(fleet_control.spend_view()))

# `over` is the plane's verdict against this agent's recorded spend. It must trip the
# break-glass switch too, so a human finds a mark.
print("\n--- the plane's own ceiling verdict ---")
try:
    os.remove(os.environ["FLEET_PAUSE_FILE"])
except OSError:
    pass
plane.over = True
agent_budget.record(USAGE)
fleet_control.flush_spend(timeout=10)          # the verdict rides back on the push
try:
    agent_budget.check()
    check("`over` from the plane stops the agent", False, "did not raise")
except agent_budget.BudgetExceeded as e:
    check("`over` from the plane stops the agent", True)
    check("  ...and leaves the local pause file, so it survives losing the control plane",
          os.path.exists(os.environ["FLEET_PAUSE_FILE"]))
plane.over = False

print("\n--- inter_agent_thread_cap: unreadable is not 'off' ---")
# Governance owns the number; the agent owns enforcing it. The direction that matters is what
# happens when the answer cannot be read — a loop of agents acknowledging each other has
# already happened once, so "I could not read the policy" must never resolve to "no policy".
use(plane.url)
plane.settings = {"inter_agent_thread_cap": 5}
check("the plane's value is used", fleet_control.inter_agent_thread_cap() == 5)

use(plane.url)
plane.settings = {"inter_agent_thread_cap": 0}
check("0 is honoured — but ONLY because the plane said it",
      fleet_control.inter_agent_thread_cap() == 0)

use(plane.url)
plane.settings = {}                       # field missing, not zero
check("a MISSING field falls back to the default, it does not read as 0",
      fleet_control.inter_agent_thread_cap() == fleet_control.DEFAULT_THREAD_CAP)

use(plane.url)
plane.settings = {"inter_agent_thread_cap": "not-a-number"}
check("a malformed value falls back to the default rather than raising into the fetch loop",
      fleet_control.inter_agent_thread_cap() == fleet_control.DEFAULT_THREAD_CAP)

use(f"http://127.0.0.1:{free_port()}")    # nothing listening
check("an unreachable plane falls back to the default, not to unlimited",
      fleet_control.inter_agent_thread_cap() == fleet_control.DEFAULT_THREAD_CAP)

use("")                                   # no control plane configured at all
check("with no plane configured the local default still applies",
      fleet_control.inter_agent_thread_cap() == fleet_control.DEFAULT_THREAD_CAP)

use(plane.url)
plane.settings = {"inter_agent_thread_cap": 6}
before = plane.hits["pause"]
[fleet_control.inter_agent_thread_cap() for _ in range(5)]
check("the value is cached, so this is not a request per message",
      plane.hits["pause"] - before == 1, f"{plane.hits['pause'] - before} requests")

# THE POINT OF MOVING IT ONTO /fleet/pause. That call already happens before every poll, so
# the cap must ride along on it rather than cost a second round trip. If this ever reads 1,
# the cap has drifted back onto its own endpoint.
use(plane.url)
plane.settings = {"inter_agent_thread_cap": 4}
fleet_control.paused()                      # the call the worker already makes
before = plane.hits["pause"]
check("the cap is free after paused() — it rode in on the same response",
      fleet_control.inter_agent_thread_cap() == 4 and plane.hits["pause"] == before,
      f"{plane.hits['pause'] - before} extra requests")

# A plane that has not shipped the field yet must not break the kill switch.
use(plane.url)
plane.settings = {}
p, _ = fleet_control.paused()
check("an older plane with no cap field still answers the kill switch", p is False)
check("  ...and the cap falls back to the local default",
      fleet_control.inter_agent_thread_cap() == fleet_control.DEFAULT_THREAD_CAP)
plane.settings = {"inter_agent_thread_cap": 8}


# ---- The drainer: a blip must clear itself, without any work happening first ----------------
# THE BUG THIS PINS. A failed spend push used to latch "unrecorded spend", and the latch could
# only be cleared by a SUCCESSFUL push — which happens only after a model call, which the latch
# refused. One transient timeout took agent2 out until it was restarted (2026-08-22). The
# deciding property is not "the write is async": it is that RECOVERY NEEDS A REACHABLE PLANE
# AND NOTHING ELSE. So every assertion below is made without a single call being allowed.
print("\n--- the spend drainer ---")
import time                                                                   # noqa: E402

use(plane.url)
plane.over = False
try:
    os.remove(os.environ["FLEET_PAUSE_FILE"])         # the `over` test above left one
except OSError:
    pass
fleet_control.flush_spend(timeout=5)                  # start from an empty queue
plane.spend_status = 503                              # the plane stops taking spend
before = plane.hits["spend"]
fleet_control.queue_spend("dev/agent1", 0.25, detail="task-A")
check("queueing a spend never raises, whatever the plane is doing", True)
check("  ...and does not block the caller on the network",
      fleet_control.spend_view()["pending"] == 1)

deadline = time.time() + 5
while fleet_control.spend_view()["error"] is None and time.time() < deadline:
    time.sleep(0.05)
view = fleet_control.spend_view()
check("the failure is REMEMBERED, not swallowed", bool(view["error"]), str(view))
check("  ...and the money is still owed to the plane",
      view["pending"] == 1 and abs(view["pending_usd"] - 0.25) < 1e-9, str(view))
check("  ...and it was really tried, not just queued",
      plane.hits["spend"] > before, f"{plane.hits['spend'] - before} attempts")

plane.spend_status = None                             # the plane comes back
flushed = fleet_control.flush_spend(timeout=10)
view = fleet_control.spend_view()
check("RECOVERY NEEDS NOTHING BUT A REACHABLE PLANE — no model call, no restart",
      flushed and view["pending"] == 0, str(view))
check("  ...and the plane's verdict is picked up from the push that finally landed",
      view["ceiling"] == 500.0 and view["over"] is False, str(view))

# The grace window, from the caller's side: refuse eventually, never on the first blip.
agent_budget.SPEND_GRACE = 300
plane.spend_status = 503
fleet_control.queue_spend("dev/agent1", 0.10, detail="task-B")
deadline = time.time() + 5
while fleet_control.spend_view()["error"] is None and time.time() < deadline:
    time.sleep(0.05)
try:
    agent_budget.check()
    blip_allowed = True
except agent_budget.BudgetExceeded:
    blip_allowed = False
check("a blip does NOT stop the agent — this is the regression that bricked a pod",
      blip_allowed)

agent_budget.SPEND_GRACE = 0.01                       # pretend the outage has lasted
time.sleep(0.05)
try:
    agent_budget.check()
    stale_allowed = True
except agent_budget.BudgetExceeded as e:
    stale_allowed = False
    stale_msg = str(e)
check("spend the plane has not counted for a long time DOES stop it", not stale_allowed)
check("  ...and says so in a way that does not send anyone looking for a restart",
      "no restart is needed" in stale_msg, stale_msg[:120])

plane.spend_status = None
fleet_control.flush_spend(timeout=10)
try:
    agent_budget.check()
    recovered = True
except agent_budget.BudgetExceeded:
    recovered = False
check("and once the queue drains the agent works again, with no restart", recovered)
agent_budget.SPEND_GRACE = 300


# ---- /fleet/status: one poll, and a rollout that cannot brick the fleet ---------------------
print("\n--- /fleet/status ---")
use(plane.url)
plane.has_status = True
plane.budget = {"spend_ceiling": 10.0, "your_total_24h": 4.0, "over": False}
fleet_control._status_path["path"] = "/fleet/status"
before = plane.hits["status"]
fleet_control.paused()
b = fleet_control.plane_budget()
check("the ceiling and the 24h total ride in on the kill-switch poll",
      plane.hits["status"] - before == 1 and b["ceiling"] == 10.0 and b["total_24h"] == 4.0,
      str(b))
check("  ...so reading the ceiling costs no extra request",
      fleet_control.plane_budget()["ceiling"] == 10.0 and plane.hits["status"] - before == 1)

# UNREADABLE IS NOT "OFF", AND NULL IS NOT MISSING. Three states, and the middle one is the
# only one a plane may use to mean unlimited.
plane.budget = {"spend_ceiling": None, "your_total_24h": 4.0, "over": False}
use(plane.url)
fleet_control.paused()
b = fleet_control.plane_budget()
check("an explicit null ceiling means UNLIMITED — the plane said so",
      b["known"] is True and b["ceiling"] is None, str(b))

plane.budget = {}                                   # the field is simply absent
use(plane.url)
fleet_control.paused()
b = fleet_control.plane_budget()
check("an ABSENT ceiling is 'the plane did not say', not 'unlimited'",
      b["known"] is False, str(b))

# The rollout guard. An agent on the new image meeting a plane without the endpoint must keep
# working: unreachable means PAUSED, so a 404 read as an outage would stop the fleet on a
# routine deploy.
plane.has_status = False
plane.budget = {"spend_ceiling": 10.0, "your_total_24h": 0.0, "over": False}
use(plane.url)
fleet_control._status_path["path"] = "/fleet/status"
p, reason = fleet_control.paused()
check("a plane with no /fleet/status does NOT read as unreachable", p is False, reason)
check("  ...the kill switch still works through the fallback",
      fleet_control._status_path["path"] == "/fleet/pause")
check("  ...the thread cap still arrives", fleet_control.inter_agent_thread_cap() == 8)
check("  ...and the ceiling is simply unknown, so local limits apply unchanged",
      fleet_control.plane_budget()["known"] is False)
plane.has_status = True
fleet_control._status_path["path"] = "/fleet/status"

# ---- The ceiling is enforced against a number a restart cannot reset ------------------------
# /workspace is scratch: a fresh pod starts its local ledger at zero. If the ceiling were
# checked against the local ledger alone, restarting an agent would clear its ceiling. And it
# cannot be checked against the plane's total alone either, because that counts only what has
# been PUSHED — infra found agent1 with zero records lifetime, which means its `over` had been
# vacuously false the whole time.
print("\n--- the ceiling survives a restart, and does not wait for the push ---")
use(plane.url)
try:
    os.remove(os.environ["FLEET_PAUSE_FILE"])
except OSError:
    pass
fleet_control.flush_spend(timeout=10)
plane.over = False
plane.budget = {"spend_ceiling": 10.0, "your_total_24h": 9.5, "over": False}
fleet_control.paused()
agent_budget.start_task("task-0002-test", requester="boss@agents.local")
try:
    agent_budget.check()
    check("under the ceiling on the plane's own number, work continues", True)
except agent_budget.BudgetExceeded as e:
    check("under the ceiling on the plane's own number, work continues", False, str(e))

plane.budget = {"spend_ceiling": 10.0, "your_total_24h": 10.5, "over": False}
use(plane.url)
fleet_control.paused()
try:
    agent_budget.check()
    check("A RESTART CANNOT CLEAR THE CEILING — an empty local ledger is not zero spend",
          False, "did not raise")
except agent_budget.BudgetExceeded as e:
    check("A RESTART CANNOT CLEAR THE CEILING — an empty local ledger is not zero spend", True)
    check("  ...and the plane's ceiling is named as the one that stopped it",
          "fleet control plane" in str(e), str(e))

# And the other half: spend that has NOT reached the plane still counts against the ceiling,
# which is the hole `over` alone left open.
try:
    os.remove(os.environ["FLEET_PAUSE_FILE"])
except OSError:
    pass
plane.budget = {"spend_ceiling": 10.0, "your_total_24h": 9.0, "over": False}
use(plane.url)
fleet_control.paused()
plane.spend_status = 503
fleet_control.queue_spend("dev/agent1", 2.0, detail="task-0002-test")
try:
    agent_budget.check()
    check("unpushed spend counts against the ceiling too — `over` alone cannot see it",
          False, "did not raise")
except agent_budget.BudgetExceeded:
    check("unpushed spend counts against the ceiling too — `over` alone cannot see it", True)
plane.spend_status = None
fleet_control.flush_spend(timeout=10)
plane.budget = {"spend_ceiling": 10.0, "your_total_24h": 0.0, "over": False}

print("\n" + ("ALL FLEET CONTROL TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
plane.stop()
import shutil; shutil.rmtree(WS, ignore_errors=True)
sys.exit(0 if all_ok else 1)
