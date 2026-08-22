# fleet-knowledge — global scope, operator-only

`FLEET.md` is the one memory file **no agent can write**. It is injected read-only into every
agent's prompt, ahead of the shared and personal notes, and it is the tier where a wrong belief
reaches every agent in every tenant at once.

## Where it lives

**https://github.com/df360-net/fleet-knowledge** — that repository is the source of truth, and
this folder no longer holds a copy.

It used to. The copy existed because the live version was a bare git repo on a Windows box with
no backup and no review, so a reviewable copy in version control was worth the duplication. That
reasoning is spent: the file now lives in a GitHub repository, which is version-controlled,
backed up and reviewable on its own. A second copy here would only be a second answer to "what
does FLEET.md say" — and this one would lose, because the agents read the other one.

That is not hypothetical. This folder's copy was stale within minutes of the GitHub repo being
created, and it had been stale in the other direction for weeks before that: the remote was
empty while `MEMORY_FLEET_REMOTE` pointed at it, so no agent received this layer at all until
someone went looking for a file to delete.

## Changing it

A commit and a push to that repository. Agents re-clone their memory at boot, so a change lands
on each agent the next time it restarts — and reaches a running agent's next task through
`agent_memory.sync()`, which pulls before the notes are read.

The old flow — `scp` to Zeenie, then `docker exec` into an agent to push into a bare repo,
because the host had no git — described a machine that has since been wiped.

## What belongs in it

Only what is true for **every agent in every tenant**, and only what is worth the blast radius.
A fact that is true for one tenant belongs in that tenant's `AGENT.md`, which agents maintain
themselves. The test: would this still be true on completely different hardware, for a team that
has never met the agent that learned it?

The first entry exists because a per-agent fact — the reachable preview port range — was sitting
in tenant-shared memory naming one agent's range as though it were everyone's. That is risk 5 in
the fleet design, "shared memory becomes a shared wrong belief", and the correction could not go
in the shared file itself, because agents own that one and the harness never writes it.

**A wrong sentence here cannot be corrected by the agent that reads it** — the file says so
itself, and tells the agent to report it instead. Two such sentences were found and fixed in
2026-08: agents described as sharing a machine, which stopped being true when the fleet spanned
two boxes, and a preview port range described as reachable from outside the container, which
nothing publishes. The second is the shape to watch for: not merely out of date, but something
an agent will act on and hand to a human as an address.
