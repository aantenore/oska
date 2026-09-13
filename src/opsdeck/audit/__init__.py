"""Audit event sinks."""

from opsdeck.audit.events import new_event
from opsdeck.audit.sink import AuditSink, JsonlSink, MemorySink, MultiSink

__all__ = [
    "AuditSink",
    "JsonlSink",
    "MemorySink",
    "MultiSink",
    "new_event",
]
