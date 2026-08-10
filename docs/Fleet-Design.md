# Autonomous Agents — Fleet Design

How the working single-agent system in [Autonomous-Agents-Design.md](Autonomous-Agents-Design.md)
becomes a platform that other teams can run — **potentially hundreds of agents across a company**,
on real hardware or public cloud.

The near-term deliverable is small and unchanged: **two agents on Zeenie**. What changes is the
standard those two are held to. Every decision in phases 1–2 is justified against the end state,
because the point of this document is not a bigger plan — it is knowing **which decisions are
cheap now and expensive later**.

*Status: 2026-08-09. **Phase 1 complete; phase 2 all but done.** Built, tested and running on
Zeenie as **two agents**: D1 (`fleet_identity.py`), D3 (`TaskEnvelope`, `agent_inbox.py`,
`agent_outbox.py`, `run(envelope)`), D6 (`agent_principal.py`), D7 (`agent_budget.py`) and D5
(`agent_memory.py`). The cattle test passed on real hardware — agent1's container and its 821MB
workspace volume were destroyed and it came back with all 53,029 bytes of memory byte-identical.
`dev/agent2` then came up from six lines of compose knowing everything agent1 had learned.
Outstanding in phase 2: item 7 (preview router) and the rename half of item 8, both of which
touch live deployments. D2 and D8 are still design. It supersedes §14 (Roadmap) of the
main design doc and corrects two of its assumptions — see §12. Companion documents:
[Autonomous-Agents-Design.md](Autonomous-Agents-Design.md) (the system as it exists),
[agent-reminder.md](../agent-reminder.md) (context handoff).*

---

## 1. Where this came from

The first version of this plan optimised for a personal fleet on one laptop, and about a third of
it was wrong. The reasoning that produced the worst error is worth recording, because the same
trap is available on every future decision:

> *Throughput is not the bottleneck — one human sends the tasks and reads the diffs — therefore
> don't build the dispatch seam.*

The conclusion (don't build a work queue) was right. The inference (therefore don't build the
seam) was backwards. **Assurance topology is a policy you can change on a Tuesday. Dispatch
topology is welded into `handle_message`'s signature, into every safety check written against
`email.message`, and into the at-most-once semantics.** Deferring a *feature* is cheap. Deferring
the *seam that makes the feature possible* is how a rewrite gets scheduled.

That is why this document is organised around one-way doors rather than around a roadmap.

---

## 2. The end state

```
   many humans                     ┌─────────────────────────────────────┐
   many teams ──mail/Jira/API──▶   │  intake: normalise to TaskEnvelope  │
                                   └──────────────┬──────────────────────┘
                                                  │ lease-based claim
   ┌──────────────────────────────────────────────▼───────────────────────────┐
   │  CONTROL PLANE   (tenant-scoped, versioned API, authenticated principals) │
   │    registry · dispatch · budget grants · approval routing · peer relay    │
   └───┬─────────────────────────┬─────────────────────────┬──────────────────┘
       │                         │                         │
  ┌────▼─────┐             ┌─────▼────┐              ┌─────▼────┐
  │ acme/    │             │ acme/    │              │ beta/    │    agents are CATTLE
  │ agent-01 │             │ agent-02 │              │ agent-01 │    nothing durable inside
  └────┬─────┘             └─────┬────┘              └─────┬────┘
       │  git clone/push         │                         │
       ▼                         ▼                         ▼
   acme-knowledge (git)      acme-knowledge          beta-knowledge   ← MEMORY is the pet
   fleet-knowledge (ro, operator-only, via PR)

   previews: no host ports. one router, name-addressed.
   apps:     addressed <tenant>/<app>. no slot integers.
```

Two sentences carry most of it:

- **The agent becomes cattle; its memory becomes the pet.**
- **Mail stays the human interface, permanently. It stops being the machine transport.**

---

## 3. What the debate settled

Constraints agreed before design, and the corrections that came out of designing against them.

