"""Printing a service report, in prose or as JSON.

Both forms say the same thing, and both name what is missing. A report that
silently left out the numbers it could not compute would read as a healthy
deployment; a report that printed them as zero would read as a perfect one.
Neither is true, so an absent number is printed with the reason it is absent.
"""

from __future__ import annotations

import json

from oska.observability.metrics import Measure, ServiceReport

__all__ = ["render_json", "render_text"]

_RATES = frozenset(
    {
        "fallback_rate",
        "verification_failure_rate",
        "guardrail_block_rate",
        "unsupported_rate",
    }
)


def render_text(report: ServiceReport) -> str:
    """The report as lines a person reads."""
    lines = [f"{report.turns} turn(s) {_window(report)}"]
    for name, measure in report.measures():
        lines.append(f"  {name}: {_value(name, measure)}")
        if measure.known and measure.note:
            lines.append(f"    note: {measure.note}")
    unknown = report.unknown()
    if unknown:
        lines.append("not computed:")
        lines.extend(f"  {name}: {reason}" for name, reason in unknown)
    return "\n".join(lines)


def render_json(report: ServiceReport) -> str:
    """The report as JSON, with the same measures and the same reasons."""
    document = report.model_dump(mode="json")
    document["unknown"] = {name: reason for name, reason in report.unknown()}
    return json.dumps(document, indent=2, sort_keys=True)


def _window(report: ServiceReport) -> str:
    window = report.window
    if window.since is None and window.until is None:
        return "over an empty record"
    days = f"the last {window.days} day(s)" if window.days is not None else "the whole record"
    since = window.since.isoformat() if window.since else "the first event"
    until = window.until.isoformat() if window.until else "the last event"
    return f"over {days}, {since} to {until}"


def _value(name: str, measure: Measure) -> str:
    if not measure.known:
        return "not computed"
    shown = f"{measure.value}" if name in _RATES else f"{measure.value} ms"
    return f"{shown} over {measure.basis} {measure.over}"
