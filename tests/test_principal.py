"""D6: who is asking, and the rule that no decision reads a field the sender wrote.

The tests that matter here are the negative ones. It is easy to write an attestation layer that
accepts every valid message and also accepts a forged one — it passes the happy-path test
either way. So most of what follows is an attacker with an open relay on the LAN trying each
field in turn, plus the loops that cost real money if the guard is decorative.
"""
import os, sys, tempfile

WS = tempfile.mkdtemp(prefix="prin-ws-")
os.environ.update({"WORKSPACE_ROOT": WS, "STATE_FILE": os.path.join(WS, ".processed.json"),
                   "FLEET_HMAC_SECRET": "test-key-not-a-real-one"})
# IDENTITY IS PINNED, not inherited. These assertions name agent1's mailbox and id, so
# without this the suite passes in agent1's container and fails in agent2's — where "a peer
# called dev/agent2" is the agent running the test, and a self-loop refusal is CORRECT. A unit
# test that varies with the host is testing the host. See the same fix in test_notes.py.
os.environ.update({"TENANT": "dev", "AGENT_NAME": "agent1", "AGENT_DOMAIN": "agents.local"})
for _v in ("AGENT_ADDRESS", "VALIDATOR_NAME", "VALIDATOR_ADDRESS", "ALLOWED_SENDERS"):
    os.environ.pop(_v, None)
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
import agent_inbox, agent_principal, fleet_identity
from dataclasses import replace

all_ok = True
ME = fleet_identity.AGENT_ID                       # dev/agent1


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def mail(frm=b"Boss <boss@agents.local>", subject=b"Do a thing", mid=b"<m1@x>", extra=b""):
    return (b"From: " + frm + b"\r\nTo: agent1@agents.local\r\nSubject: " + subject +
            b"\r\nMessage-ID: " + mid + b"\r\n" + extra +
            b"Content-Type: text/plain\r\n\r\nplease.\r\n")


def env(**kw):
    """A parsed human envelope, optionally with fields forced on top."""
    e = agent_inbox.envelope_from_bytes(mail(), seq=1)
    return replace(e, **kw) if kw else e


def headers(d):
    return b"".join(f"{k}: {v}\r\n".encode() for k, v in d.items())


# ---- The baseline: a person sending a normal email ---------------------------
print("\n--- an ordinary human task still runs ---")
d = agent_principal.admit(env())
check("admitted", d.allowed, d.reason)
check("resolved as a human", d.principal.kind == "human", str(d.principal))
check("identified by address, not display name",
      d.principal.principal_id == "boss@agents.local", d.principal.principal_id)
check("HONESTLY MARKED UNATTESTED — mail cannot prove who sent it",
      d.principal.attested is False)
check("tenant comes from OUR identity, never the message",
      d.principal.tenant == fleet_identity.TENANT, d.principal.tenant)


# ---- The rule: sender-controlled fields do not survive admission -------------
print("\n--- a stranger's claims are stripped, not believed ---")
spoof = agent_inbox.envelope_from_bytes(
    mail(extra=headers({"X-Agent-Hops": "0", "X-Agent-Purpose": "review-request"})), seq=2)
check("the transport lifts them verbatim (its job is not to judge)",
      spoof.hops == 0 and spoof.purpose == "review-request")
d = agent_principal.admit(spoof)
check("admitted — no agent id claimed, so it is just a person with odd headers", d.allowed,
      d.reason)
check("PURPOSE IS STRIPPED: an unattested sender cannot route itself",
      d.envelope.purpose == "", repr(d.envelope.purpose))
check("  ...and the sanitised copy is what comes back", d.envelope is not spoof)

# The one that costs money: a hop counter the sender can reset is not a hop counter.
loop = agent_principal.admit(env(hops=99))
check("a hop count from a human is forced to 0, not enforced against",
      loop.allowed and loop.envelope.hops == 0, f"{loop.allowed} {loop.envelope.hops}")


# ---- Attestation -------------------------------------------------------------
print("\n--- an agent has to prove it is one ---")
PEER = "dev/agent2"


