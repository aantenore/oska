"""What the runtime asks of the stores that keep its record.

Two stores hold the account of a conversation between turns and across
restarts: the write ledger, which holds a receipt for every write attempted,
and the session store, which holds what a session carries from one turn to the
next. Three more hold what is read about conversations afterwards: the audit
trail, the proposals drafted from sessions, and the change requests composed
from those. The runtime reaches each through one of these interfaces and
nothing else, so where they are kept — a file on one machine, a database shared
by several processes — is configuration. The interfaces of the proposals and
the change requests are declared with the contracts, because the evolution
layer that keeps them in files sits above this one; they are named here too.

Every implementation has to keep the same promises, and one behavioral
contract is run against each of them. The one that matters most is `claim`:
the check that nothing settles a write's key and the record of its intent are
one atomic step, across threads and across processes, because that step is
what stops the same write from being sent twice.

A store is opened under a namespace, and nothing it reads or writes reaches
past it: two tenants, or two domains of one platform, pointed at one database
never see each other's sessions or receipts. A file-backed store is its own
namespace, because a platform gives each (tenant, domain) files of its own.

`purge` is the one operation that removes a namespace outright, and it is
narrow on purpose: a store removes only the rows of the namespace it was opened
under, never the default namespace, and a file-backed store removes nothing,
because its namespace is its file and the file belongs to whoever made it. It
exists for a caller that created a namespace for a while, such as a
conformance run, and is finished with it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from oska.contracts.receipt import (
    Classification,
    ReceiptStatus,
    VerificationStatus,
    WriteReceipt,
)
from oska.contracts.stores import ChangeRequestRecordStore, ProposalRecordStore

if TYPE_CHECKING:
    from collections.abc import Iterable

    from oska.contracts.events import AuditEvent
    from oska.contracts.session import SessionState

__all__ = [
    "DEFAULT_NAMESPACE",
    "AuditStore",
    "ChangeRequestRecordStore",
    "LedgerStore",
    "ProposalRecordStore",
    "SessionStateStore",
    "StorageError",
    "refuse_default_purge",
    "storage_namespace",
]

DEFAULT_NAMESPACE = "default"
"""The namespace of a single catalog, and of every row written before namespaces existed."""


def storage_namespace(tenant: str, domain: str) -> str:
    """The namespace the record of one tenant in one domain is kept under.

    Tenant and domain names are lowercase letters, digits, `-` and `_`, so the
    slash between them cannot be part of either and no two pairs share a
    namespace; nor can a pair equal the default namespace, which has no slash.
    """
    return f"{tenant}/{domain}"


class StorageError(RuntimeError):
    """A store cannot be opened as configured, or was asked for what it never does."""


def refuse_default_purge(namespace: str) -> None:
    """Raise unless `namespace` is one a store may purge: never the default one."""
    if namespace == DEFAULT_NAMESPACE:
        raise StorageError(
            f"the {DEFAULT_NAMESPACE!r} namespace holds the record of a deployment "
            "and is never purged"
        )


@runtime_checkable
class LedgerStore(Protocol):
    """Write receipts, keyed by session and by the identity of the intent."""

    def key(
        self,
        integration_id: str,
        operation: str,
        payload: dict,
        idempotency_fields: list[str] | None = None,
    ) -> str:
        """Identity of a write: the same key twice is the same intent twice."""
        ...

    def new_receipt(
        self,
        session_id: str,
        key: str,
        integration_id: str,
        operation: str,
        payload: dict,
    ) -> WriteReceipt:
        """A receipt for an intent that has not been sent yet."""
        ...

    def record(self, receipt: WriteReceipt) -> WriteReceipt:
        """Store a receipt, whatever else the ledger holds for its key."""
        ...

    def claim(self, receipt: WriteReceipt) -> WriteReceipt:
        """Record the receipt unless its key is settled, atomically; return the holder."""
        ...

    def update(
        self,
        receipt_id: str,
        *,
        status: ReceiptStatus | None = None,
        response: dict | None = None,
        verification: VerificationStatus | str | None = None,
        evidence: dict | None = None,
        error: str | None = None,
        classification: Classification | None = None,
    ) -> WriteReceipt | None:
        """Fill in what the connector and the read-back reported; omitted fields stay."""
        ...

    def get(self, receipt_id: str) -> WriteReceipt | None:
        """One receipt by its identifier."""
        ...

    def lookup(self, session_id: str, key: str) -> WriteReceipt | None:
        """The most recent receipt for this intent in this session."""
        ...

    def settled(self, session_id: str, key: str) -> WriteReceipt | None:
        """The receipt that closes this intent for this session, or nothing."""
        ...

    def count(self, session_id: str, operation: str) -> int:
        """How many times this session asked for this operation, in any state."""
        ...

    def for_session(self, session_id: str) -> list[WriteReceipt]:
        """Every receipt of a session, oldest first."""
        ...

    def delete_session(self, session_id: str) -> int:
        """Remove every receipt of one session and report how many went."""
        ...

    def sessions(self) -> list[str]:
        """Every session holding a receipt, oldest receipt first."""
        ...

    def purge(self) -> int:
        """Remove every receipt and claim of this ledger's namespace; report the receipts."""
        ...


@runtime_checkable
class SessionStateStore(Protocol):
    """The state each session carries from one turn to the next."""

    def load(self, session_id: str) -> SessionState:
        """The state of a session, or a new one when it has never been saved."""
        ...

    def save(self, state: SessionState) -> None:
        """Store the state of a session, replacing what was there."""
        ...

    def sessions(self) -> list[str]:
        """Every stored session, most recently written first."""
        ...

    def updated_at(self, session_id: str) -> datetime | None:
        """When this session was last written, or nothing."""
        ...

    def exists(self, session_id: str) -> bool:
        """Whether this store holds any state for the session, without creating any."""
        ...

    def expired(self, before: datetime) -> list[str]:
        """The sessions last written before this moment, oldest first."""
        ...

    def delete(self, session_id: str) -> int:
        """Remove the stored state of one session and report whether there was any."""
        ...

    def purge(self) -> int:
        """Remove every session of this store's namespace and report how many went."""
        ...


@runtime_checkable
class AuditStore(Protocol):
    """The audit trail: appended to by the runtime, read by the report and the improvement job.

    `location` names where the trail is kept, for a person and for a cache
    key; it never carries a credential.
    """

    @property
    def location(self) -> str:
        """Where this trail is kept, in words a person can find it by."""
        ...

    def emit(self, event: AuditEvent) -> None:
        """Append one event."""
        ...

    def events(self) -> list[AuditEvent]:
        """Every readable event of the trail, in the order it was appended."""
        ...

    def exists(self) -> bool:
        """Whether a trail was ever started here, as opposed to one that holds nothing."""
        ...

    def delete_sessions(
        self, session_ids: Iterable[str], *, keep_kinds: Iterable[str] = ()
    ) -> dict[str, int]:
        """Remove the events of these sessions, except those of the kinds kept; count by session."""
        ...

    def purge(self) -> int:
        """Remove every event of this store's namespace and report how many went."""
        ...
