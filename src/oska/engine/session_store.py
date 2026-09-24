"""Session state kept in a SQLite file.

The state is stored as one JSON document per session, so the shape can grow
without a migration. Every call opens and closes its own connection: a session
is read at the start of a turn and written at the end of it, and nothing holds
the file in between.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from oska.config import DEFAULT_BUSY_TIMEOUT_SECONDS
from oska.engine.state import SessionState

__all__ = ["SessionStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL
);
"""



class SessionStore:
    """Reads and writes session state."""

    def __init__(self, path: Path | str, *, busy_timeout: float | None = None) -> None:
        self._path = Path(path)
        self._ready = False
        self._opening = threading.Lock()
        self._busy_timeout = DEFAULT_BUSY_TIMEOUT_SECONDS if busy_timeout is None else busy_timeout

    @property
    def path(self) -> Path:
        """File this store writes to."""
        return self._path

    def load(self, session_id: str) -> SessionState:
        """The state of a session, or a new one when it has never been saved."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return SessionState(session_id=session_id)
        return SessionState.model_validate_json(row["state"])

    def save(self, state: SessionState) -> None:
        """Store the state of a session, replacing what was there."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, updated_at, state) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at, "
                "state = excluded.state",
                (
                    state.session_id,
                    datetime.now(UTC).isoformat(),
                    state.model_dump_json(),
                ),
            )

    def sessions(self) -> list[str]:
        """The identifiers of every stored session, most recently written first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [row["session_id"] for row in rows]

    def updated_at(self, session_id: str) -> datetime | None:
        """When this session was last written, or nothing when it was never written."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return None if row is None else datetime.fromisoformat(row["updated_at"])

    def expired(self, before: datetime) -> list[str]:
        """The sessions last written before this moment, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM sessions WHERE updated_at < ? ORDER BY updated_at",
                (before.isoformat(),),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def delete(self, session_id: str) -> int:
        """Remove the stored state of one session and report whether there was any."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            return int(cursor.rowcount)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._opening:
            if not self._ready:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                starting = sqlite3.connect(self._path, timeout=self._busy_timeout)
                try:
                    starting.execute("PRAGMA journal_mode=WAL")
                    starting.executescript(_SCHEMA)
                    starting.commit()
                finally:
                    starting.close()
                self._ready = True
        connection = sqlite3.connect(self._path, timeout=self._busy_timeout)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()
