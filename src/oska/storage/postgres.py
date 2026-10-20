"""PostgreSQL: the driver, the connections, and the schema every store brings up to date.

The driver is `psycopg` 3, installed with the optional extra `postgres`. It is
imported when a store is opened, never when this package is, so the base
install does not need it and a deployment that asks for PostgreSQL without it
is told which extra to install.

The schema is a list of numbered migrations. Before a store is used for the
first time in a process, the migrations not yet recorded in `oska_migrations`
are applied, in order, inside one transaction that holds a transaction-level
advisory lock. Two processes starting at once therefore queue on the lock: the
second finds the work done and applies nothing. A migration is appended with the
next number and never edited once released.

Every row carries the namespace of the store that wrote it, and every key and
every query names it, so tenants and domains pointed at one database never
read each other's rows. The second migration added it; rows written before it
belong to the default namespace. The third added the audit trail, the
proposals and the change requests with their packages, namespaced from the
start, so several replicas append to one trail and review one queue.

Every call opens its own connection and closes it, the way the SQLite stores
do, and a connection waits at most `busy_timeout` seconds for a lock another
one holds before the statement fails.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from oska.config import DEFAULT_BUSY_TIMEOUT_SECONDS
from oska.storage.protocols import StorageError

__all__ = ["MIGRATIONS", "Database", "Migration", "driver", "jsonb", "migrate"]

EXTRA = "postgres"
MIGRATION_LOCK = int.from_bytes(
    hashlib.sha256(b"oska.storage.migrations").digest()[:8], "big", signed=True
)
"""The advisory lock the migrations are applied under, the same in every process."""


@dataclass(frozen=True)
class Migration:
    """One numbered step of the schema: statements applied together, once."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "receipts, write claims and session state",
        (
            """
            CREATE TABLE IF NOT EXISTS oska_receipts (
                seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                receipt_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                ledger_key TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload JSONB NOT NULL,
                response JSONB,
                status TEXT NOT NULL,
                verification TEXT NOT NULL,
                verification_evidence JSONB,
                created_at TIMESTAMPTZ NOT NULL,
                error TEXT,
                classification TEXT,
                revision TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS oska_receipts_session_key "
            "ON oska_receipts (session_id, ledger_key)",
            "CREATE INDEX IF NOT EXISTS oska_receipts_session_operation "
            "ON oska_receipts (session_id, operation)",
            """
            CREATE TABLE IF NOT EXISTS oska_write_claims (
                session_id TEXT NOT NULL,
                ledger_key TEXT NOT NULL,
                receipt_id TEXT,
                claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (session_id, ledger_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS oska_sessions (
                session_id TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL,
                state JSONB NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS oska_sessions_updated ON oska_sessions (updated_at)",
        ),
    ),
    Migration(
        2,
        "a namespace in every key, so tenants and domains sharing a database stay apart",
        (
            # Rows written before this step belong to the default namespace.
            "ALTER TABLE oska_receipts ADD COLUMN IF NOT EXISTS "
            "namespace TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE oska_write_claims ADD COLUMN IF NOT EXISTS "
            "namespace TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE oska_sessions ADD COLUMN IF NOT EXISTS "
            "namespace TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE oska_receipts DROP CONSTRAINT IF EXISTS oska_receipts_pkey",
            "ALTER TABLE oska_receipts ADD PRIMARY KEY (namespace, receipt_id)",
            "ALTER TABLE oska_write_claims DROP CONSTRAINT IF EXISTS oska_write_claims_pkey",
            "ALTER TABLE oska_write_claims ADD PRIMARY KEY (namespace, session_id, ledger_key)",
            "ALTER TABLE oska_sessions DROP CONSTRAINT IF EXISTS oska_sessions_pkey",
            "ALTER TABLE oska_sessions ADD PRIMARY KEY (namespace, session_id)",
            "DROP INDEX IF EXISTS oska_receipts_session_key",
            "DROP INDEX IF EXISTS oska_receipts_session_operation",
            "DROP INDEX IF EXISTS oska_sessions_updated",
            "CREATE INDEX IF NOT EXISTS oska_receipts_namespace_session_key "
            "ON oska_receipts (namespace, session_id, ledger_key)",
            "CREATE INDEX IF NOT EXISTS oska_receipts_namespace_session_operation "
            "ON oska_receipts (namespace, session_id, operation)",
            "CREATE INDEX IF NOT EXISTS oska_sessions_namespace_updated "
            "ON oska_sessions (namespace, updated_at)",
        ),
    ),
    Migration(
        3,
        "the audit trail, proposals and change requests, shared by every replica",
        (
            """
            CREATE TABLE IF NOT EXISTS oska_audit_events (
                namespace TEXT NOT NULL,
                seq BIGINT GENERATED ALWAYS AS IDENTITY,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                at TIMESTAMPTZ NOT NULL,
                event JSONB NOT NULL,
                PRIMARY KEY (namespace, seq)
            )
            """,
            "CREATE INDEX IF NOT EXISTS oska_audit_events_namespace_session "
            "ON oska_audit_events (namespace, session_id)",
            """
            CREATE TABLE IF NOT EXISTS oska_proposals (
                namespace TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                state TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                proposal JSONB NOT NULL,
                PRIMARY KEY (namespace, proposal_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS oska_change_requests (
                namespace TEXT NOT NULL,
                request_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                request JSONB NOT NULL,
                PRIMARY KEY (namespace, request_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS oska_change_request_files (
                namespace TEXT NOT NULL,
                request_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (namespace, request_id, name)
            )
            """,
        ),
    ),
)

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS oska_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def driver() -> Any:
    """The `psycopg` module, or a `StorageError` naming the extra that provides it."""
    try:
        import psycopg
    except ImportError as error:
        raise StorageError(
            f"the postgres backend needs the psycopg driver; add the [{EXTRA}] extra to the "
            f"requirement oska was installed with, as in: pip install 'oska[{EXTRA}]'"
        ) from error
    return psycopg


def migrate(connection: Any) -> list[int]:
    """Apply the migrations this database has not recorded, and name the ones applied.

    The advisory lock is taken before anything is read, so the check and the
    work are one step for every process that runs them at the same time.
    """
    applied: list[int] = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        connection.execute(_MIGRATIONS_TABLE)
        done = {
            row["version"] for row in connection.execute("SELECT version FROM oska_migrations")
        }
        for migration in MIGRATIONS:
            if migration.version in done:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO oska_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
            applied.append(migration.version)
    return applied


class Database:
    """One database address, the connections made to it, and its schema kept current."""

    def __init__(self, url: str, *, busy_timeout: float | None = None) -> None:
        self._psycopg = driver()
        self._url = url
        self._busy_timeout = DEFAULT_BUSY_TIMEOUT_SECONDS if busy_timeout is None else busy_timeout
        self._ready = False
        self._opening = threading.Lock()

    @property
    def identity(self) -> str:
        """A name for this database that tells two addresses apart and carries no credential."""
        return hashlib.sha256(self._url.encode("utf-8")).hexdigest()[:12]

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """A connection in a transaction: committed when the block ends, rolled back on error."""
        self._migrate_once()
        with self._open(autocommit=False) as connection:
            yield connection

    def _migrate_once(self) -> None:
        with self._opening:
            if self._ready:
                return
            with self._open(autocommit=True) as connection:
                migrate(connection)
            self._ready = True

    def _open(self, *, autocommit: bool) -> Any:
        from psycopg.rows import dict_row

        return self._psycopg.connect(
            self._url,
            autocommit=autocommit,
            row_factory=dict_row,
            connect_timeout=max(1, math.ceil(self._busy_timeout)),
            options=f"-c lock_timeout={int(self._busy_timeout * 1000)}",
        )


def jsonb(value: Any) -> Any:
    """A value for a JSONB column, rendered the way the ledger renders JSON everywhere."""
    if value is None:
        return None
    from psycopg.types.json import Jsonb

    from oska.governance.ledger import canonical_json

    return Jsonb(value, dumps=canonical_json)
