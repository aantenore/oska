"""Opening the stores a deployment is configured with.

`storage.backend` in the runtime configuration names the backend and
`storage.url_env` names the environment variable its address is read from. The
address itself is never written into a configuration file, because it usually
carries a credential. SQLite needs no address: its files live under the state
directory, where `ledger.path` and `sessions.path` put them.

The same backend keeps the audit trail: a JSONL file at `audit.path` with the
files backend, a table of the database otherwise, so every replica appends to
one trail. The proposals and the change requests follow the same setting; they
are opened by `oska.services`, because their file stores belong to the
evolution layer above this one.

Every opener takes the namespace the store is kept under. A database store
puts it in every key and every query; a file store needs nothing of it,
because its files already live under the state directory of one tenant in one
domain.

The implementations are imported when they are opened, so an installation that
keeps its record in SQLite never needs the driver of any other backend.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from oska.config import RuntimeConfig, Settings, StorageConfig, state_path
from oska.storage.protocols import (
    DEFAULT_NAMESPACE,
    AuditStore,
    LedgerStore,
    SessionStateStore,
    StorageError,
)

__all__ = ["POSTGRES", "database_url", "open_audit", "open_ledger", "open_sessions"]

POSTGRES = "postgres"


def database_url(storage: StorageConfig, env: Mapping[str, str] | None = None) -> str:
    """The address of the configured database, read from the variable the configuration names."""
    source = os.environ if env is None else env
    name = storage.url_env
    url = (source.get(name) or "").strip()
    if not url:
        raise StorageError(
            f"storage.backend is {storage.backend}, which reads its address from the "
            f"environment variable {name}; set {name} to the database URL"
        )
    return url


def open_ledger(
    runtime: RuntimeConfig,
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> LedgerStore:
    """The write ledger this deployment is configured to keep."""
    storage = runtime.storage
    if storage.backend == POSTGRES:
        from oska.storage.postgres_ledger import PostgresLedger

        return PostgresLedger(
            database_url(storage, env),
            busy_timeout=storage.busy_timeout_seconds,
            namespace=namespace,
        )
    from oska.governance.ledger import WriteLedger

    return WriteLedger(
        state_path(settings.state_dir, runtime.ledger.path),
        busy_timeout=storage.busy_timeout_seconds,
    )


def open_sessions(
    runtime: RuntimeConfig,
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> SessionStateStore:
    """The session store this deployment is configured to keep."""
    storage = runtime.storage
    if storage.backend == POSTGRES:
        from oska.storage.postgres_sessions import PostgresSessionStore

        return PostgresSessionStore(
            database_url(storage, env),
            busy_timeout=storage.busy_timeout_seconds,
            namespace=namespace,
        )
    from oska.storage.sqlite_sessions import SessionStore

    return SessionStore(
        state_path(settings.state_dir, runtime.sessions.path),
        busy_timeout=storage.busy_timeout_seconds,
    )


def open_audit(
    runtime: RuntimeConfig,
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> AuditStore:
    """The audit trail this deployment is configured to keep."""
    storage = runtime.storage
    if storage.backend == POSTGRES:
        from oska.storage.postgres_audit import PostgresAuditStore

        return PostgresAuditStore(
            database_url(storage, env),
            busy_timeout=storage.busy_timeout_seconds,
            namespace=namespace,
        )
    from oska.audit.sink import JsonlSink

    return JsonlSink(state_path(settings.state_dir, runtime.audit.path))

