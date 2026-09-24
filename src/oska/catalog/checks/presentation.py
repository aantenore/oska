"""Presentation and subject checks: how an entity is shown and what a procedure acts on."""

from __future__ import annotations

from oska.catalog.checks.common import ValidationIssue, issue, operations_of
from oska.contracts.skill import OperationSpec, Skill, SkillFrontmatter, SkillType
from oska.ontology.naming import WARNING_CODES, binding_issues, entity_issues, subject_issues

__all__ = ["presentation_issues"]


def presentation_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    """How an entity is shown, what a procedure acts on, and what it binds a view to.

    An entity shows and compares only fields it declares. A procedure's subject
    is an entity of the catalog, and one that joins the focus is one some
    operation here can name a record of. Every presentation binding names
    evidence of a known kind that this procedure can actually hold.
    """
    if skill.type is SkillType.entity:
        found = entity_issues(skill)
    elif skill.type in (SkillType.runbook, SkillType.composite):
        operations = _reachable_operations(skill.frontmatter, by_id)
        writes = set(skill.frontmatter.capabilities.writes)
        found = [
            *subject_issues(skill, by_id, operations),
            *binding_issues(
                skill, by_id, operations, [spec for spec in operations if spec.name in writes]
            ),
        ]
    else:
        return []
    return [
        issue(skill, code, message).model_copy(
            update={"severity": "warning" if code in WARNING_CODES else "error"}
        )
        for code, message in found
    ]


def _reachable_operations(
    frontmatter: SkillFrontmatter, by_id: dict[str, Skill]
) -> list[OperationSpec]:
    """Every operation the integrations of this procedure, or of its runbooks, declare."""
    integrations = list(frontmatter.integrations)
    for member in frontmatter.runbooks:
        held = by_id.get(member)
        if held is not None:
            integrations.extend(held.frontmatter.integrations)
    return [spec for name in integrations for spec in operations_of(by_id.get(name))]
