# The fleet mail server, for hp-tiger

Hand this directory to whoever hosts the box. It is the mail stack lifted out of the agents'
compose file and hardened for a LAN it now actually crosses.

```
mail/
├── docker-compose.mail.yml          the stack
├── config/dms/user-patches.sh       postfix hardening, run by DMS on every start
└── config/roundcube/custom.inc.php  Roundcube, now authenticating
```

## The one decision baked in: TLS is on

The ask was "enable SMTP AUTH so `:25` stops being an open relay". **SMTP AUTH is not a flag
that can be turned on by itself.** Postfix will not advertise an AUTH mechanism over an
unencrypted connection — that is precisely why the current Roundcube config sends
unauthenticated, and the comment saying so has been in the repo the whole time.

So enabling AUTH without TLS produces a server that offers no way to authenticate and clients
that quietly relay instead. `SSL_TYPE=self-signed` is what makes the rest real.

Self-signed, because `agents.local` exists in no public DNS and never will. The value is that
**mailbox passwords stop crossing the LAN in clear** once four agents on two boxes are
authenticating over the wire — not that it proves the server's identity. The agents already
expect this: `TLS_VERIFY` defaults to `false` in both `agent_inbox` and `agent_outbox`, added
originally for exactly this case.

> **Do not set `TLS_VERIFY=true` on the agents** without first giving this server a
> certificate they can chain to. Every agent stops receiving mail at once, and the symptom is
> a login failure that reads like a wrong password.

## What changed, and what breaks if it is undone

| Change | Why | If reverted |
|---|---|---|
| `PERMIT_DOCKER` removed | it trusted "connected networks", which was a private bridge on zeenie and is the whole house LAN here | `:25` is an open relay again — anything on the LAN can task an agent and spend its budget |
| `SSL_TYPE=self-signed` | without it there is no AUTH to enable | AUTH silently unavailable; clients fall back to relaying |
| submission on `:587`, `permit_sasl_authenticated,reject` | a client that simply offers no credentials would otherwise be accepted | `:587` becomes another `:25` |
| plaintext-auth patch **deleted** | TLS exists now, so credentials go inside STARTTLS | passwords cross the LAN in clear |

## SPOOF_PROTECTION is off, deliberately

Turning it on makes Postfix reject a `MAIL FROM` that does not match the authenticated login.
**Each agent legitimately sends as two identities**: itself, and its reviewer
(`validator1@agents.local`), which is what makes a sign-off visibly not the worker grading its
own homework. One credential, two From addresses — so enabling it would break every reviewer
email the moment it was switched on.

The residual risk is that one authenticated agent could send mail claiming to be another. That
is already handled where it counts: agent-to-agent decisions are made on an HMAC signature and
never on the `From` header, and `agent_principal` **refuses** unsigned mail that claims to come
from a fleet mailbox. Spoofing the header buys nothing.

**The clean fix, when we want it:** give each validator its own mailbox and credential, so
every identity authenticates as itself and `SPOOF_PROTECTION=1` can go on with no exceptions.
That needs a small change in `agent_outbox` to select credentials by `from_addr`, plus four
more mailboxes. Worth doing; not worth blocking the move.

## Accounts to create

Use `provision_agent.py` (in the repo root) on this box — it creates the mailbox and prints a
k8s Secret on stdout, so the password never lands in a file:

```bash
python3 provision_agent.py agent1 | kubectl --context <box> -n fleet apply -f -
python3 provision_agent.py agent2 | kubectl --context <box> -n fleet apply -f -
python3 provision_agent.py agent3 | kubectl --context <box> -n fleet apply -f -
python3 provision_agent.py agent4 | kubectl --context <box> -n fleet apply -f -
python3 provision_agent.py fleet  | kubectl --context <box> -n fleet apply -f -
```

Plus `boss@agents.local` for the operator, by hand or by the same script.

**`fleet@agents.local` is a service identity, not an agent.** It gets a mailbox and a
credential; it must **not** appear in `FLEET_PEERS` and must **not** be given
`FLEET_HMAC_SECRET`. An address that cannot sign is refused the moment it claims to be an
agent, which is the correct behaviour and worth keeping.

## Verifying the move

Three checks, and the third is the one worth doing properly:

1. An agent can read: IMAP login on `143` with STARTTLS succeeds.
2. An agent can send: submission on `587` with credentials succeeds.
3. **A stranger cannot relay.** From a LAN host with no credentials, attempt to send through
   `:25` to an outside address. It must be refused. This is the check the whole exercise is
   for, and it is the one that passes by accident if `PERMIT_DOCKER` finds its way back in.
