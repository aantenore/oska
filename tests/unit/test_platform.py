"""A platform of several domains and tenants, served from one process."""

from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import oska.engine.factory as factory
import oska.platform.runtime as platform_runtime
from oska.api.app import create_app
from oska.catalog.registry import SkillRegistry
from oska.config import ResilienceConfig, Settings
from oska.connectors.mock import MockConnector
from oska.contracts.common import Message
from oska.contracts.platform import HostIdentity
from oska.engine.session_store import SessionStore
from oska.engine.state import SessionState
from oska.planner.scripted import ScriptedModelProvider
from oska.platform import (
    DomainMismatch,
    ManifestError,
    PackError,
    Platform,
    PlatformError,
    TenantMismatch,
    TenantOverlayError,
    UnknownDomain,
    UnknownTenant,
    check_provides,
    load_pack,
    load_platform,
    resolve_dependencies,
)
from oska.platform.assembly import merge_connectors
from oska.platform.packs import check_requires
from oska.platform.versions import satisfies
from oska.storage import DEFAULT_NAMESPACE, SessionStateStore, storage_namespace

ACCESS_QUESTION = "what access does this person have"
OFF_SCOPE = "book me a flight to Lisbon next Tuesday"
SCENARIOS = Path(__file__).resolve().parents[2] / "examples" / "ops" / "scenarios"


class Briefs:
    """A scripted provider that keeps the last user message of every call it answers."""

    name = "briefs"

    def __init__(self, script: list[dict]) -> None:
        self._inner = ScriptedModelProvider(script)
        self.briefs: list[str] = []

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        said = [message.content for message in messages if message.role == "user"]
        self.briefs.append(said[-1] if said else "")
        return self._inner.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


def _off_scope_script() -> list[dict]:
    return [
        {
            "expect_contains": OFF_SCOPE,
            "response": {
                "kind": "off_scope",
                "runbook_id": None,
                "confidence": 0.95,
                "language": "en",
            },
        },
        {"expect_contains": '"facts"', "response": {"reply": "Not here."}},
    ]


class MemorySessions:
    """A session store that is not a file, shared per namespace like a database would be."""

    held: dict[str, dict[str, SessionState]] = {}

    def __init__(self, namespace: str) -> None:
        self._rows = MemorySessions.held.setdefault(namespace, {})

    def load(self, session_id: str) -> SessionState:
        held = self._rows.get(session_id)
        return SessionState(session_id=session_id) if held is None else held.model_copy(deep=True)

    def save(self, state: SessionState) -> None:
        self._rows[state.session_id] = state.model_copy(deep=True)

    def sessions(self) -> list[str]:
        return list(self._rows)

    def updated_at(self, session_id: str) -> datetime | None:
        return datetime.now(UTC) if session_id in self._rows else None

    def exists(self, session_id: str) -> bool:
        return session_id in self._rows

    def expired(self, before: datetime) -> list[str]:
        return []

    def delete(self, session_id: str) -> int:
        return 1 if self._rows.pop(session_id, None) is not None else 0


