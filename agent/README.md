# `agent/` — the code that runs inside the container

Everything here ships in the image and executes as the agent. Nothing here is run by hand on a
workstation, and nothing outside this folder is copied into the image except `tests/`.

| | |
|---|---|
| `agent_worker.py` | the loop: fetch a task, run it, review it, reply. The container's entrypoint |
| `agent_brain.py` | the model call and the tool loop |
| `agent_inbox.py` / `agent_outbox.py` | the mail transports, and the only mail-shaped code |
| `agent_envelope.py` / `agent_principal.py` | what a task is, and who is allowed to send one |
| `agent_peer.py` | agent-to-agent messaging, hop counting and attestation |
| `agent_validator.py` | the review gate |
| `agent_budget.py` | spend ceilings and the kill switch |
| `agent_memory.py` / `agent_notes.py` | the part that survives the container |
| `agent_delivery.py` / `ship_app.py` | how work becomes a repository, an image and a deployment |
| `fleet_*.py` | identity, and the clients for the fleet control plane |
| `git_auth.py` | one credential path for every git push |

## The container layout is FLAT, and deliberately so

The image copies `agent/*.py` **directly into `/app`**, not into `/app/agent`. These modules
import each other as top-level names (`import agent_brain`), which is what keeps them runnable,
testable and hot-patchable without a package on `sys.path`. Making this a Python package would
mean an `__init__.py` and rewriting every import in the fleet, or a `PYTHONPATH` that has to be
correct in the image, in compose, in the pod spec and in CI — four places to get right for no
gain. **This folder organises the source tree; it does not change what the agent runs.**

Two consequences worth knowing:

- `tests/` must work in **both** layouts — `../agent` here, `..` in the image — because the
  tests ship in the image as its only self-check. Each test puts both on `sys.path`.
- A path like `/app/agent_worker.py` inside a container is correct and is not a stale reference.

## What is NOT here

Operator tooling stays at the repo root, because it runs on a host and not in the container:
`provision_agent.py` (creates a mailbox, prints a k8s Secret), `scripts/task_agent.py`, the
`Dockerfile` and `docker-compose.yml`, and `mail/` (reasoning only — the mail config itself
lives in `infra-fleet/mail/` and auto-deploys from there).
