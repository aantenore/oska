"""Proposals for new procedures learned from a session."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ProposalState(StrEnum):
    """Lifecycle state of a proposal."""

    draft = "draft"
    submitted = "submitted"
    approved = "approved"
    rejected = "rejected"
    published = "published"


class QualityReport(BaseModel):
    """Result of the lints run against a proposed skill."""

    issues: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0, default=0.0)


class ProposalTransition(BaseModel):
    """One recorded state change of a proposal."""

    state: ProposalState
    at: datetime
    by: str
    note: str = ""


class SkillProposal(BaseModel):
    """A candidate skill awaiting review."""

    proposal_id: str
    state: ProposalState = ProposalState.draft
    source_session_id: str
    skill_markdown: str
    policy: dict | None = None
    quality: QualityReport = Field(default_factory=QualityReport)
    history: list[ProposalTransition] = Field(default_factory=list)