def _write_platform(directory: Path, document: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "platform.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _manifest(examples: Path, *, tenants: list[str] | None = None) -> dict:
    return {
        "name": "trial",
        "packs": {
            "ops": {"path": str(examples / "ops")},
            "access": {"path": str(examples / "access")},
        },
        "domains": [
            {
                "name": "operations",
                "packs": ["ops"],
                "host": {"name": "the operations desk"},
                "tenants": tenants or ["default"],
            },
            {
                "name": "access",
                "packs": ["access"],
                "host": {"name": "the access desk"},
                "tenants": tenants or ["default"],
            },
        ],
    }


def _copy_pack(source: Path, target: Path, **changes: object) -> Path:
    shutil.copytree(source, target)
    manifest = yaml.safe_load((target / "pack.yaml").read_text(encoding="utf-8"))
    manifest.update(changes)
    (target / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return target


def _events_text(result: object) -> str:
    return json.dumps([event.model_dump(mode="json") for event in result.events])  # type: ignore[attr-defined]


# Packs


def test_an_example_pack_loads_with_its_layout_and_provides_what_it_declares(
    examples: Path,
) -> None:
    for name in ("ops", "access"):
        pack = load_pack(examples / name)
        assert pack.name == name
        assert pack.version == "0.1.0"
        assert pack.skills.is_dir() and pack.connectors.is_file() and pack.scenarios.is_dir()
        assert pack.guardrails is None
        check_provides(pack, SkillRegistry([pack.skills]))
    assert load_pack(examples / "ops").manifest.level == 3
    assert load_pack(examples / "access").manifest.level == 2


def test_provides_that_disagrees_with_the_skills_is_refused(
    examples: Path, tmp_path: Path
) -> None:
    pack = load_pack(
        _copy_pack(
            examples / "access",
            tmp_path / "access",
            provides={
                "entities": ["entity.user", "entity.group", "entity.ghost"],
                "procedures": ["runbook.access_check"],
            },
        )
    )

    with pytest.raises(PackError) as refused:
        check_provides(pack, SkillRegistry([pack.skills]))

    message = str(refused.value)
    assert "entity.ghost, which it does not declare" in message
    assert "entity.access_request, which provides.entities omits" in message
    assert "runbook.access_grant, which provides.procedures omits" in message


def test_a_pack_missing_its_catalog_or_bindings_or_a_valid_version_is_refused(
    examples: Path, tmp_path: Path
) -> None:
    without_skills = _copy_pack(examples / "access", tmp_path / "a")
    shutil.rmtree(without_skills / "skills")
    without_bindings = _copy_pack(examples / "access", tmp_path / "b")
    (without_bindings / "configs" / "connectors.json").unlink()
    bad_version = _copy_pack(examples / "access", tmp_path / "c", version="1.0")

    with pytest.raises(PackError, match="no skills/"):
        load_pack(without_skills)
    with pytest.raises(PackError, match="connectors.json"):
        load_pack(without_bindings)
    with pytest.raises(PackError, match="MAJOR.MINOR.PATCH"):
        load_pack(bad_version)
    with pytest.raises(PackError, match="no pack.yaml"):
        load_pack(tmp_path / "absent")


def test_check_requires_refuses_a_runtime_version_outside_the_range(examples: Path) -> None:
    """OSKA-PLT-4: a pack whose `requires` range does not accept the runtime is refused."""
    pack = load_pack(examples / "ops")
    assert pack.manifest.requires["oska"] == ">=0.1.0,<1.0.0"

    check_requires(pack, runtime_version="0.5.0")  # inside the range: accepted

    with pytest.raises(PackError, match="requires oska >=0.1.0,<1.0.0"):
        check_requires(pack, runtime_version="1.0.0")


def test_a_skill_id_declared_by_two_packs_of_one_domain_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    """OSKA-PLT-7: a skill identifier declared by two packs of one domain refuses the
    platform, before anything asks what either pack provides."""
    extra = _copy_pack(examples / "ops", tmp_path / "packs" / "extra", name="extra")
    document = _manifest(examples)
    document["packs"]["extra"] = {"path": str(extra)}
    document["domains"][0]["packs"] = ["ops", "extra"]

    with pytest.raises(PlatformError, match="is declared by packs ops and extra"):
        Platform.from_file(_write_platform(tmp_path, document), platform_settings)


def test_two_engines_never_share_a_connector(examples: Path) -> None:
    """OSKA-PLT-10: `merge_connectors` builds a fresh registry, and fresh connectors in
    it, every call: nothing one engine holds is the object another engine holds."""
    pack = load_pack(examples / "ops")
    resilience = ResilienceConfig()

    first = merge_connectors([pack], None, resilience)
    second = merge_connectors([pack], None, resilience)

    assert first is not second
    assert first.ids() == second.ids()
    assert first.ids(), "the ops pack binds at least one integration"
    for integration_id in first.ids():
        assert first.get(integration_id) is not second.get(integration_id)


@pytest.mark.parametrize(
    ("version", "spec", "expected"),
    [
        ("1.2.3", "1.2.3", True),
        ("1.2.3", "1.2.4", False),
        ("1.2.3", "*", True),
        ("0.2.5", ">=0.2,<0.3", True),
        ("0.3.0", ">=0.2,<0.3", False),
        ("0.1.9", ">=0.2.0", False),
        ("2.0.0", "<2.0.0", False),
    ],
)
def test_version_ranges(version: str, spec: str, expected: bool) -> None:
    assert satisfies(version, spec) is expected


def test_a_version_range_that_cannot_be_read_raises() -> None:
    with pytest.raises(ValueError):
        satisfies("1.0.0", "~1.0")
    with pytest.raises(ValueError):
        satisfies("1.0.0", "")


def test_dependencies_are_ordered_and_a_missing_or_incompatible_one_is_refused(
    examples: Path, tmp_path: Path
) -> None:
    base = load_pack(_copy_pack(examples / "ops", tmp_path / "base", name="base"))
    child = load_pack(
        _copy_pack(
            examples / "access",
            tmp_path / "child",
            name="child",
            depends_on=[{"pack": "base", "version": ">=0.1.0,<0.2.0"}],
        )
    )
    greedy = load_pack(
        _copy_pack(
            examples / "access",
            tmp_path / "greedy",
            name="greedy",
            depends_on=[{"pack": "base", "version": ">=1.0.0"}],
        )
    )

    assert [pack.name for pack in resolve_dependencies([child, base])] == ["base", "child"]
    with pytest.raises(PackError, match="depends on base >=0.1.0,<0.2.0, which is not deployed"):
        resolve_dependencies([child])
    with pytest.raises(PackError, match="and base is 0.1.0"):
        resolve_dependencies([greedy, base])


def test_a_dependency_cycle_is_refused(examples: Path, tmp_path: Path) -> None:
    first = load_pack(
        _copy_pack(examples / "ops", tmp_path / "first", name="first",
                   depends_on=[{"pack": "second"}])
    )
    second = load_pack(
        _copy_pack(examples / "access", tmp_path / "second", name="second",
                   depends_on=[{"pack": "first"}])
    )

    with pytest.raises(PackError, match="cycle: first -> second -> first"):
        resolve_dependencies([first, second])


# Manifest


def test_the_manifest_resolves_pack_paths_beside_it(examples: Path) -> None:
    manifest = load_platform(examples / "platform.yaml")

    assert manifest.packs["ops"].path == (examples / "ops").resolve()
    assert [domain.name for domain in manifest.domains] == ["operations", "access"]
    assert manifest.domain().host.name == "the operations desk"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"domains": []}, "at least one domain"),
        ({"default_domain": "nowhere"}, "default domain 'nowhere' is not declared"),
        ({"name": "Not A Name"}, "is not a name"),
    ],
)
def test_a_manifest_that_does_not_hold_up_is_refused(
    examples: Path, tmp_path: Path, change: dict, message: str
) -> None:
    path = _write_platform(tmp_path, {**_manifest(examples), **change})

    with pytest.raises(ManifestError, match=message):
        load_platform(path)


