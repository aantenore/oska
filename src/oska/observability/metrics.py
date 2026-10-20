"""The service-level numbers, computed from the audit trail and the traces.

Two sources, both already written for other reasons. The audit trail is typed,
durable and required, and says what each turn did: whether a template spoke
because no model would, whether a guardrail refused, whether the read-back after
a write confirmed it, and whether anything in the catalog matched the request at
all. The traces say how long each part of a turn took, and they are optional and
off by default.

So a number is either computed or it is missing, and a missing number says why.
A rate reported as zero because nothing recorded it reads exactly like a rate
that is genuinely zero, and the two mean opposite things: one is a healthy
deployment and the other is a blind one. `Measure` keeps them apart.

One measure is computed and still incomplete. A turn falls back to a template
when no model is configured, and when the plan a model returned did not hold up;
both are audited. It also falls back when the model provider fails while writing
the reply text, and that is not audited today — the reply is written, the turn
succeeds, and nothing records which of the two wrote it. The fallback rate
therefore carries a note saying what it does not cover, and the way to close
that gap is an audit event, not a trace.

The turn count is a number like the others. A trail that exists and holds no
turn says the window was quiet; a deployment with no trail at all says nothing,
and its count is reported missing rather than zero.

Reading the whole trail is not free, and a monitoring system asks every few
seconds. `ReportCache` keeps the report it computed for a short interval,
`DEFAULT_REPORT_INTERVAL_SECONDS` unless its caller says otherwise, and answers
from it until the interval is over.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path

from pydantic import BaseModel, Field

from oska.audit.sink import audit_trail
from oska.contracts.events import AuditEvent
from oska.contracts.receipt import VerificationStatus
from oska.storage.protocols import AuditStore

__all__ = [
    "DEFAULT_REPORT_INTERVAL_SECONDS",
    "Measure",
    "ReportCache",
    "ServiceReport",
    "Window",
    "read_spans",
    "service_report",
]

DEFAULT_REPORT_INTERVAL_SECONDS = 15.0
"""How long a computed report is served again before the trail is read anew."""

TURN_SPAN = "turn"
BLOCKED = "blocked"
NO_MODEL_REPLY = "unavailable"
OFF_SCOPE_REPLY = "off_scope"

_NO_TURNS = "no turn was recorded in this window"
_NO_TRAIL = "this deployment has no audit trail, so no turn could be counted"
_NO_WRITES = "no write was read back in this window"
_TRACING_OFF = (
    "tracing is off in this deployment, so no turn duration was recorded; "
    "switch it on to measure latency"
)
_NO_SPANS = "tracing is on but no turn was traced in this window"
_UNTRACED_REPLIES = (
    "a reply the model provider failed to write is not audited, "
    "so a fallback at the reply step is not counted here"
)


class Window(BaseModel):
    """The stretch of the trail a report was computed over."""

    since: datetime | None = None
    until: datetime | None = None
    days: int | None = None


class Measure(BaseModel):
    """One number, or the reason there is none.

    `value` is the number. `basis` is how many things it was computed over, and
    `over` names them in words, so a rate of 1.0 over two turns is not read as a
    rate of 1.0 over two thousand. `missing` is filled in exactly when there is
    no value, and `note` when there is one that does not cover everything its
    name suggests.
    """

    value: float | None = None
    basis: int = 0
    over: str = ""
    missing: str = ""
    note: str = ""

    @property
    def known(self) -> bool:
        """True when the deployment recorded enough to compute this."""
        return self.value is not None


class ServiceReport(BaseModel):
    """What one deployment can say about itself over one window."""

    turns: int = 0
    turns_missing: str = ""
    latency_p50: Measure = Field(default_factory=Measure)
    latency_p95: Measure = Field(default_factory=Measure)
    fallback_rate: Measure = Field(default_factory=Measure)
    verification_failure_rate: Measure = Field(default_factory=Measure)
    guardrail_block_rate: Measure = Field(default_factory=Measure)
    unsupported_rate: Measure = Field(default_factory=Measure)
    window: Window = Field(default_factory=Window)

    def measures(self) -> list[tuple[str, Measure]]:
        """Every measure, in the order a report prints them."""
        return [
            ("latency_p50_ms", self.latency_p50),
            ("latency_p95_ms", self.latency_p95),
            ("fallback_rate", self.fallback_rate),
            ("verification_failure_rate", self.verification_failure_rate),
            ("guardrail_block_rate", self.guardrail_block_rate),
            ("unsupported_rate", self.unsupported_rate),
        ]

    def unknown(self) -> list[tuple[str, str]]:
        """The measures that could not be computed, each with the reason."""
        return [(name, measure.missing) for name, measure in self.measures() if not measure.known]


@dataclass
class _Turn:
    """What one turn of the trail shows, as the measures need it."""

    at: datetime
    reply_kind: str = ""
    plan_failed: bool = False
    guardrail_blocked: bool = False
    nothing_matched: bool = False
    verified: list[bool] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        """True when a deterministic template spoke because no model could."""
        return self.reply_kind == NO_MODEL_REPLY or self.plan_failed

    @property
    def unsupported(self) -> bool:
        """True when nothing in the catalog matched what was asked."""
        return self.reply_kind == OFF_SCOPE_REPLY or self.nothing_matched


def service_report(
    audit_path: Path | AuditStore,
    *,
    since_days: int | None = None,
    trace_path: Path | None = None,
    now: datetime | None = None,
) -> ServiceReport:
    """Read the record of a deployment and report what it can say about itself.

    The trail is a file path or an audit store, so a deployment whose replicas
    share one trail reports what all of them served. `since_days` bounds the
    window backwards from `now`; without it the window opens at the first event
    of the trail. `trace_path` is where the durations
    are read from; without it, the trail is asked, because a deployment that
    records traces says in the audit event of every turn where they go.
    """
    moment = _aware(now)
    trail = audit_trail(audit_path)
    events = trail.events()
    since = moment - timedelta(days=since_days) if since_days is not None else None
    kept = [event for event in events if since is None or _aware(event.at) >= since]
    turns = _turns(kept)
    # A trail is appended to and is normally in order; the bounds are taken from
    # the moments themselves so that a trail concatenated out of order still
    # reports a window that contains what was counted.
    moments = [_aware(event.at) for event in kept]
    window = Window(
        since=since or (min(moments) if moments else None),
        until=max(moments) if moments else moment,
        days=since_days,
    )
    durations = _durations(trace_path or _traced_to(kept), window)
    return ServiceReport(
        turns=len(turns),
        turns_missing="" if trail.exists() else _NO_TRAIL,
        latency_p50=_percentile(durations, 0.50),
        latency_p95=_percentile(durations, 0.95),
        fallback_rate=_rate(
            sum(1 for turn in turns if turn.fell_back),
            len(turns),
            "turns",
            _NO_TURNS,
            note=_UNTRACED_REPLIES,
        ),
        verification_failure_rate=_verification_rate(turns),
        guardrail_block_rate=_rate(
            sum(1 for turn in turns if turn.guardrail_blocked),
            len(turns),
            "turns",
            _NO_TURNS,
        ),
        unsupported_rate=_rate(
            sum(1 for turn in turns if turn.unsupported),
            len(turns),
            "turns",
            _NO_TURNS,
        ),
        window=window,
    )


class ReportCache:
    """The last report computed for a trail, served again for a short interval.

    The interval is a parameter and not configuration here: the caller that
    owns the configuration decides where it comes from. A report is kept per
    trail and trace file, so two deployments read through one cache never
    answer with each other's numbers.
    """

    def __init__(
        self,
        interval_seconds: float = DEFAULT_REPORT_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = max(0.0, float(interval_seconds))
        self._clock = clock
        self._held: dict[tuple[str, str], tuple[float, ServiceReport]] = {}
        self._lock = threading.Lock()

    @property
    def interval_seconds(self) -> float:
        """How long a computed report is served again."""
        return self._interval

    def report(
        self, audit_path: Path | AuditStore, *, trace_path: Path | None = None
    ) -> ServiceReport:
        """The report of this trail, computed at most once per interval."""
        trail = audit_trail(audit_path)
        key = (trail.location, "" if trace_path is None else str(trace_path))
        now = self._clock()
        with self._lock:
            held = self._held.get(key)
            if held is not None and now - held[0] < self._interval:
                return held[1]
        computed = service_report(trail, trace_path=trace_path)
        with self._lock:
            self._held[key] = (now, computed)
        return computed


def read_spans(trace_path: Path) -> list[dict]:
    """Every trace span of a file, in the order it was appended.

    A line that is not a readable span is skipped, for the reason the audit
    reader skips one: the file is appended to while it is read, and a
    half-written last line must not stop the rest from being read.
    """
    path = Path(trace_path)
    if not path.is_file():
        return []
    spans: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                span = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(span, dict):
                spans.append(span)
    return spans


def _turns(events: Sequence[AuditEvent]) -> list[_Turn]:
    """One entry per turn of the trail, oldest first."""
    grouped: dict[tuple[str, str], list[AuditEvent]] = {}
    for event in events:
        grouped.setdefault((event.session_id, event.turn_id), []).append(event)
    turns = [
        _turn(group)
        for group in grouped.values()
        if any(event.kind == "turn" for event in group)
    ]
    turns.sort(key=lambda entry: entry.at)
    return turns


def _turn(events: Sequence[AuditEvent]) -> _Turn:
    turn = _Turn(at=_aware(events[0].at))
    for event in events:
        data = event.data
        if event.kind == "turn":
            turn.at = _aware(event.at)
        elif event.kind == "reply":
            turn.reply_kind = str(data.get("kind", ""))
        elif event.kind == "plan" and data.get("valid") is False:
            turn.plan_failed = True
        elif event.kind == "guardrail" and str(data.get("action", "")) == BLOCKED:
            turn.guardrail_blocked = True
        elif event.kind == "discovery" and not data.get("chosen"):
            turn.nothing_matched = True
        elif event.kind == "verify":
            turn.verified.append(
                str(data.get("verification", "")) == VerificationStatus.verified
            )
    return turn


def _verification_rate(turns: Sequence[_Turn]) -> Measure:
    """How often a read-back did not confirm the write it followed."""
    checks = [confirmed for turn in turns for confirmed in turn.verified]
    return _rate(
        sum(1 for confirmed in checks if not confirmed),
        len(checks),
        "read-backs",
        _NO_WRITES,
    )


def _rate(part: int, whole: int, over: str, missing: str, note: str = "") -> Measure:
    if whole <= 0:
        return Measure(basis=0, over=over, missing=missing)
    return Measure(value=round(part / whole, 4), basis=whole, over=over, note=note)


def _percentile(durations: Sequence[float] | None, quantile: float) -> Measure:
    """The nearest-rank percentile of the turn durations, in milliseconds."""
    if durations is None:
        return Measure(over="traced turns", missing=_TRACING_OFF)
    if not durations:
        return Measure(over="traced turns", missing=_NO_SPANS)
    ordered = sorted(durations)
    rank = max(1, ceil(quantile * len(ordered)))
    return Measure(
        value=round(ordered[rank - 1], 3),
        basis=len(ordered),
        over="traced turns",
    )


def _durations(trace_path: Path | None, window: Window) -> list[float] | None:
    """How long each traced turn of the window took, or nothing when none were traced."""
    if trace_path is None:
        return None
    path = Path(trace_path)
    if not path.is_file():
        return None
    kept: list[float] = []
    for span in read_spans(path):
        if span.get("name") != TURN_SPAN:
            continue
        started = _moment(span.get("started_at"))
        if started is None or not _inside(started, window):
            continue
        duration = span.get("duration_ms")
        if isinstance(duration, (int, float)):
            kept.append(float(duration))
    return kept


def _traced_to(events: Iterable[AuditEvent]) -> Path | None:
    """Where this deployment recorded its traces, as its own trail reports it."""
    found: Path | None = None
    for event in events:
        if event.kind != "trace" or not event.data.get("recording"):
            continue
        where = str(event.data.get("where", ""))
        if where:
            found = Path(where)
    return found


def _inside(moment: datetime, window: Window) -> bool:
    if window.since is not None and moment < window.since:
        return False
    return window.until is None or moment <= window.until


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _aware(moment: datetime | None) -> datetime:
    """The moment in UTC. A trail written without an offset is read as UTC."""
    if moment is None:
        return datetime.now(UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