def from_peer(purpose="review-request", hops=1, mid="<p1@x>", thread="<p1@x>", secret=None,
              claim=PEER):
    """A CONSISTENT signed message: whatever is signed is what the envelope carries.

    Deliberately takes no override kwargs. The first version did, and `from_peer(hops=0)` then
    fed 0 to the signer AND to the envelope — a matched pair that verified correctly, so the
    "hops lowered to sneak past the loop guard" test passed while testing nothing. Tampering is
    done with an explicit replace() on the result, where it is visibly AFTER the signature.
    """
    sig = agent_principal.sign(secret or agent_principal.secret_for(claim), claim, ME, mid,
                               thread, hops, purpose)
    return env(from_agent_id=claim, purpose=purpose, hops=hops, message_id=mid,
               thread_id=thread, signature=sig, requester="agent2 <agent2@agents.local>",
               reply_to="agent2@agents.local")


d = agent_principal.admit(from_peer())
check("a correctly signed peer is admitted", d.allowed, d.reason)
check("resolved as an agent, by id", d.principal.principal_id == PEER, str(d.principal))
check("AND MARKED ATTESTED — this one is a fact, not a claim", d.principal.attested is True)
check("its hops survive, because the signature covers them", d.envelope.hops == 1)
check("its purpose survives too", d.envelope.purpose == "review-request")

check("from_agent is derived from the id, so the two cannot disagree",
      from_peer().from_agent is True and env().from_agent is False)

# ---- the loop breaker --------------------------------------------------------
# A one-shot "ask agent2 for an ack" ran for ten hops before an operator stopped it: the ack
# arrived as a task, the task produced a reply, the reply was another ack. Each lap was a full
# worker AND reviewer cycle. It was bounded — hops are signed and incremented — but the bound
# was twenty, which is twenty LLM round trips to discover that nobody had anything to say.
print("\n--- an exchange that is closing does not start a task ---")
for _p in agent_principal.TERMINAL_PURPOSES:
    d = agent_principal.admit(from_peer(purpose=_p))
    check(f"REFUSED: {_p!r} closes an exchange, so it never becomes a task",
          not d.allowed, d.reason)
    check(f"  ...and the reason says it was recorded, not that it was rejected",
          "recorded" in d.reason, d.reason)

# The other half must keep working, or agents cannot collaborate at all.
for _p in ("task", "question", "review-request"):
    check(f"{_p!r} still runs — it opens an exchange rather than closing one",
          agent_principal.admit(from_peer(purpose=_p)).allowed)

# The terminal check must not become a way to silence a HUMAN. Purpose is a machine field;
# a person's mail carries none, and nothing a human sends should be droppable this way.
check("a human's message is unaffected by the terminal-purpose rule",
      agent_principal.admit(env(purpose="answer")).allowed)

print("\n--- governance's cap, passed in rather than fetched ---")
# The plane serves inter_agent_thread_cap; the worker reads it once per message and hands it
# down. admit() must stay offline — an HTTP call inside the admission check would put a round
# trip on every message and make the security surface untestable without a server.
def _at_depth(n, purpose="task"):
    return agent_principal.replace(
        from_peer(purpose=purpose, hops=1),
        references=" ".join(f"<r{i}@x>" for i in range(n)))

check("under the cap, the exchange continues",
      agent_principal.admit(_at_depth(3), thread_cap=8).allowed)
d = agent_principal.admit(_at_depth(8), thread_cap=8)
check("at the cap, this agent stops taking part", not d.allowed, d.reason)
check("  ...and names the policy, not a nearby backstop",
      "governance caps agent threads at 8" in d.reason, d.reason)

# 0 is OFF, and only the plane may say it. The local ceiling still applies underneath.
check("cap 0 does not disable the ceiling that catches broken hop counting",
      not agent_principal.admit(
          _at_depth(agent_principal.AGENT_DEPTH_CEILING), thread_cap=0).allowed)
check("cap 0 DOES allow a thread deeper than a cap would have permitted",
      agent_principal.admit(_at_depth(12), thread_cap=0).allowed)

# None means nobody has said, which must not read as 0/off.
check("no cap supplied still leaves the ceiling in force",
      not agent_principal.admit(
          _at_depth(agent_principal.AGENT_DEPTH_CEILING), thread_cap=None).allowed)

