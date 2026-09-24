"""The second example domain, read through the same catalog code as the first.

Nothing here is about access management as such. The point of the domain is
that the runtime holds up with a vocabulary it has never seen: the catalog
validates under the same rules, and the sentences the runtime says about itself
are built from this catalog's own wording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oska.catalog.registry import SkillRegistry
from oska.catalog.resolver import CapabilityResolver
from oska.catalog.validator import validate_all

ACTIONS = (
    "check what access a person has",
    "add yourself to a group",
    "remove a person from a group",
)


@pytest.fixture
def access_root(repo_root: Path) -> Path:
    """Root of the access catalog."""
    return repo_root / "examples" / "access" / "skills"


@pytest.fixture
def access_registry(access_root: Path) -> SkillRegistry:
    """Registry over the access catalog."""
    return SkillRegistry([access_root])


@pytest.fixture
def access_resolver(access_registry: SkillRegistry) -> CapabilityResolver:
    """Resolver over the access catalog."""
    return CapabilityResolver(access_registry)


def test_the_catalog_validates_with_no_issues(
    access_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    assert list(access_registry.parse_errors()) == []
    issues = validate_all(access_registry.skills(), schema_dir=schemas_dir)
    assert issues == [], "\n".join(str(issue) for issue in issues)


def test_the_capability_sentence_is_this_catalogs_wording(
    access_resolver: CapabilityResolver,
) -> None:
    scope = access_resolver.declared_scope()
    assert scope.startswith("I can ")
    for action in ACTIONS:
        assert action in scope


def test_the_actions_on_an_account_are_the_ones_that_write(
    access_resolver: CapabilityResolver,
) -> None:
    ids = [skill.id for skill in access_resolver.actions_for_entity("entity.user")]
    assert ids == ["runbook.access_grant", "runbook.access_revoke"]
    assert "runbook.access_check" not in ids
