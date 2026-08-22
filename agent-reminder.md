# autonomous-agents — context handoff for "future you"

> **Read this first, fully, before doing anything in this project.** It is the single source of
> truth for what we are building, the decisions already locked, the runtime as it actually is,
> how the user works, and where we are.
>
> **Rewritten 2026-08-22**, replacing a 2026-08-01 handoff that described a Docker-Compose stack
> on one Windows box. That stack is gone and so are its files. Nothing below is history unless a
> heading says so.
>
> Companion documents: [docs/Fleet-Design.md](docs/Fleet-Design.md) (the plan and the one-way
> doors), [docs/Autonomous-Agents-Design.md](docs/Autonomous-Agents-Design.md) (how the machinery
> works and why), [agent/README.md](agent/README.md) (what runs in the container),
> [mail/README.md](mail/README.md) (why the mail server is configured as it is).

---

## 1. What this project is (the vision — unchanged since day one)

A **fleet of autonomous developer-agents**. Each agent lives in its **own container**, has **full
access inside that container**, and is **tasked the way you would task a human engineer** — by
**email**. It reads the instruction, **builds the software**, has it independently reviewed by a
second agent, ships it, and **replies to the email** with the result plus the evidence.

It is the endpoint of the sibling workbench at `../LLM_API_call`, which produced `agent.py` — a
~200-line DeepSeek coding agent. This project scales that agent into a managed, message-driven,
governed fleet.

**The user's thesis, and this project is its production form:** a *cheap* DeepSeek agent with
*full auto-approve* inside a *scoped sandbox* beats a premium agent that needs babysitting. The
sandbox is a whole container, so the blast radius is the container — which is *safer* than a
folder on a laptop, not riskier.

---

## 2. The key reframe (the brain was never the hard part)

```
agent.py NOW:   task = sys.argv[1]         -> agent_loop() -> print(final answer)
this project:   task = next unread email   -> agent_loop() -> reply email
```

The loop, the tools and the model do not change. Everything this project has built is the
plumbing around the call: identity, attestation, budgets, memory, delivery, review, governance.
**The intelligence was never the work. The program around the LLM call is.**

The governing rule that follows from it, and the one to apply when anything is ambiguous:

> **Every safety property lives in Python, never in the prompt.** An instruction is something
> the model can talk itself out of. A harness check is not.

---

## 3. Decisions locked (do not relitigate without the user)

- **Email is the human interface, permanently.** It stopped being the machine transport: agents
  talk to each other over signed envelopes, and to the control plane over authenticated HTTP.
- **DeepSeek direct** — `https://api.deepseek.com/chat/completions`, `Authorization: Bearer
  $DEEPSEEK_API_KEY`. No proxy. **NEVER print or expose the key.** The user provisions it.
- **The agent self-verifies AND a second agent reviews, before anything is sent.** The user's
  moat across every app he has built is *reading the diff*: green build does not mean working.
  The fleet is designed to preserve a human approval beat, not to remove it.
- **Kubernetes, not Compose.** Agents are pods built from a published image. The Compose stack
  that ran everything on one box has been retired and deleted (recover from git history if ever
  needed).
- **Nothing in this repo deploys the fleet.** Pushing to `main` builds an image; the infra/ops
  side rolls it. That separation is deliberate — see §5.
- **Agents are cattle; their memory is the pet.** Nothing durable lives in a container.

---

## 4. How the fleet is shaped now

