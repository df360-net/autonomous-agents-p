"""agent_principal.py — who is asking, decided only from what can be verified.

D6 in docs/Fleet-Design.md, and the one rule the whole fleet rests on:

    NO SECURITY OR TENANCY DECISION MAY READ A FIELD THE SENDER CONTROLS.

Every inbound envelope resolves to a `Principal` here, BEFORE any handler logic runs. Nothing
downstream re-derives who the asker is, and nothing downstream is allowed to consult a raw
header — the headers were lifted into envelope fields by the transport, and the fields a sender
could have written are stripped by `admit()` unless they are attested.

WHY THIS IS NOT PARANOIA AT N=2. The mail server on this box runs with SPOOF_PROTECTION off and
an unauthenticated relay on port 25, so anything on the LAN can write `X-Agent-Id: dev/agent2`
and `X-Agent-Hops: 0`. That is not a hypothetical hardening exercise: `hops` is the field that
stops A -> B -> A running until the daily ceiling trips, and a hop counter a sender can reset is
not a hop counter. Enforcing it on an unattested field would have been worse than not having it,
because it reads like protection.

ATTESTATION, PHASE 1: a shared-secret HMAC over the fields that matter. `secret_for(agent_id)`
is the seam — today it resolves one fleet-wide key from the environment, later the control plane
hands out per-agent keys, and later still this becomes a projected ServiceAccount token verified
by TokenReview (the estate already reads SA tokens in cluster.py) or SPIFFE. The call sites do
not change; only this function does.

REPLAY is handled where it already was: the Message-ID dedupe in agent_inbox. A captured signed
message can be re-sent, but it runs at most once, which is the same guarantee the transport
always gave.

WHY AN UNVERIFIED CLAIM IS REFUSED, NOT DOWNGRADED. The tempting alternative — treat a bad
signature as "probably a human" and zero its hops — silently converts a spoof into an accepted
task. A real person's mail client never sets X-Agent-Id, so nothing legitimate is caught by
refusing; the only two senders affected are an attacker and a genuinely misconfigured agent, and
both should be loud. Fail closed here: this is blast radius, not an answer somebody is waiting
on.
"""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, replace
from email.utils import parseaddr

import fleet_identity

SIG_VERSION = "v1"

# A hop budget for machine-to-machine traffic. Three is enough for "ask a peer, peer asks a
# specialist, specialist answers" and short of anything that could be called a topology.
MAX_HOPS = int(os.environ.get("AGENT_MAX_HOPS", "3"))

# The backstop for loops that carry NO hop counter — a forwarding rule, a mailing list, an
# out-of-office responder. Those never look like agent traffic, so hops cannot see them, but
# References grows by one message on every pass and never shrinks. Twenty round trips in a
# single thread is a machine, not a conversation.
MAX_THREAD_DEPTH = int(os.environ.get("AGENT_MAX_THREAD_DEPTH", "20"))

# What one agent may ask another for. An attested message with a purpose outside this table is
# refused rather than treated as a plain task: the point of routing on purpose is that adding a
# new verb is a deliberate act on both ends, which is also how agent_peer.py stays a table
# rather than a protocol.
PURPOSES = ("task", "review-request", "review-result", "question", "answer")

# Who may task this agent. "*" means anyone, which is the default and is DELIBERATE: a task
# email that silently never runs is a worse failure than an ugly one, and the money control is
# the budget ceiling, not this. Set it (comma-separated addresses, or "@domain" for a whole
# domain) the moment this agent is reachable by anyone you would not hand a $150/day API key.
ALLOWED_SENDERS = os.environ.get("ALLOWED_SENDERS", "*").strip()


def log(msg):
    print(f"[principal] {msg}", flush=True)


@dataclass(frozen=True)
class Principal:
    """The answer to "who is asking", and how much of it is believed.

    `attested` is not decoration. It is the difference between a fact and a claim, and it is
    recorded so that a later audit can tell which decisions rested on which.
    """

    kind: str            # "human" | "agent"
    principal_id: str    # "dev/agent2" for an agent; the mailbox address for a human
    tenant: str
    attested: bool = False

    def __str__(self):
        return f"{self.kind}:{self.principal_id}" + ("" if self.attested else " (unattested)")


@dataclass
class Decision:
    """Admitted or not, and the envelope as it is allowed to be seen.

    `envelope` is returned rather than mutated in place because the sanitised copy is the only
    one anything downstream should ever hold. Handing back the original alongside a "these
    fields are not trustworthy" caveat is exactly the arrangement that gets forgotten once.
    """

    allowed: bool
    principal: Principal
    envelope: object
    reason: str = ""