def test_a_domain_naming_an_undeclared_pack_is_refused(examples: Path, tmp_path: Path) -> None:
    document = _manifest(examples)
    document["domains"][1]["packs"] = ["billing"]

    with pytest.raises(ManifestError, match="undeclared packs"):
        load_platform(_write_platform(tmp_path, document))


def test_a_pack_whose_version_the_manifest_does_not_accept_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    document = _manifest(examples)
    document["packs"]["ops"]["version"] = ">=0.2.0"

    with pytest.raises(ManifestError, match="expects pack ops >=0.2.0"):
        Platform.from_file(_write_platform(tmp_path, document), platform_settings)


# Isolation between domains


def test_a_skill_of_one_domain_is_never_ranked_in_another(example_platform: Platform) -> None:
    operations = {skill.id for skill in example_platform.engine("operations").registry.current()}
    access = {skill.id for skill in example_platform.engine("access").registry.current()}
    assert operations and access and not operations & access

    answered_here = example_platform.turn("access", None, "asks-access", ACCESS_QUESTION)
    answered_there = example_platform.turn("operations", None, "asks-ops", ACCESS_QUESTION)

    assert "runbook.access_check" in _events_text(answered_here)
    ranked = [
        entry["skill_id"]
        for event in answered_there.events
        if event.kind == "discovery"
        for entry in event.data["ranked"]
    ]
    assert set(ranked) <= operations
    assert not access & set(_events_text(answered_there).split('"'))
    assert "the operations desk" in answered_there.reply


def test_a_session_is_bound_to_the_domain_of_its_first_turn(example_platform: Platform) -> None:
    example_platform.turn("operations", None, "shared", "hello")

    with pytest.raises(DomainMismatch, match="belongs to domain operations"):
        example_platform.turn("access", None, "shared", "hello")

    assert example_platform.engine("operations").state("shared").domain == "operations"
    assert example_platform.engine("access").state("shared").turn_count == 0
    assert example_platform.turn("operations", None, "shared", "hello").reply


def test_each_domain_keeps_its_state_under_its_own_directory(
    example_platform: Platform, platform_settings: Settings
) -> None:
    example_platform.turn("operations", None, "one", "hello")
    example_platform.turn("access", None, "two", "hello")

    root = Path(platform_settings.state_dir) / "default"
    assert SessionStore(root / "operations" / "sessions.sqlite").sessions() == ["one"]
    assert SessionStore(root / "access" / "sessions.sqlite").sessions() == ["two"]
    assert example_platform.settings_for("access").state_dir == root / "access"
    assert example_platform.settings_for("access").skills_roots == [
        load_pack(example_platform.manifest.packs["access"].path).skills
    ]


def test_an_unknown_domain_or_tenant_is_refused(example_platform: Platform) -> None:
    with pytest.raises(UnknownDomain):
        example_platform.engine("billing")
    with pytest.raises(UnknownTenant):
        example_platform.engine("access", "acme")
    with pytest.raises(UnknownTenant):
        example_platform.engine("access", "../escape")


# --- every (tenant, domain) a platform serves, for retention and improvement ----


