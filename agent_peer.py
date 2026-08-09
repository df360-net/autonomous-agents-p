"""agent_peer.py — one agent asking another for something, with a human always copied in.

The sending half of D6. `agent_principal` decides whether to believe an inbound peer message;
this decides what goes out and signs it. Every inter-agent message in the fleet passes through
`send()`, which is what makes A2A later an implementation of one module rather than an edit to
every call site.

WHAT THE AGENT CHOOSES AND WHAT IT CANNOT

    the agent chooses    who to write to, why (a purpose from a fixed table), and what to say
    the harness decides  the hop count, the signature, the thread, and the CC to the boss

That split is the whole design. The hop counter is the control that stops A -> B -> A burning
the daily ceiling overnight, and a hop counter the sender can set is not a hop counter — so it
is computed here from the INBOUND envelope's count, which the agent never sees and cannot
reach. Same for the signature: an agent that could sign its own `from` field could impersonate
any agent in the tenant.

THE BOSS IS ALWAYS COPIED, AND NOT BY ASKING NICELY. Jianmin's requirement, and the reason it
is Python rather than a sentence in the prompt: a prompt instruction is followed until the one
time it isn't, and the failure mode is silent — two agents talking for an hour with nobody
watching. It is enforced one level down, in `agent_outbox.send_mail`, which EVERY outbound
message passes through: task replies and reviewer sign-offs are copied too, not only
agent-to-agent mail. Putting it at each call site would mean the next call site somebody adds
is quietly the one that is not copied.

WHY A PER-TASK SEND CAP. Hops bound the length of a CHAIN; they say nothing about width. One
agent in a loop can send its peer fifty messages inside a single task, each with hops=1 and
each perfectly valid. The cap is crude and it is the only thing standing between a confused
model and fifty tasks queued on the other agent.
"""

import os

import agent_outbox
import agent_principal
import fleet_identity

# Defined in agent_outbox, which puts it on EVERY email the fleet sends — task replies and
# reviewer sign-offs included, not only agent-to-agent. Referenced here for the messages this
# module writes about it; a second os.environ.get would be a second source of truth for one
# address, and the two would disagree the first time only one of them was changed.
BOSS_ADDRESS = agent_outbox.BOSS_ADDRESS

# Width limit, complementing the hop limit's depth. See the module docstring.
MAX_SENDS_PER_TASK = int(os.environ.get("AGENT_MAX_PEER_SENDS", "5"))

# What one agent may ask another for. Deliberately the same table agent_principal admits on:
# a verb this side can send and that side refuses is a silent dead letter.
PURPOSES = agent_principal.PURPOSES

# The inbound task this agent is currently working on. Held here, module-level, exactly as
# agent_budget holds the current task — the tool call that sends a message has no access to
# the envelope, and threading it through agent_brain's dispatch would put a security-relevant
# value on the path the model's arguments travel.
_task = {"hops": 0, "thread_id": "", "message_id": "", "task_id": "", "subject": "",
         "requester": "", "sent": 0, "from_agent": ""}


def log(msg):
    print(f"[peer] {msg}", flush=True)


def start_task(envelope, principal=None):
    """Called by the worker before the agent runs. Resets the per-task send budget."""
    _task.update(
        hops=envelope.hops, thread_id=envelope.thread_id, message_id=envelope.message_id,
        task_id=envelope.task_id, subject=envelope.subject, requester=envelope.reply_to
        or envelope.requester, sent=0,
        from_agent=principal.principal_id if principal and principal.kind == "agent" else "")


def peers():
    return list(fleet_identity.PEERS)


def _describe_peers():
    return ", ".join(peers()) if peers() else "(none configured)"


