"""What the continuous integration workflow runs, read from its own file.

OSKA-XC-30 says a conformant deployment's continuous integration runs, on
every change: static checks, the unit and scenario suites, catalog validation,
the catalog evaluation gate of OSKA-XC-29, and conformance, all offline and
without secrets. This reads `.github/workflows/ci.yml` as data and holds it to
that, rather than trusting the file's own step names.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
OPS_SKILLS = "examples/ops/skills"
ACCESS_SKILLS = "examples/access/skills"


def _runs() -> list[str]:
    """Every `run:` command of the build job, in the order the steps declare."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = document["jobs"]["build"]["steps"]
    return [step["run"] for step in steps if "run" in step]


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
