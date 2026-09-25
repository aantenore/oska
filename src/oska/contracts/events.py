"""Audit events emitted by the engine."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EventKind = Literal[
    "turn",
    "route",
    "discovery",
    "hydrate",
    "plan",
    "policy",
    "approval_requested",
    "approval_decided",
    "execute",
    "verify",
    "ledger_hit",
    "reply",
    "proposal",
    "guardrail",
    "trace",
    "budget",
    "model_fallback",
    "revision",
    "unavailable",
    "retention",
]


class AuditEvent(BaseModel):
    """One entry of the audit trail."""

    kind: EventKind
    session_id: str
    turn_id: str
    at: datetime
    data: dict = Field(default_factory=dict)
