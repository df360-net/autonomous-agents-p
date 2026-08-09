# Autonomous Agents — Design

A fleet of autonomous developer-agents, each in its own container, **tasked the way you'd task a
human engineer: by email**. An agent reads the instruction, does the work, has it independently
reviewed by a second agent, and replies with the result plus the evidence.

Phase 0 is built and proven: one worker, one reviewer, email in and email out, on Zeenie. The
agent keeps its own memory between tasks (§8), and its work now ships down a real CI/CD pipeline
into Kubernetes, behind a human approval gate (§9).

*Status: 2026-08-02. Companion documents: [agent-reminder.md](../agent-reminder.md) (context
handoff, decisions, gotchas), [README.md](../README.md) (deploy runbook), and
`../React_Typescript/github_ci_cd/docs/Calculator_EPLX_SDLC_Journey.md` — the CI/CD estate §9
plugs into.*

---

## 1. The idea this rests on

The model never does anything. DeepSeek cannot read a file, run a command, or send an email — it
can only emit JSON *asking* for one. Python does every real action and feeds the outcome back.

The model is a pure function: conversation in, a description of what it wants to happen out. The
harness is the reducer and the effect handler — it performs the action, appends the result to the
conversation, and calls the model again. **Every safety property lives in the harness, never in
the model.** That's why "give it full access" is a five-line decision rather than a negotiation.

It follows that the intelligence was never the hard part. The sibling workbench at
`../LLM_API_call` already produced `agent.py`, a ~200-line agent that builds real applications.
This project changes only its I/O:

```
agent.py  NOW:   task = sys.argv[1]        -> agent_loop() -> print(answer)
this project:    task = next unread email  -> agent_loop() -> reply email
```

The loop, the tools, the model — unchanged. The leverage is in the plumbing around the call.

---

## 2. Architecture

```
                    ┌───────────────────────────────────────────────┐
   you ──browser──▶ │ Roundcube  :8080                              │
                    └───────────────────┬───────────────────────────┘
                                        │ SMTP/IMAP
                    ┌───────────────────▼───────────────────────────┐
                    │ docker-mailserver                             │
                    │   boss@agents.local                           │
                    │   agent1@agents.local                         │
                    │   validator1@agents.local                     │
                    └───────────────────┬───────────────────────────┘
                        IMAP poll (20s) │ ▲ SMTP (two senders)
                    ┌───────────────────▼─┴─────────────────────────┐
                    │ agent-worker container  (ports 3000-3009)     │
                    │                                               │
                    │   drain_inbox ──▶ agent_loop(worker prompt)   │
                    │                        │                      │
                    │                        ▼                      │
                    │                   review gate  ◀── agent_loop │
                    │                        │        (reviewer     │
                    │                        │         prompt,      │
                    │                        ▼         fresh ctx)   │
                    │                   send_reply ×2               │
                    │                                               │
                    │   /workspace/AGENT.md AGENT-ASSETS.md         │
                    │                  AGENT-AVOID.md      (§8)     │
                    │   /workspace/task-NNNN-<slug>/  + built apps  │
                    └───────────────────┬───────────────────────────┘
                                        │ ship_app push                (§9)
                    ┌───────────────────▼───────────────────────────┐
                    │ github.com/df360-net/agent-<app>              │
                    │   Actions: test -> build -> ghcr.io           │
                    └───────────────────┬───────────────────────────┘
                                        │ ci-watcher polls the API
                    ┌───────────────────▼───────────────────────────┐
                    │ kind cluster on Zeenie                        │
                    │   ci-watcher pod ──▶ Kafka ──▶ governance pod │
                    │                        ⏸ human approves       │
                    │   Harness delegate ──▶ ns `agent-apps`        │
                    │   agent-app-proxy 3100N ──▶ NodePort 3000N    │
                    └───────────────────────────────────────────────┘
```

Three containers on one Compose bridge on Zeenie (192.168.0.21). `agents.local` exists only
inside that network — no external domain, no DNS, no TLS. Deliberate for the MVP. The lower half
is the pre-existing EPLX estate the fleet now delivers through (§9).

### Files