# A human's long thread is governed by MAX_THREAD_DEPTH, not by the agent cap.
check("the agent cap does not apply to a human",
      agent_principal.admit(env(references=" ".join(f"<r{i}@x>" for i in range(9))),
                            thread_cap=8).allowed)

print("\n--- the depth backstop sits ABOVE the hop limit, not below it ---")
# The reason agents were exempt from the depth guard: duplicating the hop limit at a different
# number silently caps conversations at whatever this happens to be. So it must be unreachable
# in normal operation, and firing means the hop counter itself has stopped working.
check("the agent ceiling is strictly above the hop limit",
      agent_principal.AGENT_DEPTH_CEILING > agent_principal.MAX_HOPS,
      f"{agent_principal.AGENT_DEPTH_CEILING} vs {agent_principal.MAX_HOPS}")
_deep = agent_principal.replace(
    from_peer(purpose="task", hops=1),
    references=" ".join(f"<r{i}@x>" for i in range(agent_principal.AGENT_DEPTH_CEILING)))
d = agent_principal.admit(_deep)
check("REFUSED: a deep thread whose hop count stayed low", not d.allowed, d.reason)
check("  ...and it says the hop counter failed, not that a tidy limit was reached",
      "hop counting has failed" in d.reason, d.reason)

# Each field in the signed set, tampered with one at a time.
print("\n--- every signed field, tampered with ---")
for label, mutation in (
    ("hops lowered to sneak past the loop guard", dict(hops=0)),
    ("purpose swapped for a different verb", dict(purpose="task")),
    ("message id replaced", dict(message_id="<other@x>")),
    ("thread id replaced", dict(thread_id="<other@x>")),
    ("claimed sender changed to a third agent", dict(from_agent_id="dev/agent3")),
):
    d = agent_principal.admit(replace(from_peer(), **mutation))
    check(f"REFUSED: {label}", not d.allowed, "was admitted!")

d = agent_principal.admit(replace(from_peer(), signature=""))
check("REFUSED: claims to be an agent but signs nothing", not d.allowed)
check("  ...and is NOT quietly downgraded to a human",
      d.principal.kind != "human", str(d.principal))
d = agent_principal.admit(replace(from_peer(), signature="v1=" + "0" * 64))
check("REFUSED: a plausible-looking signature that is simply wrong", not d.allowed)
d = agent_principal.admit(from_peer(secret="the-wrong-key"))
check("REFUSED: signed with a key we do not share", not d.allowed)

# A signature is bound to its recipient, so a captured one cannot be aimed elsewhere.
elsewhere = agent_principal.sign(agent_principal.secret_for(PEER), PEER, "dev/agent7",
                                 "<p1@x>", "<p1@x>", 1, "review-request")
d = agent_principal.admit(replace(from_peer(), signature=elsewhere))
check("REFUSED: a message signed for a DIFFERENT agent, replayed at us", not d.allowed)

# No key configured at all: everything machine-shaped is refused, humans are unaffected.
_saved = os.environ.pop("FLEET_HMAC_SECRET")
check("with no key issued, an agent message is refused",
      not agent_principal.admit(from_peer(secret="x")).allowed)
check("  ...but a person can still send a task", agent_principal.admit(env()).allowed)
os.environ["FLEET_HMAC_SECRET"] = _saved

# Per-agent keys are the seam. A key for one agent must not verify another.
os.environ["FLEET_HMAC_SECRET_DEV_AGENT2"] = "agent2-only"
check("a per-agent key is preferred over the fleet key",
      agent_principal.secret_for(PEER) == "agent2-only", agent_principal.secret_for(PEER))
check("  ...and the fleet key no longer verifies that agent",
      not agent_principal.admit(from_peer(secret=_saved)).allowed)
check("  ...while its own key does", agent_principal.admit(from_peer()).allowed)
del os.environ["FLEET_HMAC_SECRET_DEV_AGENT2"]


# ---- Loops -------------------------------------------------------------------
print("\n--- the loops that would run until the ceiling trips ---")
d = agent_principal.admit(env(requester="agent1 <agent1@agents.local>"))
check("REFUSED: mail from our own mailbox", not d.allowed, d.reason)
d = agent_principal.admit(env(requester="Someone <x@y>", reply_to="agent1@agents.local"))
check("REFUSED: reply-to points back at us — the loop is in the ANSWER", not d.allowed)
d = agent_principal.admit(env(requester="validator1 <validator1@agents.local>"))
check("REFUSED: our own reviewer's sign-off, bounced back into the inbox", not d.allowed,
      d.reason)
