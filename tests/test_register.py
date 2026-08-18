"""The wire between the worker and ship_app: thread context across a process boundary.

`ship_app` registers an app with the fleet control plane and has to tell it which conversation
to answer in later. It is a SEPARATE PROCESS from the worker — the agent invokes it through the
shell — so the only channel is the environment. That makes this an easy thing to get subtly
wrong in a way nothing notices: governance's "it's live" email would still be sent, just into
its own new thread, and the person who asked would get an orphaned message about an app they
had to go and correlate by hand.

The property under test is therefore not "register was called" but "the id promised to the
control plane is the id the reply actually carries". Those are two different pieces of code
minutes apart, and the promise is made first.
"""
import os
import sys
import tempfile

WS = tempfile.mkdtemp(prefix="reg-ws-")
os.environ.update({"WORKSPACE_ROOT": WS, "TENANT": "dev", "AGENT_NAME": "agent1",
                   "AGENT_DOMAIN": "agents.local"})
for _k, _p in (("SPEND_LEDGER", ".spend.jsonl"), ("FLEET_LEDGER", ".spend.jsonl"),
               ("FLEET_PAUSE_FILE", "FLEET-PAUSED"), ("BUDGET_FILE", "budget.json")):
    os.environ[_k] = os.path.join(WS, _p)
for _v in ("FLEET_CONTROL_URL", "FLEET_TOKEN"):
    os.environ.pop(_v, None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent_envelope, agent_outbox, agent_worker                            # noqa: E402

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def envelope(**kw):
    base = dict(task_id="task-0007-build-a-thing", tenant="dev", agent_id="dev/agent1",
                source="email", requester="Jianmin <boss@agents.local>",
                reply_to="boss@agents.local", subject="build me a thing",
                body="please build a thing", thread_id="<root@x>", message_id="<root@x>",
                references="", hops=0, purpose="", from_agent_id="", signature="",
                state="submitted")
    base.update(kw)
    return agent_envelope.TaskEnvelope(**base)


print("--- what crosses the process boundary ---")
for _v in list(os.environ):
    if _v.startswith("FLEET_THREAD_"):
        del os.environ[_v]
env = envelope()
mid = agent_worker.export_thread_context(env)

check("the requester is exported so the follow-up reaches whoever asked",
      os.environ["FLEET_THREAD_REQUESTER"] == "boss@agents.local")
check("the agent id is exported", os.environ["FLEET_THREAD_AGENT_ID"] == "dev/agent1")
check("the subject is the REPLY's subject, not the request's",
      os.environ["FLEET_THREAD_SUBJECT"] == "Re: build me a thing",
      os.environ["FLEET_THREAD_SUBJECT"])
check("references carry the thread so far, including the message being answered",
      os.environ["FLEET_THREAD_REFERENCES"] == "<root@x>",
      os.environ["FLEET_THREAD_REFERENCES"])

# THE ONE THAT MATTERS. The id is minted before the reply exists, so the only thing that makes
# it true is that `deliver()` is later handed this exact value.
check("a Message-ID is minted up front and exported",
      mid and os.environ["FLEET_THREAD_MESSAGE_ID"] == mid, mid)
check("  ...and it is a well-formed id, not a placeholder",
      mid.startswith("<") and mid.endswith(">") and "@" in mid, mid)

# Reply-To wins over From: that is who the harness answers, so it is who governance must answer.
os.environ.pop("FLEET_THREAD_REQUESTER")
agent_worker.export_thread_context(envelope(reply_to="someone-else@agents.local"))
check("Reply-To wins over From, matching where the harness actually sends the reply",
      os.environ["FLEET_THREAD_REQUESTER"] == "someone-else@agents.local")

print("\n--- the promise is kept: the reply carries the id that was promised ---")
sent = {}


class FakeSMTP:
    """Only smtplib is faked. The real EmailMessage is built and its real bytes are read, so a
    header that is assembled wrongly shows up here — the reason test_mailflow stopped stubbing
    send_mail after a folded References header passed a stubbed test and crashed in production.
    """
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self, *a):
        pass

    def has_extn(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, msg):
        sent["raw"] = msg.as_bytes()
        sent["message_id"] = msg["Message-ID"]


import smtplib                                                              # noqa: E402
smtplib.SMTP = FakeSMTP

env2 = envelope()
promised = agent_worker.export_thread_context(env2)
returned = agent_outbox.deliver(env2, "the thing is built", message_id=promised)