# ---- Attestation -------------------------------------------------------------
def secret_for(agent_id):
    """The key that signs and verifies messages for one agent. THE SEAM.

    Read from the environment at call time rather than import time so a key can be rotated by
    recreating the container without this module caching the old one, and so tests can drive it.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", agent_id or "").upper().strip("_")
    return (os.environ.get(f"FLEET_HMAC_SECRET_{slug}")
            or os.environ.get("FLEET_HMAC_SECRET")
            or "").strip()


def canonical(from_agent_id, to_agent_id, message_id, thread_id, hops, purpose):
    """The exact bytes that get signed.

    Newlines are rejected in every part so the join is injective: without that, a sender could
    smuggle a field boundary inside a value and produce two different messages with one
    signature. TO is included so a message signed for one agent cannot be replayed at another.
    """
    parts = [SIG_VERSION, from_agent_id, to_agent_id, message_id, thread_id, str(hops), purpose]
    for p in parts:
        if "\n" in p or "\r" in p:
            raise ValueError(f"newline in a signed field: {p!r}")
    return "\n".join(parts)


def sign(secret, from_agent_id, to_agent_id, message_id, thread_id, hops, purpose):
    body = canonical(from_agent_id, to_agent_id, message_id, thread_id, hops, purpose)
    return f"{SIG_VERSION}=" + hmac.new(secret.encode("utf-8"), body.encode("utf-8"),
                                        hashlib.sha256).hexdigest()


def _verify(envelope):
    """(ok, reason). Checks the signature on a message that CLAIMS to come from an agent."""
    claim = (envelope.from_agent_id or "").strip()
    supplied = (envelope.signature or "").strip()
    if not supplied:
        return False, f"claims to be {claim!r} but carries no signature"
    secret = secret_for(claim)
    if not secret:
        # No key configured means no agent traffic can be attested, which means all of it is
        # refused. That is the correct state for a fleet that has not been issued keys yet.
        return False, (f"claims to be {claim!r} but no HMAC key is configured for it "
                       f"(set FLEET_HMAC_SECRET)")
    try:
        expected = sign(secret, claim, fleet_identity.AGENT_ID, envelope.message_id,
                        envelope.thread_id, envelope.hops, envelope.purpose)
    except ValueError as e:
        return False, f"unsignable fields: {e}"
    if not hmac.compare_digest(supplied, expected):
        return False, f"signature does not verify for {claim!r}"
    return True, ""


# ---- Admission ---------------------------------------------------------------
def _address(value):
    return (parseaddr(value or "")[1] or value or "").strip().lower()


def sender_allowed(value, allow=None):
    """Allow-list match on the asker's address. "*" allows everything."""
    allow = ALLOWED_SENDERS if allow is None else allow
    entries = [e.strip().lower() for e in (allow or "").split(",") if e.strip()]
    if not entries or "*" in entries:
        return True
    addr = _address(value)
    return any(addr == e or (e.startswith("@") and addr.endswith(e)) for e in entries)


def _is_self(envelope):
    """Mail from our own mailbox or our reviewer's, however it got back here.

    Both addresses, not just the worker's: the reviewer signs off from validator1@ and a bounce
    or a forwarding rule can land that in agent1's inbox, where it reads as a brand-new task
    describing work that was just completed — which the agent would then do again.
    """
    mine = {fleet_identity.AGENT_ADDRESS.lower(), fleet_identity.VALIDATOR_ADDRESS.lower()}
    return any(_address(v) in mine for v in (envelope.requester, envelope.reply_to))