| file | role |
|---|---|
| [agent_brain.py](../agent_brain.py) | The agent loop, the four tools, the DeepSeek call, the worker's system prompt, and the `---EMAIL---` boundary. |
| [agent_validator.py](../agent_validator.py) | The reviewer: same loop, reviewer's prompt, fresh context, its own shell. |
| [agent_notes.py](../agent_notes.py) | The agent's memory across tasks: injects its three self-written notes files, and finds a free app port. |
| [agent_delivery.py](../agent_delivery.py) | Delivery conventions: repo/image/Kubernetes naming, the three-ports-per-app scheme, the CI workflow, the manifest example, and the delivery half of the task notes. |
| [ship_app.py](../ship_app.py) | The agent's only route to GitHub — `scaffold`, `push`, `status`, `logs`, `list`. On PATH in the container as `ship_app`. |
| [agent_app_proxy.py](../agent_app_proxy.py) | Runs on the `kind` docker network; republishes NodePorts `30000-30009` as `31000-31009` so a deployed pod is openable in a browser. |
| [agent_worker.py](../agent_worker.py) | I/O adapter: IMAP poll, task construction, review gate orchestration, report building, SMTP. |
| [harness_apps.py](../../React_Typescript/github_ci_cd/governance/harness_apps.py) | *(in the EPLX project)* Creates a real per-app Harness service + pipeline by cloning `deployweb` through the API. |
| [Dockerfile](../Dockerfile) | python 3.12 + node 22 + tsc + git + lsof — enough to actually build software. |
| [docker-compose.yml](../docker-compose.yml) | mailserver + Roundcube + worker; ports, volumes, env. |
| [scripts/deploy-zeenie.cmd](../scripts/deploy-zeenie.cmd) | Runs docker from the interactive session (see §11). |
| [scripts/task_agent.py](../scripts/task_agent.py) | Task the agent from a terminal instead of the browser. |
| [tests/](../tests/) | Gate, mail-path and fault-injection tests. No mail server required. |

---

## 3. The agent loop

[agent_brain.py](../agent_brain.py) — `agent_loop(task, workspace, system_prompt, messages, tag)`:

```python
messages = [system_prompt, task]              # the entire program state
for step in range(1, MAX_STEPS + 1):
    msg = call_llm(messages)                  # one HTTPS POST to DeepSeek
    messages.append(msg)
    if not msg.get("tool_calls"):             # no tool wanted => done
        return {"answer": msg["content"], ...}
    for call in msg["tool_calls"]:
        result = DISPATCH[name](**args)       # the harness does the work
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
```

**The model is stateless.** There is no session or thread id; every call resends the whole
conversation. The message array *is* the program state.

**Termination is the absence of a tool call.** Nothing declares "complete" — the model simply
stops asking for tools and starts talking.

Four load-bearing details:

- **Arguments arrive as a JSON string**, not an object. `json.loads` them.
- **`tool_call_id`** pairs each result to its request, like a promise resolving to a specific call.
- **Errors-as-text.** A failing tool returns its error *as the tool result* instead of raising.
  Observed working: the model invented a non-existent `edit_file` tool, received
  `ERROR: no such tool 'edit_file'`, and switched to `sed` unprompted. Self-healing for one
  `try/except`.
- **Truncation** at `MAX_TOOL_CHARS`, because every step resends the whole history.

### Tools

`read_file`, `list_dir`, `run_bash`, `write_file`. `run_bash` prefers bash, uses
`stdin=DEVNULL` (nothing may ever block on input) and a `BASH_TIMEOUT` kill.

### Parameters that make it reusable

`system_prompt` swaps the role — the same loop is both worker and reviewer. `messages` resumes an
earlier run, so review feedback becomes a new turn in the worker's *existing* conversation rather
than a fresh start. `tag` stamps every transcript entry with its author (§6).

---

## 4. Email as the task interface

[agent_worker.py](../agent_worker.py). `main()` is `while True: poll_once(); sleep(POLL_SECONDS)`.

