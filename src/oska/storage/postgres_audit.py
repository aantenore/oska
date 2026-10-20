"""The audit trail kept in PostgreSQL, so every replica appends to one trail.

One row per event, in the order the database received them: the identity
column is the order of the trail, as the line number is in a file. The event is
kept whole as JSON, beside the columns retention and the report select by, so
the shape of an event can grow without a migration.

A store appends and reads under one namespace, which is part of every row and
of every query. `delete_sessions` is what retention calls, and it keeps the
kinds it is told to keep, so the event recording a removal survives the
removal it records. `purge` removes the whole of the namespace, and refuses the
default one.
"""

from __future__ import annotations

from collections.abc import Iterable

from oska.contracts.events import AuditEvent
from oska.storage.postgres import Database, jsonb
from oska.storage.protocols import DEFAULT_NAMESPACE, refuse_default_purge

__all__ = ["PostgresAuditStore"]


class PostgresAuditStore:
    """Appends audit events to a PostgreSQL database and reads them back, under one namespace."""

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
        """The namespace every event of this store is kept under."""
        return self._namespace

    @property
    def location(self) -> str:
        """The database and the namespace, without the address that may carry a credential."""
        return f"postgres {self._database.identity} namespace {self._namespace}"

    def emit(self, event: AuditEvent) -> None:
        """Append one event."""
        with self._database.connect() as connection:
            connection.execute(
                "INSERT INTO oska_audit_events "
                "(namespace, session_id, turn_id, kind, at, event) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    self._namespace,
                    event.session_id,
                    event.turn_id,
                    event.kind,
                    event.at,
                    jsonb(event.model_dump(mode="json")),
                ),
            )

    def events(self) -> list[AuditEvent]:
        """Every readable event of the trail, in the order it was appended."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT event FROM oska_audit_events WHERE namespace = %s ORDER BY seq",
                (self._namespace,),
            ).fetchall()
        events: list[AuditEvent] = []
        for row in rows:
            try:
                events.append(AuditEvent.model_validate(row["event"]))
            except ValueError:
                continue
        return events

    def exists(self) -> bool:
        """Whether any event was ever kept under this namespace."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM oska_audit_events WHERE namespace = %s LIMIT 1",
                (self._namespace,),
            ).fetchone()
        return row is not None

    def delete_sessions(
        self, session_ids: Iterable[str], *, keep_kinds: Iterable[str] = ()
    ) -> dict[str, int]:
        """Remove the events of these sessions, except the kinds kept; count by session."""
        targets = sorted(set(session_ids))
        if not targets:
            return {}
        with self._database.connect() as connection:
            rows = connection.execute(
                "DELETE FROM oska_audit_events WHERE namespace = %s "
                "AND session_id = ANY(%s) AND NOT (kind = ANY(%s)) RETURNING session_id",
                (self._namespace, targets, sorted(set(keep_kinds))),
            ).fetchall()
        dropped: dict[str, int] = {}
        for row in rows:
            dropped[row["session_id"]] = dropped.get(row["session_id"], 0) + 1
        return dropped

    def purge(self) -> int:
        """Remove every event of this namespace and report how many went.

        Only this store's own namespace is reached, and the default namespace
        is refused: a purge is for a namespace its caller created and is done with.
        """
        refuse_default_purge(self._namespace)
        with self._database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM oska_audit_events WHERE namespace = %s", (self._namespace,)
            )
            return int(cursor.rowcount)
