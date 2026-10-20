"""Spans sent on to an OpenTelemetry collector, and what may leave the process with them.

The file of `tracing.path` stays what it was; `tracing.exporter: otlp` adds a
copy over OTLP. These tests hold the copy to the same tree as the file, with the
identifiers it is allowed to carry and none of the text a turn handles. The ones
that need the SDK skip where the `otel` extra is not installed.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from oska.audit import otlp
from oska.audit.otlp import (
    EXPORTED_FIELDS,
    OtlpTraceSink,
    TraceExportError,
    namespace_labels,
    tracer_for,
)
from oska.audit.sink import MemorySink
from oska.audit.trace import JsonlTraceSink, MemoryTraceSink
from oska.config import OtlpConfig, RuntimeConfig, Settings, TracingConfig
from oska.engine.factory import build_engine
from oska.planner.scripted import ScriptedModelProvider

RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
CONTAINER_RUNTIME = "integration.container_runtime"
TURN_SPANS = {"turn", "route", "plan", "execute", "reply"}
ALLOWED_ATTRIBUTES = set(EXPORTED_FIELDS.values()) | {"oska.tenant", "oska.domain"}
SDK_MODULES = (
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.resources",
    "opentelemetry.sdk.trace.export",
)


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


def _provider() -> ScriptedModelProvider:
    route = {
        "response": {
            "kind": "new_task",
            "runbook_id": "runbook.service_restart",
            "confidence": 0.9,
            "language": "en",
        }
    }
    plan = {
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
    return ScriptedModelProvider([route, plan])


def _in_memory() -> tuple[object, object]:
    """A tracer whose spans land in a list, and that list's exporter."""
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    exporter = memory.InMemorySpanExporter()
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({resources.SERVICE_NAME: "oska"})
    )
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    return provider.get_tracer("oska"), exporter


# Configuration


def test_the_file_exporter_is_the_default_and_the_shipped_configuration_keeps_it(
    repo_root: Path,
) -> None:
    shipped = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    packaged = repo_root / "src" / "oska" / "defaults" / "configs" / "runtime.json"

    assert TracingConfig().exporter == "file"
    assert RuntimeConfig.model_validate(shipped).tracing == TracingConfig()
    assert packaged.read_bytes() == (repo_root / "configs" / "runtime.json").read_bytes()


def test_the_otlp_settings_take_their_defaults_from_the_standard() -> None:
    config = TracingConfig.model_validate({"enabled": True, "exporter": "otlp"}).otlp

    assert config.endpoint is None
    assert config.protocol == "http/protobuf"
    assert config.service_name == "oska"
    assert config.headers_env is None


@pytest.mark.parametrize(
    ("section", "complaint"),
    [
        ({"exporter": "zipkin"}, "exporter"),
        ({"otlp": {"protocol": "http/json"}}, "protocol"),
        ({"otlp": {"headers": {"authorization": "Bearer x"}}}, "never from the file"),
        ({"otlp": {"endpoint": "https://user:pass@collector/v1/traces"}}, "credentials"),
        ({"otlp": {"endpoint": "collector:4317"}}, "http:// or https://"),
        ({"otlp": {"endpoint": " "}}, "no spaces"),
        ({"otlp": {"headers_env": "NOT A NAME"}}, "environment variable"),
        ({"otlp": {"service_name": ""}}, "service_name"),
        ({"otlp": {"sampler": "always"}}, "sampler"),
    ],
)
def test_the_configuration_refuses_what_does_not_hold_up(section: dict, complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        TracingConfig.model_validate({"enabled": True, **section})


def test_a_grpc_endpoint_may_be_a_bare_address() -> None:
    config = OtlpConfig.model_validate({"protocol": "grpc", "endpoint": "collector:4317"})

    assert config.endpoint == "collector:4317"


def test_a_pair_namespace_names_its_tenant_and_domain() -> None:
    assert namespace_labels("default") == {}
    assert namespace_labels("north/ops") == {"tenant": "north", "domain": "ops"}
    assert namespace_labels("run-1/north/ops") == {"tenant": "north", "domain": "ops"}


# The missing extra


def test_a_missing_extra_is_one_line_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in SDK_MODULES:
        monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(TraceExportError) as raised:
        tracer_for(OtlpConfig(service_name="missing-extra"))

    message = str(raised.value)
    assert "[otel]" in message
    assert "pip install 'oska[otel]'" in message
    assert "\n" not in message


def test_an_engine_asked_to_export_without_the_extra_does_not_start(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for module in SDK_MODULES:
        monkeypatch.setitem(sys.modules, module, None)
    runtime = _runtime_config(
        repo_root,
        tmp_path,
        tracing={"enabled": True, "exporter": "otlp", "otlp": {"service_name": "no-extra"}},
    )
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"runtime_config": runtime}
    )

    with pytest.raises(TraceExportError, match=r"\[otel\]"):
        build_engine(settings, provider=_provider(), audit_sink=MemorySink())