| Settled | Correction found while designing |
|---|---|
| Split memory into shared and personal | **Splitting does not reduce prompt cost** — same bytes, N copies on the wire. The cost levers are the cap and prefix caching. The split's payoff is learning transfer and single-point maintenance. Don't sell it as a cost fix or nobody builds the cap. |
| No LLM in the health-check path | Holds, and strengthens: at multi-tenant scale a hallucinated *routing* decision is a cross-tenant data leak, not a quality bug. |
| The registry is derived, not maintained | Holds for registry **content**. Registry **identity schema** is a one-way door and cannot be deferred — see D1. |
| Steal A2A's card shape, skip the protocol | Holds *now*; A2A earns its place at the first agent this project did not write. Three free things keep it a short hop — see §6. |
| Agents are pets, so StatefulSet not Deployment | **Reversed.** With memory external, agents are cattle and **Deployment beats StatefulSet**. The constraint was right *given* pets. |

### The definition that reconciles "derived" with "heartbeat"

A heartbeat is a record, and records were supposed to be forbidden. The rule that resolves it:

> **Derived means the fact expires unless reality keeps re-asserting it.**

A heartbeat row with a TTL qualifies — nobody has to remember to delete it. A hand-edited
`agents.yaml` does not.

---

## 4. One-way doors

Ranked by cost of being wrong. These are the decisions the two-agent build exists to get right.

### D1 — Identity and naming

`agent_id = <tenant>/<name>` (`dev/agent-01`, later `acme/web-03`), **injected by the platform;
the agent never chooses or asserts it**. One module, `fleet_identity.py`, holds pure derivations:
mailbox, memory path, k8s namespace and labels, GitHub org, registry key, budget key, audit key.

*Cost later:* the id is written into provisioned mailboxes, git commit authorship, k8s labels,
budget ledgers, audit logs and approval records. Renaming across hundreds is a migration with
irreversible history.

*Now:* rename to `dev/agent-01`. **Delete `AGENT_NAME` and `AGENT_ADDRESS` as independent env
vars** — they are derivations, and letting them be independent inputs is exactly what allows a
mailbox and a workspace to be paired wrongly.

*Bonus:* **GitHub org-per-tenant dissolves the repo-collision problem entirely.** Two tenants can
both own `agent-todo`. The repo-topic ownership scheme considered earlier was a workaround for
`df360-net` being a user account rather than an org — a Zeenie artifact, not a design.

### D2 — The tenancy unit is the team

One tenant = one team. An agent belongs to exactly one tenant for life. Not per-project (agents
accumulate cross-project memory within a team, and that is the value); not per-agent (then there
is no shared memory and no approver group).

Enforced in **five independent places, by five different mechanisms** — deliberately, so that no
single bug is sufficient:

1. Kubernetes namespaces (`agents-<t>`, `apps-<t>`) — the API server refuses.
2. **Credential scope** — each agent holds only its tenant's GitHub token, registry and DB creds.
   *This is the one that actually bounds a compromise.*
3. NetworkPolicy between namespaces.
4. Control-plane authz, tenant taken from the authenticated principal.
5. Memory scoping (D5).

### D3 — TaskEnvelope, and leases instead of consume-then-run

```python
TaskEnvelope(
  envelope_version, task_id, tenant, agent_id,
  source,        # "email" | "jira" | "api"
  requester, reply_to, thread_id, hops, purpose,
  state,         # A2A names: submitted/working/input-required/completed/failed/canceled
                 # plus ours: abandoned (lease expiry)
  lease_until, deadline, budget_usd,
  body, attachments,
)
```

Today's at-most-once is *consume, then run* — `\Seen` is set before the work starts. That is
correct for a pet that never dies mid-task. **Cattle die routinely; a rolling upgrade is exactly
that.** So dispatch becomes claim-with-lease: claim with an expiry, renew while working, release
on completion. A dead agent's lease expires and the task becomes `abandoned` — **visible, not
vanished**, which is what principle 7 requires.

*Cost later:* every guard in `handle_message` is currently written against `email.message` — the
loop guard, the sender allow-list, threading, dedupe. Changing transport with those in place means
rewriting the safety layer, which is the last code anyone should want to rewrite.

This retires `flock` as the *primary* at-most-once guard (a pets-era mechanism: one volume ⇒ one
consumer). Keep it as a cheap backstop against a double-started container; demote it from
load-bearing.

