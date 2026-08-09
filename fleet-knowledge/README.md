# fleet-knowledge — global scope, operator-only

The source of truth for `FLEET.md`, the one memory file **no agent can write** (D5). It is
injected read-only into every agent's prompt, ahead of the shared and personal notes.

## Why it is in this repo rather than only in the bare repo

The live copy is a bare git repo on Zeenie at `memory-remotes/fleet-knowledge.git`, which is
outside this repository on purpose — it is machine state with its own history. But that bare
repo has no backup and no review: an operator edit is a `git push` with nobody looking at it,
and the whole point of global scope is that it is the tier where a wrong belief reaches every
agent in every tenant at once.

So the reviewable copy lives here, in version control, and gets deployed to the bare repo. If
the two disagree, this one is right.

## Deploying a change

There is no host git on Zeenie, and the agents' own clones of this repo deliberately have no
working push URL. So an edit goes through a container acting as the operator:

```sh
scp fleet-knowledge/FLEET.md zeenie:C:/Users/jianm/autonomous-agents/
ssh zeenie "docker cp C:\\Users\\jianm\\autonomous-agents\\FLEET.md agent1:/tmp/FLEET.md"
ssh zeenie "docker exec agent1 sh -c 'rm -rf /tmp/fk && git clone -q /remotes/fleet-knowledge.git /tmp/fk && cp /tmp/FLEET.md /tmp/fk/ && cd /tmp/fk && git -c user.name=operator -c user.email=you@example.com commit -qam \"operator: ...\" && git push -q origin main'"
```

Agents pick it up on their next task — `agent_memory.sync()` pulls before the notes are read.

## What belongs here

Only what is true for **every agent in every tenant**, and only what is worth the blast radius.
A fact that is true for one tenant belongs in that tenant's `AGENT.md`, which agents maintain
themselves. The test is: would this still be true on completely different hardware, for a team
that has never met the agent that learned it?

The first entry exists because a per-agent fact — the reachable preview port range — was
sitting in tenant-shared memory naming one agent's range as though it were everyone's. That is
risk 5 in the fleet design ("shared memory becomes a shared wrong belief"), and the correction
could not go in the shared file itself, because agents own that one and the harness never
writes it.
