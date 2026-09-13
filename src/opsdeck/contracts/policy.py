"""Policy documents attached to a procedure."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Effect = Literal["allow", "deny", "require_second_approval"]

CONDITION_OPERATORS = ("eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte")


class PolicyRule(BaseModel):
    """One rule of a policy document."""

    operation: str
    effect: Effect
    when: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_per_session: int | None = None
    reason: str = ""


class Policy(BaseModel):
    """Rules governing the write operations of one procedure."""

    skill_id: str
    rules: list[PolicyRule] = Field(default_factory=list)
    default: Literal["deny"] = "deny"