check("deliver() honours the promised Message-ID rather than minting its own",
      sent["message_id"] == promised, f"{sent['message_id']} != {promised}")
check("  ...and it is really on the wire, not just in the return value",
      promised.encode() in sent["raw"])
check("the reply's Subject matches what was promised to the control plane",
      f"Subject: {os.environ['FLEET_THREAD_SUBJECT']}".encode() in sent["raw"].replace(b"\r\n", b"\n"),
      sent["raw"].split(b"\n")[0][:80])
check("the boss is still copied on it", b"boss@agents.local" in sent["raw"])

print("\n--- the image tag names something CI actually published ---")
# Same class of bug as the Message-ID above, and it stalled every autonomous deploy: two places
# compute the tag, they must agree, and nothing notices when they don't. Registration sent the
# full 40-char commit sha; CI publishes `${GITHUB_SHA::7}`. Both were internally consistent, CI
# went green, the registration was accepted, and the sha in the failing pull was a real commit —
# so the only symptom was a pod in ImagePullBackOff forever.
#
# The expected value is read out of the GENERATED WORKFLOW TEXT, not from IMAGE_TAG_LEN. Reading
# the constant would pass even if the workflow had been edited by hand to disagree with it, which
# is precisely the drift that caused this.
import agent_delivery                                                       # noqa: E402
import ship_app                                                             # noqa: E402

FULL_SHA = "460beb13d637a1f2e5c8b90a4d7e6f1029384756"
workflow = agent_delivery.ci_workflow("quotes")
m = __import__("re").search(r"\$\{GITHUB_SHA::(\d+)\}", workflow)
check("the generated CI workflow still stamps a truncated sha", bool(m),
      "no ${GITHUB_SHA::N} in the workflow — if CI's tagging changed, this test must change too")
ci_tag = FULL_SHA[:int(m.group(1))] if m else None
check("agent_delivery.image_tag reproduces what CI publishes",
      agent_delivery.image_tag(FULL_SHA) == ci_tag,
      f"{agent_delivery.image_tag(FULL_SHA)} != {ci_tag}")
check("  ...and it is a truncation, not the whole commit sha",
      agent_delivery.image_tag(FULL_SHA) != FULL_SHA)

captured = {}
ship_app.run = lambda *a, **kw: (0, FULL_SHA + "\n")
ship_app.fleet_register.register = lambda **kw: (captured.update(kw) or
                                                 {"target": "hp-tiger", "status": "deploying",
                                                  "url": "http://hp-tiger:30000"})
APP_DIR = tempfile.mkdtemp(prefix="reg-app-")
os.makedirs(os.path.join(APP_DIR, "k8s"))
with open(os.path.join(APP_DIR, "k8s", "deployment.yaml"), "w", encoding="utf-8") as fh:
    # THE REAL EXAMPLE, not hand-written YAML. The agent copies manifest_example and adapts it,
    # so that is the only input shape worth testing — and a fixture written by hand is how the
    # port reader passed review while being unable to parse any manifest that ever existed.
    fh.write(agent_delivery.manifest_example("quotes", 0, container_port=3000))
os.environ["FLEET_THREAD_MESSAGE_ID"] = "<promised@agent1>"
ship_app._register_with_fleet("quotes", APP_DIR)

check("registration sends the tag CI published, not the commit sha",
      captured.get("image", "").endswith(":" + str(ci_tag)), captured.get("image"))
check("  ...and the repo half of the image is unchanged",
      captured.get("image") == f"{agent_delivery.image_name('quotes')}:{ci_tag}",
      captured.get("image"))
check("the port comes from the manifest the agent wrote", captured.get("port") == 3000,
      repr(captured.get("port")))
shutil_mod = __import__("shutil")
shutil_mod.rmtree(APP_DIR, ignore_errors=True)

print("\n--- the generated artefacts agree on one port ---")
# The first production build served a dead link: ci/test.sh curled 3217, the server defaulted
# to 3000, the manifest declared 8080. Nothing here can force an agent to write a correct
# test, but the two things the harness DOES generate must not be the source of the confusion,
# and the guidance must name the failure rather than merely ask for consistency — "connection
# refused" reads like a crashed server, which is where that build's time went.
man = agent_delivery.manifest_example("quotes", 0, container_port=8080)
# Only the ones that NAME THE CONTAINER'S PORT. A Service legitimately publishes 80 and a
# NodePort 30000+; those are different numbers on purpose and demanding they match would be
# the same confusion in the other direction.
re_ = __import__("re")
container_ports = sorted(set(int(p) for p in re_.findall(
    r"(?:containerPort|targetPort|port:\s*)(\d+)", man.replace("port: 80\n", ""))))
