"""Connector bindings: the declarative mock, HTTP, commands and the registry."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from opsdeck.connectors.base import Connector, ConnectorError, timed
from opsdeck.connectors.http import HttpConnector
from opsdeck.connectors.mock import MockConnector
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.connectors.shell import ShellConnector
from opsdeck.contracts.common import ConnectorResult

RUNTIME = "integration.container_runtime"
DEPLOYMENTS = "integration.deployment_control"
ALERTING = "integration.alerting"
INCIDENTS = "integration.incident_tracker"


@pytest.fixture
def connectors_config(examples_root: Path) -> Path:
    """The configuration binding the example catalog to the fixtures."""
    return examples_root / "configs" / "connectors.json"


@pytest.fixture
def registry(connectors_config: Path) -> ConnectorRegistry:
    """A registry over the example fixtures."""
    return ConnectorRegistry.from_config(connectors_config)


def ok(result: ConnectorResult) -> dict:
    """Assert a call succeeded and return its data."""
    assert result.ok, result.error
    return result.data


# The mapping keys, one by one.


def test_match_and_payload_field_return_the_record(registry: ConnectorRegistry) -> None:
    data = ok(registry.get(RUNTIME).execute("service.status", {"service": "payments"}))
    assert data["service"]["name"] == "payments"
    assert data["service"]["environment"] == "production"


def test_missing_record_is_reported_as_not_found(registry: ConnectorRegistry) -> None:
    result = registry.get(RUNTIME).execute("service.status", {"service": "absent"})
    assert (result.ok, result.error) == (False, "not found")


def test_undeclared_operation_is_refused(registry: ConnectorRegistry) -> None:
    result = registry.get(RUNTIME).execute("service.delete", {"service": "payments"})
    assert result.ok is False
    assert result.error == "undeclared operation service.delete"


def test_project_contains_and_limit_narrow_the_log_lines(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    everything = ok(runtime.execute("service.logs", {"service": "payments"}))["lines"]
    assert isinstance(everything, list)

    matching = ok(
        runtime.execute("service.logs", {"service": "payments", "pattern": "timeout"})
    )["lines"]
    assert matching
    assert len(matching) < len(everything)
    assert all("timeout" in line.lower() for line in matching)

    limited = ok(
        runtime.execute(
            "service.logs", {"service": "payments", "pattern": "timeout", "lines": 2}
        )
    )["lines"]
    assert limited == matching[-2:]


def test_contains_is_case_insensitive(registry: ConnectorRegistry) -> None:
    lines = ok(
        registry.get(RUNTIME).execute("service.logs", {"service": "payments", "pattern": "ERROR"})
    )["lines"]
    assert lines and all("error" in line.lower() for line in lines)


def test_where_selects_the_current_deployment(registry: ConnectorRegistry) -> None:
    data = ok(registry.get(DEPLOYMENTS).execute("deployment.get", {"service": "payments"}))
    assert data["deployment"]["current"] is True
    assert data["deployment"]["version"] == "3.2.0"


def test_filter_and_limit_narrow_a_list(registry: ConnectorRegistry) -> None:
    control = registry.get(DEPLOYMENTS)
    everything = ok(control.execute("deployment.list", {}))["deployments"]
    assert len(everything) == 8

    for_service = ok(control.execute("deployment.list", {"service": "payments"}))["deployments"]
    assert [record["id"] for record in for_service] == ["dep-1002", "dep-1003"]

    limited = ok(control.execute("deployment.list", {"service": "payments", "limit": 1}))
    assert len(limited["deployments"]) == 1


def test_field_map_translates_a_payload_field(registry: ConnectorRegistry) -> None:
    data = ok(registry.get(ALERTING).execute("alert.list", {"alert_id": "alt-9002"}))
    assert [record["id"] for record in data["alerts"]] == ["alt-9002"]


def test_filter_on_several_fields(registry: ConnectorRegistry) -> None:
    data = ok(
        registry.get(ALERTING).execute("alert.list", {"service": "payments", "state": "firing"})
    )
    assert {record["id"] for record in data["alerts"]} == {"alt-9001", "alt-9002"}


# Writes, and the read-back that proves them.


def test_restart_sets_the_status_and_increments_the_counter(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    before = ok(runtime.execute("service.status", {"service": "reporting"}))["service"]
    assert (before["status"], before["restarts"]) == ("stopped", 7)

    written = ok(runtime.execute("service.restart", {"service": "reporting"}))["service"]
    assert (written["status"], written["restarts"]) == ("running", 8)

    after = ok(runtime.execute("service.status", {"service": "reporting"}))["service"]
    assert (after["status"], after["restarts"]) == ("running", 8)


def test_scale_writes_a_payload_field(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    ok(runtime.execute("service.scale", {"service": "search", "replicas": 5}))
    after = ok(runtime.execute("service.status", {"service": "search"}))["service"]
    assert after["replicas"] == 5


def test_rollback_writes_the_current_deployment(registry: ConnectorRegistry) -> None:
    control = registry.get(DEPLOYMENTS)
    ok(control.execute("deployment.rollback", {"service": "payments", "target_version": "3.1.7"}))
    after = ok(control.execute("deployment.get", {"service": "payments"}))["deployment"]
    assert after["version"] == "3.1.7"
    assert after["state"] == "rolled_back"
    assert after["current"] is True


def test_silence_writes_the_alert_the_verification_reads_back(registry: ConnectorRegistry) -> None:
    alerting = registry.get(ALERTING)
    written = ok(alerting.execute("alert.silence", {"alert_id": "alt-9001", "minutes": 30}))
    assert written["alert"]["state"] == "silenced"
    assert written["alert"]["silence_minutes"] == 30

    read_back = ok(alerting.execute("alert.list", {"alert_id": "alt-9001"}))["alerts"]
    assert read_back[0]["state"] == "silenced"


def test_note_appends_to_the_list_and_sets_the_last_one(registry: ConnectorRegistry) -> None:
    tracker = registry.get(INCIDENTS)
    before = ok(tracker.execute("incident.get", {"incident_id": "inc-501"}))["incident"]
    assert before["notes"] == []

    ok(tracker.execute("incident.note", {"incident_id": "inc-501", "note": "pool raised to 80"}))
    ok(tracker.execute("incident.note", {"incident_id": "inc-501", "note": "errors back down"}))

    after = ok(tracker.execute("incident.get", {"incident_id": "inc-501"}))["incident"]
    assert after["notes"] == ["pool raised to 80", "errors back down"]
    assert after["last_note"] == "errors back down"


def test_incident_list_filters_by_state(registry: ConnectorRegistry) -> None:
    data = ok(registry.get(INCIDENTS).execute("incident.list", {"state": "open"}))
    assert [record["id"] for record in data["incidents"]] == ["inc-501"]


def test_writes_do_not_reach_the_file(registry: ConnectorRegistry, examples_root: Path) -> None:
    ok(registry.get(RUNTIME).execute("service.restart", {"service": "reporting"}))
    on_disk = json.loads((examples_root / "fixtures" / "services.json").read_text("utf-8"))
    stored = next(item for item in on_disk["services"] if item["name"] == "reporting")
    assert (stored["status"], stored["restarts"]) == ("stopped", 7)


def test_two_registries_do_not_share_state(connectors_config: Path) -> None:
    first = ConnectorRegistry.from_config(connectors_config)
    second = ConnectorRegistry.from_config(connectors_config)
    ok(first.get(RUNTIME).execute("service.restart", {"service": "reporting"}))
    fresh = ok(second.get(RUNTIME).execute("service.status", {"service": "reporting"}))["service"]
    assert fresh["restarts"] == 7


def test_reset_reloads_the_fixture(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    ok(runtime.execute("service.restart", {"service": "reporting"}))
    runtime.reset()
    after = ok(runtime.execute("service.status", {"service": "reporting"}))["service"]
    assert (after["status"], after["restarts"]) == ("stopped", 7)


def test_a_missing_payload_field_is_an_error(registry: ConnectorRegistry) -> None:
    result = registry.get(RUNTIME).execute("service.scale", {"service": "search"})
    assert result.ok is False
    assert "replicas" in (result.error or "")


# Failure injection.


def test_behaviors_from_the_registry_make_an_operation_fail(connectors_config: Path) -> None:
    registry = ConnectorRegistry.from_config(
        connectors_config,
        behaviors={RUNTIME: {"service.restart": {"fail": True, "error": "runtime unreachable"}}},
    )
    result = registry.get(RUNTIME).execute("service.restart", {"service": "reporting"})
    assert (result.ok, result.error) == (False, "runtime unreachable")
    untouched = ok(registry.get(RUNTIME).execute("service.status", {"service": "reporting"}))
    assert untouched["service"]["restarts"] == 7


def test_failure_without_a_message_still_reports_one(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    runtime.set_behavior("service.restart", {"fail": True})
    result = runtime.execute("service.restart", {"service": "reporting"})
    assert result.ok is False
    assert "service.restart" in (result.error or "")


def test_override_replaces_the_lookup(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    runtime.set_behavior("service.status", {"override": {"service": {"name": "made up"}}})
    data = ok(runtime.execute("service.status", {"service": "absent"}))
    assert data["service"]["name"] == "made up"


def test_fail_after_lets_the_first_calls_through(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    runtime.set_behavior("service.status", {"fail_after": 2, "error": "runtime gone"})
    assert runtime.execute("service.status", {"service": "search"}).ok is True
    assert runtime.execute("service.status", {"service": "search"}).ok is True
    third = runtime.execute("service.status", {"service": "search"})
    assert (third.ok, third.error) == (False, "runtime gone")


def test_a_behavior_can_be_removed_again(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    runtime.set_behavior("service.status", {"fail": True})
    assert runtime.execute("service.status", {"service": "search"}).ok is False
    runtime.set_behavior("service.status", None)
    assert runtime.execute("service.status", {"service": "search"}).ok is True


def test_reset_forgets_the_call_count(registry: ConnectorRegistry) -> None:
    runtime = registry.get(RUNTIME)
    runtime.set_behavior("service.status", {"fail_after": 1})
    assert runtime.execute("service.status", {"service": "search"}).ok is True
    assert runtime.execute("service.status", {"service": "search"}).ok is False
    runtime.reset()
    assert runtime.execute("service.status", {"service": "search"}).ok is True


def test_a_mock_can_be_built_on_its_own(tmp_path: Path) -> None:
    fixture = tmp_path / "records.json"
    fixture.write_text(json.dumps({"records": [{"key": "a", "count": 1}]}), encoding="utf-8")
    connector = MockConnector(
        id="integration.sample",
        fixture_path=fixture,
        operations={
            "record.get": {"collection": "records", "match": "key", "as": "record"},
            "record.bump": {
                "collection": "records",
                "match": "key",
                "as": "record",
                "set": {"count": "+2", "label": "$payload.label"},
            },
        },
    )
    assert isinstance(connector, Connector)
    ok(connector.execute("record.bump", {"key": "a", "label": "done"}))
    record = ok(connector.execute("record.get", {"key": "a"}))["record"]
    assert (record["count"], record["label"]) == (3, "done")
    assert connector.operations() == ["record.bump", "record.get"]


def test_latency_is_filled_in(registry: ConnectorRegistry) -> None:
    result = registry.get(RUNTIME).execute("service.status", {"service": "search"})
    assert result.latency_ms >= 0


def test_timed_replaces_the_reported_latency() -> None:
    @timed
    def call() -> ConnectorResult:
        return ConnectorResult(ok=True, latency_ms=999)

    assert call().latency_ms < 999


# HTTP.


def http_connector(handler, **kwargs) -> HttpConnector:
    """An HTTP connector over a handler instead of a network."""
    operations = kwargs.pop(
        "operations",
        {
            "service.status": {"method": "GET", "path": "/services/{service}"},
            "service.logs": {
                "method": "GET",
                "path": "/services/{service}/logs",
                "query_fields": ["lines", "pattern"],
            },
            "service.scale": {
                "method": "POST",
                "path": "/services/{service}/scale",
                "body_fields": ["replicas"],
            },
        },
    )
    return HttpConnector(
        id=RUNTIME,
        base_url="https://runtime.test/api/v1",
        headers={"Authorization": "Bearer token"},
        operations=operations,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_http_templates_the_path_and_sends_the_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "service": {"name": "payments"}})

    connector = http_connector(handler)
    data = ok(connector.execute("service.status", {"service": "payments"}))
    assert data["service"]["name"] == "payments"
    assert seen[0].url.path == "/api/v1/services/payments"
    assert seen[0].headers["authorization"] == "Bearer token"
    connector.close()


def test_http_sends_only_the_declared_query_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "lines": []})

    connector = http_connector(handler)
    ok(connector.execute("service.logs", {"service": "payments", "lines": 10, "secret": "x"}))
    assert dict(seen[0].url.params) == {"lines": "10"}
    connector.close()


def test_http_sends_only_the_declared_body_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    connector = http_connector(handler)
    ok(connector.execute("service.scale", {"service": "search", "replicas": 3, "extra": "no"}))
    assert json.loads(seen[0].content) == {"replicas": 3}
    assert seen[0].method == "POST"
    connector.close()


def test_http_reports_a_non_success_status_with_a_truncated_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="x" * 4000)

    connector = http_connector(handler, error_body_limit=64)
    result = connector.execute("service.status", {"service": "payments"})
    assert result.ok is False
    assert result.error == "http 503: " + "x" * 64
    connector.close()


def test_http_wraps_a_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain text")

    connector = http_connector(handler)
    assert ok(connector.execute("service.status", {"service": "payments"})) == {
        "text": "plain text"
    }
    connector.close()


def test_http_wraps_a_json_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    connector = http_connector(handler)
    assert ok(connector.execute("service.status", {"service": "payments"})) == {"data": [1, 2]}
    connector.close()


def test_http_refuses_an_undeclared_operation() -> None:
    connector = http_connector(lambda request: httpx.Response(200, json={}))
    result = connector.execute("service.delete", {"service": "payments"})
    assert result.error == "undeclared operation service.delete"
    connector.close()


def test_http_reports_a_missing_path_field() -> None:
    connector = http_connector(lambda request: httpx.Response(200, json={}))
    result = connector.execute("service.status", {})
    assert result.ok is False
    assert "service" in (result.error or "")
    connector.close()


def test_http_reports_a_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    connector = http_connector(handler)
    result = connector.execute("service.status", {"service": "payments"})
    assert result.ok is False
    assert "refused" in (result.error or "")
    connector.close()


# Commands.


def shell_connector() -> ShellConnector:
    """A command connector over a harmless command."""
    return ShellConnector(
        id="integration.sample",
        operations={
            "sample.echo": {"argv": ["printf", "%s", "{value}"], "timeout": 10},
            "sample.fail": {"argv": ["false"], "timeout": 10},
            "sample.pwd": {"argv": ["pwd"], "timeout": 10},
        },
    )


def test_shell_substitutes_a_payload_field() -> None:
    data = ok(shell_connector().execute("sample.echo", {"value": "reporting"}))
    assert data == {"stdout": "reporting", "stderr": "", "returncode": 0}


def test_shell_keeps_a_value_with_spaces_as_one_argument() -> None:
    data = ok(shell_connector().execute("sample.echo", {"value": "one two; rm -rf /"}))
    assert data["stdout"] == "one two; rm -rf /"


def test_shell_accepts_numbers_and_booleans() -> None:
    assert ok(shell_connector().execute("sample.echo", {"value": 42}))["stdout"] == "42"
    assert ok(shell_connector().execute("sample.echo", {"value": True}))["stdout"] == "True"


@pytest.mark.parametrize("value", [["a", "b"], {"a": 1}, None])
def test_shell_refuses_a_value_that_is_not_a_single_one(value: object) -> None:
    result = shell_connector().execute("sample.echo", {"value": value})
    assert result.ok is False
    assert "value" in (result.error or "")


def test_shell_refuses_a_null_byte() -> None:
    result = shell_connector().execute("sample.echo", {"value": "a\0b"})
    assert result.ok is False
    assert "null byte" in (result.error or "")


def test_shell_refuses_a_missing_field() -> None:
    result = shell_connector().execute("sample.echo", {})
    assert result.ok is False
    assert "value" in (result.error or "")


def test_shell_reports_a_non_zero_exit() -> None:
    result = shell_connector().execute("sample.fail", {})
    assert result.ok is False
    assert result.data["returncode"] != 0


def test_shell_refuses_an_undeclared_operation() -> None:
    result = shell_connector().execute("sample.absent", {})
    assert result.error == "undeclared operation sample.absent"


def test_shell_reports_a_command_that_is_not_there() -> None:
    connector = ShellConnector(
        id="integration.sample",
        operations={"sample.missing": {"argv": ["opsdeck-no-such-command"]}},
    )
    result = connector.execute("sample.missing", {})
    assert result.ok is False
    assert "could not be run" in (result.error or "")


def test_shell_runs_in_the_declared_directory(tmp_path: Path) -> None:
    connector = ShellConnector(
        id="integration.sample",
        operations={"sample.pwd": {"argv": ["pwd"], "cwd": str(tmp_path)}},
    )
    data = ok(connector.execute("sample.pwd", {}))
    assert data["stdout"].strip().endswith(tmp_path.name)


def test_shell_times_out() -> None:
    connector = ShellConnector(
        id="integration.sample",
        operations={"sample.sleep": {"argv": ["sleep", "5"], "timeout": 0.05}},
    )
    result = connector.execute("sample.sleep", {})
    assert result.ok is False
    assert "timed out" in (result.error or "")


# The registry.


def test_the_registry_binds_every_integration(registry: ConnectorRegistry) -> None:
    assert registry.ids() == sorted([ALERTING, RUNTIME, DEPLOYMENTS, INCIDENTS])
    assert registry.has(RUNTIME) is True
    assert registry.warnings == []


def test_an_unbound_integration_is_an_error(registry: ConnectorRegistry) -> None:
    with pytest.raises(ConnectorError):
        registry.get("integration.absent")


def test_a_connector_can_be_registered_by_hand(registry: ConnectorRegistry) -> None:
    replacement = shell_connector()
    registry.register(RUNTIME, replacement)
    assert registry.get(RUNTIME) is replacement


def test_fixture_paths_resolve_against_the_configuration_file(
    connectors_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = ConnectorRegistry.from_config(connectors_config)
    assert ok(registry.get(RUNTIME).execute("service.status", {"service": "search"}))


def test_environment_variables_expand_in_headers_and_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSDECK_TEST_HOST", "runtime.internal")
    monkeypatch.setenv("OPSDECK_TEST_TOKEN", "secret")
    config = tmp_path / "connectors.json"
    config.write_text(
        json.dumps(
            {
                "integrations": {
                    RUNTIME: {
                        "type": "http",
                        "base_url": "https://${OPSDECK_TEST_HOST}/api",
                        "headers": {"Authorization": "Bearer ${OPSDECK_TEST_TOKEN}"},
                        "operations": {"service.status": {"method": "GET", "path": "/s"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ConnectorRegistry.from_config(config)
    connector = registry.get(RUNTIME)
    assert connector.base_url == "https://runtime.internal/api"
    assert registry.warnings == []
    connector.close()


def test_a_variable_that_is_not_set_becomes_empty_and_is_reported(tmp_path: Path) -> None:
    config = tmp_path / "connectors.json"
    config.write_text(
        json.dumps(
            {
                "integrations": {
                    RUNTIME: {
                        "type": "http",
                        "base_url": "https://runtime.test/${MISSING_PREFIX}api",
                        "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                        "operations": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ConnectorRegistry.from_config(config, env={})
    connector = registry.get(RUNTIME)
    assert connector.base_url == "https://runtime.test/api"
    assert len(registry.warnings) == 2
    assert "MISSING_TOKEN" in " ".join(registry.warnings)
    assert "MISSING_PREFIX" in " ".join(registry.warnings)
    connector.close()


def test_an_unknown_connector_type_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "connectors.json"
    config.write_text(
        json.dumps({"integrations": {RUNTIME: {"type": "carrier pigeon"}}}), encoding="utf-8"
    )
    with pytest.raises(ConnectorError):
        ConnectorRegistry.from_config(config)


def test_the_real_binding_example_builds(examples_root: Path) -> None:
    registry = ConnectorRegistry.from_config(
        examples_root / "configs" / "connectors.real.example.json", env={}
    )
    assert registry.ids() == sorted([ALERTING, RUNTIME, DEPLOYMENTS, INCIDENTS])
    assert registry.get(DEPLOYMENTS).operations() == [
        "deployment.get",
        "deployment.list",
        "deployment.rollback",
    ]
    assert registry.warnings  # the access tokens are not set in the test environment
    registry.get(RUNTIME).close()
    registry.get(ALERTING).close()
    registry.get(INCIDENTS).close()
