"""Governance and guardrail checks: the policy a procedure runs under and the promises it declares.

The guardrail rule reads the configuration the deployment runs under: the
identifiers a skill declares have to be defined by the guardrail catalog this
installation configures, so a promise nothing keeps is refused where a catalog
is published rather than in the middle of a turn.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from oska.catalog.checks.common import (
    POLICY_SCHEMA,
    ValidationIssue,
    issue,
    pointer,
    schema_validator,
)
from oska.contracts.policy import Policy
from oska.contracts.skill import Skill, SkillType

__all__ = ["guardrail_issues", "policy_issues"]


def policy_issues(skill: Skill, schema_dir: Path) -> list[ValidationIssue]:
    frontmatter = skill.frontmatter
    if frontmatter.type is SkillType.composite:
        if frontmatter.policy:
            return [
                issue(
                    skill,
                    "unexpected_policy",
                    "a composite runs each runbook under its own policy",
                )
            ]
        return []
    if frontmatter.policy is None:
        if frontmatter.capabilities.writes:
            return [issue(skill, "missing_policy", "a procedure that writes needs a policy")]
        return []
    path = (skill.path.parent / frontmatter.policy).resolve()
    if not path.is_file():
        return [issue(skill, "missing_policy_file", f"{frontmatter.policy} does not exist")]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [issue(skill, "invalid_policy_file", f"{frontmatter.policy}: {exc}")]
    issues = [
        issue(skill, "policy_schema", f"{pointer(error.absolute_path)}{error.message}")
        for error in schema_validator(schema_dir, POLICY_SCHEMA).iter_errors(payload)
    ]
    if issues:
        return issues
    try:
        policy = Policy.model_validate(payload)
    except ValidationError as exc:
        return [issue(skill, "invalid_policy", str(exc))]
    if policy.skill_id != skill.id:
        issues.append(
            issue(skill, "policy_skill_mismatch", f"policy names {policy.skill_id}")
        )
    declared = set(frontmatter.capabilities.writes)
    for rule in policy.rules:
        if rule.operation == "*":
            continue
        if rule.operation not in declared and rule.operation not in frontmatter.capabilities.reads:
            issues.append(
                issue(
                    skill,
                    "policy_undeclared_operation",
                    f"{rule.operation} is not a capability of this procedure",
                )
            )
    return issues


def guardrail_issues(skill: Skill, declared: frozenset[str]) -> list[ValidationIssue]:
    """Every guardrail a skill declares is one the configured catalog defines."""
    return [
        issue(
            skill,
            "guardrail_unknown_id",
            f"{name} is not declared by the guardrail catalog",
        )
        for name in skill.frontmatter.guardrails
        if name not in declared
    ]