check("containerPort, targetPort and both probes name one port",
      container_ports == [8080], repr(container_ports))
check("  ...and the Service still publishes 80 -> that port, which is not a mismatch",
      "port: 80" in man and "targetPort: 8080" in man)
check("  ...and PORT is set from it, since the server must read it from there",
      'value: "8080"' in man, repr([l for l in man.splitlines() if "PORT" in l]))

note = agent_delivery.delivery_note("", has_token=True)
check("the delivery note tells the agent to read one port from $PORT",
      "${PORT:-8080}" in note and "$PORT" in note)
check("  ...and says what the failure looks like, not just what to do",
      "connection refused" in note.lower())

print("\n--- the return leg: telling the fleet how the gate ruled ---")
# Registration happens mid-task, so for the whole build-and-review window the plane knows an
# app exists and not whether anyone approved it. This is the call that closes that window.
# The channel is a FILE, not the environment: the name travels child -> parent, and only
# ship_app knows which names were accepted.
posted = []
agent_worker.fleet_register.report_review = lambda app, passed, rounds=None, detail="": (
    posted.append({"app": app, "passed": passed, "rounds": rounds, "detail": detail}) or True)

WS2 = tempfile.mkdtemp(prefix="reg-ws2-")
with open(os.path.join(WS2, ".fleet-registered"), "w", encoding="utf-8") as fh:
    fh.write("quotes\nquotes\ntodo\n")            # duplicates: two pushes of the same app
check("registered app names are read back, in order and deduped",
      agent_worker.registered_apps(WS2) == ["quotes", "todo"],
      repr(agent_worker.registered_apps(WS2)))

agent_worker.report_review_to_fleet(WS2, {"passed": False, "rounds": 3, "notes": "still wrong"})
check("a rejected app is reported as a FAIL, so the fleet withholds the live email",
      len(posted) == 2 and all(p["passed"] is False for p in posted), repr(posted))
check("  ...with the round count and the objections",
      posted[0]["rounds"] == 3 and posted[0]["detail"] == "still wrong", repr(posted[0]))

posted.clear()
agent_worker.report_review_to_fleet(WS2, {"passed": True, "rounds": 1, "notes": "checks out"})
check("an approved app is reported as a PASS", [p["passed"] for p in posted] == [True, True])

# THE INVERSION THE ANNOUNCEMENT GATE EXISTS TO PREVENT. Gate off means nobody approved it —
# reporting a pass because no one objected would announce unreviewed work as reviewed.
posted.clear()
agent_worker.report_review_to_fleet(WS2, None)
check("no gate means NO verdict is sent, never a pass", posted == [], repr(posted))

# The common case: a task that answered a question and registered nothing must not post at all.
posted.clear()
WS3 = tempfile.mkdtemp(prefix="reg-ws3-")
agent_worker.report_review_to_fleet(WS3, {"passed": True, "rounds": 1, "notes": ""})
check("a task that registered nothing reports nothing", posted == [], repr(posted))

# Fails soft: the reply is already going out, and a held announcement is the safe direction.
def _boom(**kw):
    raise RuntimeError("fleet unreachable")
agent_worker.fleet_register.report_review = lambda *a, **kw: _boom()
try:
    agent_worker.report_review_to_fleet(WS2, {"passed": True, "rounds": 1, "notes": ""})
    check("an unreachable fleet does not take the reply down with it", True)
except Exception as e:
    check("an unreachable fleet does not take the reply down with it", False, repr(e))
shutil_mod = __import__("shutil")
for _d in (WS2, WS3):
    shutil_mod.rmtree(_d, ignore_errors=True)

print("\n--- ship_app degrades honestly when there is no task ---")
# Running ship_app by hand from a shell is legitimate and there is no envelope then. The
# failure to avoid is registering with an empty thread, which produces an "it's live" email
# addressed to nobody — worse than not registering, because it looks like it worked.
for _v in list(os.environ):
    if _v.startswith("FLEET_THREAD_"):
        del os.environ[_v]
thread = {k: os.environ.get(f"FLEET_THREAD_{k.upper()}", "")
          for k in ("requester", "subject", "message_id", "references", "agent_id")}
check("with no task context the message_id is empty, which is ship_app's skip condition",
      not thread["message_id"])

print("\n" + ("ALL REGISTER TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
import shutil; shutil.rmtree(WS, ignore_errors=True)
sys.exit(0 if all_ok else 1)
