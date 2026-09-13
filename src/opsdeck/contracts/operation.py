"""Operations as emitted by the planner."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlannedOperation(BaseModel):
    """One operation the planner proposes, bound to a declared integration operation."""

    integration_id: str
    operation: str
    payload: dict = Field(default_factory=dict)
    write: bool = False
    purpose: str = ""
