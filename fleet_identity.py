"""fleet_identity.py — who this agent is, derived from two inputs and nothing else.

D1 in docs/Fleet-Design.md, and the most expensive one-way door in the plan. An agent's
identity ends up written into a provisioned mailbox, git commit authorship, Kubernetes labels,
the spend ledger, audit lines and approval records. Renaming across a fleet later is a
migration with irreversible history, so the shape is settled now, at two agents, when it costs
an afternoon.

THE RULE: TENANT and AGENT_NAME are inputs. Everything else is DERIVED.

Before this module, `AGENT_NAME`, `AGENT_ADDRESS`, `VALIDATOR_NAME` and `VALIDATOR_ADDRESS`
were four independent environment variables. Four independent inputs for one identity is what
allows a mailbox to be paired with the wrong workspace: a copy-pasted compose block that
changes three of them and forgets the fourth produces an agent that reads one inbox, signs as
someone else, and writes its memory to a third place. Nothing would report that; it would just
quietly be two identities wearing one container.

So the derived values are computed here, and a leftover environment variable that DISAGREES
with the derivation is a startup error rather than an override. Agreeing is fine — that is
what lets the existing deployment keep its exact current values while the shape changes
underneath it.

NOT DERIVED HERE, DELIBERATELY:
  - AGENT_PASSWORD, and every other secret. Secrets are issued, not computed.
  - The Kubernetes namespace and the GitHub owner. Both are still read as environment by
    agent_delivery and ship_app; they move here with D4, when app addressing stops using the
    global slot integer. Adding unused derivations now would be dead code claiming to be a
    design.

TODAY'S VALUES ARE UNCHANGED. With TENANT=dev and AGENT_NAME=agent1 this produces exactly the
mailboxes the running system already uses. The rename to a tenant-scoped address is an
operational step — a new mailbox in the mail server, and a different address to write to —
and it is deliberately not bundled into a refactor whose whole claim is that nothing changed.
"""

import os
import re
import sys

# Lowercase, digits, hyphens; must start and end alphanumeric. The same shape governance's
# cluster.py requires of an app name, and for the same reason: this string becomes a DNS label,
# a mailbox local-part and a path component, and each of those rejects a different thing.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

TENANT = os.environ.get("TENANT", "dev").strip()
NAME = os.environ.get("AGENT_NAME", "agent1").strip()
DOMAIN = os.environ.get("AGENT_DOMAIN", "agents.local").strip()


def _reject(what, value, why):
    sys.exit(f"fleet_identity: refusing to start — {what} {value!r} {why}\n"
             f"  TENANT and AGENT_NAME must match {NAME_RE.pattern} "
             f"(lowercase letters, digits and hyphens).")


if not NAME_RE.match(TENANT):
    _reject("TENANT", TENANT, "is not a usable identifier")
if not NAME_RE.match(NAME):
    _reject("AGENT_NAME", NAME, "is not a usable identifier")

# The one string that names this agent everywhere: ledger rows, registry keys, audit lines.
# Tenant-qualified from the first row, because history cannot be back-filled with a tenant.
AGENT_ID = f"{TENANT}/{NAME}"


def address(name=None):
    """The mailbox for an agent in this tenant."""
    return f"{name or NAME}@{DOMAIN}"


def validator_name(name=None):
    """The reviewer identity that signs off this agent's work.

    Derived rather than configured so the two can never drift apart: agent1 -> validator1,
    agent-01 -> validator-01. A name that does not contain "agent" gets a prefix instead,
    which is ugly but unambiguous, and never silently collides with the worker's own mailbox.
    """
    name = name or NAME
    return name.replace("agent", "validator", 1) if "agent" in name else f"validator-{name}"


def validator_address(name=None):
    return address(validator_name(name))


AGENT_ADDRESS = address()
VALIDATOR_NAME = validator_name()
VALIDATOR_ADDRESS = validator_address()


def check_environment(env=None):
    """A leftover env var that disagrees with the derivation stops the process.

    Returns the list of conflicts (empty when clean) so this is testable without a subprocess.
    Called for its side effect at import: the whole point is that the disagreement is loud at
    start-up rather than discovered later from mail arriving in the wrong inbox.
    """
    env = os.environ if env is None else env
    conflicts = []
    for var, derived in (("AGENT_ADDRESS", AGENT_ADDRESS),
                         ("VALIDATOR_NAME", VALIDATOR_NAME),
                         ("VALIDATOR_ADDRESS", VALIDATOR_ADDRESS)):
        supplied = (env.get(var) or "").strip()
        if supplied and supplied != derived:
            conflicts.append(
                f"{var}={supplied!r} but TENANT={TENANT!r} AGENT_NAME={NAME!r} derives "
                f"{derived!r}"
            )
    return conflicts


_conflicts = check_environment()
if _conflicts:
    sys.exit(
        "fleet_identity: refusing to start — the environment contradicts this agent's "
        "identity.\n  " + "\n  ".join(_conflicts) +
        "\n\nThese are derived from TENANT and AGENT_NAME and must not be set independently: "
        "an agent that reads one inbox and signs as another is not detectable from its output. "
        "Remove them from the environment, or change AGENT_NAME so the derivation matches."
    )