def admit(envelope):
    """Resolve the principal and decide whether this envelope may run at all.

    Returns a Decision whose `envelope` is the sanitised copy: for anything not attested, the
    machine fields are put back to the values a stranger cannot influence. Callers use that copy
    and nothing else.
    """
    # 1. TENANCY, from our own identity and never from the payload. The inbox stamps these from
    #    fleet_identity today, so this cannot currently fail — which is the point of asserting
    #    it here rather than trusting it: the day a broker or an HTTP endpoint builds an
    #    envelope from a JSON body, "tenant" becomes a sender-controlled field, and the check
    #    that stops it is already written and already covered by a test.
    if envelope.tenant != fleet_identity.TENANT or envelope.agent_id != fleet_identity.AGENT_ID:
        return Decision(False, Principal("unknown", "", envelope.tenant), envelope,
                        f"addressed to {envelope.agent_id!r} in tenant {envelope.tenant!r}, "
                        f"but this is {fleet_identity.AGENT_ID!r} in {fleet_identity.TENANT!r}")

    claim = (envelope.from_agent_id or "").strip()
    if claim:
        # 2a. A MACHINE SENDER MUST PROVE IT. See the module docstring on why this refuses
        #     instead of downgrading to "probably a human".
        ok, why = _verify(envelope)
        if not ok:
            return Decision(False, Principal("unknown", claim, envelope.tenant), envelope, why)
        if claim == fleet_identity.AGENT_ID:
            return Decision(False, Principal("agent", claim, envelope.tenant, True), envelope,
                            "attested message from ourselves — a loop, not a task")
        claimed_tenant = claim.split("/")[0]
        if claimed_tenant != fleet_identity.TENANT:
            # Cross-tenant traffic is a control-plane relay decision, not something two agents
            # arrange between themselves; until there is a control plane there is no such path.
            return Decision(False, Principal("agent", claim, claimed_tenant, True), envelope,
                            f"cross-tenant message from {claim!r} into "
                            f"{fleet_identity.TENANT!r} — not relayed by the control plane")
        principal = Principal("agent", claim, claimed_tenant, attested=True)
        clean = envelope                      # every machine field is covered by the signature
    else:
        # 2b. A HUMAN. Unauthenticated, because mail is; so nothing it could have written is
        #     allowed to survive into a decision. hops and purpose go back to their defaults
        #     whatever arrived in the headers.
        principal = Principal("human", _address(envelope.requester), fleet_identity.TENANT,
                              attested=False)
        clean = replace(envelope, hops=0, purpose="", signature="")

    # 3. NEVER TAKE ORDERS FROM OURSELVES. Cheap, and the loop it prevents is the expensive one.
    if _is_self(clean):
        return Decision(False, principal, clean, "mail from this agent's own mailbox")

    # 3b. MAIL FROM A FLEET MAILBOX MUST BE SIGNED. Without this, an unsigned message from
    #     agent1 arrives at agent2 as an ordinary HUMAN request: hops forced to 0, purpose
    #     stripped, and the hop-based loop guard therefore never engages — only thread depth
    #     would eventually stop an A -> B -> A ping-pong. The self-check above does not catch
    #     it, because agent1's address is not agent2's own.
    #
    #     Refusing on a sender-controlled field does NOT break D6's rule. The rule forbids
    #     GRANTING on one. Denying is fail-closed: a stranger forging `From: agent1@` is
    #     refused, which is the outcome we want, and a real agent is unaffected because a real
    #     agent signs.
    if principal.kind == "human" and _address(clean.requester) in fleet_identity.fleet_addresses():
        return Decision(False, principal, clean,
                        f"{_address(clean.requester)} is a fleet mailbox but the message is "
                        f"not signed — an agent that cannot prove it is one is not one")

    # 4. WHO MAY ASK. Attested agents are governed by holding a key, which is strictly stronger
    #    than an address match, so the address allow-list applies to people.
    if principal.kind == "human" and not sender_allowed(clean.requester):
        return Decision(False, principal, clean,
                        f"{principal.principal_id!r} is not in ALLOWED_SENDERS")

    # 5. HOP BUDGET. Only ever non-zero on an attested envelope, by construction of 2b.
    if clean.hops >= MAX_HOPS:
        return Decision(False, principal, clean,
                        f"hop limit reached ({clean.hops} >= {MAX_HOPS})")

    # 6. THREAD DEPTH — for traffic that carries NO hop counter, which is the only traffic it
    #    can help with. A forwarding rule, a mailing list or an out-of-office responder produces
    #    a loop with no counter in it, and References growing on every pass is the only signal
    #    available. An ATTESTED agent message already carries a hop count that is incremented by
    #    the harness and covered by the signature, so this guard would only duplicate it — and
    #    worse, duplicate it at a DIFFERENT number, silently capping long agent conversations at
    #    whatever this happens to be set to rather than at the hop limit that was chosen for it.
    depth = len(clean.references.split())
    if principal.kind != "agent" and depth >= MAX_THREAD_DEPTH:
        return Decision(False, principal, clean,
                        f"thread is {depth} messages deep (>= {MAX_THREAD_DEPTH}) — a loop")

    # 7. ROUTING. An unknown verb from a peer is a version skew, and guessing is how one agent
    #    silently does the wrong job for another.
    if principal.kind == "agent" and clean.purpose not in PURPOSES:
        return Decision(False, principal, clean,
                        f"unknown purpose {clean.purpose!r} from {principal.principal_id}")

    return Decision(True, principal, clean)