def test_headers_come_from_the_named_variable_and_never_appear_in_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OSKA_TEST_OTLP_HEADERS", "Authorization=Bearer%20s3cret,x-scope=ops")
    assert otlp._headers("OSKA_TEST_OTLP_HEADERS") == {
        "authorization": "Bearer s3cret",
        "x-scope": "ops",
    }
    assert otlp._headers(None) is None

    monkeypatch.setenv("OSKA_TEST_OTLP_HEADERS", "s3cret-without-a-key")
    with pytest.raises(TraceExportError) as malformed:
        otlp._headers("OSKA_TEST_OTLP_HEADERS")
    assert "s3cret" not in str(malformed.value)

    monkeypatch.delenv("OSKA_TEST_OTLP_HEADERS")
    with pytest.raises(TraceExportError, match="OSKA_TEST_OTLP_HEADERS"):
        otlp._headers("OSKA_TEST_OTLP_HEADERS")


# The export


def test_a_turn_reaches_the_collector_as_the_same_tree_with_only_identifiers(
    repo_root: Path, tmp_path: Path
) -> None:
    tracer, exporter = _in_memory()
    path = tmp_path / "traces.jsonl"
    sink = OtlpTraceSink(path, tracer, {"tenant": "north", "domain": "ops"})
    engine = build_engine(
        _settings(repo_root, tmp_path / "state"),
        provider=_provider(),
        audit_sink=MemorySink(),
        trace_sink=sink,
    )

    result = engine.turn("exported", RESTART_REQUEST)

    spans = exporter.get_finished_spans()  # type: ignore[attr-defined]
    written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    turn = next(span for span in spans if span.name == "turn")
    assert [span.name for span in spans] == [entry["name"] for entry in written]
    assert TURN_SPANS <= {span.name for span in spans}
    assert turn.parent is None
    for span in spans:
        assert span.context.trace_id == turn.context.trace_id
        if span is not turn:
            assert span.parent.span_id == turn.context.span_id
        attributes = dict(span.attributes)
        assert set(attributes) <= ALLOWED_ATTRIBUTES, span.name
        assert "session.id" not in attributes
        assert attributes["oska.turn"] == 1
        assert attributes["oska.tenant"] == "north"
        assert attributes["oska.domain"] == "ops"
        for value in attributes.values():
            assert not (isinstance(value, str) and ("wedged" in value or "search" in value))
    assert turn.resource.attributes["service.name"] == "oska"
    assert result.pending is not None
    assert "wedged" not in json.dumps([dict(span.attributes) for span in spans])


def test_the_session_id_is_exported_only_when_configured(tmp_path: Path) -> None:
    """A collector keeps what it receives; the session leaves only on request."""
    tracer, exporter = _in_memory()
    kept = OtlpTraceSink(tmp_path / "kept.jsonl", tracer, include_session=True)
    left = OtlpTraceSink(tmp_path / "left.jsonl", tracer)

    with kept.span("turn", session="s1", turn=1):
        pass
    with left.span("turn", session="s2", turn=1):
        pass

    first, second = exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert first.attributes["session.id"] == "s1"
    assert "session.id" not in second.attributes


def test_the_exported_timing_is_the_recorded_timing(tmp_path: Path) -> None:
    tracer, exporter = _in_memory()
    sink = OtlpTraceSink(tmp_path / "traces.jsonl", tracer)

    with sink.span("turn", session="s1", turn=1) as recorded:
        pass

    (span,) = exporter.get_finished_spans()  # type: ignore[attr-defined]
    started = int(recorded.started_at.timestamp() * 1_000_000) * 1_000
    assert span.start_time == started
    assert span.end_time == started + int(recorded.duration_ms * 1_000_000)
    assert "oska.tenant" not in span.attributes