```
   you ──▶ Roundcube on hp-tiger, or scripts/task_agent.py
             │  mail to agentN@agents.local
             ▼
   ┌──────────────────────────────────────────────┐
   │ docker-mailserver on hp-tiger                │  submission :587, IMAP :143
   │   boss@ agent1..4@ validator1..4@ (aliases)  │  STARTTLS, SPOOF_PROTECTION=1
   └───────────────┬──────────────────────────────┘
      IMAP poll    │ ▲ SMTP (one login, two From addresses)
   ┌───────────────▼─┴────────────────────────────┐
   │ agentN pod   ns `fleet`, on either box       │
   │   admit() -> run(envelope) -> review gate    │
   │   /memory/tenant  /memory/fleet   (git)      │
   │   /workspace/task-NNNN-<slug>/    (scratch)  │
   └───────────────┬──────────────────────────────┘
      ship_app     │ push                         │ HTTP, token-scoped
   ┌───────────────▼───────────┐   ┌──────────────▼───────────────────┐
   │ github.com/df360-net/     │   │ fleet control plane  :8091       │
   │   agent-<app>             │   │  kill switch + thread cap        │
   │   Actions -> ghcr.io      │   │  spend ledger and ceiling        │
   └───────────────────────────┘   │  app registration -> box, port,  │
                                   │    URL; renders the Deployment   │
                                   │  review verdicts (gate the       │
                                   │    "it is live" email)           │
                                   └──────────────────────────────────┘
```

**The task lifecycle, end to end:** mail arrives → `agent_inbox` builds a `TaskEnvelope` →
`agent_principal.admit()` attests it and strips anything the sender could have forged → memory
syncs → the worker builds in a scratch workspace → `ship_app push` sends it to GitHub, CI builds
the image, and `register()` tells the control plane → the review gate rules → the verdict is
reported to the plane → the reply and the reviewer's sign-off are sent → the plane emails the
live address into the same thread once the pod is actually serving.

---

## 5. Environment facts (verified — trust these)

### The boxes

| box | address | OS | role |
|---|---|---|---|
| zeenie | 192.168.0.105, `ssh zeenie` | Win 11, Docker Desktop | kind cluster `learn`; agent pods |
| hp-tiger | 192.168.0.102/.101, `ssh jay@hp-tiger` | Ubuntu | mail server, fleet control plane, agent pods |
| murphy | 192.168.0.104, `ssh murphy` | Win 11 | spare runtime box, unused |
| elitebook | 192.168.0.100 | Ubuntu 26.04, user `jay` | unrelated |

All addresses are **hand-assigned static** (the NETGEAR C6300BD could not hold that many DHCP
reservations, so the pool was cut to .2–.99 and every box fixed at .100+). They do not drift.

- **`ssh zeenie` lands in cmd.exe, `ssh jay@hp-tiger` lands in a Linux shell.** The same remote
  command does not work on both. On zeenie chain with `&`; for anything richer use
  `ssh zeenie 'powershell -NoProfile -Command "..."'`.
- **Do not read a timeout as "the box is down"** — Windows blocks ICMP, so ping failing is normal.
- **SSH survives the Wi-Fi profile flip on both boxes now.** The cause was never the profile: the
  OpenSSH inbound rule was scoped to Private, so an update flipping Wi-Fi to Public dropped
  port 22. Fixed at the rule (`Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Profile Any`),
  because a rule cannot be flipped back by an update.
- **Docker Desktop on zeenie does not auto-start, and has been seen to stop on its own** with the
  machine up throughout. The hazard is that it is **silent**: the agents are not down, they are
  absent — mail queues in Dovecot and nothing alerts. Any watchdog must run in the interactive
  session (Docker Desktop needs the desktop credential context) and must alert on the **engine
  pipe**, never on agent liveness. Not built.
- **`docker pull`/`build` over SSH always fails on zeenie** with `error getting credentials — A
  specified logon session does not exist`, even for public images: the CLI resolves registry
  credentials through `docker-credential-desktop.exe`, which needs the Windows credential vault,
  and SSH is a network logon with none. Emptying `auths`, deleting `credsStore`, a clean
  `--config` and disabling CLI hooks were all tried and all failed. Use a `schtasks /IT`
  scheduled task in the interactive session. Non-registry commands over SSH are fine.
- **`docker logs --since` on zeenie takes LOCAL time**, so a UTC timestamp silently returns an
  empty log. Use `--since 60m`.

### Mail

docker-mailserver on hp-tiger, domain `agents.local`. Mailboxes `boss@`, `agent1..4@`;
`validatorN@` is a **send-as alias for `agentN@`, not an account** — one login, two From
addresses, which is what makes a reviewer's sign-off visibly not the worker grading its own
homework. `SPOOF_PROTECTION=1`, submission on `:587` and IMAP on `:143`, both **STARTTLS with
self-signed certificates** (so agents run with `TLS_VERIFY=false`).