### D4 — Address by name; the slot integer dies

`NODE_PORT_BASE + index` is a flat global integer namespace with 20 entries and no tenancy — two
teams both want slot 3. Apps become `<tenant>/<app>`; the NodePort becomes an implementation
detail the cluster assigns and the proxy discovers by listing Services, which `cluster.list_apps()`
already does.

*Delete:* `ports_for`, `index_of_node_port`, `claimed_indexes`, `suggest_index`, `slot_deployed`,
`ship_app._index_for`.

**This is the cheapest one-way door to walk back today and one of the most expensive later.** An
afternoon at 5 apps; a migration project with a data-cleanup phase at 500. Note that the earlier
proposal to *stripe* slots by agent ordinal would have welded it shut.

### D5 — Memory: external, versioned, three scopes

Memory is a **git repository with a remote**, cloned at agent start, written via `note_write`
(flock → commit → push). Not a docker volume.

One change delivers: durability independent of the container (⇒ cattle), tenant scoping,
attribution and audit (`git log`), rollback of a bad shared lesson (`git revert`), human
editability in a browser, and substrate independence (`git pull` works on Compose, in kind, and in
cloud). **Everything else in this plan gets easier once it is true, and almost nothing else does
that.**

| Scope | Location | Writer |
|---|---|---|
| `global` | `fleet-knowledge` | **operator only, via PR — no agent writes this** |
| `tenant` | `<tenant>-knowledge` | any agent in the tenant, via `note_write` |
| `personal` | `<tenant>-knowledge/agents/<name>/` | that agent only |

