"""Bringing a SQLite file up before its first use, when others may be doing the same.

Both SQLite stores keep their file in write-ahead mode and create their tables on
first use. Switching a file to write-ahead mode takes a lock that SQLite does
not wait for the way it waits for an ordinary write, so two processes opening a
fresh file at the same moment can see one of them told the file is locked. The
preparation is therefore tried again, briefly, until it succeeds or the busy
timeout of the store runs out.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

__all__ = ["prepare"]

_PAUSE_SECONDS = 0.01


def prepare(
    path: Path,
    busy_timeout: float,
    schema: str,
    widen: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    """Create the file, put it in write-ahead mode and apply its schema, once it can be."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + busy_timeout
    while True:
        connection = sqlite3.connect(path, timeout=busy_timeout)
        connection.row_factory = sqlite3.Row
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(schema)
            if widen is not None:
                widen(connection)
            connection.commit()
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or time.monotonic() >= deadline:
                raise
        finally:
            connection.close()
        time.sleep(_PAUSE_SECONDS)
