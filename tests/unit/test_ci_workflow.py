"""What the continuous integration workflow runs, read from its own file.

OSKA-XC-30 says a conformant deployment's continuous integration runs, on
every change: static checks, the unit and scenario suites, catalog validation,
the catalog evaluation gate of OSKA-XC-29, and conformance, all offline and
without secrets. This reads `.github/workflows/ci.yml` as data and holds it to
that, rather than trusting the file's own step names.

A second job exercises the relational backend against a live PostgreSQL 16
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
POSTGRES_JOB = "postgres"
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
