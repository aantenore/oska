"""Audit event sinks, and the diagnostic traces kept beside them."""

from oska.audit.events import new_event
from oska.audit.sink import AuditSink, JsonlSink, MemorySink, MultiSink
from oska.audit.trace import (
    JsonlTraceSink,
    MemoryTraceSink,
    NullTraceSink,
    Span,
    TraceSink,
)

__all__ = [
    "AuditSink",
    "JsonlSink",
    "JsonlTraceSink",
    "MemorySink",
    "MemoryTraceSink",
    "MultiSink",
    "NullTraceSink",
    "Span",
    "TraceSink",
    "new_event",
]
