"""Construction of audit events."""

from __future__ import annotations

from datetime import UTC, datetime

from oska.contracts.events import AuditEvent, EventKind


def new_event(
    kind: EventKind,
    session_id: str,
    turn_id: str,
    data: dict | None = None,
) -> AuditEvent:
    """An event stamped with the moment it was raised."""
    return AuditEvent(
        kind=kind,
        session_id=session_id,
        turn_id=turn_id,
        at=datetime.now(UTC),
        data=dict(data or {}),
    )
