"""The last mile: the address reaches the customer, and nothing reaches them that they can't open.

Four defects, found on j-fleet7 and j-fleet8, three of them only because a human clicked something.
Every one was invisible to every automated check the fleet had:

  1. the agent held the app's address and was instructed to throw it away
  2. front-end code shipped absolute paths, so the page rendered and every button was dead
  3. a localhost URL went out in mail, where localhost means the READER's machine
  4. finished and hung looked identical in the log

WHAT IS TESTED HERE AND WHAT IS NOT. Three of the four are PROMPT changes, and a prompt is not
something a test can prove works — the model is what decides. So the prompt assertions are
narrow and honest: the instruction that caused the defect is GONE, and the one replacing it is
present. That catches a regression, which is the whole claim.

The parts that are Python — the address note, the absolute-path scan, the outbound scrub, the
idle cadence — are tested as behaviour, because this repository's own rule is that a safety
property lives in Python and never in the prompt. Item 3 in particular is enforced at the one
function every message passes through, the same argument the boss-Cc rule is built on.

No API calls, no mail server: smtplib is stubbed.
"""
import os
import sys
import tempfile as _tf

os.environ.update({
    "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "8025", "SMTP_USER": "",
    "AGENT_ADDRESS": "agent1@agents.local", "AGENT_NAME": "agent1",
    "BOSS_ADDRESS": "boss@agents.local",
    "WORKSPACE_ROOT": os.path.join(os.path.dirname(__file__), "ws-lastmile"),
})
_SANDBOX = _tf.mkdtemp(prefix="sandbox-")
# NO TEST MAY TOUCH REAL FLEET STATE — see the note in test_mailflow.py. The container points
# these at a shared host directory, and a suite that only overrides WORKSPACE_ROOT inherits
# them: one run wrote a ceiling trip into the production ledger and paused both live agents.
for _k, _p in (("SPEND_LEDGER", ".spend.jsonl"), ("FLEET_LEDGER", ".spend.jsonl"),
               ("FLEET_PAUSE_FILE", "FLEET-PAUSED"), ("BUDGET_FILE", "budget.json")):
    os.environ[_k] = os.path.join(_SANDBOX, _p)
_HERE = os.path.dirname(os.path.abspath(__file__))
# BOTH LAYOUTS: the source tree keeps the modules in agent/, the image copies them flat into
# /app beside this tests/ directory, and this suite gates the image.
sys.path[:0] = [os.path.join(_HERE, "..", "agent"), os.path.join(_HERE, "..")]

import agent_brain
import agent_delivery
import agent_outbox
import agent_validator
import agent_worker
import ship_app


# ---- 1. the address is the deliverable -------------------------------------------------
print("--- 1. the agent reports the address it was given ---")

# THE SENTENCE THAT COST A CUSTOMER THEIR APP. Written when the agent COMPUTED addresses from
# APP_HOST + PROXY_PORT_BASE + slot; carried over onto an address the control plane assigns and
# returns, which is not the same thing. Asserted as an absence because that is the regression
# that would silently reinstate the defect.
for text, where in ((agent_brain.SYSTEM_PROMPT, "the system prompt"),
                    (agent_delivery.delivery_note("", True), "the delivery note")):
    assert "do not repeat the one ship_app prints" not in text, where
    assert "the address will follow" not in text, f"{where} still defers the address"
    assert "do not compute" in text.lower(), f"{where} must still forbid INVENTING one"
print("PASS  neither the prompt nor the note tells the agent to withhold the printed address")

note = ship_app._live_address_note("lunchvote",
                                   {"status": "deployed",
                                    "url": "https://apps.j-fleet8.df360.net/lunchvote/"})
assert "https://apps.j-fleet8.df360.net/lunchvote/" in note, note
assert "deployed now" in note, note
pending = ship_app._live_address_note("lunchvote",
                                      {"status": "desired", "url": "https://x/lunchvote/"})
assert "minute or two" in pending, "an address that is not serving yet has to say so"
print("PASS  the note carries the address and says whether it is serving")

# DEGRADES TO THE INSTRUCTION, NOT TO SILENCE. An unreachable control plane must not quietly
# restore the old behaviour by saying nothing about the address at all.
blind = ship_app._live_address_note("lunchvote", {})
assert "Report the address" in blind, blind
assert "printed" in blind, blind
print("PASS  with no answer from the control plane it still says to report the printed address")


# ---- 2. absolute paths in the front end ------------------------------------------------
print("\n--- 2. front-end paths that 404 under the app's prefix ---")

app_dir = _tf.mkdtemp(prefix="app-")
os.makedirs(os.path.join(app_dir, "public"), exist_ok=True)
with open(os.path.join(app_dir, "public", "index.html"), "w", encoding="utf-8") as fh:
    fh.write("""<!doctype html>
    <img src="/logo.png">
    <script>
      fetch('/api/vote', {method:'POST'});
      fetch('api/tally');                      // correct: relative
      const cdn = "//cdn.example.com/x.js";    // protocol-relative, external, not this bug
    </script>""")
# WHERE MOST FRONT ENDS ACTUALLY KEEP THEIR FETCH CALLS. The first version of the scanner did
# not read .js at all, so an app whose page was clean and whose script was not passed silently —
# the scanner's own version of the bug it looks for. Found by mutating the suffix list and
# watching nothing fail.
with open(os.path.join(app_dir, "public", "app.js"), "w", encoding="utf-8") as fh:
    fh.write("""document.querySelector('#reset').onclick = () => fetch('/api/reset');\n""")
