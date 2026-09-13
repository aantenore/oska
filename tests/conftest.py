"""Fixtures shared by the test suite."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BODY = """## Purpose

Something worth doing, described in one paragraph.

## Steps

1. Read the state.
2. Report it.

## Verification

Nothing was changed, so there is nothing to read back.
"""


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Root of the checkout."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def schemas_dir() -> Path:
    """Directory holding the JSON schemas."""
    return REPO_ROOT / "schemas"


@pytest.fixture(scope="session")
def examples_root() -> Path:
    """Root of the example domain."""
    return REPO_ROOT / "examples" / "ops"


@pytest.fixture
def skills_root(examples_root: Path) -> Path:
    """Root of the example catalog."""
    return examples_root / "skills"


@pytest.fixture
def registry(skills_root: Path) -> SkillRegistry:
    """Registry over the example catalog."""
    return SkillRegistry([skills_root])


@pytest.fixture
def resolver(registry: SkillRegistry) -> CapabilityResolver:
    """Resolver over the example catalog."""
    return CapabilityResolver(registry)


@pytest.fixture
def example_settings(repo_root: Path, tmp_path: Path) -> Settings:
    """Settings over the example domain, with all state under a temporary directory."""
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )


@pytest.fixture
def catalog_copy(skills_root: Path, tmp_path: Path) -> Path:
    """A writable copy of the example catalog."""
    target = tmp_path / "skills"
    shutil.copytree(skills_root, target)
    return target


@pytest.fixture
def write_skill(tmp_path: Path) -> Callable[..., Path]:
    """Write a skill file into a temporary catalog and return its path."""

    def _write(
        frontmatter: dict,
        body: str = DEFAULT_BODY,
        *,
        root: Path | None = None,
        policy: dict | None = None,
    ) -> Path:
        base = root or tmp_path / "skills"
        name = frontmatter["id"].split(".", 1)[1]
        directory = base / frontmatter["type"] / name / f"v{frontmatter.get('version', 1)}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "skill.md"
        header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        path.write_text(f"---\n{header}---\n\n{body}", encoding="utf-8")
        if policy is not None:
            (directory / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def runbook_frontmatter() -> Callable[..., dict]:
    """Build a valid runbook frontmatter, overriding any field."""

    def _build(**overrides: object) -> dict:
        frontmatter: dict = {
            "id": "runbook.sample",
            "type": "runbook",
            "version": 1,
            "status": "published",
            "description": "A sample procedure used by the tests, long enough to pass the checks.",
            "triggers": ["first trigger", "second trigger", "third trigger"],
            "runtime": {"action": "do the sample thing", "action_on_selection": None, "order": 1},
        }
        frontmatter.update(overrides)
        return frontmatter

    return _build
