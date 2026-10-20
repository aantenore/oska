"""What the evolution layer asks of the stores that keep its review queue.

The proposals drafted from sessions and the change requests composed from them
are kept by the evolution layer in directories, and by the storage layer in a
database every replica shares. Neither layer may reach into the other, so the
interfaces both keep to are declared here, beside the records they hold.
`oska.storage` names them with the ledger, the session and the audit interfaces.

A store is opened under a namespace and reaches nothing past it. `purge`
removes the whole of that namespace and refuses the default one; a directory
store removes nothing, because its namespace is its directory and the
directory belongs to whoever made it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import builtins
    from pathlib import Path

    from oska.contracts.change import ChangeRequest, ChangeRequestState
    from oska.contracts.proposal import ProposalState, SkillProposal

__all__ = ["ChangeRequestRecordStore", "ProposalRecordStore"]


@runtime_checkable
class ProposalRecordStore(Protocol):
    """The proposals of the learning loop: the review queue every replica reads."""

    @property
    def location(self) -> str:
        """Where the proposals are kept, in words a person can find them by."""
        ...

    def save(self, proposal: SkillProposal) -> object:
        """Write a proposal, replacing any earlier copy."""
        ...

    def get(self, proposal_id: str) -> SkillProposal:
        """Read one proposal. Raises `KeyError` when it is not there."""
        ...

    def list(self, state: ProposalState | str | None = None) -> builtins.list[SkillProposal]:
        """Every proposal, filtered by state when one is named, sorted by id."""
        ...

    def delete(self, proposal_id: str) -> None:
        """Remove one proposal. Raises `KeyError` when it is not there."""
        ...

    def purge(self) -> int:
        """Remove every proposal of this store's namespace and report how many went."""
        ...


@runtime_checkable
class ChangeRequestRecordStore(Protocol):
    """The change requests composed from proposals, with the package each one carries.

    A package is a set of text files named by their path inside the package,
    laid out as a catalog root is under `ChangeRequest.package`.
    """

    @property
    def location(self) -> str:
        """Where the requests are kept, in words a person can find them by."""
        ...

    def save(
        self,
        request: ChangeRequest,
        *,
        skill_markdown: str | None = None,
        policy: dict | None = None,
    ) -> object:
        """Write a request, and its package when the file is given."""
        ...

    def get(self, request_id: str) -> ChangeRequest:
        """Read one request. Raises `KeyError` when it is not there."""
        ...

    def list(
        self, state: ChangeRequestState | str | None = None
    ) -> builtins.list[ChangeRequest]:
        """Every request held, oldest first, filtered by state when one is named."""
        ...

    def files(self, request_id: str) -> dict[str, str]:
        """The files of one request's package, by their path inside it."""
        ...

    def add_file(self, request_id: str, name: str, text: str) -> None:
        """Write one more file into a request's package, replacing one of the same name."""
        ...

    def write_package(self, request_id: str, target: Path) -> builtins.list[Path]:
        """Write the package of one request into a directory, and name the files written."""
        ...

    def apply(
        self, request_id: str, catalog_root: Path, *, by: str
    ) -> builtins.list[Path]:
        """Copy the package of one open request into a catalog root, and record who did."""
        ...

    def reject(self, request_id: str, *, by: str, note: str = "") -> ChangeRequest:
        """Close one open request without copying anything, recording who and why."""
        ...

    def delete(self, request_id: str) -> None:
        """Remove one request and its package. Raises `KeyError` when it is not there."""
        ...

    def drop_package(self, request_id: str) -> bool:
        """Remove the package of one request, keep its record, and say whether any went."""
        ...

    def purge(self) -> int:
        """Remove every request of this store's namespace and report how many went."""
        ...
