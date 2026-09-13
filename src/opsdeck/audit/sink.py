"""Where audit events go.

A sink takes an event and is done with it. Nothing downstream may change what
the runtime does, so a sink returns nothing and the runtime never reads back
what it wrote.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from opsdeck.contracts.events import AuditEvent


@runtime_checkable
class AuditSink(Protocol):
    """Anything that accepts audit events."""

    def emit(self, event: AuditEvent) -> None:
        """Take one event."""
        ...


class MemorySink:
    """Keeps events in a list. Used by tests and by the turn result."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        """Append the event."""
        self.events.append(event)

    def clear(self) -> None:
        """Drop everything held."""
        self.events.clear()


class JsonlSink:
    """Appends one JSON object per line to a file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """File this sink appends to."""
        return self._path

    def emit(self, event: AuditEvent) -> None:
        """Append the event as a single line."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")


class MultiSink:
    """Passes every event to each of the sinks it holds, in order."""

    def __init__(self, *sinks: AuditSink) -> None:
        self.sinks: list[AuditSink] = list(sinks)

    def emit(self, event: AuditEvent) -> None:
        """Hand the event to every sink."""
        for sink in self.sinks:
            sink.emit(event)
