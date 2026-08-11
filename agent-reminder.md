# autonomous-agents — context handoff for "future you"

> **Read this first, fully, before doing anything in this project.** It is the single source of
> truth for what we're building, the decisions already locked, the runtime environment, how the
> user works, and exactly where we are. Written 2026-08-01.
>
> **Currency warning (2026-08-09).** §§1-6 and 9 still hold. §7 is rewritten below. §§8, 10 and
> 11 describe subsystems that have since been rebuilt — each now carries a note saying what
> changed. The current plan and its status live in
> [docs/Fleet-Design.md](docs/Fleet-Design.md); when that and this disagree, that one wins.

---

## 1. What this project is (the vision)

A **fleet of autonomous developer-agents**. Each agent lives in its **own container**, is given
**full access inside that container**, and is **tasked the way you'd task a human engineer** —
by **email** (and later Jira tickets + Confluence specs). It reads the instruction, **builds the
software**, verifies it, and **reports back by replying to the email**. Think: a team of virtual
junior engineers you manage from your inbox.

It is the natural endpoint of the sibling **learning workbench** at `../LLM_API_call`: that repo
taught the fundamentals and produced `agent.py` (a ~200-line DeepSeek coding agent). This project
**scales that agent into a managed, message-driven fleet.**

**The user's thesis (this project is its production form):** a *cheap* DeepSeek agent with *full
auto-approve* inside a *scoped sandbox* is a real edge over Copilot/Claude Code (which demand
constant babysitting). Here the sandbox is upgraded from "a folder" to "a whole container" — the
blast radius is the container, which is *safer*, not riskier.

---

## 2. The key reframe (why this is tractable — the brain is already done)

`agent.py` already builds real apps (proven on address_book, taskflow, tic-tac-toe). Strip it to
its I/O and the entire new project is visible:

```
agent.py NOW:   task = sys.argv[1]         → agent_loop() → print(final answer)
this project:   task = next unread email   → agent_loop() → reply email (+ later Jira/Confluence)
```

**The middle — the agent loop, tools, DeepSeek, full-access build — does NOT change.** The only
new engineering is:
1. **I/O adapters** — an "inbox loop" (poll IMAP for tasks) + reporting (reply via SMTP).
2. **Runtime packaging** — the agent baked into a container image, one instance per worker.

The intelligence isn't the work. The plumbing around it is. (Same lesson as the workbench, one
level up: leverage lives in the program around the LLM call, not in the call.)

---

## 3. Decisions already locked (do NOT relitigate without the user)

- **MVP scope:** email-only, **1 agent**. Prove the full loop before adding Jira/Confluence or
  scaling out.
- **Mail:** **self-hosted mail server on Zeenie** (docker-mailserver) + **Roundcube** webmail so
  the user has a browser UI to send/read mail. All on Zeenie's LAN — no external domain/DNS needed
  for the MVP.
