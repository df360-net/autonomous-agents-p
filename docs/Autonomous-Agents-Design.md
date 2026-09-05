# Autonomous Agents — Design

A fleet of autonomous developer-agents, each in its own container, **tasked the way you'd task a
human engineer: by email**. An agent reads the instruction, does the work, has it independently
reviewed by a second agent, and replies with the result plus the evidence.

Built and running: **four agents as Kubernetes pods across two boxes**, tasked by mail, keeping
their own memory between tasks (§8), shipping through GitHub CI into pods the fleet control
plane deploys and addresses (§9), with a review verdict gating the announcement.

*Status: 2026-08-22. This document describes the machinery and why each piece is shaped as it
is; [Fleet-Design.md](Fleet-Design.md) owns the plan and the decisions still open, and wins
where the two disagree. Companion documents: [agent-reminder.md](../agent-reminder.md) (context
handoff), [agent/README.md](../agent/README.md) (what runs in the container),
[mail/README.md](../mail/README.md) (why the mail server is configured as it is).*

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
   you ──▶ Roundcube on hp-tiger, or scripts/task_agent.py
             │  mail to agentN@agents.local
   ┌─────────▼────────────────────────────────────┐
   │ docker-mailserver on hp-tiger                │  submission :587, IMAP :143
   │   boss@  agent1..4@  validator1..4@ (alias)  │  STARTTLS, SPOOF_PROTECTION=1
   └─────────┬────────────────────────────────────┘
     IMAP    │ ▲ SMTP — one login, two From addresses
   ┌─────────▼─┴──────────────────────────────────┐
   │ agentN pod   namespace `fleet`, either box   │
   │                                              │
   │   agent_inbox ──▶ TaskEnvelope               │
   │        │                                     │
   │   agent_principal.admit()  attest + sanitise │
   │        │                                     │
   │   agent_memory.sync()      BEFORE the prompt │
   │        │                                     │
   │   agent_loop(worker) ──▶ review gate ◀── agent_loop(reviewer, fresh ctx)
   │        │                       │             │
   │        │                       ▼             │
   │        │                 report_review ──────┼──▶ plane
   │        ▼                                     │
   │   send_reply ×2                              │
   │                                              │
   │   /memory/tenant  /memory/fleet   (git)      │
   │   /workspace/task-NNNN-<slug>/    (scratch)  │
   └──────┬─────────────────────────┬─────────────┘
          │ ship_app push           │ ship_app register  (token-scoped HTTP)
   ┌──────▼──────────────────┐  ┌───▼──────────────────────────────────┐
   │ github.com/df360-net/   │  │ fleet control plane   hp-tiger:8091  │
   │   agent-<app>           │  │   kill switch + inter-agent cap      │
   │   Actions ──▶ ghcr.io   │  │   spend ledger + fleet ceiling       │
   └─────────────────────────┘  │   app registry: assigns box, port,   │
                                │     URL; renders Deployment+Service  │
                                │   review verdicts gate the           │
                                │     "it is live" email               │
                                └──────────────────────────────────────┘