The global tier holds what is true for every tenant ("run `python` not `python3`", "`curl | head`
strands on EPIPE"). An agent *physically cannot* write it, which is the mechanism that prevents
one tenant's secret leaking into fleet-wide knowledge. Boring, airtight, just repo permissions.

Today's three files map cleanly: most of `AGENT-AVOID.md` (25,791 chars of machine facts) is
tenant-or-global; `AGENT-ASSETS.md` stays personal; `AGENT.md` splits between environment facts
and working conventions.

**Built as `agent_memory.py`, with two deliberate departures from the above.**

*No `note_write` tool.* The agent keeps writing plain files with ordinary tools; the **harness**
commits and pushes at the end of every task. Durability must not depend on the model remembering
to call something — the same reason the budget check lives inside `call_llm` and not in the
prompt. It also preserves the property the notes mechanism actually runs on: the agent owns these
files and no format is imposed on them. A tool call is a format.

*No `flock`.* flock coordinates processes on one machine, and the concurrency that matters is two
agents in different containers pushing to one tenant repo. That is pull-rebase-and-retry plus a
`*.md merge=union` driver, so two agents appending to the same notes file merge instead of
conflicting. flock would have looked correct at N=1 and failed at exactly the moment N=2 made it
necessary.

Scope is assigned by the harness, not the agent — "is this lesson mine or the fleet's?" is a
question about blast radius. `AGENT.md` and `AGENT-AVOID.md` are tenant; `AGENT-ASSETS.md` is
personal, because it is a list of things this agent *runs*, and shared it makes "fix the booking
app" ambiguous between two agents who both believe they own it.

*Failure posture:* unreachable remote with a local clone → work offline, commit locally, push with
the next task. Unreachable remote and **no** local clone → refuse to start. An agent with amnesia
does not do less work, it does the *wrong* work — it redeploys apps it already runs.

Two bugs worth recording, both found by tests written against real git rather than a mock:
`-c merge.union.driver=true` does **not** enable the union merge — `union` is a built-in, and
defining `merge.union.driver` *replaces* it with a custom driver running `true`, which exits 0 and
silently discards the second agent's notes. And a clone that `git init`s before it fetches leaves a
valid-looking empty repo behind when the fetch fails, so the next start finds "memory" and runs
with none — arriving at the exact amnesia the module refuses, by the failure path. Cloning is now
atomic via a staging directory.

### D6 — Principal resolution, and the rule

Every inbound envelope and control-plane request resolves to a `Principal{agent_id, tenant}`
**before any handler logic runs**. Phase-1 implementation: per-agent HMAC secret. Later: projected
ServiceAccount token verified by `TokenReview` (the estate already reads SA tokens in `cluster.py`),
or SPIFFE.

> **No security or tenancy decision may read a field the sender controls.**

That rule kills the `X-Agent-Id` header as an enforcement mechanism. The header is the *transport
encoding* of `envelope.hops` and `envelope.agent_id`; the check reads the envelope, and the
envelope is attested. At N=2 on a LAN the difference is invisible — which is precisely why it must
be settled now, while it costs nothing.

**Built as `agent_principal.py`.** `admit(envelope)` resolves a `Principal{kind, principal_id,
tenant, attested}` and returns a **sanitised copy** of the envelope: anything the sender could
have written and did not sign for is put back to its default before the worker ever sees it. A
message that *claims* to be an agent and cannot prove it is **refused, not downgraded to human** —
downgrading turns a spoof into an accepted task, and no real person's mail client sets
`X-Agent-Id`. Refusals are logged and dropped, never answered: a refusal that replies can be
aimed at a third party, and if the cause was a loop, the reply is another lap.

Two loop guards, because they fail differently. `hops` catches machine traffic and is only ever
non-zero on an attested envelope. **Thread depth** catches loops that carry no counter at all — a
forwarding rule, a mailing list, an out-of-office responder — because `References` grows by one on
every pass and never shrinks. A hop counter alone would not have seen those.

`secret_for(agent_id)` is the seam: one fleet-wide HMAC key today, per-agent keys from the control
plane next, `TokenReview` or SPIFFE after that, with no call site changing. Replay is handled where
it already was, by the Message-ID dedupe in `agent_inbox`.

**The sending half is `agent_peer.py`,** and the split it enforces is the whole design:

| the agent chooses | the harness decides |
|---|---|
| who to write to, why (a purpose from a fixed table), what to say | hop count, signature, thread, **the CC to the operator** |

`hops` is read from the INBOUND envelope, which the model never sees and cannot reach from a tool
argument. The operator CC is enforced one level lower still, in `agent_outbox.send_mail`, so it
covers *every* message the fleet sends — task replies and reviewer sign-offs included, not just
peer mail. Neither is a parameter of the tool, so there is nothing to validate and nothing to
forge; the tests pass each and get `TypeError`.

Two guards that hops alone cannot provide. A **per-task send cap**, because hops bound a chain's
depth and say nothing about its width — one confused run can send a peer fifty valid hop-1
messages. And a refusal to write to **the agent who sent the current task**, whatever the purpose:
their message *arrived as* this task, so the finished answer already goes back to them, and a
second message forks the conversation into two tasks per leg. That one was found live, doubling a
ten-round exchange.

*Measured on a real ten-round exchange:* the hop limit had to go from 3 to 100, because 3 was
chosen as a loop guard and was being asked to serve as a conversation-length policy. The spend
ceilings are the actual bound and were untouched.

Agent-to-agent messages are **relayed and attested by the control plane**, not sent peer-to-peer.
That puts authentication, the audit log and hop enforcement in one place.

### D7 — Budget ledger schema and grants

Ledger line: `{ts, tenant, agent_id, task_id, requester, model, prompt_tokens, cached_tokens,
completion_tokens, usd}`. Append-only, shipped to the control plane. Ceilings at four levels:
task, agent, **tenant**, fleet.

Enforcement stays in the agent (fail closed *before* the call) against a **lease-based grant** —
"you may spend $0.50 on task X" — so tenant budgets are enforced without a synchronous check per
call. `BudgetExceeded` subclasses `LLMError`, which means the existing `except LLMError` path in
`agent_loop` already does the right thing: honest answer, gate skipped, human emailed. *The correct
failure path is already written.*

*Cost later:* chargeback history cannot be reconstructed. If the first 5,000 rows lack `tenant` and
`requester`, that quarter has no attribution, forever.

### D8 — Model and provider are tenant configuration

`agent_brain.MODEL` and the API key are module-level process env. A company wants per-tenant model
choice, per-tenant keys (their billing, their data residency), per-tenant provider. Resolve from
the tenant record and thread through `call_llm`. Cheap now; touches every call site later.

---

## 5. Cheap to defer — and the seam that keeps it cheap

| Deferred | Seam |
|---|---|
| Real broker (Kafka / SQS / Temporal) | `agent_inbox.next_task()` / `agent_outbox.deliver()` |
| A2A protocol | `agent_peer.py` verb table + A2A state names + A2A-shaped card |
| Splitting the control plane into services | versioned tenant-scoped URLs + `tenant` column |
| Coordinator / router agent | routing decided *outside* the agent, carried in `envelope.route` |
| Retrieval instead of injection | `agent_notes.context_block(task_text=..., tier=...)` |
| Real workload identity (SPIFFE / SA token) | the `Principal` resolution function |
| Ephemeral preview environments | `preview.request()` returning a URL the agent never builds |
| Kubernetes / cloud scheduler | made cheap by D5 — nothing durable in the container |
| Rich registry content (skills, drift, dashboards) | the identity schema, which lands in phase 1 |
| Risk-tiered approval | `tenant.approval_policy`, default "human approves everything" |

---

## 6. Transport, previews, and A2A

### Mail is the interface, not the transport

Mail's virtues are real and must not be lost: humans already scale on it, it is auditable by
default, async, survives restarts, threading gives conversation identity for free, and adoption
cost inside a company is zero.

Mail's problems are **all machine-side**: no authentication that isn't a security system built on
SMTP, no tenancy, no lease or redelivery semantics, no backpressure, no per-tenant rate limiting,
unstructured payloads — and on Zeenie specifically, one Postfix with `SPOOF_PROTECTION=0` and
unauthenticated relay on `:25`.

| Path | Transport |
|---|---|
| human → agent | mail, Jira, Slack, web form — plural, normalised to one envelope |
| agent → human | mail |
| agent ↔ agent | **not mail** — control-plane relayed, authenticated |
| agent ↔ control plane | **not mail** — authenticated HTTP |

`handle_message` currently fuses IMAP transport, MIME→text parsing, and execution. Split into
`agent_inbox.py`, `agent_outbox.py`, and `run(envelope)`.

### Previews: the agent must never construct a URL

Today `handle_message` interpolates `http://{APP_HOST}:{port}` into the prompt, and
`agent_delivery.ports_for()` returns a `url`. Ten published host ports per agent does not survive
hundreds of agents on shared infrastructure.

Replace both with one call the agent is *told* the answer by:

```python
preview.request(app, task_id) -> {"bind_port": 8412, "url": "http://todo.agent-01.dev...:3000"}
```

The agent binds inside its own container; one router reaches it over the container network and
routes by `Host` header. On Zeenie that is a small proxy plus wildcard DNS; in cloud it is an
Ingress — same code path, different resolver. Path-prefix routing (`/p/agent-01/todo/`) is the
alternative and is worse: it breaks apps with absolute asset paths, which is most of them.

### A2A: three free things now

1. Card field names stay A2A-compatible where they overlap (`name`, `description`, `url`,
   `version`, `capabilities`, `skills[]`), plus our `source` field for provenance.
2. **Adopt A2A's task lifecycle state names in `TaskEnvelope` now.** Free today; the later adapter
   becomes a serialization change rather than a semantics change.
3. All inter-agent messages go through `agent_peer.py` with an explicit verb table, so A2A becomes
   an implementation of one module rather than a change to every call site.

Do not build JSON-RPC, streaming, push notifications or discovery. Note the coupling: A2A delegates
authentication to the transport, so D6 is also the work that makes A2A adoptable.

---

## 7. Assurance: cross-review becomes compliance evidence

Human review does not scale to hundreds of agents. If cross-review is going to substitute for part
of it, then the canary rate, reviewer-agreement rate and first-round pass rate stop being dashboard
garnish and **become the evidence that the gate is real** — recorded per-tenant, per-reviewer,
immutably, *from the first review*. Last quarter's canary results cannot be reconstructed.

Two tiers, because they see different things:

| Tier | Sees | Catches |
|---|---|---|
| in-container validator (exists today) | live workspace, can re-run and re-curl | fabricated verification, stale assets, dead URLs |
| cross-agent reviewer (new) | the pushed repo, transcript, attachments, answer | shared-context rationalisation |

**The canary is the mechanism worth most.** `tests/test_faultinject.py` already contains a
known-bad artifact. Promote it from a test asset to a weekly production probe and record
catch-rate per reviewer. It is the only mechanism that *measures* rubber-stamping instead of hoping
to prevent it.

Honest limitation: both reviewers read the same tenant knowledge, so shared blind spots are now
shared *by design*. That is a real cost of D5 and it is why the canary matters.

**The human keeps the last word on anything that deploys.** That survives at hundreds because
tenancy makes it affordable: 200 agents across 25 teams is 8 agents per approver group. What
doesn't scale is one human approving everyone's work — a routing problem, not an approval-model
problem.

---

## 8. Safety: the loop guard and the budget

The structural fix matters more than any counter. **Two verbs only:** `review-request` (emitted
only by a run a human started) and `review-verdict` (terminal — parked, never dispatched). A→B→A
becomes *unreachable*, not merely improbable. Hop count and thread depth are belts, enforced on
**envelope fields, never mail headers**.

Every new verb, forever, must state its terminal condition in that table. That is the extension
point.

Budget per D7, plus a fleet kill switch: when the fleet ceiling trips, intake returns nothing —
nothing fetched, nothing marked consumed, backlog intact — and one email goes out. Clearable by
hand at 3am without Docker Desktop running.

---

## 9. Load-bearing on assumptions that break, ranked by cost

1. **Slot integers** — `agent_delivery` (5 functions), `ship_app._index_for`,
   `agent_app_proxy.mappings`, `cluster.list_apps().slot`, the dashboard, every generated manifest,
   and the agents' own notes prose.
2. **`handle_message`** fusing transport + envelope + execution, every guard on `email.message`.
3. **`WORKSPACE_ROOT` as both memory and scratch, inside the container** — blocks cattle, blocks
   migration, blocks rolling upgrade.
4. **`AGENT_NAME` / `AGENT_ADDRESS` as independent self-asserted env** — identity by convention.
5. **`APP_HOST=192.168.0.21`** interpolated into prompt text, `agent_delivery`, `cluster.py`,
   `governance_app`, notification bodies.
6. **`GITHUB_OWNER` as one user account** — no org, hence no org runners, hence polling; and one
   global repo namespace.
7. **`agent_notes` two scopes + global `MAX_INJECT_CHARS`** — no tenant tier, no per-file cap.
8. **`agent_brain.MODEL` / API key as process env** — no per-tenant key or attribution.
9. **`.processed.json`** — local file, dies with the container, no lease.
10. **`governance_app`** — no tenancy in schema, **no identity on `/approve`** (verified: `do_POST`
    reads only `dep_id`; anyone who can reach `:8091` can approve any deploy), single replica.
11. **`harness_apps.py`** cloning a pipeline per app — should be one templated pipeline with the
    app as an input.
12. **docker-mailserver with `SPOOF_PROTECTION=0`** and unauthenticated `:25` — acceptable once
    mail is only the human interface; disqualifying if mail stays the machine transport.

---

## 10. Phases

Phases 1–2 deliver **two agents on Zeenie** and nothing more.

### Phase 1 — Reshape the primitives (still one agent; no user-visible change) — **COMPLETE**

Done while there is one agent, ~5 apps, and no history to migrate.

1. ✅ `fleet_identity.py`; `AGENT_NAME`/`AGENT_ADDRESS` become derived. *(D1)* — the rename to
   `dev/agent-01` was **dropped**: it costs a new mailbox and a changed address for the human, and
   `dev/agent1` already satisfies the only thing D1 was for, which is that the id be
   tenant-qualified from the first ledger row.
2. ✅ `TaskEnvelope` + `agent_inbox.py` + `agent_outbox.py`; `handle_message` → `run(envelope)`.
   *(D3)*
3. ✅ `Principal` resolution (HMAC) and the no-sender-controlled-fields rule; loop guard,
   allow-list, hop count and thread depth all enforced on envelope fields. *(D6)*
4. ✅ `agent_budget.py` with the full ledger schema, `usage` capture in `call_llm`, ceilings,
   `BudgetExceeded(LLMError)`, kill switch. *(D7)*
5. ✅ Move the notes block to the **front** of the prompt and measure the cache-hit rate. The
   measured answer was **not** the predicted one — see §12.

On (5): the notes are currently injected *after* the variable task text, so the 48,565 constant
characters cannot be cached as a prefix and are re-sent as novel bytes on every call — a recent
task ran 123 steps. Moving them is nearly free. **Verify the size of the win by measurement rather
than assuming the provider's cache semantics.**

*Dropped from earlier drafts:* ordinal-derived port blocks and slot stripes (they entrench D4),
`X-Agent-Hops` as the enforcement mechanism (demoted to transport encoding), `flock` as the primary
at-most-once guard (demoted to backstop under D3).

**Acceptance:** a real task runs end to end with no behaviour change, and the ledger shows tenant,
requester and cost per task.

### Phase 2 — External state, name addressing, the second agent

6. ✅ Memory → git remote, three scopes; `/workspace` becomes scratch only. *(D5)* — remotes are
   bare repos bind-mounted from the host today, so nothing leaves the laptop; repointing
   `MEMORY_TENANT_REMOTE` at GitHub is a one-line change the agent cannot detect.
7. `preview.request()` + router + wildcard hostnames; **no agent publishes a host port.**
8. Kill slot integers; apps addressed `<tenant>/<app>`. *(D4)* — **partially done.** The full
   rename touches live repos, live deployments and the Harness pipelines, so it is still
   pending. What shipped is the guard that made a second agent safe without it: `agent-<app>`
   is a fleet-wide name, so two agents asked to build "a todo list" resolve to the same
   repository and the second silently overwrites the first's running application. `ship_app`
   now records the owning agent in the repo and refuses a push from anyone else.
9. ✅ Stand up `dev/agent2` — six lines of compose, one `provision-agent.ps1` run. Nothing
   else. *That is the test that phases 1–2 worked, and it passed:* agent2 came up with 32,865
   bytes of agent1's machine knowledge already in its memory, its own empty inventory, a
   derived identity with zero conflicts, and all eight suites green — having been told nothing.
10. Cross-review via control-plane relay, with the canary, recorded per reviewer from review one.
    **Partially done:** agents can now ask each other for a review over signed mail
    (`agent_peer.py`), which is the capability. What is missing is the *assurance* half — the
    canary rate, the reviewer-agreement rate and the per-reviewer record. Those need somewhere to
    write them, which is the control plane.

**Acceptance — the cattle test, run at N=2:**
`docker compose rm -f -v agent-02 && docker compose up agent-02` and it returns with all its
memory, its assets and its identity.

### Phase 3 — Control-plane shape (still 2 agents; a second tenant becomes possible)

Tenant-scoped versioned API, `tenant` column everywhere, identity on `/approve` with tenant-scoped
visibility, lease-based dispatch with visible `abandoned`, budget grants, registry record with the
derived card. Onboard a throwaway second tenant on the same laptop purely to prove the boundary is
enforced by five mechanisms and not by convention.

**Acceptance:** `onboard-tenant acme` is one command plus one human approval. If it is a
twelve-step runbook, hundreds of agents across teams never happens. That is the criterion, not a
nicety.

### Phase 4 — Off the laptop, and rolling upgrade

Real hardware or cloud; **Deployment, not StatefulSet**; retire `agent_app_proxy.py`, the
`schtasks` deploy, the LAN IP and the `.local` mail domain; real Ingress; one templated Harness
pipeline replaces per-app clones.

**Acceptance:** roll a new agent image across the fleet with tasks in flight. Every in-flight task
either completes or shows as `abandoned` with a retry decision. Nothing vanishes.

### Phase 5 — Scale out

Third tenant; N agents per tenant; per-tenant dispatch topology (queue vs addressed); an A2A
adapter when the first foreign agent appears; a coordinator agent **only if** the registry shows
humans are actually mis-routing.

---

## 11. Risks and open questions

1. **Phase 1 is a large refactor of a working system for zero visible change.** Real risk of a long
   stretch with a broken agent. Mitigation: land it in slices with all five test suites green at
   each step, and keep `dev/agent-01` producing values byte-identical to today's `agent1`.
2. **The direction is wrong if cross-review does not substitute for human review.** N agents
   produce N× reviewable work. Phase 2 item 10 is placed before anything that scales for this
   reason. If agent-02 catches nothing the in-container validator missed, stop at N=2.
3. **"Agents physically cannot write global scope" is not free today.** There is one GitHub token
   with `repo` scope. Genuine per-repo write separation needs separate tokens or a GitHub App.
4. **Wildcard-DNS services are an external dependency** and are often blocked on corporate
   networks. The design survives — swap the resolver, keep Host-header routing — but this should be
   discovered deliberately, not during a demo.
5. **Shared tenant memory becomes a shared wrong belief.** Today a confidently-wrong lesson costs
   one agent; in tenant scope it costs all of them, and the system prompt tells agents to trust
   their notes. Mitigations: git history, `git revert`, per-entry attribution, and the existing
   "if the notes and the machine disagree, believe the machine" rule.
6. **Declarative environments rot into 400-line shell scripts.** The replay must be *logged and
   aggregated*, so an operator can see "31 agents apt-install chromium at start, 90s each" and bake
   it into the base image. Without that feedback loop the mechanism decays.
7. **The control plane becomes a single point of failure for delivery.** Slot/route allocation
   fails closed, so an outage stops shipping fleet-wide. Accepted deliberately — shipping is the
   money-and-blast-radius path — but it now has three jobs and one replica.
8. **Zeenie's operational floor.** Docker Desktop needs a manual click after reboot; the Wi-Fi
   profile resets to Public and drops port 22; `docker pull`/`build` over SSH is broken. N agents
   multiply "which one didn't come back".

### Answered by measurement

- **Does the provider's context cache actually apply to this prefix?** Yes, and the *rationale in
  the first draft of this document was wrong.* Within one run the prefix is already byte-identical,
  so there was never a win to be had there; measured on task-0033, the first call of a conversation
  was 16–26% cached and every later call was 99%. The real win is across *conversations* — a new
  one re-sends the whole notes block as novel tokens, and there are two per task because the
  reviewer starts fresh. A predicted 90% hit rate came out at 45% on first measurement, because
  that run was the one *filling* the cache and could not have shown a gain — an error in the
  experiment, not the mechanism.
- **Is injected memory the cost driver?** No. A real 130-step build that shipped an app cost
  **$0.4969** at 99% cache hit; a trivial question cost **$0.0093**. The driver is conversation
  growth, at a 112:1 prompt:completion ratio. This is why the memory-scope work in phase 2 is
  sequenced for correctness reasons and not cost ones.
- **Separately discovered:** `NOTES_MAX_CHARS=8000` is applied **per file**, so only 22,360 of
  48,565 characters of notes are actually injected. About 40% of what the agent wrote to itself is
  already invisible to it — which is an argument for retrieval (D2) on grounds of *fidelity*, not
  tokens.

### Open

- **Does the in-container validator survive cross-review?** If it adds nothing once cross-review
  exists, deleting it saves a review round of spend per task.
- **Auto-retry or human decision on `abandoned`?** Per-tenant policy; default should be human.

---

## 12. What this corrects in the main design doc

- **§14 Roadmap** is superseded by §10 here.
- **§8 (memory)** assumes two scopes in a container volume. Both change: three scopes, external git.
- **§9 (delivery)** is built on the app-slot integer, which D4 removes. The chain itself — GitHub →
  Actions → ci-watcher → Kafka → governance approval → Harness → cluster — is unchanged and stays.
- **§7 (local previews)** assumes published host ports. §6 here replaces that with name addressing.
- The principle list in **§13 stands unchanged**, and this document is an application of it. One
  addition earned here: *deferring a feature is cheap; deferring the seam that makes the feature
  possible is how a rewrite gets scheduled.*
