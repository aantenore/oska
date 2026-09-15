"""What a deployment can say about itself, read back from what it already wrote.

The numbers a person is woken up for are not the lines a system logs. Five say
almost everything worth alerting on: how long a turn takes at the tail, how
often the model failed and a deterministic template spoke instead, how often the
read-back after a write did not confirm it, how often a guardrail refused, and
how often a request found no procedure at all. The last of those is not an
error rate; it is the product signal that the catalog is missing something.

Nothing here is recorded for its own sake. Every number is derived from the
audit trail, which a deployment has to keep anyway, and from the diagnostic
traces, which it does not. A deployment that runs with tracing off — the default
— can still answer four of the five, and this package says so in the report
rather than reporting a zero for the one it cannot.
"""

from opsdeck.observability.metrics import (
    Measure,
    ServiceReport,
    Window,
    service_report,
)
from opsdeck.observability.report import render_json, render_text

__all__ = [
    "Measure",
    "ServiceReport",
    "Window",
    "render_json",
    "render_text",
    "service_report",
]
