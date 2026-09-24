"""What one turn accumulates while it runs: its events, its outcomes and what it read."""

from __future__ import annotations

from dataclasses import dataclass, field

from oska.audit.events import new_event
from oska.audit.sink import AuditSink
from oska.contracts.common import Outcome
from oska.contracts.events import AuditEvent, EventKind
from oska.engine.state import SessionState

__all__ = ["TurnContext"]


@dataclass
class TurnContext:
    """What one turn accumulates: its events, its outcomes and what it read."""

    state: SessionState
    turn_id: str
    audit: AuditSink
    events: list[AuditEvent] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    entities: dict[str, dict] = field(default_factory=dict)
    entity_ids: list[str] = field(default_factory=list)

    def emit(self, kind: EventKind, data: dict | None = None) -> AuditEvent:
        """Raise one audit event, keep it on the turn and hand it to the sink."""
        event = new_event(kind, self.state.session_id, self.turn_id, data)
        self.events.append(event)
        self.audit.emit(event)
        return event

    def record(self, outcome: Outcome) -> Outcome:
        """Add one outcome to the turn."""
        self.outcomes.append(outcome)
        return outcome
