# autonomous-agents

A fleet of autonomous developer-agents, tasked by email. You email `agentN@agents.local` from a
webmail UI in your browser; the agent builds the thing in its own container, a second agent
reviews it independently, and you get back what it built, the files it wrote, and the commands
it actually ran — plus the address, once the fleet has deployed it.

Four agents run as Kubernetes pods across two boxes today.

Context, decisions and where we are: [agent-reminder.md](agent-reminder.md). The machinery and
why it is shaped that way: [docs/Autonomous-Agents-Design.md](docs/Autonomous-Agents-Design.md).
The plan and the decisions still open: [docs/Fleet-Design.md](docs/Fleet-Design.md).

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
python tests/test_principal.py   # who may task this agent: hops, thread depth, attestation
python tests/test_register.py    # thread context across a process boundary, and the image tag
python tests/test_faultinject.py # SPENDS REAL API CALLS - proves the gate rejects bad work
```

Every suite except `test_faultinject.py` runs in CI and **gates the image**
([.github/workflows/agent-runtime.yml](.github/workflows/agent-runtime.yml)). An agent image
that boots and misbehaves is worse than one that does not build: it deploys, reports healthy,
and spends money doing the wrong thing.

`test_faultinject.py` is excluded deliberately, and it is also the most important one. It hands
the real reviewer a known-bad reply (the one that claimed "after exactly 11 months $10,000
reaches $20,000") and fails if it waves it through — everything else can pass while the gate is
a rubber stamp, and only this catches that. It stays out of CI because it calls the real
DeepSeek API, costs money per run, and has returned opposite verdicts on identical input. A
gate that fails randomly gets ignored, and an ignored gate is worse than an absent one.

## Known trade-offs (deliberate)

- **Self-signed TLS.** Submission on `:587` and IMAP on `:143` both require STARTTLS, and
  `SPOOF_PROTECTION=1` is on, but the certificate is self-signed — so agents run with
  `TLS_VERIFY=false`. Do not turn that on without first giving the server a certificate they
  can chain to: every agent stops receiving mail at once and the symptom reads like a wrong
  password. Reasoning in [mail/README.md](mail/README.md).
- **At-most-once tasking.** A message is flagged `\Seen` *before* the build starts, and its
  `Message-ID` is recorded in `/workspace/.processed.json`. If the worker dies mid-build the
  task is dropped, not retried — better than an agent that rebuilds and re-emails on a loop.
  Lease-based claiming is designed and not built (D3 in the fleet design).
- **Long-running servers still block.** `run_bash` is synchronous with a 300s kill. The
  system prompt tells the agent to background servers; that is a prompt, not an enforcement.
- **A preview is not reachable from outside the pod.** Nothing publishes an agent's ports. The
  agent is told never to offer a local port as an address, and the fleet emails the real one
  once the app is actually serving.
- **Nothing measures the reviewers yet.** The gate's verdict now reaches the control plane and
  withholds the announcement on a fail or on silence, but the canary rate and reviewer
  agreement are not recorded, and they cannot be reconstructed after the fact.
