"""A proposed catalog change, as every layer that keeps or reads one sees it.

The evolution layer composes a change request and a person decides on it; the
store that keeps it may be a directory on one machine or a database shared by
several replicas. Both kinds of store hold the same record and keep the same
rules about it, so the record and the rules are declared here, once.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from oska.contracts.proposal import ActorSource, QualityReport

__all__ = [
    "POLICY_FILENAME",
    "SKILL_FILENAME",
    "ChangeRequest",
    "ChangeRequestError",
    "ChangeRequestState",
    "package_file",
    "require_actor",
    "require_open",
]

SKILL_FILENAME = "skill.md"
POLICY_FILENAME = "policy.json"


class ChangeRequestError(RuntimeError):
    """A change request cannot be read, applied or rejected the way it was asked."""


class ChangeRequestState(StrEnum):
    """Where a change request stands.

    `open` is what composing one produces, and the only state from which a
    person may decide. `applied` and `rejected` are decisions, and both are
    final: a request is a record of one review, not a queue entry to reuse.
    """

    open = "open"
    applied = "applied"
    rejected = "rejected"


class ChangeRequest(BaseModel):
    """What one review is about: a package, its origin and its decision.

    `publisher` names how the package was published for review. A request
    published as a branch records the repository, the branch, the commit and,
    when one was opened, what the pull request command answered.

    `actor_source` and `actor_verified` say whether `actor` is an identity
    the runtime established or only a name the approving call gave; a request
    written before they were kept reads as stated.
    """

    request_id: str
    proposal_id: str
    skill_id: str
    version: int
    actor: str
    actor_source: ActorSource = ActorSource.stated
    actor_verified: bool = False
    created_at: datetime
    quality: QualityReport = Field(default_factory=QualityReport)
    state: ChangeRequestState = ChangeRequestState.open
    package: str
    decided_at: datetime | None = None
    decided_by: str = ""
    note: str = ""
    applied_to: str = ""
    publisher: str = "directory"
    repository: str = ""
    branch: str = ""
    commit: str = ""
    pushed: bool = False
    pull_request: str = ""


def require_actor(by: str, refusal: str) -> str:
    """The actor a decision records, or the refusal of a decision that names nobody."""
    actor = by.strip()
    if not actor:
        raise ChangeRequestError(refusal)
    return actor


def require_open(request: ChangeRequest) -> None:
    """Refuse to decide on a request that is already decided."""
    if request.state is not ChangeRequestState.open:
        raise ChangeRequestError(
            f"change request {request.request_id} is {request.state.value}, not open"
        )


def package_file(name: str) -> str:
    """A file name inside a package, refused when it would reach outside it."""
    relative = PurePosixPath(name)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ChangeRequestError(f"{name!r} is not a path inside the package")
    return relative.as_posix()
