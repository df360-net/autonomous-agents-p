"""agent_envelope.py — the one thing a task is, whatever brought it.

D3 in docs/Fleet-Design.md. Until now a task WAS an `email.message.Message`, and so was every
safety check written against it: the loop guard read `From`, dedupe read `Message-ID`,
threading read `References`. That is fine while mail is the only source and there is one
consumer. It stops being fine the moment a task can arrive from Jira, an API or a broker,
because then changing the transport means rewriting the safety layer — which is the last code
anybody should want to rewrite.

So the transport is now a detail that produces one of these, and everything downstream reads
the envelope. `agent_inbox` builds it from IMAP today; `agent_outbox` sends the answer back
along whatever the envelope says. The worker never sees a header.

STATE NAMES ARE A2A'S ON PURPOSE. `submitted / working / input-required / completed / failed /
canceled` are the Agent2Agent task lifecycle names, plus our own `abandoned` for a lease that
expired. Adopting the vocabulary now is free, and it means the day a foreign agent needs to
talk to one of ours, the adapter is a serialization change rather than a semantics change.

WHAT IS DECLARED BUT NOT YET ENFORCED. `lease_until`, `deadline` and `budget_usd` are carried
and ignored. Leases need a claim store to be real (phase 3) and the point of putting the
fields here now is that the *shape* is settled before there is history in it — the same
argument that put `tenant` in the spend ledger on day one. `hops` and `purpose` are populated
by the inbox but not yet acted on; the loop guard that reads them lands with D6, along with
the rule that makes them trustworthy.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ENVELOPE_VERSION = 1

# A2A's task lifecycle, plus ours. Kept as data so a state can be validated rather than typoed.
STATES = ("submitted", "working", "input-required", "completed", "failed", "canceled",
          "abandoned")


@dataclass
class TaskEnvelope:
    """One unit of work, independent of how it arrived."""

    # --- identity -------------------------------------------------------------
    task_id: str                      # also the workspace directory name
    tenant: str
    agent_id: str                     # "<tenant>/<name>", from fleet_identity

    # --- provenance -----------------------------------------------------------
    source: str = "email"             # "email" | "jira" | "api" | ...
    requester: str = ""               # who asked; authenticated where the transport can
    reply_to: str = ""                # where the answer goes, which is not always the asker

    # --- the request ----------------------------------------------------------
    subject: str = ""
    body: str = ""
    attachments: List[Any] = field(default_factory=list)

    # --- conversation ---------------------------------------------------------
    # thread_id is the conversation, message_id is this one message in it. Mail supplies both;
    # a broker would supply a correlation id and its own message id. Threading a reply is the
    # transport's job, from these two fields.
    thread_id: str = ""
    message_id: str = ""
    references: str = ""

    # --- machine-to-machine, populated but not yet enforced (see D6) -----------
    hops: int = 0
    purpose: str = ""                 # "" for a human task; "review-request" etc. later
    from_agent: bool = False          # did a machine send this, rather than a person

    # --- lifecycle, declared now so the shape is fixed before there is history --
    state: str = "submitted"
    lease_until: Optional[float] = None
    deadline: Optional[float] = None
    budget_usd: Optional[float] = None

    envelope_version: int = ENVELOPE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """For logging and, later, for handing a task to something that is not this process."""
        return {k: v for k, v in self.__dict__.items() if k != "attachments"}
