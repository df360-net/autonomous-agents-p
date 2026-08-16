#!/usr/bin/env python3
"""provision_agent.py — create an agent's mailbox and emit its secrets, on stdout, once.

Replaces provision-agent.ps1, which only ran on Windows. hp-tiger is Ubuntu and now hosts the
mail server, so a PowerShell script was a prerequisite that could not be met on the machine
that has to meet it.

    python3 provision_agent.py agent3 | kubectl --context kind-hp-tiger -n fleet apply -f -

THE PASSWORD IS NEVER WRITTEN DOWN. It is generated here, handed to the mail server, printed
once into a pipe, and forgotten. No file, no `.env` line, no console either side of the pipe
can scroll back to. That is the whole delivery mechanism, and it is why this prints a Secret
manifest rather than a password: the only consumer is `kubectl apply -f -`, so the value never
exists anywhere a human or a log could later read it.

Run it again for the same agent and you get a NEW password, applied to both the mailbox and
the Secret in the same run — which is how a rotation works and why re-running is safe.
`setup email add` updates an existing account rather than failing, so the two cannot drift
apart. What is NOT safe is running it and discarding the output: the mailbox password would
change while the Secret still held the old one, and the agent would fail to log in with a
message about credentials rather than about provisioning. Hence --dry-run, which changes
nothing at all.

WHAT THIS DOES NOT ISSUE. FLEET_HMAC_SECRET is fleet-level, generated once, and identical
across every agent — it is how agents prove to each other that they are agents, and a
per-agent value would silently partition the fleet into agents that cannot talk. It lives in
the shared Secret and is never minted here. Same for DEEPSEEK_API_KEY and GITHUB_TOKEN.
FLEET_TOKEN is minted by the control plane, which is the only thing that can bind it to an
actor.
"""

import argparse
import base64
import re
import secrets
import subprocess
import sys

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
DOMAIN = "agents.local"
# The container name on the mail host, not a hostname — this script runs ON hp-tiger and talks
# to the local docker socket, because `setup email add` is a command inside that container.
MAIL_CONTAINER = "mailserver"


def die(msg):
    sys.exit(f"provision_agent: {msg}")


def b64(value):
    return base64.b64encode(value.encode()).decode()


def new_password(nbytes=24):
    """URL-safe so it survives every config format between here and Dovecot without quoting."""
    return secrets.token_urlsafe(nbytes)


def mailbox_add(address, password, dry_run):
    """Create or update one mailbox. Idempotent by virtue of `setup email add`.

    stderr is captured rather than inherited SO THE PASSWORD CANNOT REACH THE TERMINAL: it is
    an argument to this command, and a failure that echoed the invocation would print it to
    whatever console is running the pipe. On failure we report the exit code and a scrubbed
    message, never the command line.
    """
    cmd = ["docker", "exec", MAIL_CONTAINER, "setup", "email", "add", address, password]
    if dry_run:
        print(f"# DRY RUN: would run docker exec {MAIL_CONTAINER} setup email add "
              f"{address} <generated-password>", file=sys.stderr)
        return
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        detail = (p.stderr or p.stdout).replace(password, "***").strip()[:300]
        die(f"could not create {address} (exit {p.returncode}): {detail}")
    print(f"# mailbox {address} created or updated", file=sys.stderr)


def secret_manifest(agent, address, password):
    """The per-agent Secret, in the shape the fleet app spec names as `agent-<n>`.

    AGENT_PASSWORD and SMTP_PASSWORD are the same value on purpose: one mailbox, one
    credential, read with it over IMAP and send with it over submission. They are two keys
    because they are two consumers — agent_inbox reads the first, agent_outbox the second —
    and collapsing them into one name would make it impossible to rotate the sending
    credential separately later, which is exactly what per-validator credentials would need.

    FLEET_TOKEN is deliberately absent. It is minted by the control plane against an actor,
    and a token this script invented would authenticate as nobody.
    """
    return f"""apiVersion: v1
kind: Secret
metadata:
  name: {agent}
  namespace: fleet
  labels:
    app.kubernetes.io/managed-by: fleet-provisioner
    fleet.agent: {agent}
  annotations:
    fleet/mailbox: {address}
type: Opaque
data:
  AGENT_PASSWORD: {b64(password)}
  SMTP_PASSWORD: {b64(password)}
  SMTP_USER: {b64(address)}
"""


def main():
    ap = argparse.ArgumentParser(
        description="Create an agent mailbox and print its k8s Secret on stdout.",
        epilog="Pipe stdout straight into kubectl. Do not save it to a file.")
    ap.add_argument("agent", help="agent name, e.g. agent3 — or 'fleet' for the "
                                  "governance sender identity")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the manifest with a throwaway password and touch nothing")
    args = ap.parse_args()

    agent = args.agent.strip().lower()
    if not NAME_RE.match(agent):
        die(f"{agent!r} is not a usable identifier (lowercase letters, digits, hyphens)")

    address = f"{agent}@{DOMAIN}"
    password = new_password()

    # fleet@ is a SERVICE identity, not an agent: governance sends the "it's live" email from
    # it. It gets a mailbox and a credential like anyone else, and deliberately no HMAC key and
    # no place in FLEET_PEERS — an address that cannot sign is refused by agent_principal the
    # moment it claims to be an agent, which is the correct outcome and worth preserving.
    if agent == "fleet":
        print("# NOTE: 'fleet' is the governance sender identity. Do NOT add it to "
              "FLEET_PEERS and do NOT give it FLEET_HMAC_SECRET.", file=sys.stderr)

    mailbox_add(address, password, args.dry_run)
    sys.stdout.write(secret_manifest(agent, address, password))

    print(f"# {address} provisioned. The password exists only in the mailbox and in the "
          f"manifest above — if that manifest did not reach kubectl, re-run this.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
