# autonomous-agents — Phase 0

One autonomous developer-agent, tasked by email. You email `agent1@agents.local` from a
webmail UI in your browser; it builds the thing in its own container and replies with what
it built, the files it wrote, and the commands it actually ran.

Context, decisions and roadmap: [agent-reminder.md](agent-reminder.md).

## The pieces

| file | what it is |
|---|---|
| [agent_brain.py](agent_brain.py) | `../LLM_API_call/agent.py`'s loop, talking straight to DeepSeek, every tool auto-approved (the container is the sandbox). Returns a structured result instead of printing. |
| [agent_validator.py](agent_validator.py) | The review gate. Same loop, reviewer's prompt, fresh context, its own shell. Nothing is sent until it signs off (or 3 rounds are spent). |
| [agent_notes.py](agent_notes.py) | The agent's memory between tasks: pastes its three self-written notes files (`AGENT.md`, `AGENT-ASSETS.md`, `AGENT-AVOID.md`) into every task, and finds an app port nothing is listening on. |
| [agent_delivery.py](agent_delivery.py) | Delivery conventions: `agent-<app>` repos, `ghcr.io/<owner>/agent-<app>` images, three ports per app slot, and the CI + manifest templates. |
| [ship_app.py](ship_app.py) | On PATH in the container as `ship_app`. `scaffold` / `push` / `status` / `logs` / `list` — the agent's only route to GitHub. Needs `GITHUB_TOKEN` with `repo` **and** `workflow` scope. |
| [agent_app_proxy.py](agent_app_proxy.py) | Runs on the `kind` docker network on Zeenie; republishes NodePorts `30000-30009` as `31000-31009` so a deployed pod opens in a browser. |
| [agent_worker.py](agent_worker.py) | The I/O adapter: poll IMAP → run the task in its own workspace → review → reply over SMTP. |
| [Dockerfile](Dockerfile) | python 3.12 + node 22 + tsc + git — enough for the agent to actually build software. |
| [docker-compose.yml](docker-compose.yml) | docker-mailserver + Roundcube + the worker, on one bridge. |

## Deploy to Zeenie

Zeenie is the runtime. Author here, run there.

**0. Zeenie must be ready.** After a Windows reboot two things bite (see
`agent-reminder.md` §5): the Wi-Fi profile flips to Public and firewalls port 22, and Docker
Desktop does not auto-start — click its icon.

**1. Copy the project over** (from this laptop):

```powershell
ssh zeenie "mkdir C:\Users\jianm\autonomous-agents"
scp -r agent_brain.py agent_delivery.py agent_notes.py agent_validator.py agent_worker.py `
       ship_app.py Dockerfile docker-compose.yml config zeenie:C:/Users/jianm/autonomous-agents/
```

**2. Create `.env` on Zeenie** — never commit or echo the key:

```powershell
ssh zeenie
notepad C:\Users\jianm\autonomous-agents\.env    # copy .env.example, fill in DEEPSEEK_API_KEY + passwords
```

**3. Start the mail server and create the two mailboxes.** On a first-ever start there is a
deadline: docker-mailserver refuses to start Dovecot until at least one account exists, and
it shuts itself down after **120 seconds** if none appears. Have these commands ready:

```
cd C:\Users\jianm\autonomous-agents
docker compose up -d mailserver
docker exec mailserver setup email add boss@agents.local <BOSS_PASSWORD>
docker exec mailserver setup email add agent1@agents.local <AGENT1_PASSWORD>
docker exec mailserver setup email add validator1@agents.local <VALIDATOR1_PASSWORD>
docker exec mailserver setup email list
```

`validator1` only ever *sends* (the reviewer's sign-off), so the worker never needs its
password — but give it a real mailbox anyway so replies to it don't bounce.

The passwords must match `.env`. The accounts land in `config/dms/postfix-accounts.cf`, which
is bind-mounted from this repo — so they survive `docker compose down` even though the mail
store (a named volume) does not. Prove auth works before going further:

```
docker exec mailserver doveadm auth test agent1@agents.local <AGENT1_PASSWORD>
```

**4. Bring up the rest.** Anything that touches a registry (`pull`, `build`) **cannot be run
over SSH** on Zeenie: Docker Desktop's CLI resolves credentials through
`docker-credential-desktop.exe`, which needs the Windows credential vault, and an SSH session
is a network logon with no access to it — so even anonymous public pulls die with
`A specified logon session does not exist`. Emptying `auths`, deleting `credsStore`, using a
clean `--config` dir and disabling CLI hooks were all tried; none work. Run it under the
logged-in interactive token instead (one-time setup):

```
schtasks /create /tn agents-deploy /tr C:\Users\jianm\autonomous-agents\deploy-zeenie.cmd /sc once /st 00:00 /ru jianm /it /f
```

then, any time you need to build or pull:

```
ssh zeenie "schtasks /run /tn agents-deploy"     # see scripts/deploy-zeenie.cmd
ssh zeenie "type C:\Users\jianm\autonomous-agents\deploy.log"
```

Everything that does *not* hit a registry — `compose up` on cached images, `ps`, `logs`,
`exec` — works fine over plain SSH:

```
docker compose logs -f agent-worker
```

**5. Use it.** Open `http://192.168.0.105:8080`, log in as `boss@agents.local`, and email
`agent1@agents.local`:

> Subject: Build a tic-tac-toe web app
> Two players, no framework, runs from a single HTML file. Reply when done.

Watch `docker compose logs -f agent-worker` while it works. The reply lands in the boss
inbox: the agent's own summary first, then the evidence — files written and every command
it ran, so you can read the diff instead of trusting a green tick.

Inspect what it built:

```
docker exec -it agent1 bash -lc "ls /workspace"
docker cp agent1:/workspace/task-0001-build-a-tic-tac-toe-web-app ./out
```

## Run it without any of that

The brain is standalone — useful for a quick check that DeepSeek and the tool loop are fine:

```powershell
$env:WORKSPACE="C:\tmp\ws"; python agent_brain.py "write add.py with a test"
```

`python agent_worker.py --once` does exactly one poll cycle and exits.

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
