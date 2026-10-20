"""What the continuous integration workflow runs, read from its own file.

OSKA-XC-30 says a conformant deployment's continuous integration runs, on
every change: static checks, the unit and scenario suites, catalog validation,
the catalog evaluation gate of OSKA-XC-29, and conformance, all offline and
without secrets. This reads `.github/workflows/ci.yml` as data and holds it to
that, rather than trusting the file's own step names.

The build job runs on every Python version the package supports. A package job
builds the wheel, installs it into a fresh virtual environment and runs the
newcomer path from an empty directory outside the checkout.

A third job exercises the relational backend against a live PostgreSQL 16
service container: the storage contract suite and both reference conformance
suites, each twice in a row over the same database, which only passes when a
run leaves nothing behind.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
OPS_SKILLS = "examples/ops/skills"
ACCESS_SKILLS = "examples/access/skills"
PACKAGE_JOB = "package"
POSTGRES_JOB = "postgres"
PYTHON_VERSIONS = ["3.11", "3.12", "3.13"]
TYPE_CHECK_VERSION = "3.12"
NEWCOMER_PATH = (
    "oska --help",
    "oska new platform acme",
    "oska new pack billing --into packs",
    "oska pack check packs/billing",
    "oska conform --platform platform.yaml",
    "oska run --platform platform.yaml --session s1 --text",
)
POSTGRES_IMAGE = "postgres:16"
FIRST_PARTY_ACTIONS = ("actions/checkout@", "actions/setup-python@")
CONTRACT_SUITE = "tests/unit/test_storage_contract.py"
ISOLATION_SUITE = "tests/unit/test_conformance_postgres.py"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _runs(job: str = "build") -> list[str]:
    """Every `run:` command of a job, in the order the steps declare."""
    steps = _jobs()[job]["steps"]
    return [" ".join(step["run"].split()) for step in steps if "run" in step]


def test_the_workflow_lints() -> None:
    assert any(run.strip().startswith("ruff check") for run in _runs())


def test_the_workflow_type_checks_once_on_one_python_version() -> None:
    steps = _jobs()["build"]["steps"]
    checks = [step for step in steps if " ".join(step.get("run", "").split()) == "mypy"]
    assert len(checks) == 1
    assert checks[0]["if"] == f"matrix.python-version == '{TYPE_CHECK_VERSION}'"
    assert TYPE_CHECK_VERSION in PYTHON_VERSIONS


def test_the_workflow_runs_the_unit_and_scenario_suites() -> None:
    assert any(run.strip().startswith("pytest") for run in _runs())


def test_the_workflow_validates_the_catalog() -> None:
    assert any(run.strip() == "oska validate" for run in _runs())


def test_the_workflow_runs_the_catalog_evaluation_gate_on_both_reference_catalogs() -> None:
    runs = _runs()
    assert any(
        run.strip().startswith("oska evaluate catalog") and OPS_SKILLS in run for run in runs
    )
    assert any(
        run.strip().startswith("oska evaluate catalog") and ACCESS_SKILLS in run for run in runs
    )


def test_the_workflow_runs_conformance_on_both_reference_catalogs() -> None:
    runs = _runs()
    conform = [run for run in runs if run.strip().startswith("oska conform")]
    assert any(run.strip() == "oska conform" for run in conform), (
        "the default `oska conform` call is the operational catalog"
    )
    assert any(ACCESS_SKILLS in run for run in conform)


def test_the_workflow_runs_without_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text


def test_a_job_runs_the_relational_backend_against_a_live_postgres_service() -> None:
    job = _jobs()[POSTGRES_JOB]
    images = [service["image"] for service in job["services"].values()]
    assert images == [POSTGRES_IMAGE]
    used = [step["uses"] for step in job["steps"] if "uses" in step]
    assert used and all(action.startswith(FIRST_PARTY_ACTIONS) for action in used), used
    assert {"OSKA_TEST_POSTGRES_URL", "OSKA_DATABASE_URL"} <= set(job["env"])
    runs = _runs(POSTGRES_JOB)
    assert any(run.startswith("pip install") and "postgres" in run for run in runs)
    assert any('"postgres"' in run and "OSKA_RUNTIME_CONFIG" in run for run in runs), (
        "the conformance steps must run with a runtime configuration naming postgres"
    )


def test_the_postgres_job_runs_every_suite_twice_in_a_row() -> None:
    runs = _runs(POSTGRES_JOB)
    suites = [
        run
        for run in runs
        if run.startswith("pytest") and CONTRACT_SUITE in run and ISOLATION_SUITE in run
    ]
    assert len(suites) == 2
    conform = [run for run in runs if run.startswith("oska conform")]
    assert conform.count("oska conform") == 2
    assert sum(1 for run in conform if ACCESS_SKILLS in run) == 2
    assert sum(1 for run in conform if "--platform" in run) == 2
    selected = next(i for i, run in enumerate(runs) if "OSKA_RUNTIME_CONFIG" in run)
    assert all(runs.index(run) > selected for run in conform)
    assert "namespace LIKE 'conformance/%'" in runs[-1]


def test_the_build_job_runs_on_every_supported_python_version() -> None:
    job = _jobs()["build"]
    assert job["strategy"]["matrix"]["python-version"] == PYTHON_VERSIONS
    setups = [
        step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@")
    ]
    assert [step["with"]["python-version"] for step in setups] == ["${{ matrix.python-version }}"]


def test_the_package_job_builds_and_checks_the_distributions() -> None:
    runs = _runs(PACKAGE_JOB)
    built = next(i for i, run in enumerate(runs) if "python -m build" in run)
    checked = next(i for i, run in enumerate(runs) if "twine check dist/*" in run)
    assert "pip install build" in runs[built]
    assert "pip install twine" in runs[checked]
    assert built < checked


def test_the_package_job_installs_the_wheel_into_a_fresh_environment() -> None:
    runs = _runs(PACKAGE_JOB)
    installs = [run for run in runs if "dist/" in run and ".whl" in run and "[api]" in run]
    assert len(installs) == 1
    assert "python -m venv" in installs[0] and "$RUNNER_TEMP" in installs[0]
    assert "pip install -e" not in " ".join(runs), "the package job installs the built wheel"


def test_the_package_job_runs_the_newcomer_path_outside_the_checkout() -> None:
    steps = _jobs()[PACKAGE_JOB]["steps"]
    path = [step for step in steps if "oska new platform" in step.get("run", "")]
    assert len(path) == 1
    assert "working-directory" not in path[0], "the step moves itself out of the checkout"
    script = path[0]["run"]
    lines = [line.strip() for line in script.splitlines()]
    assert lines[0] == "set -euo pipefail"
    assert 'cd "$RUNNER_TEMP/workdir"' in lines
    moved = lines.index('cd "$RUNNER_TEMP/workdir"')
    positions = [
        next(i for i, line in enumerate(lines) if command in line) for command in NEWCOMER_PATH
    ]
    assert positions == sorted(positions) and positions[0] > moved
    assert 'grep -q "I can"' in script and 'test -n "$greeting"' in script
    assert "configs/" not in script, "the packaged defaults are what the path uses"
