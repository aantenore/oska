"""What one session remembers between turns."""

from __future__ import annotations

from pydantic import BaseModel, Field

from opsdeck.contracts.common import Message, PendingApproval, PendingGoal

__all__ = ["SessionState"]


class SessionState(BaseModel):
    """Everything a session carries from one turn to the next.

    `language` is empty until a turn settles it, so the runtime default applies
    to a session that has not been spoken to yet. `last_entities` holds the
    entities the most recent read touched, which is what the reply draws its
    suggestions from.
    """

    session_id: str
    language: str = ""
    pending_goal: PendingGoal | None = None
    pending_approval: PendingApproval | None = None
    last_entities: list[str] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    receipts: list[str] = Field(default_factory=list)
    turn_count: int = 0
