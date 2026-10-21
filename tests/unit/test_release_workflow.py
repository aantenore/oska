"""What the release workflow does and does not do, read from its own file.

A release is cut by pushing a version tag, never by a push to a branch. The
distributions are built and checked in one job, published in another that
alone may mint the OpenID Connect token PyPI trusts, behind the `pypi`
environment, and only then is the GitHub release created from the matching
`CHANGELOG.md` section. No secret is stored in the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
BUILD_JOB = "build"
PUBLISH_JOB = "publish"
RELEASE_JOB = "github-release"
ENVIRONMENT = "pypi"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish@release/v1"


def _document() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict[str, Any]:
    return _document()["jobs"]


def _triggers() -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean True.
    document = _document()
    return document["on"] if "on" in document else document[True]


def _needs(job: str) -> list[str]:
    needs = _jobs()[job].get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def test_it_runs_only_on_version_tags() -> None:
    triggers = _triggers()

    assert set(triggers) == {"push"}
    assert triggers["push"] == {"tags": ["v*.*.*"]}
    assert "branches" not in triggers["push"]


def test_only_the_publish_job_may_mint_the_identity_token() -> None:
    document = _document()
    assert "id-token" not in (document.get("permissions") or {})

    for name, job in _jobs().items():
        permissions = job.get("permissions") or {}
        if name == PUBLISH_JOB:
            assert permissions.get("id-token") == "write"
        else:
            assert "id-token" not in permissions, name


def test_the_publish_job_waits_in_the_pypi_environment() -> None:
    job = _jobs()[PUBLISH_JOB]

    assert job["environment"] == ENVIRONMENT
    assert any(step.get("uses") == PUBLISH_ACTION for step in job["steps"])


def test_the_distributions_are_built_and_checked_before_they_are_published() -> None:
    assert BUILD_JOB in _needs(PUBLISH_JOB)
    runs = [" ".join(s["run"].split()) for s in _jobs()[BUILD_JOB]["steps"] if "run" in s]

    assert any("python -m build" in run for run in runs)
    assert any("twine check dist/*" in run for run in runs)
    assert any("oska conform --platform platform.yaml" in run for run in runs)


def test_the_release_is_created_after_the_publish_from_the_changelog_section() -> None:
    assert PUBLISH_JOB in _needs(RELEASE_JOB)
    build = _jobs()[BUILD_JOB]["steps"]
    extract = [s for s in build if "changelog section" in s.get("name", "").lower()]
    release = _jobs()[RELEASE_JOB]["steps"]
    create = " ".join(s["run"] for s in release if "run" in s)

    assert len(extract) == 1
    assert "CHANGELOG.md" in extract[0]["run"]
    assert "SystemExit" in extract[0]["run"]
    assert "gh release create" in create
    assert "--notes-file" in create
    assert _jobs()[RELEASE_JOB]["permissions"] == {"contents": "write"}


def test_the_tag_must_match_the_version_in_the_project_file() -> None:
    steps = _jobs()[BUILD_JOB]["steps"]

    runs = [s["run"] for s in steps if "run" in s]

    assert any("pyproject.toml" in run and "GITHUB_REF_NAME" in run for run in runs)


def test_it_stores_and_reads_no_secret() -> None:
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