d = agent_principal.admit(from_peer(claim=ME))
check("REFUSED: a correctly signed message from OURSELVES is a loop, not a task",
      not d.allowed, d.reason)

check(f"REFUSED: at the hop limit ({agent_principal.MAX_HOPS})",
      not agent_principal.admit(from_peer(hops=agent_principal.MAX_HOPS)).allowed)
check("  ...and one hop below it is fine",
      agent_principal.admit(from_peer(hops=agent_principal.MAX_HOPS - 1)).allowed)

deep = " ".join(f"<r{i}@x>" for i in range(agent_principal.MAX_THREAD_DEPTH))
d = agent_principal.admit(env(references=deep))
check("REFUSED: a thread this deep is a mail loop", not d.allowed, d.reason)
check("  ...and this guard needs no hop counter, which is the point",
      "loop" in d.reason and d.envelope.hops == 0)
# It applies ONLY to traffic with no hop count. An attested agent already carries one, and
# duplicating it here would silently cap long agent conversations at this number instead of
# at the hop limit chosen for them.
d = agent_principal.admit(replace(from_peer(), references=deep))
check("  ...but an ATTESTED agent is exempt — its hop count already bounds the chain",
      d.allowed, d.reason)
check("a normal back-and-forth is untouched",
      agent_principal.admit(env(references="<a@x> <b@x> <c@x>")).allowed)


# ---- Routing and tenancy -----------------------------------------------------
print("\n--- routing, allow-list, tenancy ---")
d = agent_principal.admit(from_peer(purpose="delete-everything"))
check("REFUSED: a verb we do not implement is version skew, not a request", not d.allowed)
d = agent_principal.admit(from_peer(purpose=""))
check("REFUSED: an attested peer with no purpose at all", not d.allowed)

d = agent_principal.admit(env(tenant="acme"))
check("REFUSED: an envelope stamped with someone else's tenant", not d.allowed, d.reason)
d = agent_principal.admit(env(agent_id="dev/agent9"))
check("REFUSED: addressed to a different agent entirely", not d.allowed, d.reason)
sig = agent_principal.sign(agent_principal.secret_for("acme/agent1"), "acme/agent1", ME,
                           "<x@x>", "<x@x>", 1, "task")
d = agent_principal.admit(from_peer(claim="acme/agent1", mid="<x@x>", thread="<x@x>",
                                    purpose="task"))
check("REFUSED: a VALID signature from another tenant — no peer-to-peer across tenants",
      not d.allowed, d.reason)

check("allow-list: * lets anyone in", agent_principal.sender_allowed("a@b.com", "*"))
check("allow-list: an exact address", agent_principal.sender_allowed("Boss <a@b.com>", "a@b.com"))
check("allow-list: case does not matter", agent_principal.sender_allowed("A@B.com", "a@b.com"))
check("allow-list: a whole domain", agent_principal.sender_allowed("x@corp.com", "@corp.com"))
check("allow-list: a stranger is out", not agent_principal.sender_allowed("x@evil.com",
                                                                          "@corp.com"))
check("allow-list: a lookalike domain does not slip through",
      not agent_principal.sender_allowed("x@notcorp.com.evil.com", "@corp.com"))
_saved_allow = agent_principal.ALLOWED_SENDERS
agent_principal.ALLOWED_SENDERS = "@agents.local"
check("a listed human is admitted", agent_principal.admit(env()).allowed)
d = agent_principal.admit(env(requester="Nobody <nobody@elsewhere.com>"))
check("REFUSED: an unlisted human", not d.allowed, d.reason)
check("an attested agent does not need to be on the address list — it holds a key",
      agent_principal.admit(from_peer()).allowed)
agent_principal.ALLOWED_SENDERS = _saved_allow


