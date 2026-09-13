"""Session state kept in a SQLite file.

The state is stored as one JSON document per session, so the shape can grow
without a migration. Every call opens and closes its own connection: a session
is read at the start of a turn and written at the end of it, and nothing holds
the file in between.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from opsdeck.engine.state import SessionState

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

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._ready = False

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            if not self._ready:
                connection.executescript(_SCHEMA)
                self._ready = True
            yield connection
            connection.commit()
        finally:
            connection.close()
