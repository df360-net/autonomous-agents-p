# FLEET.md — true for every agent, maintained by the operator

You cannot edit this file. If something here is wrong, say so in your reply and it will be
fixed centrally. Everything below is deliberately short: this is the small set of facts that
must not be re-learned by each agent one mistake at a time.

## You are one of several agents

Other agents run on this machine, in this same tenant, with their own mailboxes and their own
workspaces. You do not share a filesystem with them.

- **`AGENT.md` and `AGENT-AVOID.md` are SHARED.** Every agent in the tenant reads and writes
  them. Something you write there will be read by an agent that was not there when you learned
  it, so write the symptom and the fix, not "as discussed above".
- **`AGENT-ASSETS.md` is YOURS ALONE.** It is your inventory of what you built and still run.
  You cannot see another agent's, and they cannot see yours.

## When shared notes and your task disagree, the task wins

The shared notes were written by whichever agent learned the lesson, on its own container. Some
of what is true there is true only *for that agent*. Anything the task itself tells you about
**your** machine — your workspace path, your free port, the range reachable from outside your
container — is generated fresh for you at the moment the task starts, and overrides any number
written in a shared note.

The port range is the known example: it is **different for each agent**, and `AGENT.md` names
one agent's range as though it were everyone's. Use the range in your task.

This is the general rule the system prompt already gives you, and it is worth repeating because
inherited notes read as authoritative in a way your own guesses do not: **if the notes and the
machine disagree, believe the machine, then correct the note.**

## Writing to another agent

`message_agent` sends mail to a peer. Use it to ask for a review of something you are unsure
of, to ask about an app they own, or to hand over work that is genuinely theirs — not to get
another agent to do your task for you.

**Every message between agents is copied to the human who runs this fleet, always.** You cannot
turn that off and there is no argument that removes it. Write to a peer the way you would write
with somebody reading over your shoulder, because one is.

It is email, not a function call. The other agent is a separate container polling its own
mailbox; it answers minutes or hours later, long after your task has ended. Send, then finish
your work and say in your reply that you asked them. Never wait for an answer and never invent
one.

There is a limit on how many messages one task may send, and on how far a chain of them can
travel. If you are refused, that is a loop guard doing its job — say what you needed in your
reply and let a human carry it across.

## Apps belong to the agent that built them

`ship_app` records which agent owns each repository and will refuse a push to one owned by
somebody else. If you are asked to change an app you did not build, that is a request for the
agent that owns it — say so in your reply rather than trying to route around it. Picking a
distinct app name is the fix when the collision is accidental.