- **Agent brain:** **`agent.py` + DeepSeek**, adapted. Talks **directly to DeepSeek**
  (`https://api.deepseek.com/chat/completions`, `Authorization: Bearer $DEEPSEEK_API_KEY`) —
  NOT through the workbench proxy (the proxy only added auth + logged the wire for learning; a
  container doesn't need the wire view).
- **Substrate:** **Docker Compose FIRST** (far less friction than Kind for a 1-agent MVP — no
  image-loading, no manifests). **Graduate into Kind pods later** — that's where the user's
  original "run a few containers in the Kind cluster" vision lands, as step 2, not step 1.
- **Dev/deploy split:** author files **here** (main laptop, good tooling, git-trackable), **deploy
  to Zeenie** over SSH and run there.
- **Autonomy vs. verification (IMPORTANT):** do NOT make the agent fully hands-off-blind. The
  user's moat across every app has been *reading the diff* — green build ≠ working; each app hid a
  bug only human review caught. So the worker must **self-test AND email back "here's what I built
  + how I verified it,"** keeping a human approval beat in the loop, at least until a worker is
  trusted.

---

## 4. Phase 0 architecture (what we're building right now)

Three containers on Zeenie, wired together:

```
┌──────────────┐   IMAP/SMTP    ┌───────────────────────────┐
│  mailserver  │◄──────────────►│  agent-worker (container) │
│ docker-      │  agent1@…      │  inbox loop (poll IMAP)   │
│  mailserver  │                │     ↓                     │
│  boss@…      │                │  agent_loop()  (DeepSeek, │
│  agent1@…    │                │   full shell in workspace)│
└──────▲───────┘                │     ↓                     │
       │                        │  reply via SMTP + summary │
┌──────┴───────┐                └───────────────────────────┘
│  Roundcube   │  ← user sends/reads mail here (webmail in browser)
└──────────────┘
```

**The loop:** user emails `agent1@agents.local` from Roundcube → worker polls IMAP → runs
`agent_loop(task)` in its workspace (full shell, container-bounded) → replies via SMTP with the
result + what it built + how it tested it.

---

## 5. Environment facts (verified — trust these)

**Runtime target = Zeenie (the user's 2nd laptop).** Everything runs there; you drive it over SSH.
- Connect: `ssh zeenie` (alias in `~/.ssh/config`) → `192.168.0.105`, user `jianm`, key
  `~/.ssh/id_ed25519`. HAND-ASSIGNED STATIC, not a DHCP reservation: the NETGEAR C6300BD
  could not hold reservations for this many devices, so the pool was cut to .2–.99 and every
  box given a fixed address at .100+. It will not drift across a reboot.
- **A second runtime box exists**: `ssh murphy` → `192.168.0.104`, `LAPTOP-MURPHY`, Win11,
  user `jianm`, same key, admin over SSH. Blank — nothing of ours runs there yet. It is the
  box to reach for when the fleet should span two machines. Same cmd.exe caveat as Zeenie.
  Its Wi-Fi profile is already pinned Private and its OpenSSH firewall rule set to all
  profiles, so it survives the reboot failure described below. ZEENIE IS NOT PINNED.
- Rest of the LAN, all static: elitebook .100 (Ubuntu 26.04, user `jay`), hp-tiger .102/.101,
  lenovo .103.
- **Remote default shell over SSH is cmd.exe**, NOT bash. Chain with `&` / `&&`; no `head`, `;`,
  or unix pipes. For anything richer: `ssh zeenie 'powershell -NoProfile -Command "..."'`.
- Docker Desktop **v29.5.3**. **After a Windows reboot on Zeenie, SSH breaks two ways:** (1) Wi-Fi
  profile resets to **Public** → firewall silently drops port 22 (ssh *timeout*, not *refused*;
  ping failing is normal — Windows blocks ICMP by default, NOT evidence the host is down). Fix
  (admin PowerShell on Zeenie): `Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory
  Private`. (2) **Docker Desktop does not auto-start** — the user must click its icon; until then
  `docker` over SSH fails with `cannot find ... dockerDesktopLinuxEngine`.
- Zeenie already runs a **Kind cluster `learn`** (`kindest/node`, k8s API on 6443) — this is
  **Kubernetes, not OpenShift** (the user calls them "OCP containers" but drive with `kubectl`,
  not `oc`). This Kind cluster is where the fleet eventually graduates (step 2). It also has a
  stopped data-lakehouse lab (kafka/debezium/iceberg/nessie/dremio/superset/minio/opensearch/
  airflow/spark) — unrelated to this project.

**DeepSeek:**
- Endpoint `https://api.deepseek.com/chat/completions`, `Authorization: Bearer $DEEPSEEK_API_KEY`.
- Model `deepseek-chat` (cheap; deprecated alias but still works; current models are
  `deepseek-v4-flash` / `deepseek-v4-pro`). Keep it env-configurable.
- Cost is tiny (input ~free, output ~$1.10/M; ~4.4M tokens ≈ $0.05). Running several agents is
  affordable — the whole point of the thesis.
- **NEVER print or expose the API key.** The user provides it themselves (e.g. into a `.env` on
  Zeenie). It lives in the Windows env on the main laptop; confirm/provision it on Zeenie without
  echoing it.

**The `agent.py` brain (source in `../LLM_API_call/agent.py`) — reuse, don't rebuild:**
- ~200-line loop; tools: `read_file`, `list_dir`, `run_bash`, `write_file`; dispatch by name.
- `run_bash` prefers Git Bash, `stdin=subprocess.DEVNULL`, `timeout=300`. **HARNESS GOTCHA:**
  `run_bash` is **synchronous** → any long-running server (vite/express/`http.server`) **BLOCKS
  the loop** until the 300s timeout. For the worker, servers must be **backgrounded or
  built-but-not-run** (the user hit this live with a tic-tac-toe `http.server`).
- Safety lives in the harness: `MAX_STEPS`, permission policy (`CONFINE_TO_ROOT` auto-approves
  inside root), errors-as-text (a failing tool returns its error so the model self-heals).
- System prompt carries machine facts: use `python` not `python3`; Node 24 + global `tsc`; libsql
  (`@libsql/client` + `drizzle-orm/libsql`) NOT better-sqlite3 (no native build tools); don't
  start long-running servers.

---

## 6. How the user (Jianmin) works — match this

- Experienced **TS/React dev learning AI-agent internals**. Explains best via analogies to his
  world (React/DOM, TS declarations). Likes **predict-then-observe** experiments.
- **"Verify, don't trust" is his discipline and his edge.** Green build ≠ working. Always read the
  result and exercise the real flow; report failures plainly. Design the fleet to preserve this
  (self-verify + report for approval — see §3).
- Prefers a **concrete MVP over boiling the ocean**; likes to watch it run and iterate.
- Cost-conscious — DeepSeek cheapness is load-bearing to the thesis.
- Related project memory lives under
  `C:\Users\jianm\.claude\projects\c--Users-jianm-DEV-AI-Learning-LLM-API-call\memory\`
  (see `zeenie-remote-laptop.md`, `thesis-proven-cline-deepseek.md`).

---

## 7. Where we are RIGHT NOW / next steps

**Updated 2026-08-09. TWO AGENTS, `dev/agent1` and `dev/agent2`, running on Zeenie.** Phase 0
is ancient history; phases 1 and 2 of [docs/Fleet-Design.md](docs/Fleet-Design.md) are
essentially done:

| what | where | state |
|---|---|---|
| identity derived from TENANT + AGENT_NAME | `fleet_identity.py` | D1 done |
| a task is an envelope, mail is one transport | `agent_envelope/inbox/outbox.py` | D3 done |
| who is asking, resolved before any handler runs | `agent_principal.py` | D6 done |
| spend ledger + configurable ceilings + kill switch | `agent_budget.py` | D7 done |
| memory is a git repo OUTSIDE the container | `agent_memory.py` | D5 done |
| a second agent | `docker-compose.yml` + `provision-agent.ps1` | done |

**The cattle test passed on real hardware.** agent1's container *and* its 821MB workspace
volume were destroyed and recreated; all 53,029 bytes of memory came back byte-identical from
the git remote. That is the property everything else rests on: the container is disposable,
the memory is not.

**Adding an agent is six lines of compose plus `provision-agent.ps1 <name>`.** It comes up
already knowing what the others learned — tenant memory is shared, `AGENT-ASSETS.md` is not.

Still open in phase 2: the preview router (no agent should publish a host port) and addressing
apps as `<tenant>/<app>` instead of a slot integer. Both touch live deployments, so both wait
on a decision from Jianmin rather than on code.

**Ceilings are $20/task, $150/day, $500/fleet**, set high deliberately. Measured: a trivial
question ~$0.01, a real 130-step build that shipped an app ~$0.50.

<details>
<summary>Historical — Phase 0, 2026-08-01</summary>

Phase 0 steps 1–5 were **DONE and deployed** (2026-08-01). The stack runs on Zeenie at
`C:\Users\jianm\autonomous-agents`; step 6 (end-to-end build task) was exercised the same day.

| # | step | state |
|---|---|---|
| 1 | brain, direct-to-DeepSeek — `agent_brain.py` | done, smoke-tested against the real API |
| 2 | inbox loop — `agent_worker.py` | done, mail path tested against a fake SMTP |
| 3 | `Dockerfile` (python 3.12 + node 22 + tsc + git) | done, built on Zeenie |
| 4 | `docker-compose.yml` (DMS + Roundcube + worker) | done, all three healthy |
| 5 | deployed to Zeenie, `.env` + both mailboxes provisioned | done |
| 6 | end-to-end task by email | **PROVEN** — see below |

**The first real task worked (2026-08-01).** `boss@` emailed "Build a tic-tac-toe web app" →
agent1 picked it up within 20s, built it in `/workspace/task-0001-…` over **20 steps / 19 tool
calls in ~2 minutes**, and replied with `index.html` + `test.js` (20 assertions, green). Two
things worth knowing:
- **The self-verification beat works, honestly.** Its CAVEATS admitted it could not run a real
  browser, and confessed a test-data bug it had made and fixed mid-run. That is exactly the
  behaviour §3 was designed to preserve.
- **Then we verified it anyway, and it held.** An *independent* DOM-mock check (not the
  agent's) exercised win → freeze-after-win → occupied-cell-ignored → draw → reset: all pass.
  So this app did **not** hide a bug, breaking the streak — but keep checking; that discipline
  is the point.
- The model asked for a nonexistent `edit_file` tool at step 11, got `ERROR: no such tool`
  back as text, and immediately self-healed with `sed`. Errors-as-text earns its keep.

</details>

Roundcube: `http://192.168.0.105:8080`, log in as `boss@agents.local`. Mailbox passwords live in
`.env` on Zeenie (gitignored) and are written there by `provision-agent.ps1` — do not
hand-write them, or the mailbox and the file drift apart.

**Three deployment gotchas that cost real time — do not rediscover them:**
- **`docker pull`/`build` over SSH ALWAYS fails on Zeenie** with `error getting credentials —
  A specified logon session does not exist`, even for public images. Docker Desktop's CLI goes
  through `docker-credential-desktop.exe`, which needs the Windows credential vault; SSH is a
  network logon and has none. Emptying `auths`, deleting `credsStore`, a clean `--config` dir,
  and disabling CLI hooks were **all tried and all failed.** Fix: run docker from the
  logged-in interactive session via `scripts/deploy-zeenie.cmd` +
  `ssh zeenie "schtasks /run /tn agents-deploy"`. Non-registry commands (`up` on cached
  images, `ps`, `logs`, `exec`) are fine over plain SSH.
- **Never bind-mount docker-mailserver's data/state onto a Windows path.** Postfix's
  `postscreen` dies with SIGSEGV on a missing `postscreen_cache.db` and port 25 silently stops
  accepting mail while IMAP still looks fine. Use named volumes; bind-mount only
  `./config/dms/` (which is also what makes the accounts survive a `compose down`).
- **DMS won't start Dovecot until at least one account exists** and it gives you a 120s window
  before it shuts itself down. Create the mailboxes during that window on a first-ever start.

---

## 8. The review gate (added 2026-08-01, after Phase 0 worked)

`agent_validator.py` sits between the worker and the outbox. **Nothing is emailed until a
reviewer signs it off** — or until 3 rounds are spent, at which point it sends anyway with the
objections banner-ed on top (the user's call: a task that silently vanishes is worse than one
that arrives flagged, and it keeps the human as the last word).

- The reviewer **is `agent_loop` with a different system prompt**. No new machinery.
- **Fresh context** — it never sees the worker's message history, so it can't inherit its
  rationalisations. Same reason you don't review your own PR.
- **Its own tools**, same workspace: it re-runs the tests and recomputes the numbers itself. A
  reviewer that can only read the summary is a rubber stamp.
- On FAIL the objections go back into the worker's **existing** conversation (`agent_loop`
  now takes `messages=` to resume), so it fixes rather than restarts.
- **`validator1@agents.local` emails its own sign-off** when it passes, threaded under the
  worker's reply — a visibly separate voice, listing what it re-ran and what it could NOT
  verify. On a failure it stays quiet (the objections already ride on the worker's email).
- Guards, because a gate fails in both directions: an unparseable verdict counts as FAIL, a
  crashed reviewer counts as FAIL, and it is told not to block on style or on work nobody
  asked for. `VALIDATION_ROUNDS=0` disables the whole gate.

**What it actually does and does not catch** (measured by fault injection — `tests/`):
- **CATCHES, reliably: fabricated verification.** Given the real bad reply claiming "I verified
  that after exactly 11 months $10,000 reaches $20,000", it recomputed ($10,660.71), noted that
  *no command in the record performs that check*, and rejected it. Twice, reproducibly.
- **DOES NOT catch: a convention you never stated.** It kept endorsing "9 years 11 months"
  while printing $19,980.06 at month 119 and $20,096.61 at month 120 in the same paragraph.
  Prompting it harder ("substitute the answer back into the question") did NOT change this.
  The lesson is not "the reviewer is bad at maths" — it computed everything correctly. The
  question was genuinely ambiguous (continuous doubling time vs. first whole month at which
  you hold the money) and it picked the other reading. **Telling a model to apply a rule is
  not the same as it applying the rule.** The candidate fix, parked and untried: require
  ambiguity to be *disclosed* ("9.93 years continuous; $20,000 in hand at month 120") rather
  than trying to make it guess the intended convention.

**Refinements from the first evening of real use (all deployed and holding):**
- **`---EMAIL---` marker.** Three prompt revisions FAILED to stop backstage narration ("The
  numbers check out. Here's the write-up...", "You're right, the reviewer caught...") reaching
  the recipient. Fix: the agent marks the boundary itself and `agent_brain.strip_preamble` cuts
  there — **fail-open**, so no marker means the whole answer is sent untouched (a leaked
  preamble is a wart; a truncated reply is a defect). Worked first try. The lesson: giving it a
  sanctioned place to be verbose beat three attempts to forbid the verbosity.
- **Attribution.** Every transcript entry is stamped `by` at execution time, and the report
  groups them under `[agent1]` / `[validator1]` headers. Before this the merged list read as
  all-worker — it fooled Claude into crediting agent1 with the reviewer's work, and would have
  fooled the user. Consistently the reviewer runs 4-10x more commands than the worker.
- **Three checklist rules, each added after a real miss:** check the answer against ITSELF (it
  caught a one-cent prose/table mismatch on the next run); check each example actually
  demonstrates its point ("growth accelerates — the next $10,000 arrives in the 10 years after
  that" describes *linear* growth and refutes its own thesis); reject unfilled placeholders
  (`[Your name]` reached the inbox).
- **Markdown is banned** in both agents' output — no renderer in a mail client.

**The recurring lesson, learned the hard way three times:** *telling a model to apply a rule is
not the same as it applying the rule.* The Markdown ban worked immediately; the preamble ban in
the same deploy failed outright. Instructions shift the odds. Anything that must ALWAYS hold
belongs in the harness (like the marker), not the prompt.

**Tests live in `tests/` and need no mail server:** `test_gate.py` (stubbed brain: pass-first,
fail-then-fix, never-passes, two-sender emails, threading), `test_mailflow.py` (MIME decode,
threading headers, self-mail loop guard), `test_faultinject.py` (**spends real API calls** —
feeds the reviewer a known-bad reply and asserts it rejects it; this is the only test that
proves the gate isn't a rubber stamp).

---

## 9. Apps that actually run (added 2026-08-01, proven end-to-end)

An agent that builds a web app you cannot open is a demo. Three things had to change:
- **Compose publishes `3000-3009`** on the worker, and the harness assigns one port per task,
  injected into the task text as machine notes. It picks the **first port with nothing
  listening** (`agent_notes.free_port` — a TCP *connect*, not a bind: the agent's servers live in
  this same container on `0.0.0.0`, so a bind test says "free" until you steal a live app's port).
  Only when all ten are busy does it fall back to `3000 + seq % 10` and `lsof -t -i:PORT | xargs
  -r kill -9`. It used to *always* rotate and kill — fine for throwaways, fatal once apps are
  maintained: task 22 would have killed task 12's booking app. See §10.
- **The prompt flipped from "never start a server" to "background it and LEAVE it running"**,
  with the URL reported in the reply. `nohup cmd > log 2>&1 &` — the redirect is load-bearing;
  without it the backgrounded child holds the capture pipe and `run_bash` hangs to its timeout.
- **The reviewer curls it**: a URL that does not answer, or serves something other than what was
  promised, is a FAIL. It is told not to settle for HTTP 200 — check the body contains the
  thing (it ran `curl -s localhost:3001/ | grep -c 'class="cell"'` → 9).

**Proven 2026-08-01:** "Build me a tic-tac-toe web app… send me the link" → agent1 wrote
html/css/js + a Node static server, backgrounded it, reported `http://192.168.0.105:3001`;
validator1 independently fetched it from both inside and outside; the user played it in a
browser. Claude then drove the real `game.js` through its own DOM harness: win, freeze after
win, taken-cell ignored, nine-move tie, New Game keeps the score, Reset Scores clears it — all
correct.

**Gotchas found doing it:**
- The worker burned its full 300s timeout on `curl … | head -20` — `head` closes the pipe,
  curl takes EPIPE, the pipeline strands. The *reviewer* used `--max-time 5` unprompted; the
  worker did not. Worth putting in the prompt.
- **Timeouts do not kill the server.** Python kills only the direct child (bash); the `nohup`'d
  node process is orphaned and keeps serving. The hang cost 5 minutes and cost the app nothing.
- Apps die on `compose up`/redeploy, and the port range holds 10 — the 11th task evicts the 1st.
  The agent is told to say this in its reply, and it does.
- **What each layer catches:** agent1 builds and syntax-checks; validator1 proves it is
  reachable and structurally right; nobody but the human exercises what it actually *does*.
  That gap has held on every task so far — the reviewer could drive the DOM (Claude's harness
  proves it is possible without a browser) but has never chosen to.

---

## 10. The agent's own memory (added 2026-08-02, Jianmin's idea)

> **Superseded in part (2026-08-09, D5).** The three files, and the "the agent owns them, the
> harness never writes them" rule, are all still exactly right. What changed is WHERE they
> live: a git repo outside the container, not `/workspace`. `AGENT.md` and `AGENT-AVOID.md` are
> now shared by every agent in the tenant; `AGENT-ASSETS.md` is private to each; and a fourth
> file, `FLEET.md`, is operator-only — no agent can write it. See `agent_memory.py`.

**His framing:** "We don't have to re-invent the wheel. We can do similar things like Claude
code… tell the agent this is your environment, you own and maintain this environment. Ask the
agent to create two files: `agent.md` — every prompt he needs to read this doc; `agent-assets.md`
— every app he builds, he needs to write a summary for himself for future reference." Then, when
I started to seed them: **"Both files are created by the agent himself. So he can keep adding
whatever he wants."** That constraint is load-bearing — see below. Then a third: **an
"agent-avoid-things.md" — "anything learned from using the container or building the apps,
anything that is not working and should be avoided. This is the agent's lessons learned file."**
Shipped as `AGENT-AVOID.md` for consistency with the other two.

**What prompted it (task-0013, the timezone bug report).** The email named neither the folder nor
the cause. The agent found the app unaided — `ls /workspace`, recognised the folder from the
subject line, read the source, then checked the container clock four ways (`date`, `timedatectl`,
`$TZ`, `/etc/timezone`, `Intl.DateTimeFormat()`) and diagnosed the timezone convention correctly.
Then it **copied the app into its new task folder and fixed the copy**, wrote `PORT=3003` into
`.env` while the server kept announcing 3002, and finally `pkill -f dist/src/index.js`'d both
copies at once because it could not tell its fork from the original. **Discovery was never the
problem. Identity was.** So a registry is worth building — but not the schema I had drafted.

`agent_notes.py` + prompt changes. Three files, split by **when you reach for them**:
`/workspace/AGENT.md` (how this machine works — read while planning),
`/workspace/AGENT-ASSETS.md` (what exists, where, on which port, how to start it — read while
orienting), `/workspace/AGENT-AVOID.md` (what burned it and what works instead — scanned before
acting). They live in one `FILES` table in `agent_notes.py`, so a fourth is one row.
- **The harness never writes, seeds or parses them, and imposes no format.** They are notes to
  itself; depending on their structure would turn them into a schema it has to serve.
- **Injected, not requested.** All three are pasted verbatim into every task (capped at
  `NOTES_MAX_CHARS=8000`, told where to read the rest). Principle 1 again — "read AGENT.md first"
  is an instruction the model can silently skip, and this is exactly what it does not know it is
  missing.
- **Ports come from the OS, not the file** (§9). What a port is *for* is in the notes; whether it
  is *busy* is a fact about the machine. That is what lets the harness stay out of the file.
- **The reviewer enforces upkeep** — when the task built/changed/deployed/retired something
  durable, it reads `AGENT-ASSETS.md` **from disk** (the copy in the task text is the *before*
  state) and checks the path exists, the port is really listening, the start command really
  starts it. Stale entry = FAIL; formatting = explicitly not a fail.
- The prompt also says: work on the thing **where it already lives**, do not fork it into the new
  task folder; and if it is already serving on a port, keep that port.
- The worker logs `notes UPDATED / unchanged` with before/after sizes each task.

**Not solved:** nothing reconciles the notes with reality except the agent. Container restart
kills every server while the file still claims they run; the reviewer only checks assets the
current task touched.

---

## 11. Delivery to Kubernetes (added 2026-08-02, Jianmin's idea) — HALF BUILT

> **Still accurate, with one addition (2026-08-09).** `agent-<app>` is a FLEET-WIDE name, so two
> agents asked to build "a todo list" resolve to the same repository. `ship_app` now records the
> owning agent and refuses a push from anyone else. The proper fix — addressing apps as
> `<tenant>/<app>` — is D4 and is not built.

**His framing:** "Going forward, any applications need to be checked into GitHub, the GitHub CI
auto kicks off, the Harness agent does the CD. Let the agent understand we have Kubernetes pods.
Each app should deploy to a pod." Choices he made when asked: **one repo per app**, **one Harness
pipeline per app**, build the whole chain at once.

Full write-up in [docs/Autonomous-Agents-Design.md](docs/Autonomous-Agents-Design.md) §9. The
short version for future-you:

- The other half already existed: `../React_Typescript/github_ci_cd` is a complete SDLC mockup
  (Actions → ghcr → Kafka → governance pod with a HUMAN APPROVAL GATE → Harness → kind). Built for
  one app. §9 connects the fleet to it. **Read that project's `docs/Calculator_EPLX_SDLC_Journey.md`
  before touching any of it.**
- **`df360-net` is a USER account, not an org** → GitHub allows no org-level runners → the Zeenie
  runner is permanently repo-scoped to `calculator-ci-demo`. This is why agent CI is
  GitHub-hosted and a watcher polls, instead of copying the calculator's self-hosted-runner push.
- New files: `agent_delivery.py` (naming, 3-ports-per-app, CI + manifest templates), `ship_app.py`
  (the agent's only GitHub route — on PATH as `ship_app`), `agent_app_proxy.py`, and
  `github_ci_cd/governance/harness_apps.py` (clones `deployweb` per app via the Harness API).
- **Ports: app slot N gives 3000+N local preview, 30000+N NodePort, 31000+N browser.**
- **PROVEN 2026-08-02:** a hand-written probe app went push → CI green → ghcr → Harness → 2 pods →
  `http://192.168.0.105:31000` in a browser. Kept as a reference deployment.
- **NOT wired yet:** ci-watcher pod, governance per-app routing, the approval gate for agent apps.
  That probe was deployed by calling `harness_apps.py deploy` directly, bypassing all three.
- **Needs Jianmin:** `GITHUB_TOKEN` in `.env` on Zeenie, PAT with `repo` AND `workflow` scope.
  Without `workflow`, pushes touching `.github/workflows/` are rejected with a misleading error.

**Traps that cost real time — all now handled in code, do not rediscover:**
- `<+artifact.image>` in a manifest is NOT resolved → pods `InvalidImageName`. Harness resolves
  expressions in the **values** file; the manifest must use `{{.Values.image}}`.
- `imagePullSecrets: [ghcr-cred]` is required and secrets are namespaced (copied to `agent-apps`).
- Harness returns **HTTP 400 + RESOURCE_NOT_FOUND_EXCEPTION**, not 404, for "does not exist".
- Harness identifiers reject hyphens; everything else in the system uses them. See `ident()`.
- Kafka/redpanda-console are in the lab's pause runbook and were stopped — the CD chain needs them.

---

**DECISION (2026-08-01):** stay on **Docker Compose while the agent design is still moving**;
graduate to Kind pods only once it is settled. Rationale: iteration on Compose is edit + rebuild
in ~1 minute, and Kubernetes would tax every experiment with manifests, image loads and — the
real cost — the loss of "just publish a port". Kind's `extraPortMappings` are fixed at cluster
creation, so serving an agent-built app from a pod needs an ingress or another proxy container.
Feature work first, pods second.

**Roadmap after the MVP works:**
- Add **Jira** (task source + status transitions) + **Confluence** (agent writes docs) via
  **Atlassian Cloud free tier** — do NOT self-host Jira/Confluence (Data Center = licenses + huge
  JVMs + DB; overkill).
- **Scale to N agents**; **graduate Compose → Kind pods** (k8s Secrets/Deployments in the `learn`
  cluster). This realizes the original "few containers in Kind" vision.
- Harden: long-build handling, backgrounding built apps (port-forward to reach them), per-agent
  identity/routing, and the human approval gate before an agent "ships."
