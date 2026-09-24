"""What a tenant overlay may change, and what a platform's bindings may read.

A tenant may move an HTTP binding its packs declare, by its `base_url` and its
`headers`, and nothing else: not the kind of connector, not the operations, not
where a fixture is read from. The environment a binding reads is the one the
platform manifest grants: a domain's bindings read the variables its domain
declares, a tenant's changes read the variables its tenant declares, and a
reference to any other variable refuses the platform when it is assembled. A
tenant's runtime fragment may lower what a connector call or a reply may cost,
and never raise it past the platform's own value.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from oska.config import Settings
from oska.connectors.http import HttpConnector
from oska.connectors.mock import MockConnector
from oska.connectors.registry import ConnectorRegistry
from oska.platform import Platform, PlatformError, TenantOverlayError

RUNTIME = "integration.container_runtime"
PLATFORM_URL = "http://127.0.0.1:9"


def _ops_pack(examples: Path, target: Path, *, binding: dict | None = None) -> Path:
    """The example ops pack with the container runtime bound over HTTP, or as given."""
    shutil.copytree(examples / "ops", target)
    path = target / "configs" / "connectors.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    bindings["integrations"][RUNTIME] = binding or {
        "type": "http",
        "base_url": PLATFORM_URL,
        "headers": {"Accept": "application/json"},
        "operations": {"service.status": {"method": "GET", "path": "/services/{service}"}},
    }
    path.write_text(json.dumps(bindings), encoding="utf-8")
    return target


def _platform(
    root: Path,
    examples: Path,
    *,
    ops: Path | None = None,
    domain_environment: dict | None = None,
    tenants: dict | None = None,
) -> Path:
    document: dict = {
        "name": "trial",
        "packs": {
            "ops": {"path": str(ops or examples / "ops")},
            "access": {"path": str(examples / "access")},
        },
        "domains": [
            {
                "name": "operations",
                "packs": ["ops"],
                "host": {"name": "the operations desk"},
                "tenants": ["default", "acme"],
            },
            {
                "name": "access",
                "packs": ["access"],
                "host": {"name": "the access desk"},
                "tenants": ["default", "acme"],
            },
        ],
    }
    if domain_environment is not None:
        document["domains"][0]["environment"] = domain_environment
    if tenants is not None:
        document["tenants"] = tenants
    root.mkdir(parents=True, exist_ok=True)
    path = root / "platform.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _overlay(root: Path, tenant: str, *, connectors: dict | None = None,
             runtime: dict | None = None) -> None:
    directory = root / "tenants" / tenant
    directory.mkdir(parents=True, exist_ok=True)
    if connectors is not None:
        (directory / "connectors.json").write_text(
            json.dumps({"integrations": connectors}), encoding="utf-8"
        )
    if runtime is not None:
        (directory / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")


# What a tenant may change in a binding


def test_a_tenant_may_move_an_http_binding_by_its_base_url_and_headers(
    examples: Path, tmp_path: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACME_RUNTIME_TOKEN", "acme-secret")
    ops = _ops_pack(examples, tmp_path / "packs" / "ops")
    path = _platform(
        tmp_path, examples, ops=ops, tenants={"acme": {"environment": {"prefix": "ACME_"}}}
    )
    _overlay(
        tmp_path,
        "acme",
        connectors={
            RUNTIME: {
                "type": "http",
                "base_url": "http://127.0.0.1:19",
                "headers": {"Authorization": "Bearer ${ACME_RUNTIME_TOKEN}"},
            }
        },
    )
    held = Platform.from_file(path, platform_settings)

    moved = held.engine("operations", "acme").connectors.get(RUNTIME)
    kept = held.engine("operations", "default").connectors.get(RUNTIME)
    assert isinstance(moved, HttpConnector) and isinstance(kept, HttpConnector)
    assert moved.base_url == "http://127.0.0.1:19"
    assert moved._client.headers["Authorization"] == "Bearer acme-secret"
    assert moved.operations() == kept.operations() == ["service.status"]
    assert kept.base_url == PLATFORM_URL
    assert "Authorization" not in kept._client.headers


def test_a_tenant_may_not_change_the_kind_of_a_binding(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    path = _platform(tmp_path, examples)
    _overlay(
        tmp_path,
        "acme",
        connectors={
            RUNTIME: {
                "type": "shell",
                "operations": {"service.status": {"argv": ["cat", "/etc/hosts"], "cwd": "/"}},
            }
        },
    )

    with pytest.raises(TenantOverlayError, match="as 'mock'.*may not change it to 'shell'"):
        Platform.from_file(path, platform_settings)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("operations", {"service.status": {"method": "DELETE", "path": "/everything"}}),
        ("resilience", {"retries": 50}),
        ("timeout", 600),
    ],
)
def test_a_tenant_may_change_nothing_but_base_url_and_headers(
    examples: Path, tmp_path: Path, platform_settings: Settings, key: str, value: object
) -> None:
    ops = _ops_pack(examples, tmp_path / "packs" / "ops")
    path = _platform(tmp_path, examples, ops=ops)
    _overlay(tmp_path, "acme", connectors={RUNTIME: {"type": "http", key: value}})

    with pytest.raises(TenantOverlayError, match=f"may not change '{key}'"):
        Platform.from_file(path, platform_settings)


def test_a_tenant_may_not_point_a_mock_binding_at_another_fixture(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    elsewhere = examples / "access" / "fixtures" / "directory.json"
    path = _platform(tmp_path, examples)
    _overlay(tmp_path, "acme", connectors={RUNTIME: {"type": "mock", "fixture": str(elsewhere)}})

    with pytest.raises(TenantOverlayError, match="may not change 'fixture'"):
        Platform.from_file(path, platform_settings)


def test_a_tenant_may_not_give_a_mock_binding_an_address(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    path = _platform(tmp_path, examples)
    _overlay(tmp_path, "acme", connectors={RUNTIME: {"base_url": "http://127.0.0.1:19"}})

    with pytest.raises(TenantOverlayError, match="a mock binding reads no base_url or headers"):
        Platform.from_file(path, platform_settings)


def test_a_tenant_that_moves_a_binding_brings_its_own_headers(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    ops = _ops_pack(
        examples,
        tmp_path / "packs" / "ops",
        binding={
            "type": "http",
            "base_url": PLATFORM_URL,
            "headers": {"Authorization": "Bearer ${RUNTIME_TOKEN}"},
            "operations": {},
        },
    )
    path = _platform(
        tmp_path, examples, ops=ops, domain_environment={"names": ["RUNTIME_TOKEN"]}
    )
    _overlay(tmp_path, "acme", connectors={RUNTIME: {"base_url": "http://127.0.0.1:19"}})

    with pytest.raises(TenantOverlayError, match="declares its own headers"):
        Platform.from_file(path, platform_settings)


def test_a_mock_fixture_outside_its_pack_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    elsewhere = (examples / "access" / "fixtures" / "directory.json").resolve()
    ops = _ops_pack(
        examples,
        tmp_path / "packs" / "ops",
        binding={"type": "mock", "fixture": str(elsewhere), "operations": {}},
    )
    path = _platform(tmp_path, examples, ops=ops)

    with pytest.raises(PlatformError, match="outside the pack"):
        Platform.from_file(path, platform_settings)


# What the bindings of a platform may read from the environment


def test_a_tenant_header_reading_a_variable_its_tenant_does_not_declare_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTHER_TENANT_TOKEN", "not-yours")
    ops = _ops_pack(examples, tmp_path / "packs" / "ops")
    path = _platform(
        tmp_path, examples, ops=ops, tenants={"acme": {"environment": {"prefix": "ACME_"}}}
    )
    _overlay(
        tmp_path,
        "acme",
        connectors={RUNTIME: {"headers": {"Authorization": "Bearer ${OTHER_TENANT_TOKEN}"}}},
    )

    with pytest.raises(TenantOverlayError, match="OTHER_TENANT_TOKEN"):
        Platform.from_file(path, platform_settings)


def test_a_tenant_may_not_read_what_its_domain_declares(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    ops = _ops_pack(examples, tmp_path / "packs" / "ops")
    path = _platform(
        tmp_path, examples, ops=ops, domain_environment={"names": ["RUNTIME_TOKEN"]}
    )
    _overlay(
        tmp_path, "acme", connectors={RUNTIME: {"headers": {"X-Token": "${RUNTIME_TOKEN}"}}}
    )

    with pytest.raises(TenantOverlayError, match="RUNTIME_TOKEN"):
        Platform.from_file(path, platform_settings)


def test_a_pack_binding_reads_only_what_its_domain_declares(
    examples: Path, tmp_path: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNTIME_TOKEN", "domain-secret")
    ops = _ops_pack(
        examples,
        tmp_path / "packs" / "ops",
        binding={
            "type": "http",
            "base_url": PLATFORM_URL,
            "headers": {"Authorization": "Bearer ${RUNTIME_TOKEN}"},
            "operations": {},
        },
    )

    with pytest.raises(PlatformError, match="RUNTIME_TOKEN"):
        Platform.from_file(_platform(tmp_path / "bare", examples, ops=ops), platform_settings)

    granted = Platform.from_file(
        _platform(
            tmp_path / "granted", examples, ops=ops, domain_environment={"names": ["RUNTIME_TOKEN"]}
        ),
        platform_settings,
    )
    bound = granted.engine("operations").connectors.get(RUNTIME)
    assert isinstance(bound, HttpConnector)
    assert bound._client.headers["Authorization"] == "Bearer domain-secret"


def test_one_catalog_without_a_manifest_reads_the_environment_as_before(tmp_path: Path) -> None:
    fixture = tmp_path / "elsewhere" / "fixture.json"
    fixture.parent.mkdir()
    fixture.write_text("{}", encoding="utf-8")
    path = tmp_path / "configs" / "connectors.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "integrations": {
                    "integration.api": {
                        "type": "http",
                        "base_url": "http://127.0.0.1:9",
                        "headers": {"X-Token": "${ANY_NAME_AT_ALL}"},
                    },
                    "integration.data": {"type": "mock", "fixture": str(fixture)},
                }
            }
        ),
        encoding="utf-8",
    )

    registry = ConnectorRegistry.from_config(path, env={})

    assert isinstance(registry.get("integration.data"), MockConnector)
    assert registry.warnings == ["integration.api: ANY_NAME_AT_ALL is not set in the environment"]


def test_a_tenant_named_in_the_manifest_must_be_served(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    path = _platform(tmp_path, examples, tenants={"ghost": {"environment": {"names": ["X"]}}})

    with pytest.raises(PlatformError, match="ghost"):
        Platform.from_file(path, platform_settings)


# How far a tenant's runtime fragment may reach


@pytest.mark.parametrize(
    ("section", "key", "above"),
    [
        ("resilience", "retries", 99),
        ("resilience", "timeout_seconds", 9999.0),
        ("resilience", "backoff_seconds", 999.0),
        ("resilience", "max_response_bytes", 10**12),
        ("reply", "conversation_window", 999),
    ],
)
def test_a_tenant_may_not_raise_a_cost_past_the_platform(
    examples: Path,
    tmp_path: Path,
    platform_settings: Settings,
    section: str,
    key: str,
    above: float,
) -> None:
    path = _platform(tmp_path, examples)
    _overlay(tmp_path, "acme", runtime={section: {key: above}})

    wanted = rf"{section}\.{key} .*{above}.*platform"
    with pytest.raises(TenantOverlayError, match=wanted) as refused:
        Platform.from_file(path, platform_settings)

    plain = Platform.from_file(_platform(tmp_path / "plain", examples), platform_settings)
    platform_value = getattr(getattr(plain.runtime, section), key)
    assert str(platform_value) in str(refused.value)


def test_a_tenant_may_lower_a_cost(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    path = _platform(tmp_path, examples)
    _overlay(
        tmp_path,
        "acme",
        runtime={"resilience": {"timeout_seconds": 1.0}, "reply": {"conversation_window": 2}},
    )
    held = Platform.from_file(path, platform_settings)

    assert held.runtime_for("acme").resilience.timeout_seconds == 1.0
    assert held.runtime_for("acme").reply.conversation_window == 2
    platform_window = held.runtime.reply.conversation_window
    assert held.runtime_for("default").reply.conversation_window == platform_window
