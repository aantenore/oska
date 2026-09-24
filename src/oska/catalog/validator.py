"""The catalog validator: every check a skill must pass, run in one call.

A skill is checked against the schema, against the rest of the catalog, and
against the configuration the deployment runs under. The checks themselves live
in `oska.catalog.checks`, one module per concern; this module runs them in a
fixed order and reads which guardrail identifiers the configuration defines.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from oska.catalog.checks.common import (
    POLICY_SCHEMA,
    PROPOSAL_SCHEMA,
    SKILL_SCHEMA,
    ValidationIssue,
    schema_validator,
)
from oska.catalog.checks.contract import (
    capability_issues,
    operation_issues,
    reference_issues,
    runtime_issues,
    schema_issues,
)
from oska.catalog.checks.governance import guardrail_issues, policy_issues
from oska.catalog.checks.inputs import principal_input_issues
from oska.catalog.checks.ontology import ontology_issues
from oska.catalog.checks.presentation import presentation_issues
from oska.config import Settings, guardrail_catalog_path, load_runtime_config, load_settings
from oska.contracts.skill import Skill

__all__ = [
    "GUARDRAIL_CATALOG_KEY",
    "POLICY_SCHEMA",
    "PROPOSAL_SCHEMA",
    "SKILL_SCHEMA",
    "CatalogError",
    "ValidationIssue",
    "declared_guardrail_ids",
    "schema_validator",
    "validate",
    "validate_all",
]

GUARDRAIL_CATALOG_KEY = "guardrails"


class CatalogError(Exception):
    """A catalog that carries an error, raised where it would otherwise be served.

    The issues travel on the exception, so a caller reports what was found
    instead of running the validator again to find out.
    """

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


def validate(
    skill: Skill,
    all_skills: Sequence[Skill],
    *,
    schema_dir: Path | None = None,
    settings: Settings | None = None,
    guardrail_ids: frozenset[str] | None = None,
) -> list[ValidationIssue]:
    """Check one skill against the schema and against the rest of the catalog.

    `guardrail_ids` is what the guardrail catalog defines; without it the
    configuration this installation runs under is read to find out.
    """
    directory = schema_dir or load_settings().schemas_dir
    declared = (
        declared_guardrail_ids(settings) if guardrail_ids is None else frozenset(guardrail_ids)
    )
    issues: list[ValidationIssue] = []
    issues.extend(schema_issues(skill, directory))
    by_id = _current_by_id(all_skills)
    issues.extend(reference_issues(skill, by_id))
    issues.extend(capability_issues(skill, by_id))
    issues.extend(principal_input_issues(skill, by_id))
    issues.extend(operation_issues(skill))
    issues.extend(ontology_issues(skill, by_id))
    issues.extend(presentation_issues(skill, by_id))
    issues.extend(runtime_issues(skill))
    issues.extend(policy_issues(skill, directory))
    issues.extend(guardrail_issues(skill, declared))
    return issues


def validate_all(
    all_skills: Sequence[Skill],
    *,
    schema_dir: Path | None = None,
    settings: Settings | None = None,
) -> list[ValidationIssue]:
    """Check every skill in the catalog."""
    declared = declared_guardrail_ids(settings)
    return [
        issue
        for skill in all_skills
        for issue in validate(
            skill, all_skills, schema_dir=schema_dir, guardrail_ids=declared
        )
    ]


def declared_guardrail_ids(settings: Settings | None = None) -> frozenset[str]:
    """The identifiers the configured guardrail catalog defines.

    A deployment that configures no catalog, or names one that is not on disk,
    defines none, and a skill that declares an identifier there is declaring
    something nothing can keep. Reading this file is all the catalog layer does
    with it; what each entry means, and what enforces it, belongs above.
    """
    try:
        resolved = settings or load_settings()
        path = guardrail_catalog_path(
            load_runtime_config(settings=resolved).guardrails, resolved
        )
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    entries = document.get(GUARDRAIL_CATALOG_KEY) if isinstance(document, dict) else None
    return frozenset(entries) if isinstance(entries, dict) else frozenset()


def _current_by_id(skills: Sequence[Skill]) -> dict[str, Skill]:
    current: dict[str, Skill] = {}
    for skill in skills:
        held = current.get(skill.id)
        if held is None or _rank(skill) > _rank(held):
            current[skill.id] = skill
    return current


def _rank(skill: Skill) -> tuple[int, int]:
    return (1 if skill.is_published else 0, skill.version)

