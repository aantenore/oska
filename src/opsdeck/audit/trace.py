"""Diagnostic traces: how long each part of a turn took, and what it was nested in.

A trace answers "why was that slow" and "which way did the turn go". It is not
the audit record and is never a substitute for one: the audit trail is typed,
durable and required, traces are optional, off by default, and a deployment may
throw them away without losing anything it has to keep. Nothing in the runtime
reads a trace back, and no decision is taken from one. When a question can only
be answered from a trace, the answer is to raise an audit event, not to turn
tracing on.

A span records a name, the fields handed to it, the moment it opened and how
long it stayed open, and the span it opened inside. Sinks differ only in where
that goes: nowhere, a list, or a file of one JSON object per line.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol, runtime_checkable

__all__ = [
    "JsonlTraceSink",
    "MemoryTraceSink",
    "NullTraceSink",
    "Span",
    "TraceSink",
    "destination",
]


@dataclass
class Span:
    """One timed stretch of a turn."""

    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_id: str | None = None
    depth: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float = 0.0
    fields: dict = field(default_factory=dict)

    def set(self, **fields: object) -> None:
        """Add fields to this span while it is open."""
        self.fields.update(fields)

    def as_dict(self) -> dict:
        """The span as a plain object, with the duration rounded to the microsecond."""
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "fields": dict(self.fields),
        }


@runtime_checkable
class TraceSink(Protocol):
    """Anything that can open a span."""

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open a span, and record it when the block ends."""
        ...


class NullTraceSink:
    """Records nothing. In force whenever tracing is off, which is the default."""

    @contextmanager
    def span(self, name: str, **fields: object) -> Iterator[Span]:
        """Hand back a span that goes nowhere."""
        yield Span(name=name, fields=dict(fields))


class _RecordingSink:
    """Timing and nesting, shared by the sinks that keep what they record."""

    def __init__(self) -> None:
        self._open: list[Span] = []

    @contextmanager
    def span(self, name: str, **fields: object) -> Iterator[Span]:
        """Time this stretch and record it under the span it opened inside."""
        parent = self._open[-1] if self._open else None
        current = Span(
            name=name,
            parent_id=parent.span_id if parent else None,
            depth=len(self._open),
            fields=dict(fields),
        )
        self._open.append(current)
        started = perf_counter()
        try:
            yield current
        finally:
            current.duration_ms = (perf_counter() - started) * 1000
            self._open.pop()
            self._record(current)

    def _record(self, span: Span) -> None:
        raise NotImplementedError


class MemoryTraceSink(_RecordingSink):
    """Keeps spans in a list, in the order they closed. Used by tests."""

    def __init__(self) -> None:
        super().__init__()
        self.spans: list[Span] = []

    def _record(self, span: Span) -> None:
        self.spans.append(span)

    def named(self, name: str) -> list[Span]:
        """Every recorded span with this name."""
        return [span for span in self.spans if span.name == name]

    def clear(self) -> None:
        """Drop everything held."""
        self.spans.clear()


class JsonlTraceSink(_RecordingSink):
    """Appends one JSON object per closed span to a file."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """File this sink appends to."""
        return self._path

    def _record(self, span: Span) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(span.as_dict(), sort_keys=True) + "\n")


def destination(sink: TraceSink) -> str:
    """Where a sink puts what it records, in one word or one path."""
    path = getattr(sink, "path", None)
    if path is not None:
        return str(path)
    return type(sink).__name__.removesuffix("TraceSink").lower() or "none"
