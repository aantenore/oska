"""Principal and human input checks: which fields a procedure takes from the identity."""

from __future__ import annotations

from oska.catalog.checks.common import ValidationIssue, issue, operations_of, properties
from oska.contracts.skill import Skill, SkillFrontmatter, SkillType

__all__ = ["principal_input_issues"]


def principal_input_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    """Every field taken from the identity is a field some operation here accepts.

    A procedure that binds a field nothing accepts has bound nothing: the value
    would be resolved on every turn and written into no payload, and the write
    would go out carrying whatever the conversation supplied instead, which is
    the situation the declaration exists to end. The other check restates in the
    catalog what the contract already refuses, so a frontmatter that reached the
    catalog without passing through it is still caught here.
    """
    frontmatter = skill.frontmatter
    declared = frontmatter.principal_inputs
    if not declared:
        return []
    accepted = _accepted_fields(frontmatter, by_id)
    human = set(frontmatter.human_inputs)
    issues: list[ValidationIssue] = []
    for name in sorted(declared):
        if name in human:
            issues.append(
                issue(
                    skill,
                    "principal_input_shadows_human_input",
                    f"{name} is declared a human input as well, and a field is either "
                    "asked of the person or taken from the identity",
                )
            )
        if name not in accepted:
            issues.append(
                issue(
                    skill,
                    "principal_input_not_declared",
                    f"{name} is not a field any operation of this procedure accepts",
                )
            )
    return issues


def _accepted_fields(frontmatter: SkillFrontmatter, by_id: dict[str, Skill]) -> set[str]:
    """Every payload field the operations reachable from this procedure accept.

    A composite declares no integrations of its own, so the operations of its
    runbooks are what it may fill.
    """
    integrations = list(frontmatter.integrations)
    if frontmatter.type is SkillType.composite:
        for member in frontmatter.runbooks:
            held = by_id.get(member)
            if held is not None:
                integrations.extend(held.frontmatter.integrations)
    names: set[str] = set()
    for integration in integrations:
        for spec in operations_of(by_id.get(integration)):
            names |= properties(spec.input_schema)
    return names