def _two_domains_two_tenants(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> Platform:
    path = _write_platform(tmp_path, _manifest(examples, tenants=["default", "acme"]))
    return Platform.from_file(path, platform_settings)


def test_pairs_without_either_is_every_tenant_of_every_domain(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    held = _two_domains_two_tenants(examples, tmp_path, platform_settings)

    assert held.pairs() == [
        ("default", "operations"),
        ("acme", "operations"),
        ("default", "access"),
        ("acme", "access"),
    ]


def test_pairs_named_domain_or_tenant_narrows_the_reach(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    held = _two_domains_two_tenants(examples, tmp_path, platform_settings)

    assert held.pairs(domain="operations") == [("default", "operations"), ("acme", "operations")]
    assert held.pairs(tenant="acme") == [("acme", "operations"), ("acme", "access")]
    assert held.pairs("operations", "acme") == [("acme", "operations")]


def test_pairs_of_an_unknown_domain_or_tenant_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    held = _two_domains_two_tenants(examples, tmp_path, platform_settings)

    with pytest.raises(UnknownDomain):
        held.pairs(domain="billing")
    with pytest.raises(UnknownTenant):
        held.pairs(tenant="ghost")
    with pytest.raises(UnknownTenant):
        held.pairs("operations", "ghost")


def test_the_runtime_cache_evicts_the_least_recent_and_rebuilds_it(
    examples: Path, repo_root: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    runtime = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    runtime["platform"] = {"cache_size": 1}
    runtime["guardrails"]["catalog"] = str(repo_root / "configs" / "guardrails.json")
    (tmp_path / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    held = Platform.from_file(
        examples / "platform.yaml",
        platform_settings.model_copy(update={"runtime_config": tmp_path / "runtime.json"}),
    )

    first = held.engine("operations")
    held.turn("operations", None, "kept", "hello")
    held.engine("access")
    assert held.cached() == [("default", "access")]

    rebuilt = held.engine("operations")
    assert rebuilt is not first
    assert held.cached() == [("default", "operations")]
    assert rebuilt.state("kept").turn_count == 1


def _cache_of_one(repo_root: Path, tmp_path: Path, platform_settings: Settings) -> Settings:
    runtime = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    runtime["platform"] = {"cache_size": 1}
    runtime["guardrails"]["catalog"] = str(repo_root / "configs" / "guardrails.json")
    (tmp_path / "runtime-one.json").write_text(json.dumps(runtime), encoding="utf-8")
    return platform_settings.model_copy(update={"runtime_config": tmp_path / "runtime-one.json"})


def test_an_evicted_runtime_is_closed(
    examples: Path, repo_root: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    ops = _copy_pack(examples / "ops", tmp_path / "packs" / "ops")
    path = ops / "configs" / "connectors.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    bindings["integrations"]["integration.container_runtime"] = {
        "type": "http",
        "base_url": "http://127.0.0.1:9",
        "operations": {},
    }
    path.write_text(json.dumps(bindings), encoding="utf-8")
    document = _manifest(examples)
    document["packs"]["ops"] = {"path": str(ops)}
    held = Platform.from_file(
        _write_platform(tmp_path, document), _cache_of_one(repo_root, tmp_path, platform_settings)
    )

    pooled = held.engine("operations").connectors.get("integration.container_runtime")
    assert not pooled._client.is_closed  # type: ignore[attr-defined]
    held.engine("access")

    assert held.cached() == [("default", "access")]
    assert pooled._client.is_closed  # type: ignore[attr-defined]


def test_a_cold_build_of_one_pair_does_not_hold_up_another(
    examples: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = Platform.from_file(examples / "platform.yaml", platform_settings)
    started, release = threading.Event(), threading.Event()
    built: list[str] = []
    real = platform_runtime.build_engine

    def slow(settings: Settings, **kwargs: Any) -> Any:
        built.append(str(kwargs["namespace"]))
        if kwargs["namespace"] == "default/operations":
            started.set()
            release.wait(10)
        return real(settings, **kwargs)

    monkeypatch.setattr(platform_runtime, "build_engine", slow)
    engines: list[Any] = []
    first = threading.Thread(target=lambda: engines.append(held.engine("operations")))
    second = threading.Thread(target=lambda: engines.append(held.engine("operations")))
    other = threading.Thread(target=held.engine, args=("access",))
    first.start()
    try:
        assert started.wait(5)
        second.start()
        other.start()
        other.join(timeout=3)
        assert not other.is_alive(), "the build of another pair waited for this one"
        assert ("default", "access") in held.cached()
    finally:
        release.set()
        for thread in (first, second, other):
            if thread.ident is not None:
                thread.join(10)

    assert built.count("default/operations") == 1
    assert len(engines) == 2 and engines[0] is engines[1]


def test_the_session_stores_a_platform_opens_are_bounded_by_its_cache_size(
    examples: Path, repo_root: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    held = Platform.from_file(
        examples / "platform.yaml", _cache_of_one(repo_root, tmp_path, platform_settings)
    )

    held.turn("operations", None, "one", "hello")
    held.turn("access", None, "two", "hello")

    assert len(held._session_stores) == 1  # noqa: SLF001


def test_a_session_store_in_use_is_not_closed_when_the_cache_evicts(
    examples: Path, repo_root: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    """While one caller holds a pair's store, filling the cache must not close it."""
    held = Platform.from_file(
        examples / "platform.yaml", _cache_of_one(repo_root, tmp_path, platform_settings)
    )
    closed: list[object] = []
    opened = held._open_sessions  # noqa: SLF001

    def tracked(domain: str, tenant: str):  # type: ignore[no-untyped-def]
        store = opened(domain, tenant)
        original = getattr(store, "close", None)

        def close() -> None:
            closed.append(store)
            if callable(original):
                original()

        store.close = close  # type: ignore[method-assign]
        return store

    held._open_sessions = tracked  # type: ignore[method-assign]  # noqa: SLF001
    with held._session_store("operations", "default") as in_use:  # noqa: SLF001
        with held._session_store("access", "default"):  # noqa: SLF001
            pass
        assert in_use not in closed, "a store in use was closed by eviction"
        assert in_use.exists("nobody") is False
    assert len(held._session_stores) == 1  # noqa: SLF001


def test_an_absolute_state_path_is_refused(
    examples: Path, repo_root: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    runtime = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    runtime["ledger"] = {"path": str(tmp_path / "shared-ledger.sqlite")}
    (tmp_path / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

    with pytest.raises(PlatformError, match="ledger.path is absolute"):
        Platform.from_file(
            examples / "platform.yaml",
            platform_settings.model_copy(update={"runtime_config": tmp_path / "runtime.json"}),
        )


def test_an_integration_bound_by_two_packs_of_one_domain_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    extra = _copy_pack(examples / "access", tmp_path / "packs" / "extra", name="extra")
    bindings = json.loads((extra / "configs" / "connectors.json").read_text(encoding="utf-8"))
    bindings["integrations"]["integration.container_runtime"] = {"type": "shell"}
    (extra / "configs" / "connectors.json").write_text(json.dumps(bindings), encoding="utf-8")
    document = _manifest(examples)
    document["packs"]["extra"] = {"path": str(extra)}
    document["domains"][0]["packs"] = ["ops", "extra"]

    with pytest.raises(PlatformError, match="container_runtime is bound by packs ops and extra"):
        Platform.from_file(_write_platform(tmp_path, document), platform_settings)


def test_pack_guardrails_join_the_catalog_and_a_second_definition_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    pack = _copy_pack(examples / "access", tmp_path / "packs" / "access")
    entry = {
        "title": "A reviewer reads every grant",
        "description": "A grant is read by a reviewer before the request is closed.",
        "enforcement": {"point": "execution", "mechanism": "invariant", "module": "review"},
    }
    (pack / "guardrails.json").write_text(
        json.dumps({"guardrails": {"guardrail.reviewed_grants": entry}}), encoding="utf-8"
    )
    skill = pack / "skills" / "runbook" / "access_check" / "v1" / "skill.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace(
            "guardrails:\n", "guardrails:\n  - guardrail.reviewed_grants\n", 1
        ),
        encoding="utf-8",
    )
    document = _manifest(examples)
    document["packs"]["access"] = {"path": str(pack)}
    held = Platform.from_file(_write_platform(tmp_path / "one", document), platform_settings)

    assert held.engine("access").guardrails.get("guardrail.reviewed_grants") is not None
    assert held.engine("operations").guardrails.get("guardrail.reviewed_grants") is None

    (pack / "guardrails.json").write_text(
        json.dumps({"guardrails": {"guardrail.declared_operations_only": entry}}),
        encoding="utf-8",
    )
    with pytest.raises(PlatformError, match="declared_operations_only is defined by the platform"):
        Platform.from_file(_write_platform(tmp_path / "two", document), platform_settings)


# Tenants


def _tenant_overlay(root: Path, tenant: str, *, runtime: dict | None = None,
                    connectors: dict | None = None) -> None:
    directory = root / "tenants" / tenant
    directory.mkdir(parents=True, exist_ok=True)
    if runtime is not None:
        (directory / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    if connectors is not None:
        (directory / "connectors.json").write_text(json.dumps(connectors), encoding="utf-8")


def test_a_tenant_overlay_merges_its_runtime_fragment_and_keeps_its_state_apart(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    path = _write_platform(tmp_path, _manifest(examples, tenants=["default", "acme"]))
    _tenant_overlay(
        tmp_path,
        "acme",
        runtime={"reply": {"default_language": "it"}, "discovery": {"languages": ["it"]}},
    )
    held = Platform.from_file(path, platform_settings)

    assert isinstance(
        held.engine("operations", "acme").connectors.get("integration.container_runtime"),
        MockConnector,
    )
    assert held.runtime_for("acme").reply.default_language == "it"
    assert held.runtime_for("acme").discovery.languages == ["it"]
    assert held.runtime_for("default").reply.default_language == "en"

    held.turn("operations", "acme", "acme-id", "hello")
    held.turn("operations", "default", "default-id", "hello")
    root = Path(platform_settings.state_dir)
    assert SessionStore(root / "acme" / "operations" / "sessions.sqlite").sessions() == ["acme-id"]
    assert SessionStore(root / "default" / "operations" / "sessions.sqlite").sessions() == [
        "default-id"
    ]


def test_a_session_is_bound_to_the_tenant_of_its_first_turn(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    held = _two_domains_two_tenants(examples, tmp_path, platform_settings)

    held.turn("operations", "acme", "same-id", "hello")
    state = held.engine("operations", "acme").state("same-id")
    assert (state.tenant, state.domain) == ("acme", "operations")

    with pytest.raises(TenantMismatch, match="belongs to another tenant") as refused:
        held.turn("operations", "default", "same-id", "hello")
    assert "acme" not in str(refused.value)
    with pytest.raises(TenantMismatch):
        held.turn("access", "default", "same-id", "hello")
    with pytest.raises(DomainMismatch, match="belongs to domain operations"):
        held.turn("access", "acme", "same-id", "hello")

    assert held.engine("operations", "default").state("same-id").turn_count == 0
    assert held.turn("operations", "acme", "same-id", "hello").reply


@pytest.mark.parametrize(
    "fragment",
    [{"identity": {"mode": "token"}}, {"discovery": {"min_score": 0.9}}, {"ledger": {}}],
)
def test_a_tenant_may_not_change_what_belongs_to_the_platform(
    examples: Path, tmp_path: Path, platform_settings: Settings, fragment: dict
) -> None:
    path = _write_platform(tmp_path, _manifest(examples, tenants=["default", "acme"]))
    _tenant_overlay(tmp_path, "acme", runtime=fragment)

    with pytest.raises(TenantOverlayError, match="a tenant may not change"):
        Platform.from_file(path, platform_settings)


def test_a_tenant_binding_for_an_undeclared_integration_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    path = _write_platform(tmp_path, _manifest(examples, tenants=["default", "acme"]))
    _tenant_overlay(
        tmp_path, "acme", connectors={"integrations": {"integration.billing": {"type": "shell"}}}
    )

    with pytest.raises(PlatformError, match="integration.billing, which no pack"):
        Platform.from_file(path, platform_settings)


# What each domain hands its engine


def _spy_on_engines(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the keyword arguments every engine of the platform is built with."""
    built: list[dict[str, Any]] = []
    real = platform_runtime.build_engine

    def spy(settings: Settings, **kwargs: Any) -> Any:
        built.append(kwargs)
        return real(settings, **kwargs)

    monkeypatch.setattr(platform_runtime, "build_engine", spy)
    return built


def test_each_domain_hands_its_engine_its_presentation_synonyms_host_and_namespace(
    examples: Path, tmp_path: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _copy_pack(examples / "ops", tmp_path / "packs" / "ops", synonyms={"box": "service"})
    document = _manifest(examples, tenants=["default", "acme"])
    document["packs"]["ops"] = {"path": str(ops)}
    document["domains"][0]["presentation"] = True
    document["domains"][0]["host"] = {"name": "the operations desk", "voice": "brief"}
    held = Platform.from_file(_write_platform(tmp_path, document), platform_settings)
    built = _spy_on_engines(monkeypatch)

    held.engine("operations", "acme")
    held.engine("access")

    operations, access = built
    assert operations["presentation"] is True
    assert operations["synonyms"] == {"box": "service"} == held.synonyms("operations")
    assert operations["host"] == HostIdentity(name="the operations desk", voice="brief")
    assert operations["namespace"] == storage_namespace("acme", "operations") == "acme/operations"
    assert access["presentation"] is False
    assert access["synonyms"] == {}
    assert access["host"].name == "the access desk"
    assert access["namespace"] == "default/access" != DEFAULT_NAMESPACE


def test_synonyms_of_two_packs_merge_and_a_term_mapped_apart_is_refused(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    ops = _copy_pack(examples / "ops", tmp_path / "packs" / "ops", synonyms={"box": "service"})
    agreeing = _copy_pack(
        examples / "access",
        tmp_path / "packs" / "agreeing",
        name="extra",
        synonyms={"BOX": "service", "badge": "group"},
    )
    document = _manifest(examples)
    document["packs"]["ops"] = {"path": str(ops)}
    document["packs"]["extra"] = {"path": str(agreeing)}
    document["domains"][0]["packs"] = ["ops", "extra"]
    held = Platform.from_file(_write_platform(tmp_path / "one", document), platform_settings)
    assert held.synonyms("operations") == {"box": "service", "badge": "group"}
    assert held.synonyms("access") == {}

    clashing = _copy_pack(
        examples / "access", tmp_path / "packs" / "clashing", name="extra", synonyms={"Box": "host"}
    )
    document["packs"]["extra"] = {"path": str(clashing)}
    with pytest.raises(PlatformError, match="synonym 'box' is mapped to 'service' by pack ops"):
        Platform.from_file(_write_platform(tmp_path / "two", document), platform_settings)


def test_a_domain_that_enables_presentation_returns_directives_and_one_that_does_not_none(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    scenario = json.loads(
        (SCENARIOS / "27_subject_from_what_was_shown.json").read_text(encoding="utf-8")
    )
    first = scenario["turns"][0]
    for presenting in (True, False):
        document = _manifest(examples)
        document["domains"][0]["presentation"] = presenting
        held = Platform.from_file(
            _write_platform(tmp_path / str(presenting), document),
            platform_settings.model_copy(
                update={"state_dir": tmp_path / f"state-{presenting}"}
            ),
            provider=ScriptedModelProvider(scenario["provider_script"][:3]),
        )

        result = held.turn("operations", None, "shown", first["user"])

        produced = [(d.procedure, d.moment, d.view) for d in result.directives]
        wanted = [(d["procedure"], d["moment"], d["view"]) for d in first["expect"]["directives"]]
        assert produced == (wanted if presenting else [])


# Who the reply is written as


def test_the_reply_writer_is_told_who_the_domain_answers_as(
    examples: Path, platform_settings: Settings
) -> None:
    provider = Briefs(_off_scope_script())
    held = Platform.from_file(examples / "platform.yaml", platform_settings, provider=provider)

    assert held.turn("operations", None, "asks", OFF_SCOPE).reply.startswith("Not here.")

    brief = json.loads(provider.briefs[-1])
    assert brief["facts"][1:] == [
        "You answer as the operations desk.",
        "Your voice is brief and factual.",
        "You are declared to answer in: en, it.",
    ]
    assert "check the health of a service" in brief["facts"][0]


def test_a_single_catalog_tells_the_reply_writer_of_no_host(example_settings: Settings) -> None:
    provider = Briefs(_off_scope_script())
    engine = factory.build_engine(example_settings, provider=provider)

    engine.turn("asks", OFF_SCOPE)

    facts = json.loads(provider.briefs[-1])["facts"]
    assert len(facts) == 1
    assert not any("You answer as" in fact for fact in facts)


# Binding a session to a domain, through the store


def test_the_binding_is_asked_of_the_session_stores_whatever_backend_keeps_them(
    example_platform: Platform, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemorySessions.held = {}

    def memory(*_: object, namespace: str = DEFAULT_NAMESPACE, **__: object) -> SessionStateStore:
        return MemorySessions(namespace)

    monkeypatch.setattr(platform_runtime, "open_sessions", memory)
    monkeypatch.setattr(factory, "open_sessions", memory)

    example_platform.turn("operations", None, "shared", "hello")
    with pytest.raises(DomainMismatch, match="belongs to domain operations"):
        example_platform.turn("access", None, "shared", "hello")

    assert set(MemorySessions.held) == {"default/operations", "default/access"}
    assert MemorySessions.held["default/operations"]["shared"].domain == "operations"
    assert "shared" not in MemorySessions.held["default/access"]
    assert not list(Path(platform_settings.state_dir).rglob("sessions.sqlite"))


def test_asking_whether_a_session_exists_creates_no_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "nowhere" / "sessions.sqlite")

    assert store.exists("absent") is False
    assert not (tmp_path / "nowhere").exists()
    store.save(SessionState(session_id="present"))
    assert store.exists("present") and not store.exists("absent")


def test_the_http_application_closes_its_platform_when_it_shuts_down(
    examples: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = Platform.from_file(examples / "platform.yaml", platform_settings)
    closed: list[Any] = []
    real_close = platform_runtime._close

    def spy(services: Any) -> None:
        closed.append(services)
        real_close(services)

    monkeypatch.setattr(platform_runtime, "_close", spy)
    app = create_app(platform_settings, platform=held)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        warm = held.cached()
        assert warm
        assert closed == []

    assert held.cached() == []
    assert len(closed) == len(warm)


# Binding and leasing under load


class _Gated:
    """A session store whose load of one session waits until it is let through."""

    def __init__(self, inner: SessionStateStore, session_id: str, entered: Any, release: Any):
        self._inner = inner
        self._session_id = session_id
        self._entered = entered
        self._release = release

    def load(self, session_id: str) -> SessionState:
        if session_id == self._session_id:
            self._entered.set()
            self._release.wait(10)
        return self._inner.load(session_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_two_new_sessions_of_different_tenants_bind_without_waiting_for_each_other(
    examples: Path, tmp_path: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _two_domains_two_tenants(examples, tmp_path, platform_settings)
    entered, release = threading.Event(), threading.Event()
    real = platform_runtime.open_sessions

    def gated(*args: Any, **kwargs: Any) -> SessionStateStore:
        return _Gated(real(*args, **kwargs), "slow", entered, release)  # type: ignore[return-value]

    monkeypatch.setattr(platform_runtime, "open_sessions", gated)
    slow = threading.Thread(target=held.turn, args=("operations", "acme", "slow", "hello"))
    quick = threading.Thread(target=held.turn, args=("operations", "default", "quick", "hello"))
    slow.start()
    try:
        assert entered.wait(10)
        quick.start()
        quick.join(timeout=10)
        assert not quick.is_alive(), "binding one new session waited for another"
    finally:
        release.set()
        for thread in (slow, quick):
            if thread.ident is not None:
                thread.join(10)

    assert held.engine("operations", "default").state("quick").tenant == "default"
    assert held.engine("operations", "acme").state("slow").tenant == "acme"
    # the two ids pick different locks; two that picked the same one would wait
    assert held._binding_lock("slow") is not held._binding_lock("quick")  # noqa: SLF001


class _Evicting:
    """The runtime cache lock, standing in for another turn that evicts one pair's runtime.

    Every time the lock is taken, a runtime of `key` that no turn has claimed
    is pushed out of the cache first, as a build of another pair would push it
    when the cache is full. A lease that waits to claim a runtime after the lock
    that cached it was let go loses it every time.
    """

    def __init__(self, platform: Platform, key: tuple[str, str], limit: int) -> None:
        self._inner = threading.Lock()
        self._platform = platform
        self._key = key
        self._limit = limit
        self.evictions = 0

    def __enter__(self) -> _Evicting:
        self._inner.acquire()
        cache = self._platform._cache  # noqa: SLF001
        found = cache.get(self._key)
        leases = self._platform._leases  # noqa: SLF001
        if found is not None and not leases.get(id(found)) and self.evictions < self._limit:
            del cache[self._key]
            self.evictions += 1
        return self

    def __exit__(self, *_: object) -> None:
        self._inner.release()


def test_a_turn_claims_its_runtime_in_the_step_that_caches_it(
    example_platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    real = platform_runtime.build_engine

    def counted(settings: Settings, **kwargs: Any) -> Any:
        built.append(str(kwargs["namespace"]))
        return real(settings, **kwargs)

    monkeypatch.setattr(platform_runtime, "build_engine", counted)
    evicting = _Evicting(example_platform, ("default", "operations"), limit=5)
    example_platform._lock = evicting  # type: ignore[assignment]  # noqa: SLF001

    assert example_platform.turn("operations", None, "claimed", "hello").reply

    assert built == ["default/operations"]
    assert evicting.evictions == 0


def test_more_busy_pairs_than_the_cache_holds_all_run(
    examples: Path,
    repo_root: Path,
    tmp_path: Path,
    platform_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = Platform.from_file(
        _write_platform(tmp_path, _manifest(examples, tenants=["default", "acme"])),
        _cache_of_one(repo_root, tmp_path, platform_settings),
    )
    pairs = held.pairs()
    assert len(pairs) > 1
    together = threading.Barrier(len(pairs), timeout=10)
    built: list[str] = []
    real = platform_runtime.build_engine

    def waiting(settings: Settings, **kwargs: Any) -> Any:
        built.append(str(kwargs["namespace"]))
        engine = real(settings, **kwargs)
        turn = engine.turn

        def busy(*args: Any, **turn_kwargs: Any) -> Any:
            together.wait()
            return turn(*args, **turn_kwargs)

        engine.turn = busy  # type: ignore[method-assign]
        return engine

    monkeypatch.setattr(platform_runtime, "build_engine", waiting)
    replies: list[str] = []
    threads = [
        threading.Thread(
            target=lambda t=tenant, d=domain: replies.append(
                held.turn(d, t, f"{t}-{d}", "hello").reply
            )
        )
        for tenant, domain in pairs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert not any(thread.is_alive() for thread in threads)
    assert len(replies) == len(pairs)
    assert sorted(built) == sorted(storage_namespace(t, d) for t, d in pairs)
    assert len(held.cached()) == 1