**`drain_inbox`** connects, searches `UNSEEN`, and for each message: fetches with `BODY.PEEK[]`
(so fetching doesn't implicitly consume it), flags `\Seen` **before any work happens**, records
the `Message-ID` in `/workspace/.processed.json`, and returns the raw message **without running
it**.

Draining before building is deliberate: a build takes minutes and Dovecot will drop an idle IMAP
connection out from under a long task. Grab the mail, hang up, then work.

**Idempotency is at-most-once.** Flagged before the build, not after. If the worker dies
mid-build the task is dropped rather than retried — the right trade when a retry means "rebuild
the app and email the human again."

**Task construction** = subject + plain-text body (MIME-walked, `text/plain` preferred, HTML
de-tagged as fallback), plus a delimited block of machine notes: the workspace path, the assigned
port, and the URL to report. The notes are explicitly marked *not part of the request*.

**Loop guard:** mail from the agent's own address is ignored.

---

## 5. The review gate

Nothing is emailed until a reviewer signs it off, or until the rounds are spent.

```
build ──▶ review ──PASS──▶ send (2 emails)
            │
           FAIL
            │
            ▼
  objections go back into the worker's EXISTING conversation
            │
            └──▶ rework ──▶ review ... up to VALIDATION_ROUNDS
                             │
                    still failing ──▶ send anyway, objections banner-ed on top
```

Three properties make it worth the tokens:

1. **Fresh context.** The reviewer never sees the worker's message history, so it cannot inherit
   its rationalisations. Same reason you don't review your own PR.
2. **Its own tools**, in the same workspace. It recomputes the numbers and re-runs the tests
   itself. A reviewer that can only read the summary is a rubber stamp.
3. **Rework resumes**, not restarts — the worker keeps everything it already knows.

**Fail-open on exhaustion.** After the last round it sends regardless, with the objections at the
top of the email. A task that silently vanishes is worse than one that arrives flagged, and the
human stays the last word.

**Guards, because a gate fails in both directions:** an unparseable verdict counts as FAIL (a
reviewer must not wave work through by being vague); a crashed reviewer counts as FAIL; and it is
told explicitly *not* to block on style, tone, or work nobody asked for. `VALIDATION_ROUNDS=0`
disables it entirely.

### The reviewer's checklist

Each rule was added after a real miss, not from imagination:

| rule | the miss that caused it |
|---|---|
| Recompute every number | — |
| A claim of verification no command supports is the worst defect | *"I verified that after exactly 11 months $10,000 reaches $20,000"* — pure fabrication |
| Substitute the answer back into the question | 9y11m endorsed while 119 months = $19,980.06 |
| Check the answer against **itself** | prose said $722.89, table said $10,722.90 |
| Check each example actually demonstrates its point | *"the next $10,000 arrives in the 10 years after that"* — describes **linear** growth, refuting its own thesis |
| Reject unfilled placeholders | `[Your name]` reached the inbox |
| If a URL is offered, fetch it and check the body | app shipped with passing tests and never run |
| Say plainly what you could **not** verify | tone judgments dressed up as measurements |

---

## 6. The reply: prose plus evidence

Two emails on a pass, one on a failure (on failure the objections already ride on the worker's
reply; a second email would repeat them).

**agent1 →** the answer, then the harness-generated evidence block:

```
run: completed in 20 steps, 23 tool calls
review: PASSED after 1 round(s)
workspace: /workspace/task-0011-build-me-a-tic-tac-toe-web-app

FILES WRITTEN
  .../index.html   [agent1]
  /tmp/draft.txt   [validator1]

EVERYTHING THAT WAS RUN (in order, by whom)

[agent1]
    1. python3 -c " P=10000 r=0.07/12 ... "

[validator1]
    2. curl -s http://localhost:3001/ | grep -c 'class="cell"'
```

**validator1 →** *"Reviewed: <subject>"*, threaded under the reply it approved: what it checked
independently with real numbers, where its method differed from the worker's, what it could not
verify, the round history (`round 1: sent back — …`, then *"The reply you received is the
corrected one"*), and the commands it ran.

Three properties of this block matter:

- **It is generated from the recorded transcript, not from the model's account of itself.** The
  prose can drift; this cannot.
- **Attribution is stamped at execution time** (`by` on every entry, grouped under
  `[agent1]`/`[validator1]` headers). Before this the merged list read as all-worker and actively
  misled a reader into crediting the worker with the reviewer's work.
- **Commands are flattened, not truncated at the first newline.** A multi-line `python3 -c` used
  to render as `python3 -c "` — useless on precisely the email where the maths was wrong.

---

## 7. Apps that actually run — locally

An agent that builds a web app you cannot open is a demo. Everything in this section is still
true, but it has been **demoted from delivery to testing**: a `nohup`'d server is now how the
agent checks its own work, not how the work reaches anyone. Real delivery is §9.

- Compose publishes **3000–3009**; the harness picks the first port in that range with **nothing
  listening** (a TCP connect, not a bind — the agent's own servers are in this container and bound
  to `0.0.0.0`, so a bind test reports "free" right up until you steal the port from a live app)
  and injects it into the task text. Only when all ten are busy does it fall back to
  `3000 + seq % 10` and reclaim with `lsof -t -i:PORT | xargs -r kill -9`.
  The original rule *always* rotated and killed, which is fine while every app is a throwaway and
  fatal the moment one is maintained: task 22 would have shot task 12's booking app in the head to
  make room for a scratch server. The task text now also says: **if you are changing something
  already serving on a port, keep that port** — moving it breaks the link people already have.
- The prompt says **background it and leave it running**, then report the URL:
  `nohup cmd > log 2>&1 &`. **The redirect is load-bearing**: without it the background child
  holds the capture pipe and `run_bash` hangs to its timeout even though the server started fine.
- The reviewer **curls it** — from inside and outside — and is told not to settle for HTTP 200.

Proven: *"Build me a tic-tac-toe web app… send me the link"* → html/css/js + a Node static server,
backgrounded, `http://192.168.0.21:3001` in the reply, independently fetched by validator1
(`grep -c 'class="cell"'` → 9), opened and played in a browser.

**Known limits of the local server** — and the reason §9 exists: it dies when the container
restarts, nobody reviewed the code that produced it, nobody approved its release, and it is not
built from anything you could rebuild. It is a preview, and the agent is now required to describe
it as one.

---

## 8. The agent's own memory

Every task started from nothing. The container persists, the workspace persists, the apps keep
running — but the agent woke with no idea any of it existed.

We watched exactly what that costs. Sent a bug report about an app it had built the day before,
naming neither the folder nor the cause, it found the app unaided: `ls /workspace`, recognised the
folder from the email subject, read the source, then checked the container's clock four different
ways and diagnosed the timezone convention correctly. Genuinely impressive — and it only worked
because the folder happened to be named after the subject line. Then it **copied the app into its
new task folder and fixed the copy**, leaving the original running with the bug; wrote `PORT=3003`
into `.env` while the server kept announcing 3002; and finally `pkill -f dist/src/index.js`'d
both, unable to tell its fork from the original. Discovery worked. *Identity* did not.

So the agent keeps three files at the root of its workspace, in the spirit of the `CLAUDE.md` a
human engineer leaves for the next session. They are split by **when you reach for them**, not by
subject:

| file | what goes in it | read when |
|---|---|---|
| `/workspace/AGENT.md` | how this machine works and how it works in it | planning |
| `/workspace/AGENT-ASSETS.md` | what it built: where it lives, its port, how to start it | orienting |
| `/workspace/AGENT-AVOID.md` | lessons learned — what burned it, and what to do instead | before acting |

The third earns its place by being *scanned* rather than read: "have I already tried this and had
it fail?" is a different question from "what is here?", and burying the answer in a general notes
file means it is found after the mistake instead of before. It also has a natural supply. Watching
one task, the agent ran `cd app && nohup node server.js > server.log 2>&1 &` and burned its full
300-second timeout — the redirect was right, but `A && B &` backgrounds the *whole list*, so the
outer `cd` still holds the capture pipe. The system prompt warns about the missing-redirect
version of that trap and it walked into the variant anyway. Prompt text is written once by us and
scales badly; a lessons file is written by the agent, in the words it will recognise, only for
things that actually happened here.

Four decisions make this work, and each one is a rule we had already learned somewhere else:

**The agent owns all three completely.** The harness never writes them, never seeds them, never
parses them and imposes no format. They are notes to itself, and the moment we depend on their
structure they stop being notes and become a schema it has to serve. It creates them, organises
them however it likes, and corrects them. In `agent_notes.py` they are one table of
`(filename, purpose, what-to-do-if-missing, what-belongs-in-it)`, so a fourth file is one row —
and so the description the agent reads when a file is empty is the same one it reads when
deciding whether today's work belongs in it.

**The harness injects them; it does not ask for them.** Every task arrives with all three pasted
in verbatim (capped at `NOTES_MAX_CHARS` each, told where to read the rest). This is principle 1 from
§13 applied again: an instruction to "read AGENT.md first" is one the model can quietly skip, and
this is precisely the information it does not know it is missing. Reading is not optional if
there is nothing to read *from*.

**Ports come from the OS, not the notes.** What a port is *for* is in the agent's file; whether a
port is *busy* is a fact about the machine, so we ask the machine (§7). Ground truth needs no
format contract — which is what lets the harness stay out of the file entirely.

**The reviewer enforces the upkeep.** A registry nobody maintains is worse than none, because it
lies with authority. When the work built, changed, deployed or retired anything durable, the
reviewer reads `AGENT-ASSETS.md` **from disk** — the copy in the task text is the *before* state —
and checks the path exists, the port is the one actually listening, and the start command really
starts it. A stale entry is a fail. Formatting and layout are explicitly not: it is a notebook,
not a deliverable.

Only the assets file is enforced, deliberately. A missing lesson is not a defect in the work, and
a reviewer that failed tasks for insufficiently reflective note-keeping would teach the agent to
pad the file — which destroys the only property that makes it worth reading.

The worker logs `notes UPDATED / unchanged` with the before-and-after sizes of each file, so the
question this mechanism turns on is answerable from `docker logs` without reading a mailbox.

**What this does not fix:** nothing reconciles the notes with reality except the agent itself. If
it deletes an app without a note, or the container restarts and every server dies (§7), the file
says they are running until the next task notices. The reviewer catches that only for assets the
current task touched.

---

## 9. Delivery: shipping to Kubernetes

*Jianmin's idea, 2026-08-02: "Going forward, any applications need to be checked into GitHub, the
GitHub CI auto kicks off, the Harness agent does the CD. Let the agent understand we have
Kubernetes pods. Each app should deploy to a pod."*

The agent could build software and could not release it. The other half already existed: a full
enterprise SDLC mockup at `../React_Typescript/github_ci_cd` — GitHub Actions CI, ghcr.io, a Kafka
event bus, a governance app with a **human approval gate**, Harness CD, and a kind cluster. Built
for one app, the calculator. This section connects the fleet to it.

### The chain

```
  email ──▶ agent1 builds and tests locally on 3000-3009            (§7 — a preview)
              │  writes Dockerfile + ci/test.sh + k8s/deployment.yaml
              ▼
         ship_app push ──▶ github.com/df360-net/agent-<app>
              │
              ▼
    GitHub Actions (GitHub-HOSTED)   test ─▶ build ─▶ ghcr.io/df360-net/agent-<app>:<sha7>
              │
              ▼
    ci-watcher POD   polls the Actions API, emits ArtifactReady ──▶ Kafka eplx_deployments
              │
              ▼
    governance POD   SCAN ─▶ CR_CREATE ─▶ ⏸ AWAITING_APPROVAL
              │            (a HUMAN clicks Approve — the gate is the point)
              │      ensure Harness service+pipeline exist ─▶ HARNESS_TRIGGER ─▶ poll ─▶ NOTIFY
              ▼
    Harness delegate ──▶ rolling deploy into namespace `agent-apps`
              │
              ▼
    agent-app-proxy   31000+N ──▶ learn-control-plane:30000+N ──▶ the pod
              ▼
         http://192.168.0.21:3100N  in your browser
```

### Five decisions, each forced by something measured

**1. CI polls, it is not pushed to.** The calculator's last CI job runs on a self-hosted runner on
Zeenie because it must reach the LAN Kafka broker. That cannot scale to a fleet: `df360-net` is a
**user account, not an organisation** (`user/orgs` is empty; the org-runner API 404s), and GitHub
only supports org- and enterprise-level runners — so that runner is permanently scoped to one
repo, and every new agent repo would need its own registration. A watcher **inside** the cluster
polling the Actions API removes the problem: CI stays on GitHub-hosted runners, nothing in GitHub
needs a route to the LAN, and adding the hundredth repo costs nothing.

**2. One repo and one Harness pipeline per app — created by API, not by hand.** Per-app pipelines
are how Harness is really used, and a single generic catch-all pipeline pretending to be several
would teach the wrong thing. But a human clicking through a Service and a Pipeline for every app
caps an autonomous fleet at its owner's clicking speed. So
[harness_apps.py](../../React_Typescript/github_ci_cd/governance/harness_apps.py) clones the
hand-built `deployweb`/`web` pair through the API: four objects per app, idempotent, safe to
re-run on every deploy so a changed manifest takes effect.

| shared, created once | per app, created on first release |
|---|---|
| connector `ghcr`, connector `kindconnector`, environment `dev` | file `<app>.yaml` — the agent's own committed manifest |
| infrastructure `agentapps` → namespace `agent-apps` | file `<app>-values.yaml` — `image: <+artifact.image>` |
| | service `agent_<app>` |
| | pipeline `deploy_agent_<app>` |

Harness identifiers must match `^[a-zA-Z_][0-9a-zA-Z_$]*$` — **hyphens are rejected**, while every
other name in the system (repo, image, Kubernetes object) is hyphenated. One function, `ident()`,
owns that conversion.

**3. Three ports per app, from one index.** App slot `N` in 0..9 gives `3000+N` (local preview),
`30000+N` (NodePort in the cluster), `31000+N` (what a browser opens). N belongs to the *app*, not
the task — it has to survive a redeploy — so it lives in the agent's own `AGENT-ASSETS.md` and in
the committed manifest. The harness only *suggests* the lowest unclaimed slot; a collision surfaces
as a Kubernetes error, which is the same "ask the machine, not the notes" rule as §8.

**4. A proxy, because kind's ports are frozen.** `docker inspect learn-control-plane` shows exactly
one published port — `6443/tcp`, with an **empty binding** — and every Service in the cluster is
`ClusterIP`. A pod Harness has just deployed is running perfectly and reachable from nowhere.
kind's `extraPortMappings` are fixed at cluster creation, and recreating the cluster would discard
a month of state. [agent_app_proxy.py](../agent_app_proxy.py) sits on the `kind` docker network —
where the node is an ordinary TCP endpoint — and republishes ten NodePorts. Same pattern as the
`kube-api-proxy` that already rescues the API server, generalised to a block. Adding an app needs
no change to it.

**5. `ship_app` is a command, not an instruction.** Repo names, remote URLs with tokens in them,
API paths and a workflow file that must be byte-correct are all harness work: exact, unforgiving,
and identical every time. The agent decides *what* to ship; [ship_app.py](../ship_app.py) decides
*how* — `scaffold`, `push`, `status`, `logs`, `list`. The token reaches git through a `GIT_ASKPASS`
helper rather than the remote URL, so it never lands in `.git/config` for the next reader.

### What honesty requires here

The reply email changes shape. The agent finishes when **CI is green**, not when the app is live,
because a human still has to approve the release and that may take a day. It is told to report the
image and the eventual URL *marked as pending approval*, to describe the local server as a preview
that dies with the container, and specifically **not** to curl the cluster URL and report it as
down. Governance's existing NOTIFY step sends the second message when the deploy lands.

If `GITHUB_TOKEN` is absent, the delivery instructions are replaced by an explicit *"delivery is
unavailable, say so and do not claim to have pushed anything"*. An agent told to ship without
credentials will otherwise report having shipped.

### Traps found building it

- **`<+artifact.image>` in a manifest is not resolved.** Pods came up `InvalidImageName` with the
  literal expression as their image. Harness evaluates its expressions in the **values** file and
  then renders the manifest as a Go template — so the manifest must say `{{.Values.image}}` and a
  per-app values file must carry `image: <+artifact.image>`. The calculator's `web` service was
  doing this all along; it was the one part of the template not obvious from the pipeline YAML.
- **`imagePullSecrets: [ghcr-cred]` is mandatory** (the registry is private) **and secrets are
  namespaced** — `agent-apps` needed its own copy. Omit it and the failure looks like a broken
  image rather than a missing credential.
- **Harness reports "not found" as HTTP 400 with `RESOURCE_NOT_FOUND_EXCEPTION`**, not 404, on most
  NG endpoints. Treating only 404 as absence makes every first-ever create look like an outage.
- **`workflow` scope is not optional** on the agent's PAT. Without it GitHub rejects any push that
  touches `.github/workflows/`, with a message that reads like a permissions bug.
- **Kafka and redpanda-console were stopped** — they are in the lab's "pause to reclaim CPU"
  runbook, and the whole CD chain runs through them.
- `docker cp` into a *created-but-never-started* container fails if the destination directory does
  not exist in the image; and `docker pull` still cannot run over SSH (§11), so the proxy uses
  `python:3.12-slim`, which was already cached.

### Proven, and not yet

**Proven end to end (2026-08-02).** A hand-written probe app — deliberately not agent-built, to
separate pipeline faults from agent faults — went `git push` → CI green on the first run →
`ghcr.io/df360-net/agent-pipeline-probe:cc1cdf8` → Harness `Success` → two pods `1/1` in
`agent-apps` → **`http://192.168.0.21:31000` answering HTTP 200 in a browser**, the page naming its
own pod (`pipeline-probe-5498fbf8f8-sbpf4`). The probe stays as a reference deployment.

**Not yet wired:** the ci-watcher pod, the governance changes that consume a per-app event, and the
approval gate for agent apps. That deploy was triggered by calling Harness directly
(`harness_apps.py deploy`), which bypassed all three.

---

## 10. Configuration

| variable | default | meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | required; never printed |
| `DEEPSEEK_MODEL` | `deepseek-chat` | |
| `MAX_STEPS` | 200 | runaway-loop backstop |
| `MAX_TOOL_CHARS` | 8000 | per tool result, protects context |
| `BASH_TIMEOUT` | 300 | seconds before a command is killed |
| `POLL_SECONDS` | 20 | inbox poll interval |
| `VALIDATION_ROUNDS` | 3 | review rounds before sending anyway; 0 disables |
| `REVIEW_RESULT_CHARS` / `REVIEW_TRANSCRIPT_CHARS` | 800 / 20000 | how much record the reviewer is shown |
| `MAX_REPLY_CHARS` | 40000 | reply body cap |
| `NOTES_MAX_CHARS` | 8000 | per notes file, when pasted into a task (§8) |
| `APP_HOST` / `APP_PORT_BASE` / `APP_PORT_COUNT` | 192.168.0.21 / 3000 / 10 | local preview ports |
| `GITHUB_TOKEN` | — | PAT with **`repo` + `workflow`** scope. Unset = delivery disabled and the agent is told to say so (§9) |
| `GITHUB_OWNER` | `df360-net` | repos become `<owner>/agent-<app>`, images `ghcr.io/<owner>/agent-<app>` |
| `K8S_NAMESPACE` | `agent-apps` | where Harness deploys agent apps |
| `NODE_PORT_BASE` / `PROXY_PORT_BASE` | 30000 / 31000 | app slot N → NodePort `30000+N`, browser port `31000+N` |
| `HARNESS_*` | see `governance/.env` | account, org, project, API key; plus `HARNESS_AGENT_INFRA` (`agentapps`) |
| `AGENT_ADDRESS` / `AGENT_PASSWORD` | agent1@agents.local | worker mailbox (reads and sends) |
| `VALIDATOR_ADDRESS` | validator1@agents.local | reviewer; **sends only**, needs no password |

Host ports: Roundcube `8080`, SMTP `1025`, IMAP `1143`, apps `3000-3009`.

---

## 11. Deployment and its traps

Authored on the main laptop, deployed to Zeenie over SSH.

- **`docker pull`/`build` over SSH always fails** with `error getting credentials — A specified
  logon session does not exist`, even for public images. Docker Desktop's CLI resolves registry
  credentials through `docker-credential-desktop.exe`, which needs the Windows credential vault;
  SSH is a *network* logon with no access to it. Emptying `auths`, deleting `credsStore`, a clean
  `--config` dir and disabling CLI hooks were all tried and all failed. **Fix:**
  [scripts/deploy-zeenie.cmd](../scripts/deploy-zeenie.cmd) run under the interactive token via
  `schtasks /IT`. Non-registry commands (`up` on cached images, `ps`, `logs`, `exec`) are fine.
- **Never bind-mount docker-mailserver's data/state onto a Windows path.** Postfix's `postscreen`
  dies with SIGSEGV on a missing `postscreen_cache.db` and port 25 silently stops accepting mail
  **while IMAP still looks healthy**. Use named volumes; bind-mount only `./config/dms/` — which
  is also what makes the accounts survive a `compose down`.
- **DMS won't start Dovecot until one account exists**, and shuts down after 120s if none appears.
- **The healthcheck must test 25 *and* 143.** A 143-only check reports healthy while Postfix is
  dead — exactly how the bind-mount failure hid itself.
- **No TLS** (`SSL_TYPE=` empty, plaintext IMAP auth re-enabled via `user-patches.sh`,
  unauthenticated SMTP relay on `:25` via `PERMIT_DOCKER=connected-networks`). Acceptable on a
  LAN-only bridge with no internet route; remove the day this gets a certificate.

---

## 12. What we measured

Behaviour, not intentions. From the first evening of real use:

**The gate reliably catches:**
- Fabricated verification — recomputed the claim, found no supporting command in the record,
  rejected it. Reproduced twice under fault injection.
- Internal inconsistency — caught a **one-cent** prose/table mismatch, and justified blocking on
  it by quoting the requester's own words: *"he'll believe whatever I show him."* Severity
  calibrated to stated stakes.
- Unreachable or wrong-content URLs.

**The gate does not catch:**
- A convention you never stated. It endorsed "9 years 11 months" while printing $19,980.06 at
  month 119 and $20,096.61 at month 120 *in the same paragraph*. Prompting harder did not change
  it. The question was genuinely ambiguous and it picked the other reading.
- What the app *does*. It proves reachability and structure; it has never played a game. On every
  task so far, only the human exercised behaviour — and that is where the remaining defects were
  found.

**Cost:** roughly 2–3× tokens, 4–6 minutes instead of 2. Fractions of a cent on DeepSeek — the
arbitrage the whole thesis rests on.

---

## 13. Design principles earned the hard way

1. **Telling a model to apply a rule is not the same as it applying the rule.** In a single
   deploy, the Markdown ban worked immediately and the preamble ban failed outright — three
   times. Instructions shift odds. Anything that must *always* hold belongs in the harness.
2. **When you must enforce something mechanically, let the model mark the boundary.** The
   `---EMAIL---` marker works because the harness never guesses where the email starts; it cuts
   only at a mark the agent wrote. **Fail-open**: no marker means send everything untouched. A
   leaked preamble is a wart, a truncated reply is a defect — never trade down.
3. **Giving it a sanctioned place to be verbose beat forbidding the verbosity.**
4. **Evidence must come from the harness, not the narrator.** Anything the model says about what
   it did is generated text with the same failure modes as the answer.
5. **Attribution matters as much as the record.** An unlabelled merged transcript is worse than
   no transcript: it reads as authoritative and is wrong.
6. **A gate fails in both directions.** Rubber-stamping and pedantry are both failures; unclear
   verdicts must count as rejection.
7. **Never silently drop a task.** Arrive flagged instead.
8. **Green build ≠ working.** Three checking layers still shipped a sentence that contradicted
   its own argument. The human reading the diff remains the last line.
9. **Ask the machine, not the model's notes.** Where a fact is observable — is this port busy, does
   this path exist — read it from the OS. It costs a syscall, needs no format agreement, and is
   the only reason the harness can leave `AGENT-ASSETS.md` entirely to the agent (§8).
10. **Memory is a harness feature that the model writes.** Splitting it that way is what makes it
    work: the agent decides what is worth remembering, and the harness guarantees it is read.
11. **Give the agent a command, not a procedure.** Anything exact and unforgiving — a remote URL
    with a token in it, a workflow file that must be byte-correct, four Harness API calls in
    order — is harness work. `ship_app` exists so the agent chooses *what* to ship and never
    *how*; the reliability of step 5 stops depending on the model getting a recipe right (§9).
12. **Autonomy dies at the first manual step.** Per-app Harness pipelines are the right design and
    also a human clicking through a UI for every app. Automating the clicking rather than
    abandoning the design kept both (§9).
13. **Do not let the agent's own claim be the release.** A `nohup`'d dev server was the agent
    declaring itself done. Delivery now ends where a human approves, and the agent is required to
    describe its preview as a preview.

---

## 14. Roadmap

> **Superseded for everything past "finishing §9" — see [Fleet-Design.md](Fleet-Design.md).**
> The items below under *Then* were written for a personal fleet on one laptop. The goal is now a
> platform other teams can run, and three of those items change shape: "N agents" needs an identity
> and tenancy model first, "Compose → Kind pods" is deferred behind making the agent stateless
> (and then wants a Deployment, not a StatefulSet), and per-agent routing must not be decided by
> an LLM. Fleet-Design.md also corrects §7, §8 and the app-slot mechanism in §9.

**Finishing §9 — the next work, in order:**

1. **ci-watcher pod** — poll the Actions API for completed runs across `agent-*` repos, emit
   `ArtifactReady` carrying the app name, image, tag and the committed manifest. Carrying the
   manifest in the event is what keeps a GitHub token out of governance.
2. **Governance** — route on `app`: the calculator keeps `HARNESS_PIPELINE`; anything else calls
   `harness_apps.ensure_app()` then triggers `deploy_agent_<app>`. The approval gate is unchanged,
   which is the whole point of reusing it.
3. **Validator** — verify the deployed URL rather than `localhost`, and treat "CI green, awaiting
   approval" as the finished state instead of failing an app that is not live yet.
4. **Second email on DEPLOYED** — governance already has a NOTIFY step; point it at the mail
   server so the release lands in the same thread as the agent's reply.

**Then:**

- **Jira + Confluence** as a second task source and a documentation sink, via Atlassian Cloud
  free tier. Do **not** self-host Data Center — licences, huge JVMs, a database.
- **N agents.** The design is already per-container and single-threaded by choice: scale by
  running more containers, not by adding threads.
- **Compose → Kind pods** in the existing `learn` cluster (k8s Secrets, Deployments, Services).
  This is the original "a few containers in the cluster" vision, arriving as step 2 rather than
  step 1.
- **App lifecycle.** Nothing retires an app: ten slots, and no path from "deployed" to "deleted"
  that also cleans up the Harness service, the pipeline, the file-store entries and the repo.
- **Harden:** long-build handling, app lifecycle beyond container restarts, per-agent identity
  and routing, and an explicit human approval gate before an agent "ships" anything real.