# ---- The sending half --------------------------------------------------------
def outbound_headers(to_agent_id, message_id, thread_id, purpose, inbound_hops=0):
    """Headers that make an agent-to-agent message admissible at the far end.

    Here rather than in agent_peer.py because a verifier without a signer cannot be tested, and
    an untested verifier is the kind that turns out to accept everything. `inbound_hops` is the
    hop count of the task that CAUSED this message; the increment happens once, here, so no
    caller can forget it or do it twice.

    Refuses to produce headers the far end would have to reject. Failing at the sender puts the
    error in the log of the process that can be fixed, instead of a refusal line in someone
    else's container.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose {purpose!r} is not one of {PURPOSES}")
    hops = int(inbound_hops) + 1
    if hops >= MAX_HOPS:
        raise ValueError(f"refusing to send at hop {hops}: the limit is {MAX_HOPS} and the "
                         f"recipient would drop it")
    secret = secret_for(fleet_identity.AGENT_ID)
    if not secret:
        raise ValueError("no HMAC key configured — this agent cannot sign, and an unsigned "
                         "agent message is refused by design (set FLEET_HMAC_SECRET)")
    return {
        "X-Agent-Id": fleet_identity.AGENT_ID,
        "X-Agent-Hops": str(hops),
        "X-Agent-Purpose": purpose,
        "X-Agent-Signature": sign(secret, fleet_identity.AGENT_ID, to_agent_id, message_id,
                                  thread_id, hops, purpose),
    }


def exchanges_left(inbound_hops):
    """How many more agent-to-agent messages this conversation has left, counting the one this
    agent is about to write. Zero means it cannot send at all.

    Mirrors `outbound_headers` exactly — it refuses at `inbound_hops + 1 >= MAX_HOPS`, so the
    last sendable hop is MAX_HOPS - 1. Derived here rather than recomputed at the call site
    because two subtly different arithmetics for one limit is how a warning ends up promising
    one more round than the sender will actually allow.
    """
    return max(0, MAX_HOPS - 1 - int(inbound_hops or 0))


def conversation_note(inbound_hops, peer_id):
    """What the agent is told about how much conversation is left. Empty for human tasks.

    WHY THE AGENT IS TOLD AT ALL, when it cannot change the number: without it the exchange does
    not end, it stops. The maths competition ran past its own declared finish because each agent
    kept answering a message that deserved an answer, and nothing in the prompt ever said the
    room was closing. A limit the agent cannot see is a cliff; a limit it can see is a deadline,
    and a deadline produces a closing paragraph instead of a severed thread.

    It stays a NOTICE, never a control. The count comes from the inbound envelope, which the
    model cannot reach, and saying it out loud does not make it settable — an agent that reads
    "two left" and asks for more gets the same refusal as one that never read it.
    """
    if not peer_id:
        return ""
    left = exchanges_left(inbound_hops)
    head = (f"\n--- HOW MUCH OF THIS CONVERSATION IS LEFT ---\n"
            f"You and {peer_id} have exchanged {int(inbound_hops or 0)} messages in this thread. "
            f"The harness allows {MAX_HOPS} and you cannot change that — the count rides on the "
            f"envelope, not on anything you write.\n")
    if left <= 0:
        return head + (
            f"THIS CONVERSATION IS OVER. Your reply will reach the human, but {peer_id} will "
            f"refuse it — an agent at the hop limit cannot sign, and an unsigned message from a "
            f"fleet mailbox is dropped by design. So write this one for the human: what was "
            f"settled, what is still open, and what you would do next if it continued.")
    if left <= 3:
        return head + (
            f"{left} message(s) remain, INCLUDING the one you are about to write. Start closing. "
            f"Say what you have settled and what is still unresolved, rather than opening a new "
            f"line of argument you will not get to finish. If the work genuinely needs more than "
            f"this, say so plainly in your reply and let a human decide — do not try to squeeze "
            f"it into fewer, longer messages.")
    return head + (
        f"{left} messages remain, including the one you are about to write. Enough to finish "
        f"properly, not enough to drift. A round trip costs two.")


def startup_report():
    """Lines worth logging once, so the posture is visible without reading the code."""
    keyed = bool(secret_for(fleet_identity.AGENT_ID))
    lines = [f"principal: hops<{MAX_HOPS} thread<{MAX_THREAD_DEPTH} "
             f"purposes={'/'.join(PURPOSES)} | agent-to-agent attestation: "
             + ("HMAC key present" if keyed else "NO KEY — all agent traffic refused")]
    if ALLOWED_SENDERS == "*":
        lines.append("WARNING: ALLOWED_SENDERS=* — anyone who can reach the mail server can "
                     "spend this agent's budget. The ceiling is the only thing bounding it.")
    else:
        lines.append(f"principal: only {ALLOWED_SENDERS} may task this agent")
    return lines
