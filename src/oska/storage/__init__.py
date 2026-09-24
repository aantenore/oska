"""Where the record of a conversation is kept: the interfaces, and how to open a backend.

`LedgerStore` and `SessionStateStore` are what the runtime asks of the write
ledger and of the session store. SQLite implements both out of the box
(`oska.governance.ledger.WriteLedger`, `oska.storage.sqlite_sessions.SessionStore`);
PostgreSQL implements both behind the optional extra `postgres`, so several
processes can share one record. `open_ledger` and `open_sessions` pick the
backend the runtime configuration names, under the namespace they are given.
"""

from oska.storage.openers import database_url, open_ledger, open_sessions
from oska.storage.protocols import (
    DEFAULT_NAMESPACE,
    LedgerStore,
    SessionStateStore,
    StorageError,
    storage_namespace,
)

__all__ = [
    "DEFAULT_NAMESPACE",
    "LedgerStore",
    "SessionStateStore",
    "StorageError",
    "database_url",
    "open_ledger",
    "open_sessions",
    "storage_namespace",
]