**The mail configuration itself is not in this repo.** It lives in `infra-fleet/mail/` and
auto-deploys on push. [mail/README.md](mail/README.md) keeps the *reasoning* — every setting
there is one a reasonable person would "fix" back.

The human sends from Roundcube on hp-tiger. [scripts/task_agent.py](scripts/task_agent.py) is
the scriptable alternative: it does `EHLO -> STARTTLS -> login` on both legs and can watch for
the reply by `In-Reply-To`.

### The fleet control plane

`http://hp-tiger:8091`, built and run by the **infra/ops agent** (a separate Claude session — see
§6). Agents reach it with a scoped `FLEET_TOKEN`; the actor is derived server-side from the token,
so nothing in a payload can spoof it.

| endpoint | what it decides |
|---|---|
| `GET /fleet/pause` | the kill switch, and `inter_agent_thread_cap` |
| `POST /fleet/spend` | the authoritative fleet total against the ceiling |
| `POST /agent/apps` | registers an app; **the plane assigns the box, the NodePort and the URL** |
| `POST /agent/apps/<app>/review` | the review verdict, which gates the "it is live" email |
| `GET /apps/<app>` | status and the resolved address |

Two clients, and the difference matters: [agent/fleet_control.py](agent/fleet_control.py) is the
money controls (fails **closed** — unreachable means paused), and
[agent/fleet_register.py](agent/fleet_register.py) is app registration and verdicts.

### The image, and how a change reaches the fleet

Push to `main` → `.github/workflows/agent-runtime.yml` runs the offline suites → builds
`ghcr.io/df360-net/agent-runtime` and pushes **three tags**: the full sha, the 7-char sha, and
`main`. The fleet declares a **sha**, never `main` — a moving tag turns "restart the pod" into
"silently upgrade every agent".

`paths-ignore: ["**/*.md", "docs/**"]` — a documentation commit correctly does **not** build.

**The loop is: commit → CI builds and tags → relay the tag to the infra side → they roll it.**
A green build is not a deployed fix: check what the pods are actually running, then prove the
behaviour inside a running pod.

### Memory

