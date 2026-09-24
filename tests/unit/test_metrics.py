"""The Prometheus exposition, the route that serves it, and the files kept in step with it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from oska.api.errors import ApiError
from oska.api.metrics import metrics_router
from oska.audit.events import new_event
from oska.audit.sink import JsonlSink
from oska.config import MetricsConfig, Settings, state_path
from oska.observability.exposition import METRIC_NAMES, render_prometheus
from oska.observability.metrics import (
    DEFAULT_REPORT_INTERVAL_SECONDS,
    ReportCache,
    ServiceReport,
    service_report,
)
from oska.platform import Platform


def _app(settings: Settings, platform: Platform | None = None) -> FastAPI:
    """A minimal application carrying only what `get_metrics` reads.

    Mirrors the one line of error wiring `oska.api.app.create_app` does for
    every router it mounts. Each application holds a cache of its own, so no
    test is answered from a report another test computed.
    """
    app = FastAPI()
    app.state.services = SimpleNamespace(settings=settings)
    app.state.platform = platform
    app.state.metrics_cache = ReportCache()
    app.include_router(metrics_router)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    return app

_SLO_HEADING = re.compile(r"^## (oska_[a-z0-9_]+)", re.MULTILINE)


def _slo_names(repo_root: Path) -> set[str]:
    text = (repo_root / "ops" / "slo.md").read_text(encoding="utf-8")
    return set(_SLO_HEADING.findall(text))


def _alert_names(repo_root: Path) -> set[str]:
    document = yaml.safe_load((repo_root / "ops" / "alerts.yml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for group in document["groups"]:
        for rule in group["rules"]:
            match = re.search(r"oska_[a-z0-9_]+", rule["expr"])
            assert match, f"no oska_ metric name in expr: {rule['expr']!r}"
            names.add(match.group(0))
    return names


def test_the_exposition_the_slo_and_the_alerts_name_the_same_metrics(repo_root: Path) -> None:
    assert _slo_names(repo_root) == METRIC_NAMES
    assert _alert_names(repo_root) == METRIC_NAMES


def test_every_metric_name_is_oska_prefixed() -> None:
    assert all(name.startswith("oska_") for name in METRIC_NAMES)


def test_every_alert_rule_has_a_target_duration_and_severity(repo_root: Path) -> None:
    document = yaml.safe_load((repo_root / "ops" / "alerts.yml").read_text(encoding="utf-8"))
    for group in document["groups"]:
        for rule in group["rules"]:
            assert rule["for"]
            assert rule["labels"]["severity"]
            assert rule["annotations"]["summary"]


def test_turns_is_a_gauge_exported_even_when_zero() -> None:
    text = render_prometheus(ServiceReport(turns=0))

    assert "# TYPE oska_turns gauge" in text
    assert "oska_turns 0.0" in text


def test_turns_is_omitted_with_a_note_when_there_is_no_audit_trail() -> None:
    report = service_report(Path("/does/not/exist/audit.jsonl"))

    text = render_prometheus(report)
    sample_lines = [line for line in text.splitlines() if not line.startswith("#")]

    assert not any(line.startswith("oska_turns") for line in sample_lines)
    assert "# TYPE oska_turns " not in text
    assert "(not computed: this deployment has no audit trail" in text
    assert report.turns_missing


def test_an_empty_trail_is_a_count_of_zero(tmp_path: Path) -> None:
    trail = tmp_path / "audit.jsonl"
    trail.write_text("", encoding="utf-8")

    assert "oska_turns 0.0" in render_prometheus(service_report(trail))


def test_the_turn_count_is_a_gauge_in_all_three_places(repo_root: Path) -> None:
    """It falls after `forget expired`, so it is not a counter anywhere."""
    assert "oska_turns" in METRIC_NAMES
    assert not any(name.endswith("_total") for name in METRIC_NAMES)
    assert "oska_turns" in _slo_names(repo_root)
    alerts = (repo_root / "ops" / "alerts.yml").read_text(encoding="utf-8")
    assert "changes(oska_turns[30m]) == 0" in alerts
    assert "_total" not in alerts


def test_a_measure_that_is_known_is_exported_with_help_and_type_and_a_sample() -> None:
    report = ServiceReport(turns=1)
    report.fallback_rate.value = 0.25
    report.fallback_rate.basis = 4

    text = render_prometheus(report)

    assert "# HELP oska_fallback_rate" in text
    assert "# TYPE oska_fallback_rate gauge" in text
    assert "oska_fallback_rate 0.25" in text


def test_a_measure_that_could_not_be_computed_is_omitted_not_zeroed() -> None:
    report = service_report(Path("/does/not/exist/audit.jsonl"))

    text = render_prometheus(report)
    sample_lines = [line for line in text.splitlines() if not line.startswith("#")]

    assert not any(line.startswith("oska_fallback_rate") for line in sample_lines)
    assert "not computed: no turn was recorded in this window" in text
    assert "# TYPE oska_fallback_rate" not in text


def test_a_domain_label_is_attached_only_when_given() -> None:
    report = ServiceReport(turns=2)

    assert 'oska_turns{domain="ops"} 2.0' in render_prometheus(report, domain="ops")
    assert "oska_turns 2.0" in render_prometheus(report)
    assert "{domain=" not in render_prometheus(report)


@pytest.fixture
def metrics_app(example_settings: Settings) -> FastAPI:
    return _app(example_settings)


def test_the_route_serves_prometheus_text(metrics_app: FastAPI) -> None:
    client = TestClient(metrics_app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP oska_turns " in response.text


def test_the_route_answers_404_when_metrics_are_switched_off(
    example_settings: Settings, tmp_path: Path
) -> None:
    runtime = json.loads(example_settings.runtime_config.read_text(encoding="utf-8"))
    runtime["metrics"]["enabled"] = False
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    settings = example_settings.model_copy(update={"runtime_config": runtime_path})
    client = TestClient(_app(settings))

    response = client.get("/metrics")

    assert response.status_code == 404


def _one_turn(trail: Path, session_id: str = "s") -> None:
    sink = JsonlSink(trail)
    sink.emit(new_event("turn", session_id, f"t-{session_id}", {"turn": 1}))
    sink.emit(new_event("reply", session_id, f"t-{session_id}", {"kind": "outcomes"}))


def test_on_a_platform_every_tenant_and_domain_is_a_labeled_series(
    example_platform: Platform,
) -> None:
    settings = example_platform.settings_for("operations", "default")
    runtime = example_platform.runtime_for("default")
    _one_turn(state_path(settings.state_dir, runtime.audit.path))
    app = _app(example_platform.settings_for(), platform=example_platform)

    text = TestClient(app).get("/metrics").text

    assert text.count("# HELP oska_turns ") == 1
    assert 'oska_turns{tenant="default",domain="operations"} 1.0' in text
    assert '# oska_turns{tenant="default",domain="access"} not computed:' in text
    samples = [line for line in text.splitlines() if not line.startswith("#")]
    assert samples and all('tenant="' in line and 'domain="' in line for line in samples)


def test_a_report_is_served_again_until_its_interval_is_over(tmp_path: Path) -> None:
    trail = tmp_path / "audit.jsonl"
    _one_turn(trail, "first")
    moment = [0.0]
    cache = ReportCache(30.0, clock=lambda: moment[0])

    assert cache.report(trail).turns == 1
    _one_turn(trail, "second")
    moment[0] = 29.0
    assert cache.report(trail).turns == 1
    moment[0] = 30.0
    assert cache.report(trail).turns == 2
    assert cache.interval_seconds == 30.0
    assert ReportCache().interval_seconds == DEFAULT_REPORT_INTERVAL_SECONDS


def test_the_route_keeps_a_report_for_the_configured_interval(
    example_settings: Settings, tmp_path: Path
) -> None:
    runtime = json.loads(example_settings.runtime_config.read_text(encoding="utf-8"))
    runtime["metrics"]["cache_seconds"] = 0.0
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    settings = example_settings.model_copy(update={"runtime_config": runtime_path})
    app = _app(settings)
    del app.state.metrics_cache
    client = TestClient(app)
    trail = state_path(settings.state_dir, runtime["audit"]["path"])

    _one_turn(trail, "first")
    assert "oska_turns 1.0" in client.get("/metrics").text
    _one_turn(trail, "second")
    assert "oska_turns 2.0" in client.get("/metrics").text
    assert app.state.metrics_cache.interval_seconds == 0.0


def test_the_cache_interval_defaults_to_the_packaged_value(metrics_app: FastAPI) -> None:
    del metrics_app.state.metrics_cache

    assert TestClient(metrics_app).get("/metrics").status_code == 200
    assert metrics_app.state.metrics_cache.interval_seconds == 15.0
    assert MetricsConfig().cache_seconds == DEFAULT_REPORT_INTERVAL_SECONDS


def test_metrics_are_not_public_unless_the_configuration_says_so() -> None:
    assert MetricsConfig().public is False


def test_the_route_is_a_reviewer_s_read_where_an_identity_is_required(
    example_settings: Settings, tmp_path: Path
) -> None:
    runtime = json.loads(example_settings.runtime_config.read_text(encoding="utf-8"))
    runtime["identity"] = {"mode": "proxy", "required": True}
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    client = TestClient(_app(example_settings.model_copy(update={"runtime_config": runtime_path})))

    anonymous = client.get("/metrics")
    operator = client.get("/metrics", headers={"X-Principal-Subject": "eve"})
    reviewer = client.get(
        "/metrics",
        headers={
            "X-Principal-Subject": "rita",
            "X-Principal-Attributes": '{"roles": ["reviewer"]}',
        },
    )

    assert (anonymous.status_code, operator.status_code, reviewer.status_code) == (401, 403, 200)
    assert "tenant" not in anonymous.text
