"""The defaults shipped with the package: how they are found, and that they stay one copy.

An installation outside this repository has no `configs/` or `schemas/` of its
own. The runtime configuration, the model configuration, the guardrail catalog,
the phrase catalog and the schema directory are then read from the package,
unless an environment variable names another file or the working directory
holds one.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from importlib import resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oska.cli.main import app as main_app
from oska.config import (
    ENV_PREFIX,
    PACKAGED_CONFIGS,
    PACKAGED_SCHEMAS,
    PACKAGED_SOURCE,
    GuardrailsConfig,
    ReplyConfig,
    Settings,
    configuration_sources,
    guardrail_catalog_path,
    load_runtime_config,
    phrases_path,
)
from oska.kit.check import check_pack
from oska.kit.scaffold import new_pack, new_platform

runner = CliRunner()

SHIPPED = {"runtime_config", "model_config", "guardrails_catalog", "phrases", "schemas_dir"}


@pytest.fixture
def bare_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with no `configs/` or `schemas/`, and no `OSKA_` variable set."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _write_json(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _copy_runtime(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((PACKAGED_CONFIGS / "runtime.json").read_bytes())
    return path


@pytest.mark.parametrize("directory", ["configs", "schemas"])
def test_the_repository_copy_is_byte_identical_to_the_packaged_one(
    repo_root: Path, directory: str
) -> None:
    packaged = PACKAGED_CONFIGS if directory == "configs" else PACKAGED_SCHEMAS
    local = repo_root / directory
    packaged_names = sorted(path.name for path in packaged.glob("*.json"))
    local_names = sorted(path.name for path in local.glob("*.json"))
    assert packaged_names == local_names
    for name in packaged_names:
        assert (packaged / name).read_bytes() == (local / name).read_bytes(), name


def test_every_schema_the_validator_reads_is_packaged() -> None:
    from oska.catalog.validator import POLICY_SCHEMA, PROPOSAL_SCHEMA, SKILL_SCHEMA

    for name in (SKILL_SCHEMA, POLICY_SCHEMA, PROPOSAL_SCHEMA):
        assert (PACKAGED_SCHEMAS / name).is_file(), name


def test_the_packaged_default_is_used_when_nothing_else_is_there(bare_directory: Path) -> None:
    settings = Settings.from_env({})
    assert settings.runtime_config == PACKAGED_CONFIGS / "runtime.json"
    assert settings.model_config_path == PACKAGED_CONFIGS / "model.json"
    assert settings.schemas_dir == PACKAGED_SCHEMAS
    runtime = load_runtime_config(settings=settings)
    assert guardrail_catalog_path(runtime.guardrails, settings) == (
        PACKAGED_CONFIGS / "guardrails.json"
    )
    assert configuration_sources(settings, runtime) == dict.fromkeys(SHIPPED, PACKAGED_SOURCE)


def test_a_file_in_the_working_directory_wins_over_the_packaged_default(
    bare_directory: Path,
) -> None:
    _copy_runtime(bare_directory / "configs" / "runtime.json")
    _write_json(bare_directory / "configs" / "model.json", {"provider": "none"})
    _write_json(bare_directory / "configs" / "guardrails.json", {"guardrails": {}})
    _write_json(bare_directory / "configs" / "phrases.json", {"it": {}})
    (bare_directory / "schemas").mkdir()

    settings = Settings.from_env({})
    runtime = load_runtime_config(settings=settings)
    assert configuration_sources(settings, runtime) == {
        "runtime_config": str(Path("configs/runtime.json")),
        "model_config": str(Path("configs/model.json")),
        "guardrails_catalog": str(Path("configs/guardrails.json")),
        "phrases": str(Path("configs/phrases.json")),
        "schemas_dir": "schemas",
    }


def test_an_environment_variable_wins_over_the_working_directory(
    bare_directory: Path, tmp_path: Path
) -> None:
    _copy_runtime(bare_directory / "configs" / "runtime.json")
    _write_json(bare_directory / "configs" / "model.json", {"provider": "none"})
    _write_json(bare_directory / "configs" / "guardrails.json", {"guardrails": {}})
    (bare_directory / "schemas").mkdir()
    elsewhere = tmp_path / "elsewhere"
    env = {
        f"{ENV_PREFIX}RUNTIME_CONFIG": str(_copy_runtime(elsewhere / "runtime.json")),
        f"{ENV_PREFIX}MODEL_CONFIG": str(_write_json(elsewhere / "model.json", {})),
        f"{ENV_PREFIX}GUARDRAILS_CATALOG": str(
            _write_json(elsewhere / "guardrails.json", {"guardrails": {}})
        ),
        f"{ENV_PREFIX}SCHEMAS_DIR": str(elsewhere),
    }

    settings = Settings.from_env(env)
    runtime = load_runtime_config(settings=settings)
    assert configuration_sources(settings, runtime) == {
        "runtime_config": str(elsewhere / "runtime.json"),
        "model_config": str(elsewhere / "model.json"),
        "guardrails_catalog": str(elsewhere / "guardrails.json"),
        "phrases": PACKAGED_SOURCE,
        "schemas_dir": str(elsewhere),
    }


def test_an_environment_variable_wins_over_the_packaged_default(
    bare_directory: Path, tmp_path: Path
) -> None:
    named = _write_json(tmp_path / "mine" / "guardrails.json", {"guardrails": {}})
    settings = Settings.from_env({f"{ENV_PREFIX}GUARDRAILS_CATALOG": str(named)})
    runtime = load_runtime_config(settings=settings)
    assert guardrail_catalog_path(runtime.guardrails, settings) == named


def test_a_catalog_beside_the_runtime_configuration_wins_over_the_packaged_one(
    bare_directory: Path, tmp_path: Path
) -> None:
    runtime_path = _copy_runtime(tmp_path / "platform" / "runtime.json")
    beside = _write_json(runtime_path.parent / "guardrails.json", {"guardrails": {}})
    settings = Settings(runtime_config=runtime_path)
    runtime = load_runtime_config(settings=settings)
    assert guardrail_catalog_path(runtime.guardrails, settings) == beside


def test_a_catalog_named_elsewhere_is_used_as_named_even_when_missing(
    bare_directory: Path,
) -> None:
    settings = Settings.from_env({})
    configured = GuardrailsConfig(catalog=Path("custom/catalog.json"))
    assert guardrail_catalog_path(configured, settings) == Path("custom/catalog.json")


def test_the_packaged_phrase_catalog_is_read_when_nothing_else_is_there(
    bare_directory: Path,
) -> None:
    settings = Settings.from_env({})
    runtime = load_runtime_config(settings=settings)
    assert phrases_path(runtime.reply, settings) == PACKAGED_CONFIGS / "phrases.json"


def test_a_phrase_catalog_beside_the_runtime_configuration_wins_over_the_packaged_one(
    bare_directory: Path, tmp_path: Path
) -> None:
    runtime_path = _copy_runtime(tmp_path / "platform" / "runtime.json")
    beside = _write_json(runtime_path.parent / "phrases.json", {"it": {}})
    settings = Settings(runtime_config=runtime_path)
    runtime = load_runtime_config(settings=settings)
    assert phrases_path(runtime.reply, settings) == beside


def test_a_phrase_catalog_named_elsewhere_is_used_as_named_even_when_missing(
    bare_directory: Path,
) -> None:
    settings = Settings.from_env({})
    configured = ReplyConfig(phrases=Path("custom/wording.json"))
    assert phrases_path(configured, settings) == Path("custom/wording.json")


def test_oska_config_names_the_packaged_default(bare_directory: Path) -> None:
    result = runner.invoke(main_app, ["config"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["sources"] == dict.fromkeys(SHIPPED, PACKAGED_SOURCE)


def test_oska_config_names_the_file_in_force(bare_directory: Path) -> None:
    _copy_runtime(bare_directory / "configs" / "runtime.json")
    result = runner.invoke(main_app, ["config"])
    assert result.exit_code == 0, result.output
    sources = json.loads(result.output)["sources"]
    assert sources["runtime_config"] == str(Path("configs/runtime.json"))
    assert sources["model_config"] == PACKAGED_SOURCE
    assert sources["guardrails_catalog"] == PACKAGED_SOURCE
    assert sources["phrases"] == PACKAGED_SOURCE
    assert sources["schemas_dir"] == PACKAGED_SOURCE


def test_new_platform_writes_the_packaged_defaults(tmp_path: Path) -> None:
    platform_dir = new_platform("sample_platform", into=tmp_path)
    written = sorted(path.name for path in (platform_dir / "configs").iterdir())
    shipped = sorted(path.name for path in PACKAGED_CONFIGS.glob("*.json"))
    assert written == shipped
    for name in shipped:
        assert (platform_dir / "configs" / name).read_bytes() == (
            PACKAGED_CONFIGS / name
        ).read_bytes(), name


def test_the_kit_carries_no_copy_of_the_configuration() -> None:
    assert not (resources.files("oska.templates") / "platform" / "configs").is_dir()


def test_a_generated_pack_checks_in_a_directory_with_no_configuration(
    bare_directory: Path,
) -> None:
    assert not (bare_directory / "configs").exists()
    assert not (bare_directory / "schemas").exists()
    target = new_pack("billing", entity="invoice", system="ledger", into=bare_directory)
    report = check_pack(target)
    assert report.ok, (report.errors, [str(issue) for issue in report.validation_issues])
    assert report.attained_level == report.claimed_level


def test_a_platform_generated_without_a_pack_says_so_rather_than_failing(
    bare_directory: Path,
) -> None:
    platform_dir = new_platform("sample_platform", into=bare_directory)
    result = runner.invoke(main_app, ["conform", "--platform", str(platform_dir / "platform.yaml")])
    assert result.exit_code != 0
    assert "serves no packs" in result.output


def test_the_version_is_the_installed_distribution_version() -> None:
    import oska

    assert oska.__version__ == importlib.metadata.version("oska")


def test_the_package_ships_its_typing_marker() -> None:
    assert (resources.files("oska") / "py.typed").is_file()
