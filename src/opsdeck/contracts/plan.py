"""Plans produced for a single procedure."""

from __future__ import annotations

from pydantic import BaseModel, Field

from opsdeck.contracts.operation import PlannedOperation


class MissingInput(BaseModel):
    """An input the procedure needs before it can run."""

    field: str
    question: str


class Plan(BaseModel):
    """Ordered operations for one procedure, plus the inputs still missing."""

    runbook_id: str
    operations: list[PlannedOperation] = Field(default_factory=list)
    missing: list[MissingInput] = Field(default_factory=list)
    reply_intent: str = ""