```

Nothing in this repository deploys anything. Pushing to `main` builds
`ghcr.io/df360-net/agent-runtime:<sha>`; the infra/ops side declares a tag and rolls the pods.
The control plane is theirs too. **A green build is not a deployed fix.**

### Files

| file | role |
|---|---|
| [agent/agent_brain.py](../agent/agent_brain.py) | The agent loop, the four tools, the DeepSeek call, the worker's system prompt, and the `---EMAIL---` boundary. |
| [agent/agent_validator.py](../agent/agent_validator.py) | The reviewer: same loop, reviewer's prompt, fresh context, its own shell. Owns verdict parsing. |
| [agent/agent_worker.py](../agent/agent_worker.py) | I/O adapter and the container's entrypoint: poll, admit, sync memory, run, review, report, reply. |
| [agent/agent_envelope.py](../agent/agent_envelope.py) | What a task *is*, independent of the transport that carried it. |
| [agent/agent_inbox.py](../agent/agent_inbox.py) / [agent_outbox.py](../agent/agent_outbox.py) | The only mail-shaped code in the fleet. One SMTP login, two From addresses. |
| [agent/agent_principal.py](../agent/agent_principal.py) | Who is asking, resolved before any handler runs. Hops, thread depth, terminal purposes, sanitisation. |
| [agent/agent_peer.py](../agent/agent_peer.py) | Agent-to-agent messaging. The agent picks who and why; the harness owns hops, signature, thread and the operator CC. |
| [agent/agent_budget.py](../agent/agent_budget.py) | Spend ledger, four ceilings, `BudgetExceeded(LLMError)`. |
| [agent/agent_memory.py](../agent/agent_memory.py) | The part that survives the container: two git clones under `/memory`. |
| [agent/agent_notes.py](../agent/agent_notes.py) | The agent's self-written notes, and the preview port nothing is listening on. |
| [agent/agent_delivery.py](../agent/agent_delivery.py) | Delivery conventions: repo and image naming, the CI workflow, the manifest example, the delivery half of the task notes. |
| [agent/ship_app.py](../agent/ship_app.py) | The agent's only route to GitHub and to app registration. On PATH in the container as `ship_app`. |
| [agent/fleet_identity.py](../agent/fleet_identity.py) | One place that derives mailbox, memory path, labels and keys from `<tenant>/<name>`. |
| [agent/fleet_control.py](../agent/fleet_control.py) | The money controls, across a network. **Fails closed.** |
| [agent/fleet_register.py](../agent/fleet_register.py) | App registration, review verdicts, app status. |
| [agent/git_auth.py](../agent/git_auth.py) | One credential path for every git push; the token never lands in `.git/config`. |
| [agent/agent_app_proxy.py](../agent/agent_app_proxy.py) | The zeenie-era way of making a kind NodePort reachable from a browser. Not invoked by the worker; kept because the boxes differ in whether they need it. |
| [Dockerfile](../Dockerfile) | python 3.12 + node 22 + tsc + git + lsof — enough to actually build software. Copies `agent/*.py` **flat** into `/app`. |
| [provision_agent.py](../provision_agent.py) | Creates a mailbox and prints a k8s Secret. The password is never written down. |
| [scripts/task_agent.py](../scripts/task_agent.py) | Task an agent from a terminal instead of the browser. |
| [tests/](../tests/) | Offline suites, which also ship in the image as its only self-check. |

---

## 3. The agent loop

[agent_brain.py](../agent/agent_brain.py) — `agent_loop(task, workspace, system_prompt, messages, tag)`:

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

[agent_worker.py](../agent/agent_worker.py). `main()` is `while True: poll_once(); sleep(POLL_SECONDS)`.

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

### The verdict leaves the pod

Passing the gate is not the end of the verdict's life. `report_review()` posts it to the fleet
control plane, which holds the "it is live" email until someone has ruled.

> **No verdict is not a pass.** If the gate did not run, or the plane could not be reached, the
> announcement is held. Silence means nobody approved it.

That call **fails soft**, unlike the spend calls, and the asymmetry is deliberate: raising here
would abort a task whose reply has already been sent, and there is no "send anyway" fallback
available. A failure to report costs a held announcement and a log line — never a false one.

**A verdict is unanimous or it is a contradiction.** A reviewer that writes `VERDICT: FAIL`,
reworks its own reasoning and ends `VERDICT: PASS` has not passed anything; `parse_verdict`
requires every occurrence to agree and cuts the notes at the last one. Two parse bugs lived here
and both resolved toward *pass*, which is the direction a gate must never fail in.

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

## 7. The preview: an app the agent can test, and nobody else can reach

An agent that builds a web app you cannot open is a demo. Everything in this section is still
how the agent checks its own work — and it has been **demoted from delivery to testing**. Real
delivery is §9.

**Nothing publishes a pod's ports.** The preview server binds inside the agent's own container
and is reachable from the agent, from its reviewer, and from nowhere else. That fact has to be
said plainly and repeatedly, because the failure it prevents is one an agent commits
confidently: offering a local port as an address, in an email, to a human, who then cannot open
it. A local port is for testing; the fleet emails the real address itself, once a pod is
genuinely serving.

- The harness picks the first port in `APP_PORT_BASE..+APP_PORT_COUNT` with **nothing
  listening** — a TCP connect, not a bind. The agent's own servers are in this container bound
  to `0.0.0.0`, so a bind test reports "free" right up until you steal the port from a live app.
  Only when all of them are busy does it fall back to `base + seq % count` and reclaim with
  `lsof -t -i:PORT | xargs -r kill -9`.
  The original rule *always* rotated and killed, which is fine while every app is a throwaway
  and fatal the moment one is maintained: task 22 would have shot task 12's booking app in the
  head to make room for a scratch server. The task text also says: **if you are changing
  something already serving on a port, keep that port.**
- The prompt says **background it and leave it running**, then report what it verified:
  `nohup cmd > log 2>&1 &`. **The redirect is load-bearing** — without it the background child
  holds the capture pipe and `run_bash` hangs to its timeout even though the server started
  fine. The agent walked into a variant of this anyway (`cd app && nohup ... &` backgrounds the
  *whole list*, so the outer `cd` still holds the pipe), which is the sort of thing that belongs
  in a lessons file the agent writes in its own words, not in prompt text we write once.
- The reviewer **curls it** and is told not to settle for HTTP 200 — check the body contains the
  thing that was promised.

**Known limits, and the reason §9 exists:** the preview dies with the pod, nobody reviewed the
code that produced it, nobody approved its release, and it is not built from anything you could
rebuild. It is a preview, and the agent is required to describe it as one.

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

So the agent keeps three files, in the spirit of the `CLAUDE.md` a human engineer leaves for the
next session.

> **Where they live changed, and nothing else about them did.** They were files in the container's
> workspace; they are now files in a git clone under `/memory`, synced at boot and at the top of
> every task **before** the notes are pasted into the prompt. `AGENT.md` and `AGENT-AVOID.md` are
> shared by every agent in the tenant, `AGENT-ASSETS.md` is private to each, and a fourth file —
> `FLEET.md` — is operator-only and writable by no agent. The paths below are historical; the
> four rules are not. See [agent_memory.py](../agent/agent_memory.py) and D5 in
> [Fleet-Design.md](Fleet-Design.md).

They are split by **when you reach for them**, not by
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
GitHub CI auto kicks off... Let the agent understand we have Kubernetes pods. Each app should
deploy to a pod."* One repository per app; one deployment per app.

### The chain

```
  email ──▶ agentN builds and tests on a preview port           (§7 — dies with the pod)
              │  writes Dockerfile + ci/test.sh + k8s/deployment.yaml
              ▼
         ship_app push ──▶ github.com/df360-net/agent-<app>
              │
              ▼
    GitHub Actions (GitHub-hosted)  test ─▶ build ─▶ ghcr.io/df360-net/agent-<app>:<sha7>
              │
              ▼
    ship_app register ──▶ POST /agent/apps {app, image, port, replicas, thread}
              │
              │           the control plane assigns the BOX, the NodePort and the URL
              ▼
    per-box daemon ──▶ renders Deployment + Service, runs the pod
              │
              ▼
    the plane emails the live address INTO THE AGENT'S OWN THREAD, once it is serving
```

The review verdict rides alongside it: after the gate rules, `report_review()` posts pass or fail
to the plane. With `ANNOUNCE_REQUIRES_REVIEW` on, a fail — **or silence** — withholds that final
email. Registration happens mid-task, while the agent is still working; the verdict call is what
closes the window in which the plane knows an app exists and does not know whether anyone
approved it.

### Five decisions, each forced by something measured

**1. The agent registers; it does not deploy, and it does not compute an address.** This is the
one that changed most, and the reason is worth keeping. `http://{APP_HOST}:{PROXY_PORT_BASE +
slot}` was arithmetic over two environment variables, so it produced a confident URL whether or
not anything was listening on it — and it did exactly that, in emails, for apps nothing had
deployed. Nothing errored; nothing was blank; the sentence was simply false. Both variables are
gone from [agent_delivery.py](../agent/agent_delivery.py) and cannot come back, because the
allocation now belongs to whoever runs the pod. **The manifest the agent commits still carries a
NodePort. It is a plausible description of the app, not an allocation** — which is why a
collision is now impossible rather than merely unlikely.

**2. One repository per app, and the name is fleet-wide.** `agent-<app>` does not carry the
tenant or the agent, so two agents asked to build "a todo list" resolve to the same repository
and the second would silently overwrite the first's running application. `ship_app` records the
owning agent in the repo and refuses a push from anyone else. The proper fix — addressing apps as
`<tenant>/<app>` — is D4 in [Fleet-Design.md](Fleet-Design.md).

**3. CI polls nothing and pushes nowhere private.** `df360-net` is a **user account, not an
organisation** (`user/orgs` is empty; the org-runner API 404s), and GitHub supports runners only
at org and enterprise level — so a self-hosted runner is permanently scoped to one repository and
every new agent repo would need its own registration. Agent CI therefore runs on GitHub-hosted
runners and ends at the image. Nothing in GitHub needs a route to the LAN, and the hundredth repo
costs nothing.

**4. `ship_app` is a command, not an instruction.** Repository names, remote URLs with tokens in
them, API paths and a workflow file that must be byte-correct are all harness work: exact,
unforgiving, and identical every time. The agent decides *what* to ship;
[ship_app.py](../agent/ship_app.py) decides *how* — `scaffold`, `push`, `status`, `logs`, `list`.
The token reaches git through a `GIT_ASKPASS` helper ([git_auth.py](../agent/git_auth.py)) rather
than the remote URL, so it never lands in `.git/config` for the next reader.

**5. The story is told in four places and they must agree.** `delivery_note` (the task notes),
`agent_brain`'s system prompt, `ship_app scaffold` and `ship_app status`. An agent handed two
accounts of the same pipeline splits the difference and invents a third — and `scaffold` is the
*first* thing it reads, so a stale line there outranks a correct one later.

### What honesty requires here

The agent finishes when **CI is green and the app is registered**, not when it is live. It
reports the image, describes the preview as a preview, and **reports the address the control
plane assigned** — see the four corrections below. The plane sends the second message, threaded
onto the agent's own reply, when the deploy lands.

If `GITHUB_TOKEN` is absent, the delivery instructions are replaced by an explicit *"delivery is
unavailable, say so and do not claim to have pushed anything"*. An agent told to ship without
credentials will otherwise report having shipped.

### The last mile, and four defects in it (2026-09-04)

j-fleet7 and j-fleet8 each took an email in plain English all the way to a deployed app behind a
valid certificate. The product worked. **The last mile had four defects, and three were found
only because a human clicked something** — every one was invisible to every automated check the
fleet had, and two were invisible *because* of how the checks were written.

**1. The agent held the address and was told to throw it away.** `ship_app` prints the URL the
control plane assigned, and the prompt said *"do not repeat the one ship_app prints"*. So the
customer got a receipt: *"I don't have a URL to give you yet and I won't guess one."* Correct
under the rule, and the wrong deliverable.

The rule was right about the wrong thing. It was written when the agent COMPUTED addresses from
`APP_HOST + PROXY_PORT_BASE + slot` and emitted confident links for apps nothing had deployed.
**Reporting an address the control plane handed you is not inventing.** The ban on computing
stays; the ban on repeating is gone, and `ship_app status` now asks the plane for the address
again at the moment the agent is composing its reply.

The payoff is bigger than convenience. **An unverifiable promise cannot be checked.** The
reviewer wrote *"I did not try to reach a cluster address (that would not be live yet by design
and would prove nothing)"* while the app had been live, and broken, for two minutes — because
the reviewer's own brief told it not to. A URL in the reply is a claim, and the reviewer tests
claims. One fetch catches the whole of defect 2.

**2. Front-end code shipped absolute URLs, and every check reported success.** Apps are served
at `https://apps.<fleet>/<name>/`. The agent develops at `localhost:3000`, where `/api/vote` is
correct, and ships it; in the cluster the browser asks the site root and gets a 404. Pod
Running, Service correct, route published, `/healthz` ok, 200 on a valid certificate, CI green
— **nothing red anywhere and the product does not work.** Every Fleet so far shipped an app in
this state.

The prompt, the `ship_app scaffold` output and the delivery note all now say to write relative
paths and to read `$BASE_PATH` for server-rendered links. And because *every safety property
lives in Python, never in the prompt*, `ship_app push` reads the front end and names what will
404. It **warns rather than refusing**: the diagnosis is heuristic, and blocking delivery on a
guess is worse than shipping with a warning the agent and the reviewer both read.

**3. A `localhost` address went out in email.** *"Live behaviour on the running preview
(http://localhost:3000 from inside this container...)"* — honest, carefully disclaimed, and
still an invitation: the mail client linkifies it and the operator clicks it. **`localhost`
names the reader's machine.** The prompt says not to; `send_mail` enforces it, at the one
function every message passes through, for the same reason the boss Cc is enforced there.
Replaced rather than refused, because the work is done and paid for by the time a reply is sent.

**4. Finished and hung rendered identically.** The worker log ended on the last thing it did,
with no terminal marker and no heartbeat, so a healthy Fleet read as a stalled one and telling
them apart took `unseen`, a pod restart count and a timestamp window over SSH. The loop now logs
`idle — waiting for mail (last poll HH:MM:SS)` when a task ends and every `IDLE_LOG_SECONDS`
(default 300) after that. Not every poll: at `POLL_SECONDS=20` that is 180 lines an hour in the
same panel an operator reads to see what the agent *did*.

**What the tests here do and do not claim.** Three of the four are prompt changes, and a prompt
is not something a test can prove works. `tests/test_lastmile.py` asserts narrowly that the
instruction which caused each defect is gone and its replacement is present — a regression
guard, and nothing more. The parts that are Python are tested as behaviour.

#### The fix to 3 rewrote the evidence (2026-09-05)

`send_mail` ran the substitution over the **whole assembled body**, which includes the
machine-generated `EVERYTHING THAT WAS RUN` and `WHAT I RAN MYSELF` blocks. Both agents on
j-fleet9 therefore sent transcripts containing

```
curl -s a local preview inside this container | grep -c "Office Coffee Tracker"
```

**a command that could never have executed**, in the block whose entire purpose is to let a human
confirm the reviewer ran what it claims. In prose that same substitution is exactly right — *"I
tested on a local preview inside this container"* is what the writer meant. Inside evidence it is
a different operation with a different meaning, and one control was doing both.

Two faults, and the second is the general one:

* **A redaction that reads as the original text is a fabrication.** Nothing marked the line as
  edited, so a reader who does not notice now believes a record of something nobody ran, and a
  reader who does notice stops trusting the whole transcript. Same family as the health dict that
  was silent rather than wrong: the artifact looks complete, so nothing prompts anyone to check.
* **A control a line break can defeat is not reliably a control.** One line in the same email read
  `http://loc...` — the transcript truncates each command at 200 characters, that happened
  *before* the send-time scrub, and the fragment no longer matched the pattern. So one message
  both redacted an address and printed one.

The rule now applies where the text is authored, in two forms, because there are two kinds of text:

| | written by | gets | why |
|---|---|---|---|
| prose | the model | *a local preview inside this container* | the sentence is the point, and it has to read naturally |
| evidence | the runtime | `[localhost:3000 redacted]` | it must read as an edit, keep host:port, and not be clickable |

The evidence form is applied in `flatten()`, at capture, **before** the per-line truncation — which
is what closes the ordering hole. The prose form stays at the chokepoint but stops at the first
evidence header, because the argument for putting it there was that *a model can talk itself out
of an instruction*, and that argument does not reach text the runtime generated.

The headers that mark the boundary are defined once, in `agent_outbox`, and the report builders
render them from those constants: written out twice, a rename in the builder would move the
boundary to nowhere and silently restore the defect.

Leaving the raw address in the transcript was the alternative — there it is evidence rather than
an invitation — and it was rejected because mail clients linkify everywhere.

**The evidence form is confirmed in production. The prose form is CLOSED WITHOUT BEING, and that is
the right state for it rather than a gap.** A real message on `pool1` carried eight
`[localhost:3000 redacted]` markers in its command transcript. The prose path did not fire — in that
message or in any since — because the *instruction* worked: the agent's own sentences named no local
address at all.

Which is what a backstop is. The prose scrub exists for the case where the model talks itself out of
an instruction, so "never observed firing in production" is its expected reading, and the only route
to natural coverage is for the prompt to fail. Contriving a message to trigger it would show that a
regex matches a string, which the unit tests already do at less cost and with better isolation.
Carrying it as an open item would mean carrying one that can only be closed by a failure — a queue
entry that quietly asks for the failure it is waiting on.

The distinction worth keeping is between a control whose SUBJECT has not occurred and a control that
has never been shown CAPABLE of firing. This one has tests that fail when the pattern is mutated, so
it is the first kind. `resolver.py`'s ten DNS wire-parser truncation guards are the second, which is
why they rank ahead of it and of the other thirty-six: wire-format parsing of network input is the
one place in this system where the input is genuinely untrusted and attacker-shaped, and a guard
that has never fired is least trustworthy where the failure mode is a malformed packet rather than a
wrong answer.

### Traps found building it

- **One port, read from one place.** The generated app reads `PORT` with a default
  (`PORT="${PORT:-8080}"; export PORT`), the manifest's `containerPort` is the value the plane
  reads and sets `PORT` from, and nothing else declares a port. Getting this wrong presents as
  `connection refused`, which reads like a crashed server rather than a misconfiguration.
- **The image tag has exactly one definition.** `agent_delivery.image_tag()` and the CI workflow
  template are generated from the same constant, and
  [tests/test_register.py](../tests/test_register.py) pins them together. `git rev-parse --short`
  is **not** a safe substitute: `core.abbrev=auto` grows the abbreviation with the object count,
  so a repo that tags 7 characters today tags 8 later and `register()` names an image that does
  not exist. That bug stalled a real deploy.
- **The Service publishes 80 and targets the containerPort.** They are not the same number by
  design, and a test that demands they match is wrong.
- **Internal markers must stay out of the deliverable.** `.fleet-registered` was one skip-list
  entry away from being emailed to the requester as an attachment.
- **`workflow` scope is not optional** on the agent's PAT. Without it GitHub rejects any push
  that touches `.github/workflows/`, with a message that reads like a permissions bug.

### Two pipelines were retired underneath this section

Worth knowing so nobody goes looking for machinery that is gone.

**Kafka → ci-watcher → governance approval → Harness CD.** The fleet originally delivered through
the pre-existing enterprise SDLC mockup at `../React_Typescript/github_ci_cd`, including a human
approval gate and per-app Harness pipelines created by API (`harness_apps.py` cloned four objects
per app because a human clicking through a Service and a Pipeline for every app caps an
autonomous fleet at its owner's clicking speed). It was switched off. For a day the chain
genuinely ended at the image, and the prose here said so.

What survived the retirement is the *shape*, not the tooling: an image built by CI, a deploy
performed by something that is not the agent, an address the agent is told rather than computes,
and a human able to withhold the release. The lesson that outlived the machinery is decision 1
above — **do not let the agent's own claim be the release**, and do not let it construct the
address either.

The traps from that era are recorded in git history. Three are still true of anyone integrating
Harness: `<+artifact.image>` is resolved in the **values** file and the manifest must use
`{{.Values.image}}`; `imagePullSecrets` are namespaced and must be copied into the target
namespace; and Harness reports "not found" as HTTP 400 with `RESOURCE_NOT_FOUND_EXCEPTION`, not
404, so treating only 404 as absence makes every first-ever create look like an outage.

---

## 10. Configuration

An agent pod's environment, delivered as a Kubernetes Secret (see
[provision_agent.py](../provision_agent.py)) plus plain env. [.env.example](../.env.example)
carries the same list with the reasoning.

| variable | default | meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | required; **never printed** |
| `DEEPSEEK_MODEL` | `deepseek-chat` | |
| `TENANT` / `AGENT_NAME` | `dev` / — | identity. Everything else is derived from these two ([fleet_identity.py](../agent/fleet_identity.py)) |
| `AGENT_PASSWORD` | — | the mailbox password, minted by the provisioner and never written to a file |
| `FLEET_HMAC_SECRET` | — | fleet-wide, identical on every agent — it is how agents prove to each other that they are agents |
| `FLEET_CONTROL_URL` / `FLEET_TOKEN` | — | the control plane, and this agent's scoped token. **Unset means "not in use", not "unreachable"** |
| `FLEET_PAUSE_TTL` / `FLEET_HTTP_TIMEOUT` | 60 / 5 | how long a good answer is trusted, and how long to wait for one |
| `MEMORY_TENANT_REMOTE` / `MEMORY_FLEET_REMOTE` | — | the two git remotes. Tenant unset = notes stay local; tenant set and unreachable with no clone = **refuse to start** |
| `MEMORY_ROOT` | `/memory` | `fleet/` is read-only, `tenant/` is writable |
| `MAX_STEPS` | 200 | runaway-loop backstop |
| `MAX_TOOL_CHARS` | 8000 | per tool result, protects context |
| `BASH_TIMEOUT` | 300 | seconds before a command is killed |
| `POLL_SECONDS` | 20 | inbox poll interval |
| `VALIDATION_ROUNDS` | 3 | review rounds before sending anyway; 0 disables the gate |
| `MAX_REPLY_CHARS` / `NOTES_MAX_CHARS` | 40000 / 8000 | reply body cap; per notes file when pasted into a task |
| `AGENT_MAX_HOPS` / `AGENT_MAX_THREAD_DEPTH` / `AGENT_MAX_PEER_SENDS` | 20 / 20 / 25 | the loop guards. Depth catches loops that carry no counter at all |
| `AGENT_TASK_USD` / `AGENT_DAILY_USD` / `FLEET_DAILY_USD` | 20 / 150 / 500 | spend ceilings, deliberately loose |
| `ALLOWED_SENDERS` | `*` | who may task this agent |
| `GITHUB_TOKEN` | — | PAT with **`repo` + `workflow`** scope. Unset = delivery disabled, and the agent is told to say so |
| `GITHUB_OWNER` | `df360-net` | repos become `<owner>/agent-<app>`, images `ghcr.io/<owner>/agent-<app>` |
| `K8S_NAMESPACE` | `agent-apps` | where agent apps are deployed |
| `NODE_PORT_BASE` / `APP_SLOT_COUNT` | 30000 / 20 | only to **suggest** a NodePort for the manifest the agent writes; the plane allocates the real one |
| `APP_PORT_BASE` / `APP_PORT_COUNT` | 3000 / 10 | local preview ports, inside the container only (§7) |

**Two variables were deleted and their absence is load-bearing:** `APP_HOST` and
`PROXY_PORT_BASE` were the halves of `http://{APP_HOST}:{PROXY_PORT_BASE + slot}`, an expression
that produced a confident URL whether or not anything was listening. There is no expression left
in [agent_delivery.py](../agent/agent_delivery.py) capable of inventing an address.

---

## 11. Deployment and its traps

**The repository does not deploy.** Push to `main`, CI builds and tags, the infra/ops side rolls
the tag onto the pods. That separation exists because the fleet spans two boxes with different
operating systems and one of them cannot pull an image over SSH at all.

- **CI is the gate on the image.** [.github/workflows/agent-runtime.yml](../.github/workflows/agent-runtime.yml)
  runs every offline suite before it builds. An agent image that boots and misbehaves is worse
  than one that does not build: it deploys, reports healthy, and spends money doing the wrong
  thing. `test_faultinject.py` is deliberately excluded — it costs real API calls and has
  returned opposite verdicts on identical input, and a gate that fails randomly gets ignored.
- **Three tags are pushed; only a SHA is ever declared.** Full sha, 7-char sha, and `main` for
  humans. A moving tag turns "restart the pod" into "silently upgrade every agent", and turns a
  rollback into an argument about what `main` pointed at last Tuesday. Both SHA forms exist
  because app images use the 7-char convention and a reader who has just learned that will
  reasonably truncate the runtime tag by hand.
- **Documentation does not build.** `paths-ignore: ["**/*.md", "docs/**"]`.
- **`docker pull`/`build` over SSH always fails on zeenie** with `error getting credentials — A
  specified logon session does not exist`, even for public images: the CLI resolves registry
  credentials through `docker-credential-desktop.exe`, which needs the Windows credential vault,
  and SSH is a *network* logon with none. Emptying `auths`, deleting `credsStore`, a clean
  `--config` dir and disabling CLI hooks were all tried and all failed. Use a `schtasks /IT`
  task in the interactive session. Non-registry commands over SSH are fine.
- **`ssh zeenie` lands in cmd.exe; `ssh jay@hp-tiger` lands in a Linux shell.** The same remote
  command does not work on both.
- **`docker logs --since` on zeenie takes local time**, so a UTC timestamp silently returns an
  empty log. Use `--since 60m`.
- **Docker Desktop on zeenie does not auto-start and has been seen to stop on its own.** The
  hazard is that it is silent: the agents are not down, they are *absent*, mail queues in
  Dovecot, and nothing alerts. A watchdog must run in the interactive session and must test the
  **engine pipe**, not agent liveness. Not built.

### The mail server

Configured in `infra-fleet/mail/` and deployed from there; [mail/README.md](../mail/README.md)
keeps the reasoning, because every setting is one a reasonable person would "fix" back. Three
that cost real time:

- **`SSL_TYPE=self-signed` does not make its own certificate.** DMS expects the files to exist
  and aborts inside TLS setup when they are missing, with no mention of a missing file. Under
  that mode the **filenames are the configuration**: `<fqdn>-cert.pem`, `<fqdn>-key.pem`,
  `demoCA/cacert.pem`.
- **`user-patches.sh` must reach the box with LF endings.** A `\r` on the shebang makes the
  kernel look for `/bin/bash\r` and the script silently does not run — the server comes up
  unhardened and nothing reports an error.
- **Do not set `TLS_VERIFY=true` on the agents** without a certificate they can chain to. Every
  agent stops receiving mail at once and the symptom reads like a wrong password.

**Historical:** this stack ran as three Docker Compose containers on zeenie until 2026-08.
`docker-compose.yml`, `provision-agent.ps1`, `scripts/deploy-zeenie.cmd` and `config/` have been
deleted; recover them from git history if the old shape is ever needed. The trap that mattered
then and is worth remembering if anyone reintroduces a bind mount: **never bind-mount
docker-mailserver's data or state onto a Windows path** — `postscreen` dies with SIGSEGV on a
missing `postscreen_cache.db` and port 25 stops accepting mail *while IMAP still looks healthy*.

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
    with a token in it, a workflow file that must be byte-correct, a sequence of API calls in
    order — is harness work. `ship_app` exists so the agent chooses *what* to ship and never
    *how*; the reliability of that step stops depending on the model getting a recipe right (§9).
12. **Autonomy dies at the first manual step.** Per-app deployment pipelines were the right
    design and also a human clicking through a UI for every app. Automating the clicking rather
    than abandoning the design kept both — and when that whole toolchain was retired, the habit
    of automating the click survived it (§9).
13. **Do not let the agent's own claim be the release.** A `nohup`'d dev server was the agent
    declaring itself done. Delivery now ends somewhere the agent does not control, and the agent
    is required to describe its preview as a preview.
14. **An agent must never construct an address.** A URL computed from environment variables is
    produced with equal confidence whether or not anything is listening on it, and an agent that
    can compute one will email it. Delete the expression, not the mistake (§9).
15. **Unreadable is not "off".** A missing field, a malformed value or an unreachable control
    plane must never resolve to "no policy in force". Only the control plane may say zero.
16. **No verdict is not a pass.** A gate that did not run has approved nothing, and the thing it
    was gating stays held (§5).

---

## 14. Roadmap

> **Superseded for everything past today's state — see [Fleet-Design.md](Fleet-Design.md) §10.**
> That document owns the plan; this one owns the machinery. Where they disagree, it wins.

The chain this section used to describe as "next" is built and running. What remains:

1. **The assurance half of cross-review.** Agents can review each other's work; nothing yet
   *measures* whether the reviews are real. The canary rate, reviewer agreement and a
   per-reviewer record are the evidence that the gate is not a rubber stamp, and last quarter's
   canary results cannot be reconstructed after the fact.
   [tests/test_faultinject.py](../tests/test_faultinject.py) already holds a known-bad artifact;
   it needs promoting from a test asset to a periodic probe with somewhere to write results.
2. **A second tenant**, run on the same hardware purely to prove the boundary is enforced by
   mechanism rather than convention.
3. **A Docker Desktop watchdog on zeenie** (§11) — the failure mode is silent absence.
4. **App lifecycle.** Nothing retires an app. There is no path from "deployed" to "deleted" that
   also cleans up the registration, the Deployment, the Service and the repository.
5. **Jira and Confluence** as a second task source and a documentation sink, via Atlassian Cloud
   free tier. Do **not** self-host Data Center — licences, huge JVMs, a database.
6. **Node 22 → 26** before Node 22 reaches EOL on 2027-04-30. Parked deliberately; prefer 26
   over 24 by then.
