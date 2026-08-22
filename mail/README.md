# The fleet mail server — WHY it is configured as it is

**The configuration itself lives in `infra-fleet/mail/` and auto-deploys on push. It is not
here any more, and it must not be edited here.**

This directory held a working copy of the mail stack while it was being stood up on hp-tiger.
That stack is now under CD: compose, the DMS `user-patches.sh` and the Roundcube config are
version-controlled upstream and deployed from there. Keeping a second copy in this repo would
have meant two sources for one live config — the exact shape of bug this project has now fixed
three times (the image tag computed in two places, the reply subject computed in two places,
the port declared in three). A hand-edit here would be reverted by the next deploy, or worse,
would diverge quietly and be believed.

**What remains here is the reasoning**, because the settings below are all ones a reasonable
person would "fix" back, and the reasons do not survive in the file itself. If you need a mail
change, it is a PR to `infra-fleet/mail/`.

## What the agents depend on, and must not change without them

| Setting | Why | If reverted |
|---|---|---|
| `PERMIT_DOCKER` absent | it trusts "connected networks" — a private bridge on zeenie, the whole house LAN on hp-tiger | `:25` is an open relay: anything on the LAN can task an agent and spend its budget |
| `SSL_TYPE=self-signed` + **pre-generated** certs | `self-signed` sounds like the server makes its own certificate and does not — DMS v15 expects the files to exist and aborts inside TLS setup when they are missing. This is how the stack failed to start the first time. `make-certs.sh` is that missing step, and under this mode the FILENAMES are the config: `<fqdn>-cert.pem`, `<fqdn>-key.pem`, `demoCA/cacert.pem` | aborts in TLS setup with no mention of a missing file |
| submission on `:587`, `permit_sasl_authenticated,reject` | a client offering no credentials would otherwise be accepted | `:587` becomes another `:25` |
| `ENABLE_AMAVIS=0` | not for what it screens — no virus scanner, no spam filter, no external mail — but for **how it fails**: a rejection is a bounce, so the sender's message is gone and the recipient never knew there was one. Our headers are assembled by code and have already lost one reply to a folded `References` header | silent bounces on machine-generated mail, invisible on both sides |
| plaintext-auth patch deleted | TLS exists now, so credentials go inside STARTTLS | passwords cross the LAN in clear |

> **Do not set `TLS_VERIFY=true` on the agents** without first giving the server a certificate
> they can chain to. `agent_inbox` and `agent_outbox` both default it to `false` for exactly
> this reason. Every agent stops receiving mail at once, and the symptom is a login failure
> that reads like a wrong password.

### `user-patches.sh` must reach the box with LF endings

A `\r` on the shebang makes the kernel look for an interpreter named `/bin/bash\r`, and the
script then does not run — **silently**. The mail server comes up unhardened, submission
accepts unauthenticated senders, and nothing reports an error. `.gitattributes` pins
`*.sh eol=lf` here; whoever owns that file upstream needs the same pin, because the failure is
invisible rather than loud.

## SPOOF_PROTECTION is ON, via send-as aliases

It rejects a `MAIL FROM` that the authenticated login is not allowed to use. That used to be
impossible here, because **each agent legitimately sends as two identities**: itself, and its
reviewer (`validator1@agents.local`) — which is what makes a sign-off visibly not the worker
grading its own homework. One credential, two From addresses.

The fix was not the one planned. We had a two-sided change sketched — separate `validatorN@`
mailboxes with their own credentials, plus a change in `agent_outbox` to select credentials by
From address. Infra took a one-sided route instead: **`validatorN@` is a virtual alias for
`agentN@`**, which DMS folds into `smtpd_sender_login_maps`, so `agentN`'s existing login is
permitted to send as `validatorN@`. No new mailboxes, no new secrets, and no agent-side change
at all. The planned outbox work is dead and has been dropped.

### The contract this depends on

> **`validatorN@` is a send-as alias, not an account.** Authenticate as `agentN`; use
> `validatorN@` only in the `From` header. There is no validator mailbox and no validator
> password, so an attempt to log in as one fails outright.

`agent_outbox.send_mail` already works this way — a single `SMTP_USER` login regardless of
`from_addr` — and the comment at the `smtp.login` call says why, because the tempting refactor
is exactly the one that would break it. `smtp_identity_warning()` catches the misconfiguration
at boot rather than at send time, where an auth failure reads like a wrong password rather than
a wrong username.

Verified live by infra: an agent authenticated as `agentN` may send as `agentN@` and
`validatorN@`, and is refused (553) sending as any other agent or validator.

The HMAC signature remains the primary check — agent-to-agent decisions are made on it and
never on `From`, and `agent_principal` refuses unsigned mail claiming a fleet identity. Spoof
protection is defence in depth beneath that, not a replacement for it.

## Mailbox provisioning is still here, and still host-only

`provision_agent.py` (repo root) creates the mailbox and prints a k8s Secret on stdout, so the
password never lands in a file. It writes `postfix-accounts.cf`, which is deliberately
gitignored and **not** CD-managed — creating mailboxes is unaffected by the move.

```bash
python3 provision_agent.py agent1 | kubectl --context <box> -n fleet apply -f -
```

**`fleet@agents.local` is a service identity, not an agent.** It gets a mailbox and a
credential; it must **not** appear in `FLEET_PEERS` and must **not** be given
`FLEET_HMAC_SECRET`. An address that cannot sign is refused the moment it claims to be an
agent, which is correct and worth keeping.

## The check that matters

**A stranger cannot relay.** From a LAN host with no credentials, attempt to send through `:25`
to an outside address; it must be refused. This is the one that passes by accident if
`PERMIT_DOCKER` ever finds its way back in.
