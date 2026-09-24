"""The service report in Prometheus text exposition format, hand-written.

No client library: the format is a `# HELP` line, a `# TYPE` line and a sample
per series, and pulling in a dependency to produce a few lines of text would be
disproportionate. Every metric name is prefixed `oska_`, the namespace
Prometheus itself recommends. A series carries the labels its caller names and
no others: a deployment serving a single catalog has nothing to distinguish a
series by, and a platform labels each series with the `tenant` and the `domain`
it was computed for, one series per (tenant, domain), under one `# HELP` and one
`# TYPE` per metric.

Every number is a gauge over the report window, the turn count included. It is
recomputed from the audit trail, and removing a session from the trail lowers
it, so it is not a counter and is not called one.

A number the deployment could not compute is left out of the body rather than
exported as zero, for the reason `oska.observability.metrics.Measure` exists at
all: a rate reported as zero because nothing recorded it reads exactly like a
rate that is genuinely zero, and a monitoring system cannot tell the two apart
from a number alone. So the metric's `# HELP` line still prints, with the
reason appended when no series has a value, and the sample does not: a scrape
that reads a missing series is told the truth, and one that reads a zero is
not. On a labeled series the reason is a comment line of its own. The turn
count follows the same rule: a deployment with no audit trail has no count.

`METRIC_NAMES` is the single source of the name set. `ops/slo.md` and
`ops/alerts.yml` are written against it, and `tests/unit/test_metrics.py`
checks all three stay the same set, so a name changed in one and not the
others fails the build instead of a dashboard.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from oska.observability.metrics import Measure, ServiceReport

__all__ = ["METRIC_NAMES", "render_prometheus", "render_series"]

_TURNS = "oska_turns"
_LATENCY_P50 = "oska_latency_p50_ms"
_LATENCY_P95 = "oska_latency_p95_ms"
_FALLBACK_RATE = "oska_fallback_rate"
_VERIFICATION_FAILURE_RATE = "oska_verification_failure_rate"
_GUARDRAIL_BLOCK_RATE = "oska_guardrail_block_rate"
_UNSUPPORTED_RATE = "oska_unsupported_rate"

_HELP: dict[str, str] = {
    _TURNS: "Turns recorded in the audit trail over the reported window.",
    _LATENCY_P50: "Median turn duration in milliseconds, from diagnostic traces.",
    _LATENCY_P95: "95th percentile turn duration in milliseconds, from diagnostic traces.",
    _FALLBACK_RATE: (
        "Share of turns a deterministic template answered because no model could."
    ),
    _VERIFICATION_FAILURE_RATE: (
        "Share of write read-backs that did not confirm the write they followed."
    ),
    _GUARDRAIL_BLOCK_RATE: "Share of turns a declared guardrail refused.",
    _UNSUPPORTED_RATE: "Share of turns no procedure in the catalog matched.",
}

_TYPE: dict[str, str] = {
    _TURNS: "gauge",
    _LATENCY_P50: "gauge",
    _LATENCY_P95: "gauge",
    _FALLBACK_RATE: "gauge",
    _VERIFICATION_FAILURE_RATE: "gauge",
    _GUARDRAIL_BLOCK_RATE: "gauge",
    _UNSUPPORTED_RATE: "gauge",
}

def _turns(report: ServiceReport) -> Measure:
    """The turn count as a measure: a count of zero is a fact, no trail is not."""
    if report.turns_missing:
        return Measure(over="turns", missing=report.turns_missing)
    return Measure(value=float(report.turns), basis=report.turns, over="turns")


_GAUGES: list[tuple[str, Callable[[ServiceReport], Measure]]] = [
    (_TURNS, _turns),
    (_LATENCY_P50, lambda report: report.latency_p50),
    (_LATENCY_P95, lambda report: report.latency_p95),
    (_FALLBACK_RATE, lambda report: report.fallback_rate),
    (_VERIFICATION_FAILURE_RATE, lambda report: report.verification_failure_rate),
    (_GUARDRAIL_BLOCK_RATE, lambda report: report.guardrail_block_rate),
    (_UNSUPPORTED_RATE, lambda report: report.unsupported_rate),
]

METRIC_NAMES = frozenset(_HELP)
"""Every metric name this exposition can render, `oska_`-prefixed."""


def render_prometheus(report: ServiceReport, *, domain: str = "") -> str:
    """One report as Prometheus text exposition format (version 0.0.4).

    `domain` labels the series when given; a deployment serving one catalog
    names none.
    """
    return render_series([({"domain": domain} if domain else {}, report)])


def render_series(series: Sequence[tuple[Mapping[str, str], ServiceReport]]) -> str:
    """Several reports, each under its own labels, as one exposition.

    Each metric is described once and then sampled once per series that has a
    value for it. A series without one is left out, and says why.
    """
    lines: list[str] = []
    for name, getter in _GAUGES:
        measured = [(_label(labels), getter(report)) for labels, report in series]
        lines.extend(_metric_lines(name, measured))
    return "\n".join(lines) + "\n"


def _metric_lines(name: str, measured: list[tuple[str, Measure]]) -> list[str]:
    known = [(label, measure) for label, measure in measured if measure.known]
    reasons = {measure.missing for _, measure in measured if not measure.known}
    help_line = f"# HELP {name} {_HELP[name]}"
    if not known and len(reasons) == 1:
        help_line += f" (not computed: {next(iter(reasons))})"
    lines = [help_line]
    if known:
        lines.append(f"# TYPE {name} {_TYPE[name]}")
    for label, measure in measured:
        if measure.known:
            lines.append(f"{name}{label} {float(measure.value or 0.0)}")
        elif label and (known or len(reasons) > 1):
            lines.append(f"# {name}{label} not computed: {measure.missing}")
    return lines


def _label(labels: Mapping[str, str]) -> str:
    kept = [(key, value) for key, value in labels.items() if value]
    if not kept:
        return ""
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in kept) + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