def test_a_field_not_named_for_export_stays_in_the_file(tmp_path: Path) -> None:
    tracer, exporter = _in_memory()
    path = tmp_path / "traces.jsonl"
    sink = OtlpTraceSink(path, tracer)

    with sink.span("turn", session="s1", turn=1) as span:
        span.set(text=RESTART_REQUEST, payload={"service": "search"}, kind="new_task")

    (exported,) = exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert dict(exported.attributes) == {
        "oska.turn": 1,
        "oska.kind": "new_task",
    }
    assert json.loads(path.read_text(encoding="utf-8"))["fields"]["text"] == RESTART_REQUEST


def test_a_span_that_raises_is_exported_as_an_error_naming_only_the_kind(
    tmp_path: Path,
) -> None:
    tracer, exporter = _in_memory()
    sink = OtlpTraceSink(tmp_path / "traces.jsonl", tracer)

    with pytest.raises(ValueError):
        with sink.span("turn", session="s1", turn=1):
            raise ValueError(RESTART_REQUEST)

    (span,) = exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == "ValueError"
    assert not span.events


def test_a_turn_opened_inside_a_host_span_is_nested_under_it(tmp_path: Path) -> None:
    tracer, exporter = _in_memory()
    sink = OtlpTraceSink(tmp_path / "traces.jsonl", tracer)

    with tracer.start_as_current_span("request") as host:  # type: ignore[attr-defined]
        with sink.span("turn", session="s1", turn=1):
            pass

    turn = next(
        span
        for span in exporter.get_finished_spans()  # type: ignore[attr-defined]
        if span.name == "turn"
    )
    assert turn.parent.span_id == host.get_span_context().span_id


def test_the_configuration_routes_spans_to_the_collector_with_the_pair(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer, exporter = _in_memory()
    asked: list[OtlpConfig] = []

    def fake_tracer(config: OtlpConfig) -> object:
        asked.append(config)
        return tracer

    monkeypatch.setattr(otlp, "tracer_for", fake_tracer)
    runtime = _runtime_config(
        repo_root,
        tmp_path,
        tracing={
            "enabled": True,
            "path": "traces.jsonl",
            "exporter": "otlp",
            "otlp": {"endpoint": "http://collector.invalid:4318/v1/traces"},
        },
    )
    state_dir = tmp_path / "state"
    settings = _settings(repo_root, state_dir).model_copy(update={"runtime_config": runtime})
    engine = build_engine(
        settings, provider=_provider(), audit_sink=MemorySink(), namespace="north/ops"
    )

    engine.turn("configured", RESTART_REQUEST)

    spans = exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert asked and asked[0].endpoint == "http://collector.invalid:4318/v1/traces"
    assert {span.attributes["oska.domain"] for span in spans} == {"ops"}
    assert (state_dir / "traces.jsonl").is_file()


def test_the_file_exporter_builds_the_file_sink_alone(repo_root: Path, tmp_path: Path) -> None:
    runtime = _runtime_config(
        repo_root, tmp_path, tracing={"enabled": True, "path": "traces.jsonl"}
    )
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"runtime_config": runtime}
    )
    engine = build_engine(settings, provider=_provider(), audit_sink=MemorySink())

    sink = engine._trace
    assert type(sink) is JsonlTraceSink


def test_a_real_exporter_is_built_once_per_configuration() -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    config = OtlpConfig(
        endpoint="http://127.0.0.1:4318/v1/traces", service_name="built-once"
    )

    first = tracer_for(config)

    assert tracer_for(config.model_copy()) is first
    assert first.resource.attributes["service.name"] == "built-once"  # type: ignore[attr-defined]


# Nesting under concurrency


def test_two_turns_at_once_each_nest_under_their_own_turn() -> None:
    sink = MemoryTraceSink()
    both_open = threading.Barrier(2)

    def turn(session: str) -> None:
        with sink.span("turn", session=session):
            both_open.wait()
            with sink.span("route"):
                both_open.wait()

    threads = [threading.Thread(target=turn, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    turns = {span.fields["session"]: span for span in sink.named("turn")}
    routes = sink.named("route")
    assert {route.parent_id for route in routes} == {span.span_id for span in turns.values()}
    assert all(route.depth == 1 for route in routes)
