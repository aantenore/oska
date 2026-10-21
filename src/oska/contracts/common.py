"""Types shared by more than one module.

Everything a second module needs is declared here, so no module has to import
another module's internals. `MissingInput` is declared next to `Plan` because
`Plan` holds it; it is re-exported from here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from oska.contracts.events import AuditEvent
from oska.contracts.plan import MissingInput, Plan
from oska.contracts.policy import Effect, PolicyRule
from oska.contracts.skill import Skill

__all__ = [
    "ConnectorResult",
    "DiscoveryResult",
    "Hydrated",
    "Message",
    "MissingInput",
    "Outcome",
    "PendingApproval",
    "PendingGoal",
    "PendingInfo",
    "PolicyDecision",
    "RankedSkill",
    "RouteDecision",
    "TurnKind",
    "TurnResult",
]


class Message(BaseModel):
    """One conversation turn."""

    role: Literal["user", "assistant", "system"]
    content: str


class RankedSkill(BaseModel):
    """A skill scored against a query by an index."""

    skill_id: str
    score: float
    matched_on: str = ""


class DiscoveryResult(BaseModel):
    """Outcome of ranking the catalog against a request."""

    ranked: list[RankedSkill] = Field(default_factory=list)
    chosen: str | None = None
    tie: bool = False


class TurnKind(StrEnum):
    """What the user is doing in this turn."""

    new_task = "new_task"
    continue_task = "continue_task"
    provide_input = "provide_input"
    approve = "approve"
    reject = "reject"
    greeting = "greeting"
    off_scope = "off_scope"
    cancel_task = "cancel_task"


class RouteDecision(BaseModel):
    """Where the router sends the turn."""

    kind: TurnKind
    runbook_id: str | None = None
    confidence: float = 0.0
    source: Literal["fast_path", "model", "rule"]
    language: str | None = None
    reason: str = ""


class PolicyDecision(BaseModel):
    """Effect the policy engine applies to one operation."""

    effect: Effect
    rule: PolicyRule | None = None
    reason: str = ""


class PendingGoal(BaseModel):
    """The procedure the session is working on, and what it still needs."""

    runbook_ids: list[str] = Field(default_factory=list)
    step_index: int = 0
    collected_inputs: dict = Field(default_factory=dict)
    missing: list[MissingInput] = Field(default_factory=list)
    request_text: str = ""


class PendingApproval(BaseModel):
    """A write held at the approval gate, frozen on its exact payload."""

    approval_id: str
    runbook_id: str
    plan: Plan
    op_index: int
    exact_payload: dict = Field(default_factory=dict)
    payload_hash: str
    integration_id: str
    operation: str
    approvals_required: int = 1
    approvals_given: int = 0
    summary: str = ""


class ConnectorResult(BaseModel):
    """What a connector returned for one operation."""

    ok: bool
    data: dict = Field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0


class Hydrated(BaseModel):
    """A procedure with everything it references, resolved."""

    runbook: Skill
    entities: list[Skill] = Field(default_factory=list)
    integrations: list[Skill] = Field(default_factory=list)
    runbooks: list[Skill] = Field(default_factory=list)

    def operations(self) -> list[tuple[str, str]]:
        """Return (integration_id, operation name) for every declared operation."""
        return [
            (skill.id, spec.name)
            for skill in self.integrations
            for spec in skill.frontmatter.operations
        ]


class Outcome(BaseModel):
    """One observable result of a turn."""

    kind: Literal[
        "read",
        "receipt",
        "ledger_hit",
        "denied",
        "rejected",
        "invalid_plan",
        "approval_pending",
        "cancelled",
        "unavailable",
    ]
    integration_id: str | None = None
    operation: str | None = None
    data: dict = Field(default_factory=dict)
    receipt_id: str | None = None
    reason: str | None = None


class PendingInfo(BaseModel):
    """What the session is waiting for at the end of a turn."""

    kind: Literal["input", "approval"]
    question: str | None = None
    approval: PendingApproval | None = None


class Directive(BaseModel):
    """What a presentation surface may show after a turn, bound to verified evidence.

    A directive is declared by a procedure for a moment of the turn and filled
    only from what the runtime read or verified; a field the evidence cannot
    answer drops the whole directive. A directive never writes anything.
    """

    procedure: str
    moment: str
    view: str
    fields: dict = Field(default_factory=dict)


class TurnResult(BaseModel):
    """Everything one turn produced."""

    reply: str
    outcomes: list[Outcome] = Field(default_factory=list)
    pending: PendingInfo | None = None
    events: list[AuditEvent] = Field(default_factory=list)
    directives: list[Directive] = Field(default_factory=list)