Two git clones under `/memory`, synced at boot **and at the top of every task, before the notes
are pasted into the prompt** ([agent_worker.py:519](agent/agent_worker.py#L519) then
[:550](agent/agent_worker.py#L550) — the ordering is what makes a central correction land on the
same task that pulls it).

| path | remote | writer |
|---|---|---|
| `/memory/fleet` | `df360-net/fleet-knowledge` (`MEMORY_FLEET_REMOTE`) | **operator only** — no agent can write it |
| `/memory/tenant` | the tenant repo (`MEMORY_TENANT_REMOTE`) | any agent in the tenant |

`/workspace` is scratch and may be destroyed. **Editing `FLEET.md` in the GitHub repo is the
supported way to correct a wrong belief across all four agents** — but delivery is best-effort
per task and its failure is silent (an unreachable remote logs a WARNING and the agent runs on
the stale clone), so confirm the `memory: /memory/fleet <- ... ok` line rather than treating the
push as delivery.

### DeepSeek

Model `deepseek-chat` (cheap, deprecated alias, still works; current models are
`deepseek-v4-flash` / `deepseek-v4-pro`). Keep it env-configurable. Measured cost: a trivial
question ~$0.01; a real 130-step build that shipped an app ~$0.50, at a 112:1 prompt:completion
ratio and 99% cache hit. **Ceilings are $20/task, $150/day, $500/fleet**, set high deliberately.

---

## 6. How the user (Jianmin) works — match this

- Experienced **TS/React dev learning AI-agent internals**. Explains best through analogies to
  his world. Likes **predict-then-observe** experiments.
- **"Verify, don't trust" is his discipline and his edge.** Green build is not working. Exercise
  the real flow and report failures plainly.
- Prefers a **concrete MVP over boiling the ocean**; likes to watch it run and iterate.
- Cost-conscious — DeepSeek being cheap is load-bearing to the thesis.
- **He relays every outbound message himself.** Agent task emails *and* correspondence with the
  infra/ops agent: give him pasteable text, never send on his behalf.
- **The infra/ops agent is a separate Claude session** that owns the mail server, the control
  plane, the clusters and the rolls. Correspondence is numbered (`r30`, `r31`, ...) and travels
  by hand through the user. It is a peer, not a subordinate: it has caught real bugs here, and
  this side has caught real bugs there. Check its claims; expect it to check yours.

---

## 7. Where we are right now

**Four agents — `dev/agent1` … `dev/agent4` — running as pods across two boxes**, on
`agent-runtime:cc26358`. Phases 1 and 2 of [docs/Fleet-Design.md](docs/Fleet-Design.md) are done
apart from the assurance half of cross-review.

| what | where | state |
|---|---|---|
| identity derived from TENANT + AGENT_NAME | `agent/fleet_identity.py` | D1 done |
| a task is an envelope; mail is one transport | `agent/agent_{envelope,inbox,outbox}.py` | D3 done |
| who is asking, resolved before any handler runs | `agent/agent_principal.py` | D6 done |
| agent-to-agent messaging, attested and bounded | `agent/agent_peer.py` | D6 done |
| spend ledger, ceilings, kill switch | `agent/agent_budget.py` + `fleet_control.py` | D7 done |
| memory as git repos outside the container | `agent/agent_memory.py` | D5 done |
| apps addressed by the control plane, not arithmetic | `agent/agent_delivery.py`, `ship_app.py` | D4 done in effect |
| the review verdict gates the announcement | `agent/fleet_register.py` | done |

**Proven in production:** an agent built an app from an email, CI built it, the plane deployed it
and emailed the live address into the agent's own thread. The verdict gate has withheld an
announcement. Spoof protection has been exercised by a real agent sending as its own validator.
The cattle test passed: a container and its 821MB workspace were destroyed and the agent came
back with its memory byte-identical.

**Open, in rough order:**

1. **The assurance half of cross-review** — the canary rate, reviewer agreement and a
   per-reviewer record. `tests/test_faultinject.py` already holds a known-bad artifact; it needs
   promoting from a test asset to a periodic probe, with somewhere to write the results.
2. **A second tenant**, purely to prove the boundary is enforced by mechanism and not by
   convention (Fleet-Design phase 3).
3. **A Docker Desktop watchdog on zeenie** — see §5; infra has offered to help stand it up.
4. **Node 22 → 26** before Node 22 goes EOL on 2027-04-30. Parked deliberately; prefer 26 over 24.

---

## 8. The review gate, and the verdict that follows it

[agent/agent_validator.py](agent/agent_validator.py) sits between the worker and the outbox.
**Nothing is emailed until a reviewer signs it off**, or until `VALIDATION_ROUNDS` (3) are spent —
at which point it sends anyway with the objections banner-ed on top. The user's call: a task that
silently vanishes is worse than one that arrives flagged, and it keeps the human as the last word.

- The reviewer **is `agent_loop` with a different system prompt** — no new machinery.
- **Fresh context**, so it cannot inherit the worker's rationalisations.
- **Its own tools, same workspace** — it re-runs the tests and recomputes the numbers. A reviewer
  that can only read the summary is a rubber stamp.
- On FAIL the objections go back into the worker's **existing** conversation, so it fixes rather
  than restarts.
- `validatorN@` emails its own sign-off, threaded under the reply it approved.
- **A gate fails in both directions**: an unparseable verdict is a FAIL, a crashed reviewer is a
  FAIL, and it is told not to block on style or on work nobody asked for.
- **The verdict is unanimous or it is a contradiction.** A reply containing more than one
  `VERDICT:` line must agree with itself; notes are cut at the last one.

**Then the verdict leaves the pod.** `report_review()` posts pass/fail to the control plane after
the gate rules, because until then there is no verdict to send. With
`ANNOUNCE_REQUIRES_REVIEW` on, a fail — **or silence** — withholds the "it is live" email.

> **No verdict is not a pass.** If the gate did not run, or the plane could not be reached, the
> announcement is held. Silence means nobody approved it.

**What it catches, measured by fault injection:** fabricated verification, reliably — given a real
bad reply claiming *"I verified that after exactly 11 months $10,000 reaches $20,000"* it
recomputed, noted that no command in the record performs that check, and rejected it. A one-cent
prose/table mismatch. Unreachable or wrong-content URLs.

**What it does not catch:** a convention you never stated (it endorsed "9 years 11 months" while
printing $19,980.06 at month 119 in the same paragraph — genuinely ambiguous, and prompting harder
did not move it), and **what the app actually does**. It proves reachability and structure; it has
never played a game. Only the human has exercised behaviour, and that is where the remaining
defects were found.

**A severity ladder was measured, 8 runs per rung:** a leaked-secret defect was caught 8/8; a
correct answer was passed 8/8 with no false FAILs. A hedge-detecting tripwire built here was
**unwired after it fired on 3 of 8 correct passes** — a 37% tax on good work, and the same flaw
this side had criticised in someone else's version.

---

## 9. The agent's own memory (Jianmin's idea, 2026-08-02)

Three files the agent writes for itself, split by **when you reach for them**: `AGENT.md` (how
this machine works — read while planning), `AGENT-ASSETS.md` (what exists, where, on what port,
how to start it — read while orienting), `AGENT-AVOID.md` (what burned it — scanned before
acting). A fourth, `FLEET.md`, is **operator-only**: no agent can write it.

**What prompted it (task-0013).** A bug report naming neither the folder nor the cause. The agent
found the app unaided, read the source, and diagnosed a timezone convention correctly — then
**copied the app into its new task folder and fixed the copy**, wrote `PORT=3003` into `.env`
while the server kept announcing 3002, and finally killed both copies because it could not tell
its fork from the original. **Discovery was never the problem. Identity was.**

Four rules, each of which is a rule learned somewhere else:

- **The agent owns the files completely.** The harness never writes, seeds or parses them and
  imposes no format. Depend on their structure and they stop being notes and become a schema.
- **Injected, not requested.** All of them are pasted into every task (capped per file). "Read
  AGENT.md first" is an instruction the model can skip, and this is exactly what it does not know
  it is missing.
- **Ports come from the OS, not the notes.** What a port is *for* is in the file; whether it is
  *busy* is a fact about the machine.
- **The reviewer enforces upkeep** — it reads `AGENT-ASSETS.md` from disk (the copy in the task
  is the *before* state) and a stale entry is a FAIL. Formatting explicitly is not.

Scope is assigned by the harness, not the agent, because "is this lesson mine or the fleet's?" is
a question about blast radius. `AGENT.md`/`AGENT-AVOID.md` are tenant; `AGENT-ASSETS.md` is
personal, because shared it makes "fix the booking app" ambiguous between two agents who both
believe they own it.

**Not solved:** nothing reconciles the notes with reality except the agent itself.

---

## 10. Delivery: how work becomes something you can open

Jianmin's framing, 2026-08-02: *"any applications need to be checked into GitHub, the GitHub CI
auto kicks off... Let the agent understand we have Kubernetes pods. Each app should deploy to a
pod."* One repo per app; one deployment per app.

```
agent builds and tests locally on a preview port    <- DEV. dies with the pod.
agent writes Dockerfile + k8s manifest + CI file
ship_app push      -> github.com/df360-net/agent-<app>
GitHub Actions     -> ghcr.io/df360-net/agent-<app>:<sha7>
ship_app register  -> the control plane assigns the box, the NodePort and the URL
per-box daemon     -> renders Deployment + Service, runs the pod
the plane          -> emails the live address into the agent's own thread
```

**Two pipelines have been retired underneath this**, and it is worth knowing so you do not go
looking for them: the original `Kafka -> ci-watcher -> approval -> Harness` chain (switched off),
and — the more interesting one — **the agent computing its own address**. `http://{APP_HOST}:
{PROXY_PORT_BASE + slot}` was arithmetic over two environment variables, so it produced a
confident URL whether or not anything was listening. It did exactly that, in real emails. Both
variables are gone from `agent_delivery` and cannot come back: the plane allocates and reports
the address, so there is no expression left capable of inventing one.

**The story is told in four places and they must agree** — `delivery_note`, the system prompt,
`ship_app scaffold` and `ship_app status`. An agent handed two accounts of one pipeline splits
the difference and invents a third, and `scaffold` is read first.

**Traps, all handled in code — do not rediscover:**

- **One port, read from one place.** The generated app reads `PORT` with a default, the manifest's
  `containerPort` is what the plane reads, and nothing else declares a port. The symptom of
  getting this wrong is `connection refused`, which reads like a crashed server.
- **The image tag has one definition**, `agent_delivery.image_tag`, and CI's workflow template is
  generated from the same constant. `git rev-parse --short` is **not** a safe substitute:
  `core.abbrev=auto` grows with object count. `tests/test_register.py` pins the two together.
- **`workflow` scope is not optional** on the PAT, or GitHub rejects any push touching
  `.github/workflows/` with a message that reads like a permissions bug.
- `.fleet-registered` and friends must stay out of the deliverable — an internal marker was one
  skip-list entry away from being emailed to the requester.

---

## 11. Principles earned the hard way

1. **Telling a model to apply a rule is not the same as it applying the rule.** In one deploy the
   Markdown ban worked immediately and the preamble ban failed three times. Instructions shift
   odds. Anything that must *always* hold belongs in the harness.
2. **When you must enforce mechanically, let the model mark the boundary.** `---EMAIL---` works
   because the harness never guesses where the email starts. **Fail-open**: no marker means send
   everything. A leaked preamble is a wart; a truncated reply is a defect.
3. **Giving it a sanctioned place to be verbose beat forbidding the verbosity.**
4. **Evidence must come from the harness, not the narrator.** Anything the model says about what
   it did is generated text with the same failure modes as the answer.
5. **Attribution matters as much as the record.** An unlabelled merged transcript reads as
   authoritative and is wrong. Stamp `by` at execution time.
6. **A gate fails in both directions.** Rubber-stamping and pedantry are both failures.
7. **Never silently drop a task.** Arrive flagged instead.
8. **Ask the machine, not the model's notes.** Where a fact is observable, read it from the OS.
9. **Give the agent a command, not a procedure.** Anything exact and unforgiving is harness work.
10. **Do not let the agent's own claim be the release.**
11. **Unreadable is not "off".** A missing field, a malformed value or an unreachable plane must
    never resolve to "no policy". Only the control plane may say zero.
12. **No verdict is not a pass.**
13. **Refuse rather than run-but-told-to-stay-quiet.** A prompt instruction is one the model can
    talk itself out of; a refusal in Python is not.
14. **Deferring a feature is cheap. Deferring the seam that makes the feature possible is how a
    rewrite gets scheduled.**
15. **A green build is not a deployed fix.** Check the pod image, then prove the behaviour inside
    a running pod.

---

## 12. Repository layout

| | |
|---|---|
| [agent/](agent/) | everything that runs inside the container. The image copies it **flat** into `/app` — see [agent/README.md](agent/README.md) for why, and why `/app/agent_worker.py` is not a stale path |
| [tests/](tests/) | offline suites, which also ship in the image as its only self-check. Each puts both layouts on `sys.path` |
| [provision_agent.py](provision_agent.py) | creates a mailbox and prints a k8s Secret. The password is never written down |
| [scripts/task_agent.py](scripts/task_agent.py) | task an agent from a terminal instead of Roundcube |
| [mail/](mail/) | reasoning only; the config lives in `infra-fleet/mail/` under CD |
| [fleet-knowledge/](fleet-knowledge/) | reasoning only; `FLEET.md` itself lives in `df360-net/fleet-knowledge` |
| [Dockerfile](Dockerfile) | python 3.12 + node 22 + tsc + git |
