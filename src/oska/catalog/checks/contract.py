"""Schema and contract checks: the frontmatter, what it references, and what it may call.

A skill is checked against the bundled schema, its references against the rest
of the catalog, its declared capabilities against the operations its
integrations declare, every write for a read-back, and every procedure for the
wording and triggers the runtime needs.
"""

from __future__ import annotations

from pathlib import Path

from oska.catalog.checks.common import (
    SKILL_SCHEMA,
    ValidationIssue,
    issue,
    operations_of,
    pointer,
    properties,
    schema_validator,
)
from oska.catalog.loader import composite_capabilities
from oska.contracts.skill import Skill, SkillType

__all__ = [
    "capability_issues",
    "operation_issues",
    "reference_issues",
    "runtime_issues",
    "schema_issues",
]


def schema_issues(skill: Skill, schema_dir: Path) -> list[ValidationIssue]:
    payload = skill.frontmatter.model_dump(mode="json", exclude_none=True, by_alias=True)
    validator = schema_validator(schema_dir, SKILL_SCHEMA)
    return [
        issue(skill, "schema", f"{pointer(error.absolute_path)}{error.message}")
        for error in validator.iter_errors(payload)
    ]


def reference_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    checks = (
        (skill.frontmatter.entities, SkillType.entity),
        (skill.frontmatter.integrations, SkillType.integration),
        (skill.frontmatter.runbooks, SkillType.runbook),
    )
    for references, expected in checks:
        for reference in references:
            target = by_id.get(reference)
            if target is None:
                issues.append(
                    issue(skill, "unknown_reference", f"{reference} is not in the catalog")
                )
                continue
            if target.type is not expected:
                issues.append(
                    issue(
                        skill,
                        "wrong_reference_type",
                        f"{reference} is a {target.type.value}, expected {expected.value}",
                    )
                )
            elif not target.is_published:
                issues.append(
                    issue(skill, "unpublished_reference", f"{reference} is not published")
                )
    return issues


def capability_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    frontmatter = skill.frontmatter
    if frontmatter.type is SkillType.composite:
        expected = composite_capabilities(frontmatter.runbooks, by_id)
        declared = frontmatter.capabilities
        if sorted(declared.reads) != sorted(expected.reads) or sorted(declared.writes) != sorted(
            expected.writes
        ):
            return [
                issue(
                    skill,
                    "capability_mismatch",
                    "capabilities must equal the union of the capabilities of its runbooks",
                )
            ]
        return []
    declared_ops = {
        spec.name: spec
        for integration in frontmatter.integrations
        for spec in operations_of(by_id.get(integration))
    }
    issues: list[ValidationIssue] = []
    for name in frontmatter.capabilities.reads:
        spec = declared_ops.get(name)
        if spec is None:
            issues.append(
                issue(skill, "undeclared_capability", f"{name} is not declared by an integration")
            )
        elif spec.write:
            issues.append(issue(skill, "read_is_write", f"{name} is a write operation"))
    for name in frontmatter.capabilities.writes:
        spec = declared_ops.get(name)
        if spec is None:
            issues.append(
                issue(skill, "undeclared_capability", f"{name} is not declared by an integration")
            )
        elif not spec.write:
            issues.append(issue(skill, "write_is_read", f"{name} is not a write operation"))
    return issues


def operation_issues(skill: Skill) -> list[ValidationIssue]:
    if skill.type is not SkillType.integration:
        return []
    issues: list[ValidationIssue] = []
    reads = {spec.name for spec in skill.frontmatter.operations if not spec.write}
    for spec in skill.frontmatter.operations:
        if not spec.write:
            continue
        if spec.verify is None:
            issues.append(issue(skill, "missing_verify", f"{spec.name} declares no read-back"))
            continue
        if spec.verify.operation not in reads:
            issues.append(
                issue(
                    skill,
                    "unknown_verify_operation",
                    f"{spec.name}: {spec.verify.operation} is not a read operation here",
                )
            )
        for field in spec.idempotency_fields:
            if field not in properties(spec.input_schema):
                issues.append(
                    issue(
                        skill,
                        "unknown_idempotency_field",
                        f"{spec.name}: {field} is not in the input schema",
                    )
                )
    return issues


def runtime_issues(skill: Skill) -> list[ValidationIssue]:
    if skill.type not in (SkillType.runbook, SkillType.composite):
        return []
    issues: list[ValidationIssue] = []
    if skill.frontmatter.runtime is None:
        issues.append(issue(skill, "missing_runtime", "no runtime wording declared"))
    if len(skill.frontmatter.triggers) < 3:
        issues.append(issue(skill, "too_few_triggers", "at least three triggers are required"))
    if len(set(skill.frontmatter.triggers)) != len(skill.frontmatter.triggers):
        issues.append(issue(skill, "duplicate_triggers", "triggers must be distinct"))
    return issues