def send(to, purpose, subject, body):
    """Send one message to a peer agent. Returns a status line; never raises.

    Errors come back as text because that is how every other tool in this harness reports —
    the model reads the reason and adapts, where an exception would end the run.
    """
    to = (to or "").strip()
    purpose = (purpose or "").strip()
    if to not in peers():
        return (f"ERROR: '{to}' is not an agent in this fleet. Known agents: "
                f"{_describe_peers()}. You cannot send mail to a person with this tool — "
                f"answer the human in your reply instead.")
    if purpose not in PURPOSES:
        return f"ERROR: purpose must be one of {', '.join(PURPOSES)} (you sent '{purpose}')."
    if not (subject or "").strip() or not (body or "").strip():
        return "ERROR: both subject and body are required."
    # ANSWERING THE PEER WHO ASKED IS ALREADY HANDLED. Their message arrived as this task, and
    # the harness mails the finished reply straight back to them — signed, hop-stamped and
    # copied to the boss. Seen live on the first real exchange: agent2 answered with this tool
    # AND the harness sent its task reply, so agent1 got two emails and ran two tasks off one
    # question. Refused here rather than explained in the prompt, because the duplicate costs a
    # whole agent run at the far end.
    if _task["from_agent"] == fleet_identity.peer_id(to) and purpose in ("answer",
                                                                        "review-result"):
        return (f"ERROR: {to} is the agent who sent you THIS task, so your final answer "
                f"already goes back to them — writing it here as well would reach them twice "
                f"and start a second task. Just finish and give your answer normally. Use this "
                f"tool only to start something new, or to write to a different agent.")
    if _task["sent"] >= MAX_SENDS_PER_TASK:
        return (f"ERROR: you have already sent {_task['sent']} messages to other agents in "
                f"this task, which is the limit. If the work genuinely needs more, say so in "
                f"your reply and let a human decide.")

    to_id = fleet_identity.peer_id(to)
    mid = agent_outbox.new_message_id()
    try:
        # hops comes from the INBOUND envelope, not from anything the agent can influence.
        headers = agent_principal.outbound_headers(
            to_agent_id=to_id, message_id=mid, thread_id=_task["thread_id"] or mid,
            purpose=purpose, inbound_hops=_task["hops"])
    except ValueError as e:
        return (f"ERROR: refusing to send — {e}. This is a loop guard, not a bug: say what you "
                f"needed in your reply so a human can carry it across.")

    try:
        agent_outbox.send_mail(
            to=fleet_identity.address(to),
            subject=f"[{purpose}] {subject}",
            body=_wrap(body, to, purpose),
            from_name=fleet_identity.NAME, from_addr=fleet_identity.AGENT_ADDRESS,
            message_id=mid,
            # Threaded under the task that caused it, so the conversation reads in order and
            # the thread-depth guard can see the whole exchange.
            in_reply_to=_task["message_id"] or None,
            references=_task["thread_id"] or None,
            headers=headers)
    except Exception as e:
        return f"ERROR: could not send to {to}: {e}"

    _task["sent"] += 1
    log(f"{fleet_identity.AGENT_ID} -> {to_id} [{purpose}] {subject!r} "
        f"(hop {headers['X-Agent-Hops']}, cc {BOSS_ADDRESS})")
    return (f"Sent to {to} as '{purpose}', copied to {BOSS_ADDRESS}. This is asynchronous: "
            f"{to} picks it up on its next poll and answers in its own time, which will NOT be "
            f"within this task. Do not wait for a reply and do not claim to have one — finish "
            f"your work and say in your reply that you asked {to}.")


def _wrap(body, to, purpose):
    """The message as the recipient and the boss both read it."""
    return (
        f"{body}\n"
        f"\n"
        f"---\n"
        f"Sent by {fleet_identity.AGENT_ID} to {fleet_identity.peer_id(to)} while working on "
        f"{_task['task_id'] or 'a task'} ({_task['subject'] or 'no subject'}), originally "
        f"requested by {_task['requester'] or 'unknown'}.\n"
        f"{BOSS_ADDRESS} is copied on every message between agents.\n"
        f"Purpose: {purpose}. Reply to this message and it reaches {fleet_identity.NAME}."
    )


def reply_extras(envelope, principal):
    """Extra send_mail kwargs for a REPLY to a peer, so the return leg is attested too.

    Without this the exchange is only half-authenticated: agent1 signs its request, agent2
    answers with a plain email, and that answer arrives back at agent1 as unattested mail —
    which agent_principal now refuses outright, so the conversation would die silently on the
    return leg. Signing the reply is what makes a round trip possible at all.

    Returns the kwargs rather than a bare header dict because the signature covers the
    Message-ID, so the id has to be minted here and travel with the headers it was signed
    against. Splitting the two apart is how you sign one id and send another.
    """
    if not principal or principal.kind != "agent":
        return {}
    mid = agent_outbox.new_message_id()
    try:
        headers = agent_principal.outbound_headers(
            to_agent_id=principal.principal_id, message_id=mid,
            thread_id=envelope.thread_id or mid, purpose="answer",
            inbound_hops=envelope.hops)
    except ValueError as e:
        # At the hop limit the answer cannot be signed, and the peer would drop it anyway.
        # Send it unsigned: the peer refuses it, correctly, but the BOSS still gets the copy —
        # so a conversation that hits the limit ends visibly instead of in silence.
        log(f"cannot sign the reply to {principal.principal_id}: {e}")
        return {"cc": BOSS_ADDRESS}
    return {"cc": BOSS_ADDRESS, "headers": headers, "message_id": mid}
