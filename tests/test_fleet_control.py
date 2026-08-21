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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
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
        self.hits = {"pause": 0, "spend": 0, "settings": 0}
        self.settings = {"inter_agent_thread_cap": 8}
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
                if self.path.startswith("/fleet/pause"):
                    outer.hits["pause"] += 1
                    return self._reply({"paused": outer.paused})
                if self.path.startswith("/settings"):
                    outer.hits["settings"] += 1
                    return self._reply(outer.settings)
                return self._reply({"total": 0}, 200)

            def do_POST(self):
                outer.last_auth = self.headers.get("Authorization")
                n = int(self.headers.get("Content-Length") or 0)
                outer.last_body = json.loads(self.rfile.read(n) or b"{}")
                outer.hits["spend"] += 1
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

# ---- The latch: where the ledger actually becomes fail-closed ----------------
# record() runs after the model call, so it cannot refuse a spend — the money is already gone.
# The refusal has to land on the NEXT check(), and that seam is the thing worth testing: an
# implementation that "fails closed" by raising inside record() would look right, throw away
# the answer just paid for, and still not prevent a single call.
print("\n--- the fail-closed latch ---")
USAGE = {"prompt_tokens": 100, "completion_tokens": 50}
agent_budget.start_task("task-0001-test", requester="boss@agents.local")

use(plane.url)
plane.over = False
agent_budget.record(USAGE)
check("a spend the plane accepted leaves no latch", not agent_budget._remote["broken"])
agent_budget.check()
check("  ...and check() lets the next call proceed", True)

fleet_control.BASE_URL = f"http://127.0.0.1:{free_port()}"      # plane dies mid-task
usd = agent_budget.record(USAGE)
check("record() still returns the cost — the answer paid for is not thrown away", usd > 0)
check("  ...and the spend is in the LOCAL ledger regardless",
      os.path.exists(os.environ["SPEND_LEDGER"]))
check("  ...and a failed remote write latches", bool(agent_budget._remote["broken"]))
try:
    agent_budget.check()
    check("check() refuses the NEXT call while the fleet total is unknown", False, "did not raise")
except agent_budget.BudgetExceeded as e:
    check("check() refuses the NEXT call while the fleet total is unknown", True)
    check("  ...and tells the reader the work already done is safe", "workspace" in str(e), str(e))

use(plane.url)
agent_budget.record(USAGE)
check("a later successful write clears the latch — this recovers, it does not wedge",
      not agent_budget._remote["broken"])
agent_budget.check()

# `over` is the plane's verdict against every agent's spend, which is the number no per-box
# file can see. It must trip the break-glass switch too, so a human finds a mark.
print("\n--- the plane's own ceiling verdict ---")
try:
    os.remove(os.environ["FLEET_PAUSE_FILE"])
except OSError:
    pass
plane.over = True
agent_budget.record(USAGE)
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
before = plane.hits["settings"]
[fleet_control.inter_agent_thread_cap() for _ in range(5)]
check("the value is cached, so this is not a request per message",
      plane.hits["settings"] - before == 1, f"{plane.hits['settings'] - before} requests")

print("\n" + ("ALL FLEET CONTROL TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
plane.stop()
import shutil; shutil.rmtree(WS, ignore_errors=True)
sys.exit(0 if all_ok else 1)
