"""Diagnostic traces and the per-turn model budget: the two bounds around a turn.

Traces say how long a turn spent where; they are off unless a deployment asks
for them and they are never the record of what happened. The budget says how
much one turn may spend on the model; past it the model is not called again and
the turn answers from what it already has.
"""

from __future__ import annotations

import json
from pathlib import Path

from opsdeck.audit.sink import MemorySink
from opsdeck.audit.trace import (
    JsonlTraceSink,
    MemoryTraceSink,
    NullTraceSink,
    destination,
)
from opsdeck.config import Settings
from opsdeck.engine.engine import ModelBudget
from opsdeck.engine.factory import build_engine
from opsdeck.planner.scripted import ScriptedModelProvider

RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
CONTAINER_RUNTIME = "integration.container_runtime"
TURN_SPANS = {"turn", "route", "discovery", "plan", "execute", "reply"}


def _settings(repo_root: Path, state_dir: Path) -> Settings:
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state_dir,
        provider="scripted",
    )


def _runtime_config(repo_root: Path, tmp_path: Path, **sections: dict) -> Path:
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document.update(sections)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _route(kind: str, runbook_id: str | None = None) -> dict:
    return {
        "response": {
            "kind": kind,
            "runbook_id": runbook_id,
            "confidence": 0.9,
            "language": "en",
        }
    }


def _restart_plan() -> dict:
    return {
        "response": {
            "runbook_id": "runbook.service_restart",
            "operations": [
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.status",
                    "payload": {"service": "search"},
                    "write": False,
                    "purpose": "read the current state",
                },
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.restart",
                    "payload": {"service": "search"},
                    "write": True,
                    "purpose": "restart the service",
                },
            ],
            "missing": [],
            "reply_intent": "report the restart",
        }
    }


def test_the_null_sink_records_nothing_and_still_hands_back_a_span() -> None:
    sink = NullTraceSink()

    with sink.span("turn", session="s1") as span:
        span.set(kind="new_task")

    assert span.name == "turn"
    assert span.fields["kind"] == "new_task"
    assert destination(sink) == "null"


def test_spans_are_nested_and_timed() -> None:
    sink = MemoryTraceSink()

    with sink.span("turn") as outer:
        with sink.span("route"):
            pass
        with sink.span("plan") as planning:
            with sink.span("execute"):
                pass
            planning.set(runbook="runbook.service_restart")

    names = [span.name for span in sink.spans]
    by_name = {span.name: span for span in sink.spans}

    assert names == ["route", "execute", "plan", "turn"]
    assert by_name["route"].parent_id == outer.span_id
    assert by_name["execute"].parent_id == by_name["plan"].span_id
    assert by_name["execute"].depth == 2
    assert by_name["turn"].depth == 0
    assert by_name["turn"].duration_ms >= by_name["plan"].duration_ms >= 0.0
    assert by_name["plan"].fields["runbook"] == "runbook.service_restart"


def test_the_file_sink_writes_one_object_per_span(tmp_path: Path) -> None:
    path = tmp_path / "traces" / "traces.jsonl"
    sink = JsonlTraceSink(path)

    with sink.span("turn", session="s1"):
        with sink.span("route"):
            pass

    written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert [entry["name"] for entry in written] == ["route", "turn"]
    assert written[1]["fields"] == {"session": "s1"}
    assert written[0]["parent_id"] == written[1]["span_id"]
    assert written[1]["duration_ms"] >= 0.0
    assert destination(sink) == str(path)


def test_tracing_is_off_unless_the_configuration_asks_for_it(
    repo_root: Path, tmp_path: Path
) -> None:
    """Nothing is recorded and nothing is written, and the audit says nothing about it."""
    state_dir = tmp_path / "state"
    engine = build_engine(
        _settings(repo_root, state_dir),
        provider=ScriptedModelProvider(
            [_route("new_task", "runbook.service_restart"), _restart_plan()]
        ),
        audit_sink=MemorySink(),
    )

    result = engine.turn("untraced", RESTART_REQUEST)

    assert not [event for event in result.events if event.kind == "trace"]
    assert not (state_dir / "traces.jsonl").exists()
    assert result.pending is not None


def test_tracing_records_the_boundaries_of_a_turn_when_it_is_on(
    repo_root: Path, tmp_path: Path
) -> None:
    trace = MemoryTraceSink()
    engine = build_engine(
        _settings(repo_root, tmp_path / "state"),
        provider=ScriptedModelProvider([_route("new_task"), _restart_plan()]),
        audit_sink=MemorySink(),
        trace_sink=trace,
    )

    result = engine.turn("traced", RESTART_REQUEST)

    names = [span.name for span in trace.spans]
    turn_span = trace.named("turn")[0]
    assert TURN_SPANS <= set(names)
    assert names[-1] == "turn"
    assert all(span.parent_id == turn_span.span_id for span in trace.spans if span is not turn_span)
    assert [event.kind for event in result.events].count("trace") == 1


def test_the_configuration_turns_tracing_on_and_says_where_it_goes(
    repo_root: Path, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    runtime = _runtime_config(
        repo_root, tmp_path, tracing={"enabled": True, "path": "traces.jsonl"}
    )
    settings = _settings(repo_root, state_dir).model_copy(update={"runtime_config": runtime})
    engine = build_engine(
        settings,
        provider=ScriptedModelProvider(
            [_route("new_task", "runbook.service_restart"), _restart_plan()]
        ),
        audit_sink=MemorySink(),
    )

    result = engine.turn("configured", RESTART_REQUEST)

    written = (state_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    recorded = [event for event in result.events if event.kind == "trace"]
    assert [json.loads(line)["name"] for line in written][-1] == "turn"
    assert recorded and recorded[0].data["where"].endswith("traces.jsonl")


def test_a_budget_allows_what_it_declares_and_nothing_more() -> None:
    budget = ModelBudget(2)
    budget.start_turn()

    assert [budget.take(), budget.take(), budget.take()] == [True, True, False]
    assert budget.spent == 2
    assert budget.refused == 1
    assert budget.exhausted

    budget.start_turn()

    assert budget.take() is True
    assert budget.refused == 0


def test_a_turn_past_its_budget_stops_calling_the_model_and_still_answers(
    repo_root: Path, tmp_path: Path
) -> None:
    """The classification is paid for, the plan is not, and the turn says so."""
    runtime = _runtime_config(repo_root, tmp_path, turn={"max_model_calls": 1})
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"runtime_config": runtime}
    )
    provider = ScriptedModelProvider(
        [_route("new_task", "runbook.service_restart"), _restart_plan()]
    )
    engine = build_engine(settings, provider=provider, audit_sink=MemorySink())

    result = engine.turn("bounded", RESTART_REQUEST)

    spent = [event for event in result.events if event.kind == "budget"]
    assert provider.consumed() == 1
    assert [outcome.kind for outcome in result.outcomes] == ["cancelled"]
    assert "model calls it is allowed" in result.reply
    assert spent and spent[0].data == {"limit": 1, "spent": 1, "refused": 2}
    assert engine.receipts("bounded") == []
    assert engine.state("bounded").pending_goal is None
    assert result.pending is None
