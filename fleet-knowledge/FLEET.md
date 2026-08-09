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

## Apps belong to the agent that built them

`ship_app` records which agent owns each repository and will refuse a push to one owned by
somebody else. If you are asked to change an app you did not build, that is a request for the
agent that owns it — say so in your reply rather than trying to route around it. Picking a
distinct app name is the fix when the collision is accidental.
