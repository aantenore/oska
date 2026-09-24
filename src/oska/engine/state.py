"""What one session remembers between turns."""

from __future__ import annotations

from pydantic import BaseModel, Field

from oska.contracts.common import Message, PendingApproval, PendingGoal

__all__ = ["SessionState"]


class SessionState(BaseModel):
    """Everything a session carries from one turn to the next.

    `language` is empty until a turn settles it, so the runtime default applies
    to a session that has not been spoken to yet. `last_entities` holds the
    entities the most recent read touched, which is what the reply draws its
    suggestions from. `revision` is the catalog revision this session is pinned
    to, so a change published mid-conversation cannot rewrite what was already
    said. `principal` is what the channel authenticated, when it authenticated
    anything; fields resolved from it are never asked for and never written by
    the model.
    """

    session_id: str
    language: str = ""
    revision: str = ""
    principal: dict = Field(default_factory=dict)
    pending_goal: PendingGoal | None = None
    pending_approval: PendingApproval | None = None
    last_entities: list[str] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    receipts: list[str] = Field(default_factory=list)
    turn_count: int = 0