# ---- The sending half, and the round trip ------------------------------------
print("\n--- signing: the far end must accept what we produce ---")
h = agent_principal.outbound_headers(PEER, "<out@x>", "<t@x>", "review-request", inbound_hops=0)
check("the hop count is incremented for us, once", h["X-Agent-Hops"] == "1", h["X-Agent-Hops"])
check("signed as this agent", h["X-Agent-Id"] == ME, h["X-Agent-Id"])
try:
    agent_principal.outbound_headers(PEER, "<o@x>", "<t@x>", "nonsense")
    check("refuses an unknown purpose at the SENDER", False, "did not raise")
except ValueError:
    check("refuses an unknown purpose at the SENDER, where the log is useful", True)
try:
    agent_principal.outbound_headers(PEER, "<o@x>", "<t@x>", "task",
                                     inbound_hops=agent_principal.MAX_HOPS)
    check("refuses to send a message the recipient must drop", False, "did not raise")
except ValueError:
    check("refuses to send a message the recipient must drop", True)

# The whole path: sign -> compose -> parse -> admit. This is the test that would have caught a
# verifier that accepts everything, because the signer and the verifier disagree if either is
# wrong, and only agree if both are right.
os.environ["AGENT_NAME"] = "agent1"
sent = agent_principal.outbound_headers("dev/agent1", "<rt@x>", "<rt@x>", "review-request",
                                        inbound_hops=0)
raw = mail(frm=b"agent1 <someone-else@agents.local>", mid=b"<rt@x>", extra=headers(sent))
d = agent_principal.admit(agent_inbox.envelope_from_bytes(raw, seq=3))
check("ROUND TRIP: signed here, parsed off the wire — refused, because we signed as ourselves",
      not d.allowed, d.reason)
check("  ...and the reason is the self-loop, meaning the signature itself VERIFIED",
      "ourselves" in d.reason, d.reason)

# ---- Telling the agent the room is closing -----------------------------------
# The count is a NOTICE, not a control, so what is tested is that it never promises a message
# the sender would refuse — a warning that says "one left" when there are none is worse than no
# warning, because the agent writes a continuation instead of a conclusion.
print("\n--- the wind-down notice ---")
_saved_hops = agent_principal.MAX_HOPS
agent_principal.MAX_HOPS = 20

check("a human task gets no notice at all — there is no conversation to run out",
      agent_principal.conversation_note(0, "") == "")
check("ten rounds means nineteen further messages from a standing start",
      agent_principal.exchanges_left(0) == 19, agent_principal.exchanges_left(0))

# The property that matters: exchanges_left agrees with what outbound_headers will actually do,
# at every hop. Checked by asking both, rather than by asserting a number twice.
for _h in range(0, agent_principal.MAX_HOPS + 3):
    _left = agent_principal.exchanges_left(_h)
    try:
        agent_principal.outbound_headers(PEER, "<w@x>", "<t@x>", "answer", inbound_hops=_h)
        _sendable = True
    except ValueError:
        _sendable = False
    # Parenthesised deliberately: `_left > 0 != _sendable` is a CHAINED comparison in Python,
    # which reads as `_left > 0 and 0 != _sendable` and quietly never fires.
    if (_left > 0) != _sendable:
        check(f"  notice and sender disagree at hop {_h}", False,
              f"left={_left} sendable={_sendable}")
        break
else:
    check("the notice never promises a message the sender would refuse, at any hop", True)

_mid = agent_principal.conversation_note(4, "dev/agent2")
check("mid-conversation it states the budget without nagging",
      "15 messages remain" in _mid and "Start closing" not in _mid, _mid)
_near = agent_principal.conversation_note(17, "dev/agent2")
check("near the end it says to start closing", "Start closing" in _near, _near)
check("  ...and counts the message being written now, not the ones after it",
      "2 message(s) remain" in _near, _near)
_over = agent_principal.conversation_note(19, "dev/agent2")
check("at the limit it says the peer will refuse this reply, so write it for the human",
      "THIS CONVERSATION IS OVER" in _over and "refuse it" in _over, _over)
check("  ...and the sender agrees there is nothing left to send",
      agent_principal.exchanges_left(19) == 0)
check("past the limit it does not go negative",
      agent_principal.exchanges_left(99) == 0, agent_principal.exchanges_left(99))
agent_principal.MAX_HOPS = _saved_hops

import shutil; shutil.rmtree(WS, ignore_errors=True)
print("\n" + ("ALL PRINCIPAL TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
