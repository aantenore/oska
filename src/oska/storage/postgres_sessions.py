"""Session state kept in PostgreSQL, so any process may serve the next turn.

One JSON document per session, as in the SQLite store, so the shape of a
session can grow without a migration. The moment a session was last written is
taken from the clock of the process that wrote it, as SQLite takes it, so a
retention window means the same thing whichever backend holds the record.

A store reads and writes under one namespace, which is part of the key of every
row and of every query, so two stores under different namespaces over one
database never see each other's sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oska.contracts.session import SessionState
from oska.storage.postgres import Database, jsonb
from oska.storage.protocols import DEFAULT_NAMESPACE

__all__ = ["PostgresSessionStore"]


class PostgresSessionStore:
    """Reads and writes session state in a PostgreSQL database, under one namespace."""

    def __init__(
        self,
        url: str,
        *,
        busy_timeout: float | None = None,
        database: Database | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self._database = database or Database(url, busy_timeout=busy_timeout)
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        """The namespace every row of this store is kept under."""
        return self._namespace

    def load(self, session_id: str) -> SessionState:
        """The state of a session, or a new one when it has never been saved."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT state FROM oska_sessions WHERE namespace = %s AND session_id = %s",
                (self._namespace, session_id),
            ).fetchone()
        if row is None:
            return SessionState(session_id=session_id)
        return SessionState.model_validate(row["state"])

    def save(self, state: SessionState) -> None:
        """Store the state of a session, replacing what was there."""
        with self._database.connect() as connection:
            connection.execute(
                "INSERT INTO oska_sessions (namespace, session_id, updated_at, state) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (namespace, session_id) DO UPDATE SET "
                "updated_at = EXCLUDED.updated_at, state = EXCLUDED.state",
                (
                    self._namespace,
                    state.session_id,
                    datetime.now(UTC),
                    jsonb(state.model_dump(mode="json")),
                ),
            )

    def sessions(self) -> list[str]:
        """The identifiers of every stored session, most recently written first."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM oska_sessions WHERE namespace = %s "
                "ORDER BY updated_at DESC",
                (self._namespace,),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def updated_at(self, session_id: str) -> datetime | None:
        """When this session was last written, or nothing when it was never written."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM oska_sessions WHERE namespace = %s AND session_id = %s",
                (self._namespace, session_id),
            ).fetchone()
        return None if row is None else row["updated_at"]

    def exists(self, session_id: str) -> bool:
        """Whether this store holds any state for the session."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM oska_sessions WHERE namespace = %s AND session_id = %s",
                (self._namespace, session_id),
            ).fetchone()
        return row is not None

    def expired(self, before: datetime) -> list[str]:
        """The sessions last written before this moment, oldest first."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM oska_sessions WHERE namespace = %s AND updated_at < %s "
                "ORDER BY updated_at",
                (self._namespace, before),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def delete(self, session_id: str) -> int:
        """Remove the stored state of one session and report whether there was any."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM oska_sessions WHERE namespace = %s AND session_id = %s",
                (self._namespace, session_id),
            )
            return int(cursor.rowcount)
