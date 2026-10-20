"""Where the record of a conversation is kept: the interfaces, and how to open a backend.

`LedgerStore` and `SessionStateStore` are what the runtime asks of the write
ledger and of the session store; `AuditStore`, `ProposalRecordStore` and
`ChangeRequestRecordStore` are what it asks of the audit trail, the proposals
and the change requests. Files implement all five out of the box: SQLite for
the ledger and the sessions, a JSONL file for the trail, directories for the
learning loop. PostgreSQL implements all five behind the optional extra
`postgres`, so several processes share one record. `open_ledger`,
`open_sessions` and `open_audit` pick the backend the runtime configuration
names, under the namespace they are given; `oska.services` opens the proposals
and the change requests the same way.
"""

from oska.storage.openers import (
    POSTGRES,
    database_url,
    open_audit,
    open_ledger,
    open_sessions,
)
from oska.storage.protocols import (
    DEFAULT_NAMESPACE,
    AuditStore,
    ChangeRequestRecordStore,
    LedgerStore,
    ProposalRecordStore,
    SessionStateStore,
    StorageError,
    storage_namespace,
)

__all__ = [
    "DEFAULT_NAMESPACE",
    "POSTGRES",
    "AuditStore",
    "ChangeRequestRecordStore",
    "LedgerStore",
    "ProposalRecordStore",
    "SessionStateStore",
    "StorageError",
    "database_url",
    "open_audit",
    "open_ledger",
    "open_sessions",
    "storage_namespace",
]
