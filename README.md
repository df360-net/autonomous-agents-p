# autonomous-agents — Phase 0

One autonomous developer-agent, tasked by email. You email `agent1@agents.local` from a
webmail UI in your browser; it builds the thing in its own container and replies with what
it built, the files it wrote, and the commands it actually ran.

Context, decisions and roadmap: [agent-reminder.md](agent-reminder.md).

## The pieces

| file | what it is |
|---|---|
| [agent/agent_brain.py](agent/agent_brain.py) | The loop, talking straight to DeepSeek, every tool auto-approved (the container is the sandbox). Returns a structured result instead of printing. |
| [agent/agent_validator.py](agent/agent_validator.py) | The review gate. Same loop, reviewer's prompt, fresh context, its own shell. Nothing is sent until it signs off (or the rounds are spent). |
| [agent/agent_notes.py](agent/agent_notes.py) | The agent's memory between tasks: pastes its self-written notes into every task, and finds a preview port nothing is listening on. |
| [agent/agent_delivery.py](agent/agent_delivery.py) | Delivery conventions: `agent-<app>` repos, `ghcr.io/<owner>/agent-<app>` images, and the CI + manifest templates. |
| [agent/ship_app.py](agent/ship_app.py) | On PATH in the container as `ship_app`. `scaffold` / `push` / `status` / `logs` / `list` — the agent's only route to GitHub, and where an app is registered with the fleet. |
| [agent/agent_principal.py](agent/agent_principal.py) | Who is allowed to task this agent, hop budgets, and the attestation that makes agent-to-agent mail trustworthy. |
| [agent/fleet_control.py](agent/fleet_control.py) | Client for the fleet control plane: the kill switch, the spend ceiling and the inter-agent thread cap. Fails closed. |
| [agent/agent_worker.py](agent/agent_worker.py) | The I/O adapter: poll IMAP → run the task in its own workspace → review → reply over SMTP. The container's entrypoint. |
| [agent/](agent/) | Everything else that runs in the container, and why the image layout is flat. |
| [Dockerfile](Dockerfile) | python 3.12 + node 22 + tsc + git — enough for the agent to actually build software. |
| [provision_agent.py](provision_agent.py) | Creates an agent's mailbox on the mail host and prints a k8s Secret, so the password never lands in a file. |

## How it is deployed

**Nothing here deploys the fleet, and that is deliberate.** Pushing to `main` builds
`ghcr.io/df360-net/agent-runtime:<short-sha>` in CI, and the infra/ops side rolls the agents
onto a tag. The agents run as Kubernetes pods on two boxes, take their tasks from the mail
server on hp-tiger, and get their ports, deploys, spend ceiling and kill switch from the fleet
control plane.

So the loop is: commit → CI builds and tags → tell the infra side the tag → they roll it.
A green build is not a deployed fix; check what the pods are actually running.

The Docker-Compose stack that used to run all of this on one Windows box has been retired and
its files removed (`docker-compose.yml`, `provision-agent.ps1`, `scripts/deploy-zeenie.cmd`,
`config/`). Recover them from git history if you ever need the old shape.
## Run it without any of that

The brain is standalone — useful for a quick check that DeepSeek and the tool loop are fine:

```powershell
$env:WORKSPACE="C:\tmp\ws"; python agent/agent_brain.py "write add.py with a test"
```

`python agent/agent_worker.py --once` does exactly one poll cycle and exits (inside the container it is `python agent_worker.py`, since the image copies the modules flat into `/app`).

## Tests

No mail server or container needed — the brain and the mail transport are stubbed:

```powershell
python tests/test_gate.py        # review gate: pass, fail-then-fix, never-passes, two senders
python tests/test_mailflow.py    # MIME decode, threading headers, self-mail loop guard
python tests/test_faultinject.py # SPENDS REAL API CALLS - proves the gate rejects bad work
```

`test_faultinject.py` is the important one. It hands the real reviewer a known-bad reply (the
one that claimed "after exactly 11 months $10,000 reaches $20,000") and fails if it waves it
through. Everything else can pass while the gate is a rubber stamp; only this catches that.

## Known trade-offs (deliberate, MVP-only)

- **No TLS.** `SSL_TYPE=` is empty and [config/dms/user-patches.sh](config/dms/user-patches.sh)
  re-enables plaintext IMAP auth. Fine on a LAN bridge with no internet route; delete that
  patch the day this stack gets a certificate.
- **Unauthenticated SMTP on :25**, allowed by `PERMIT_DOCKER=connected-networks`, because
  Postfix won't offer AUTH without TLS.
- **At-most-once tasking.** A message is flagged `\Seen` *before* the build starts, and its
  `Message-ID` is recorded in `/workspace/.processed.json`. If the worker dies mid-build the
  task is dropped, not retried — better than an agent that rebuilds and re-emails on a loop.
- **Long-running servers still block.** `run_bash` is synchronous with a 300s kill. The
  system prompt tells the agent to background servers and kill them; that is a prompt, not
  an enforcement.
