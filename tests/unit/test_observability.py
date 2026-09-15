"""The service-level numbers, and the ones a deployment cannot produce."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opsdeck.cli.commands_slo import app
from opsdeck.contracts.events import AuditEvent, EventKind
from opsdeck.observability.metrics import service_report
from opsdeck.observability.report import render_json, render_text

runner = CliRunner()

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
ROUTED = "runbook.service_restart"


def _event(kind: EventKind, turn_id: str, at: datetime, data: dict) -> AuditEvent:
    return AuditEvent(kind=kind, session_id="s1", turn_id=turn_id, at=at, data=dict(data))


def _turn(
    turn: str,
    at: datetime,
    *,
    reply: str = "outcomes",
    chosen: str | None = ROUTED,
    plan_valid: bool | None = True,
    blocked: bool = False,
    verifications: tuple[str, ...] = (),
) -> list[AuditEvent]:
    """One turn of a trail, as the engine would have written it."""
    events = [_event("turn", turn, at, {"turn": 1, "text": "something"})]
    events.append(_event("discovery", turn, at, {"ranked": [], "chosen": chosen, "tie": False}))
    if plan_valid is not None:
        events.append(_event("plan", turn, at, {"runbook_id": ROUTED, "valid": plan_valid}))
    if blocked:
        events.append(
            _event("guardrail", turn, at, {"point": "input", "action": "blocked", "reason": "no"})
        )
    for index, status in enumerate(verifications):
        events.append(
            _event(
                "verify", turn, at, {"receipt_id": f"{turn}-{index}", "verification": status}
            )
        )
    events.append(_event("reply", turn, at, {"kind": reply, "pending": None}))
    return events


def _trail(path: Path, events: list[AuditEvent]) -> Path:
    path.write_text(
        "".join(f"{event.model_dump_json()}\n" for event in events), encoding="utf-8"
    )
    return path


def _spans(path: Path, durations: list[float], at: datetime) -> Path:
    path.write_text(
        "".join(
            json.dumps(
                {
                    "name": "turn",
                    "span_id": f"s{index}",
                    "parent_id": None,
                    "depth": 0,
                    "started_at": at.isoformat(),
                    "duration_ms": duration,
                    "fields": {},
                }
            )
            + "\n"
            for index, duration in enumerate(durations)
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def trail(tmp_path: Path) -> Path:
    """A trail of six turns: one fell back, one was refused, one found nothing."""
    events = [
        *_turn("t1", NOW - timedelta(minutes=50), verifications=("verified",)),
        *_turn("t2", NOW - timedelta(minutes=40), plan_valid=False),
        *_turn("t3", NOW - timedelta(minutes=30), blocked=True, reply="guardrail"),
        *_turn("t4", NOW - timedelta(minutes=20), reply="off_scope", chosen=None),
        *_turn("t5", NOW - timedelta(minutes=10), verifications=("failed",)),
        *_turn("t6", NOW - timedelta(minutes=5), verifications=("unverified", "verified")),
    ]
    return _trail(tmp_path / "audit.jsonl", events)


def test_every_turn_of_the_trail_is_counted(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert report.turns == 6


def test_a_turn_a_template_spoke_for_counts_as_a_fallback(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert report.fallback_rate.value == round(1 / 6, 4)
    assert report.fallback_rate.basis == 6
    assert report.fallback_rate.over == "turns"


def test_the_fallback_rate_says_what_it_does_not_cover(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert "not audited" in report.fallback_rate.note


def test_a_turn_that_found_no_procedure_counts_as_unsupported(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert report.unsupported_rate.value == round(1 / 6, 4)


def test_a_turn_a_guardrail_refused_counts_as_a_block(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert report.guardrail_block_rate.value == round(1 / 6, 4)


def test_the_verification_rate_counts_read_backs_and_not_turns(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert report.verification_failure_rate.basis == 4
    assert report.verification_failure_rate.over == "read-backs"
    assert report.verification_failure_rate.value == round(2 / 4, 4)


def test_a_window_with_no_write_says_so_instead_of_reporting_zero(tmp_path: Path) -> None:
    trail = _trail(tmp_path / "audit.jsonl", _turn("t1", NOW))

    report = service_report(trail, now=NOW)

    assert not report.verification_failure_rate.known
    assert "no write" in report.verification_failure_rate.missing


def test_an_empty_record_reports_no_rate_at_all(tmp_path: Path) -> None:
    report = service_report(tmp_path / "absent.jsonl", now=NOW)

    assert report.turns == 0
    assert [name for name, _ in report.unknown()] == [
        "latency_p50_ms",
        "latency_p95_ms",
        "fallback_rate",
        "verification_failure_rate",
        "guardrail_block_rate",
        "unsupported_rate",
    ]


def test_latency_is_not_computed_when_the_deployment_records_no_trace(trail: Path) -> None:
    report = service_report(trail, now=NOW)

    assert not report.latency_p50.known
    assert "tracing is off" in report.latency_p50.missing
    assert "tracing is off" in report.latency_p95.missing


def test_latency_is_the_nearest_rank_percentile_of_the_traced_turns(
    trail: Path, tmp_path: Path
) -> None:
    traces = _spans(
        tmp_path / "traces.jsonl", [40.0, 10.0, 30.0, 20.0], NOW - timedelta(minutes=30)
    )

    report = service_report(trail, now=NOW, trace_path=traces)

    assert report.latency_p50.value == 20.0
    assert report.latency_p95.value == 40.0
    assert report.latency_p50.basis == 4


def test_the_trail_says_where_this_deployment_put_its_traces(
    tmp_path: Path,
) -> None:
    traces = _spans(tmp_path / "traces.jsonl", [12.0], NOW - timedelta(minutes=5))
    events = _turn("t1", NOW - timedelta(minutes=5))
    events.insert(
        1,
        _event(
            "trace",
            "t1",
            NOW - timedelta(minutes=5),
            {"recording": True, "where": str(traces)},
        ),
    )
    trail = _trail(tmp_path / "audit.jsonl", events)

    report = service_report(trail, now=NOW)

    assert report.latency_p50.value == 12.0


def test_tracing_on_with_nothing_traced_in_the_window_says_so(
    trail: Path, tmp_path: Path
) -> None:
    traces = _spans(tmp_path / "traces.jsonl", [12.0], NOW - timedelta(days=90))

    report = service_report(trail, now=NOW, since_days=1, trace_path=traces)

    assert not report.latency_p50.known
    assert "no turn was traced" in report.latency_p50.missing


def test_the_window_bounds_what_is_counted(tmp_path: Path) -> None:
    events = [
        *_turn("old", NOW - timedelta(days=30), reply="off_scope", chosen=None),
        *_turn("new", NOW - timedelta(hours=1)),
    ]
    trail = _trail(tmp_path / "audit.jsonl", events)

    report = service_report(trail, since_days=7, now=NOW)

    assert report.turns == 1
    assert report.unsupported_rate.value == 0.0
    assert report.window.days == 7


def test_the_text_report_names_what_could_not_be_computed(trail: Path) -> None:
    text = render_text(service_report(trail, now=NOW))

    assert "6 turn(s)" in text
    assert "not computed:" in text
    assert "latency_p50_ms: tracing is off" in text


def test_the_json_report_carries_the_same_reasons(trail: Path) -> None:
    document = json.loads(render_json(service_report(trail, now=NOW)))

    assert document["turns"] == 6
    assert "latency_p95_ms" in document["unknown"]
    assert document["fallback_rate"]["value"] == round(1 / 6, 4)


@pytest.fixture
def slo_env(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> Path:
    """Point the command at a temporary state directory and the repository config."""
    monkeypatch.setenv("OPSDECK_RUNTIME_CONFIG", str(repo_root / "configs" / "runtime.json"))
    monkeypatch.setenv("OPSDECK_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def test_the_command_prints_the_numbers_and_the_reasons(slo_env: Path, trail: Path) -> None:
    result = runner.invoke(app, ["report", "--audit", str(trail)])

    assert result.exit_code == 0
    assert "6 turn(s)" in result.stdout
    assert "tracing is off" in result.stdout


def test_the_command_prints_json_when_asked(slo_env: Path, trail: Path) -> None:
    result = runner.invoke(app, ["report", "--audit", str(trail), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["turns"] == 6


def test_the_command_stops_when_there_is_no_trail(slo_env: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", "--audit", str(tmp_path / "absent.jsonl")])

    assert result.exit_code == 1
