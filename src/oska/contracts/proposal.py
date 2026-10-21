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


class ActorSource(StrEnum):
    """Where the actor recorded on a transition came from.

    `principal` is the identity the channel authenticated on the call.
    `operator` is the person at a terminal running the command line, whose
    authority is the terminal itself. `stated` is a name a call gave in its
    body with no identity behind it, recorded as given and never as proven.
    """

    principal = "principal"
    operator = "operator"
    stated = "stated"


VERIFIED_SOURCES = frozenset({ActorSource.principal, ActorSource.operator})


class LifecycleError(RuntimeError):
    """A proposal cannot move the way it was asked to.

    It is declared beside the proposal because a store raises it too: a move
    from a state the stored proposal has already left is refused where the
    proposal is kept, so of two decisions taken at once only the first lands.
    """


def already_moved(
    proposal_id: str, held: ProposalState | str, prior: ProposalState | str
) -> LifecycleError:
    """The refusal of a move whose proposal left `prior` before the move was written."""
    return LifecycleError(
        f"proposal {proposal_id} is {ProposalState(held).value}, not "
        f"{ProposalState(prior).value}: another decision came first"
    )


class ProposalTransition(BaseModel):
    """One recorded state change of a proposal.

    `actor_verified` is false when `by` is only what the caller said it was.
    A record written before the source was kept reads as stated.
    """

    state: ProposalState
    at: datetime
    by: str
    note: str = ""
    actor_source: ActorSource = ActorSource.stated
    actor_verified: bool = False


class SkillProposal(BaseModel):
    """A candidate skill awaiting review."""

    proposal_id: str
    state: ProposalState = ProposalState.draft
    source_session_id: str
    skill_markdown: str
    policy: dict | None = None
    quality: QualityReport = Field(default_factory=QualityReport)
    history: list[ProposalTransition] = Field(default_factory=list)