# Server code declares its own routes as absolute and is RIGHT to: app.post("/api/vote") is the
# server's routing table, not a browser request. It CANNOT be excluded by extension — server.js
# and app.js are the same suffix — so the verb list is what excludes it, and this is the case
# that pins that.
with open(os.path.join(app_dir, "server.js"), "w", encoding="utf-8") as fh:
    fh.write("""app.post('/api/vote', handler);\napp.get('/api/settings', handler);\n""")

lines = ship_app.absolute_url_warnings(app_dir)
blob = "\n".join(lines)
assert lines, "an app with absolute front-end paths must be warned about"
assert "/api/vote" in blob and "/logo.png" in blob, blob
assert "/api/reset" in blob, "a fetch in a .js file is the commonest form of this defect"
assert "/api/tally" not in blob, "a relative fetch is correct and must not be reported"
assert "/api/settings" not in blob, "a declared server route is not a browser request"
assert "cdn.example.com" not in blob, "// is protocol-relative and external, not this defect"
assert "server.js" not in blob, "the server's own route table is not a front-end request"
print(f"PASS  named {len([x for x in lines if x.startswith('    /')])} bad paths across page and "
      f"script, ignored the relative, the external and the server's routes")

clean = _tf.mkdtemp(prefix="clean-")
with open(os.path.join(clean, "index.html"), "w", encoding="utf-8") as fh:
    fh.write("""<img src="logo.png"><script>fetch('api/vote')</script>""")
assert ship_app.absolute_url_warnings(clean) == [], "a correct app must print nothing at all"
print("PASS  a correct app produces no warning")

assert "BASE_PATH" in ship_app._PREFIX_ADVICE
assert "BASE_PATH" in agent_brain.SYSTEM_PROMPT
assert "BASE_PATH" in agent_delivery.delivery_note("", True)
print("PASS  the scaffold, the prompt and the delivery note all name BASE_PATH")


# ---- 3. no address the recipient cannot open -------------------------------------------
print("\n--- 3. localhost never leaves the container ---")

for raw in ("see http://localhost:3000 for the preview",
            "http://127.0.0.1:8080/health returned 200",
            "running on http://0.0.0.0:3000/",
            "try http://[::1]:3000/api"):
    cleaned, found = agent_outbox.without_unreachable_urls(raw)
    assert found, f"not caught: {raw}"
    assert "localhost" not in cleaned and "127.0.0.1" not in cleaned, cleaned
    assert "0.0.0.0" not in cleaned and "::1" not in cleaned, cleaned
    assert "a local preview inside this container" in cleaned, cleaned
print("PASS  loopback by name, by number, by 0.0.0.0 and by v6 are all replaced")

keep = "the app is at https://apps.j-fleet8.df360.net/lunchvote/ and localhost is fine as a word"
cleaned, found = agent_outbox.without_unreachable_urls(keep)
assert cleaned == keep and not found, "a real address, and the bare word, must survive untouched"
print("PASS  the cluster address is not touched")


# THE CHOKEPOINT, AND THIS IS THE ASSERTION THAT MATTERS. Not "the helper works" — that the one
# function every message in this fleet passes through applies it, so a fourth caller added later
# cannot send an address the reader cannot open.
class _FakeSMTP:
    sent = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def has_extn(self, _name):
        return False

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


agent_outbox.smtplib.SMTP = _FakeSMTP
agent_outbox.send_mail(
    to="boss@agents.local", subject="lunchvote",
    body="Live behaviour on the running preview (http://localhost:3000 from inside this "
         "container) looked right.\nIt is deployed at https://apps.j-fleet8.df360.net/lunchvote/.",
    from_name="validator1", from_addr="validator1@agents.local")
body = _FakeSMTP.sent[-1].get_content()
assert "localhost:3000" not in body, body
assert "a local preview inside this container" in body, body
assert "https://apps.j-fleet8.df360.net/lunchvote/" in body, "the real address must survive"
print("PASS  a message sent through send_mail arrives with the unreachable address removed")

assert "NEVER WRITE IT IN YOUR OWN NOTES" in agent_validator.VALIDATOR_PROMPT
# The paragraph that told the reviewer NOT to check the address. Its exact cost: "I did not try
# to reach a cluster address (that would not be live yet by design and would prove nothing)" —
# written while the app had been live, and broken, for two minutes.
assert "Do not curl it and fail the reply" not in agent_validator.VALIDATOR_PROMPT
assert "MUST BE FETCHED" in agent_validator.VALIDATOR_PROMPT
print("PASS  the reviewer is told to fetch the address rather than told not to")


# ---- 4. finished and hung are different ------------------------------------------------
print("\n--- 4. the log says which of done and dead it is ---")

# Boot: nothing said yet, empty inbox. Without this a worker that comes up to no mail is silent,
# which is the state that reads as hung.
assert agent_worker.idle_line(False, None, 1000.0, 1000.0), "boot must print one"
# The terminal marker: the moment a task ends, whatever the cadence says.
assert agent_worker.idle_line(True, 999.9, 1000.0, 1000.0), "a finished task must be marked"
# And then quiet, so a task's own output is not buried under a heartbeat.
assert agent_worker.idle_line(False, 1000.0, 1020.0, 1020.0) is None, "must not log every poll"
inside = agent_worker.IDLE_EVERY - 1
assert agent_worker.idle_line(False, 1000.0, 1000.0 + inside, 0) is None
beat = agent_worker.idle_line(False, 1000.0, 1000.0 + agent_worker.IDLE_EVERY, 1000.0)
assert beat, "silence past the interval has to be distinguishable from a dead loop"
assert "idle" in beat and "last poll" in beat, beat
print(f"PASS  marks the end of a task, then beats every {agent_worker.IDLE_EVERY}s and not per poll")

print("\nALL ASSERTIONS PASSED")
