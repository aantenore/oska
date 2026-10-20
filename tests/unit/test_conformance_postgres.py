"""A conformance run keeps to namespaces it creates, and leaves nothing behind in them.

The offline half checks what the runner asks of the engine it builds: a fresh
namespace under `conformance/` for every scenario, the (tenant, domain) nested
inside it on the platform path, and no purge of a store that reports any other
namespace. The PostgreSQL half runs when `OSKA_TEST_POSTGRES_URL` names a
database the tests may write to: the same suites run twice over one database
pass both times, no conformance namespace holds a row afterwards, and a row
seeded in the default namespace is still there.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

import oska.platform.runtime as platform_runtime
from oska.config import Settings
from oska.conformance import ScenarioFile, load_scenarios, render_text, run_scenario, run_suite
from oska.conformance.runner import CONFORMANCE_NAMESPACE, conformance_namespace
from oska.contracts.session import SessionState
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.platform import Platform
from oska.platform.conformance import passed, render_platform_text, run_platform
from oska.storage import DEFAULT_NAMESPACE, storage_namespace

POSTGRES_URL_ENV = "OSKA_TEST_POSTGRES_URL"
TABLES = ("oska_sessions", "oska_receipts", "oska_write_claims")
RUNS = 2


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    """The operational scenarios, which write, read back and claim."""
    return repo_root / "examples" / "ops" / "scenarios"


@pytest.fixture
def writing_scenario(scenario_dir: Path) -> ScenarioFile:
    """One scenario that writes a receipt."""
    return next(s for s in load_scenarios(scenario_dir) if s.name == "02_restart_staging")


class _Spy:
    """Builds engines with `build_engine`, keeping the namespace each was asked for."""

    def __init__(self) -> None:
        self.namespaces: list[str] = []

    def __call__(self, settings: Settings, **kwargs: Any) -> Engine:
        self.namespaces.append(str(kwargs["namespace"]))
        return build_engine(settings, **kwargs)


class _Foreign:
    """A store that reports a namespace the run did not create, and counts purges."""

    namespace = DEFAULT_NAMESPACE

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.purged = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def purge(self) -> int:
        self.purged += 1
        return 0


# Offline.


def test_every_scenario_run_gets_a_fresh_conformance_namespace(
    writing_scenario: ScenarioFile, example_settings: Settings, tmp_path: Path
) -> None:
    spy = _Spy()

    first = run_scenario(writing_scenario, example_settings, state_dir=tmp_path / "a", build=spy)
    second = run_scenario(writing_scenario, example_settings, state_dir=tmp_path / "b", build=spy)

    assert first.passed and second.passed, first.failures + second.failures
    assert len(spy.namespaces) == 2 and len(set(spy.namespaces)) == 2
    for namespace in spy.namespaces:
        assert namespace.startswith(f"{CONFORMANCE_NAMESPACE}/")
        assert namespace.endswith(f"/{writing_scenario.name}")


def test_a_suite_draws_one_run_identifier_and_a_namespace_per_scenario(
    scenario_dir: Path, example_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oska.conformance.runner as runner

    spy = _Spy()
    monkeypatch.setattr(runner, "build_engine", spy)

    report = run_suite(scenario_dir, example_settings)

    assert report.passed, render_text(report)
    runs = {namespace.split("/")[1] for namespace in spy.namespaces}
    assert len(runs) == 1
    (run_id,) = runs
    assert spy.namespaces == [
        conformance_namespace(run_id, f"0/{scenario.name}")
        for scenario in load_scenarios(scenario_dir)
    ]


def test_a_namespace_outside_conformance_is_refused(
    writing_scenario: ScenarioFile, example_settings: Settings, tmp_path: Path
) -> None:
    spy = _Spy()
    for namespace in (DEFAULT_NAMESPACE, storage_namespace("default", "operations")):
        with pytest.raises(ValueError, match="conformance/"):
            run_scenario(
                writing_scenario,
                example_settings,
                state_dir=tmp_path / "state",
                build=spy,
                namespace=namespace,
            )
    assert spy.namespaces == []


def test_a_store_under_another_namespace_is_never_purged(
    writing_scenario: ScenarioFile, example_settings: Settings, tmp_path: Path
) -> None:
    foreign: list[_Foreign] = []

    def elsewhere(settings: Settings, **kwargs: Any) -> Engine:
        engine = build_engine(settings, **kwargs)
        held = _Foreign(engine._ledger)  # noqa: SLF001
        engine._ledger = held  # type: ignore[assignment]  # noqa: SLF001
        foreign.append(held)
        return engine

    result = run_scenario(
        writing_scenario, example_settings, state_dir=tmp_path / "state", build=elsewhere
    )

    assert foreign and foreign[0].purged == 0
    assert not result.passed
    assert any(
        failure.startswith("cleanup:") and repr(DEFAULT_NAMESPACE) in failure
        for failure in result.failures
    ), result.failures


def test_the_platform_nests_each_pair_inside_the_run_namespace(
    example_platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = platform_runtime.build_engine

    def spy(settings: Settings, **kwargs: Any) -> Engine:
        seen.append(str(kwargs["namespace"]))
        return real(settings, **kwargs)

    monkeypatch.setattr(platform_runtime, "build_engine", spy)

    reports = run_platform(example_platform)

    assert passed(example_platform, reports), render_platform_text(example_platform, reports)
    assert seen
    for namespace in seen:
        assert namespace.startswith(f"{CONFORMANCE_NAMESPACE}/")
        pair = namespace.rsplit("/", 2)[-2:]
        assert "/".join(pair) in {
            storage_namespace("default", "operations"),
            storage_namespace("default", "access"),
            storage_namespace("default", "data"),
        }
    assert {n.rsplit("/", 1)[-1] for n in seen} == {"operations", "access", "data"}


# Against a live PostgreSQL.


def _postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not set, so the postgres backend is not exercised")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg is not installed; install the optional extra oska[postgres]")
    return url


@pytest.fixture
def postgres_url() -> str:
    return _postgres_url()


@pytest.fixture
def postgres_runtime(repo_root: Path, tmp_path: Path, postgres_url: str) -> Path:
    """The repository runtime configuration, with the record kept in the test database."""
    directory = tmp_path / "configs"
    directory.mkdir()
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document["storage"] = {
        **document.get("storage", {}),
        "backend": "postgres",
        "url_env": POSTGRES_URL_ENV,
    }
    path = directory / "runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    shutil.copy(repo_root / "configs" / "guardrails.json", directory / "guardrails.json")
    return path


@pytest.fixture
def seeded(postgres_url: str) -> Any:
    """A session and a receipt in the default namespace, removed again after the test."""
    from oska.storage.postgres_ledger import PostgresLedger
    from oska.storage.postgres_sessions import PostgresSessionStore

    session = f"seed-{uuid.uuid4().hex}"
    sessions = PostgresSessionStore(postgres_url)
    ledger = PostgresLedger(postgres_url)
    sessions.save(SessionState(session_id=session, language="it", turn_count=7))
    payload = {"thing": "seeded"}
    key = ledger.key("integration.sample", "thing.change", payload, ["thing"])
    receipt = ledger.claim(
        ledger.new_receipt(session, key, "integration.sample", "thing.change", payload)
    )
    yield session, receipt.receipt_id, sessions, ledger
    ledger.delete_session(session)
    sessions.delete(session)


def _rows(url: str, where: str, parameters: tuple = ()) -> dict[str, int]:
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(url) as connection:
        for table in TABLES:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", parameters
            ).fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def _conformance_rows(url: str) -> dict[str, int]:
    return _rows(url, "namespace LIKE %s", (f"{CONFORMANCE_NAMESPACE}/%",))


def _assert_seed_untouched(seeded: Any) -> None:
    session, receipt_id, sessions, ledger = seeded
    state = sessions.load(session)
    assert (state.language, state.turn_count) == ("it", 7)
    assert [r.receipt_id for r in ledger.for_session(session)] == [receipt_id]


def test_the_same_suite_passes_twice_over_one_database_and_leaves_nothing(
    repo_root: Path,
    scenario_dir: Path,
    postgres_runtime: Path,
    postgres_url: str,
    seeded: Any,
    tmp_path: Path,
) -> None:
    settings = Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=postgres_runtime,
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )
    default_before = _rows(postgres_url, "namespace = %s", (DEFAULT_NAMESPACE,))

    for _ in range(RUNS):
        report = run_suite(scenario_dir, settings)
        assert report.passed, render_text(report)
        assert _conformance_rows(postgres_url) == dict.fromkeys(TABLES, 0)

    _assert_seed_untouched(seeded)
    assert _rows(postgres_url, "namespace = %s", (DEFAULT_NAMESPACE,)) == default_before


def test_the_same_scenario_passes_twice_over_one_database(
    repo_root: Path,
    writing_scenario: ScenarioFile,
    postgres_runtime: Path,
    postgres_url: str,
    tmp_path: Path,
) -> None:
    settings = Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=postgres_runtime,
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )
    spy = _Spy()

    for position in range(RUNS):
        result = run_scenario(
            writing_scenario, settings, state_dir=tmp_path / str(position), build=spy
        )
        assert result.passed, result.failures

    for namespace in spy.namespaces:
        assert _rows(postgres_url, "namespace = %s", (namespace,)) == dict.fromkeys(TABLES, 0)


def test_the_platform_passes_twice_over_one_database_and_leaves_nothing(
    repo_root: Path,
    examples: Path,
    postgres_runtime: Path,
    postgres_url: str,
    seeded: Any,
    tmp_path: Path,
) -> None:
    settings = Settings(
        runtime_config=postgres_runtime,
        model_config_path=repo_root / "configs" / "model.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="none",
    )
    platform = Platform.from_file(examples / "platform.yaml", settings)

    for _ in range(RUNS):
        reports = run_platform(platform)
        assert passed(platform, reports), render_platform_text(platform, reports)
        assert _conformance_rows(postgres_url) == dict.fromkeys(TABLES, 0)

    _assert_seed_untouched(seeded)
