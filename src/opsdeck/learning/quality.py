"""Checks a proposed procedure must pass before anyone can publish it.

A proposal is text until it parses, validates against the catalog it names and
survives the lints below. `check` runs all three and reports what failed; it
changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from opsdeck.catalog.loader import SkillParseError, parse_skill_text
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.validator import validate
from opsdeck.contracts.proposal import QualityReport
from opsdeck.contracts.skill import MIN_TRIGGERS, Skill, SkillType

DESCRIPTION_MIN = 20
DESCRIPTION_MAX = 200

#: How many lints `_lints` applies. The score divides by one more than this, so
#: a proposal that fails every lint and the parse still scores above zero.
LINT_COUNT = 6

_SKILL_FILENAME = "skill.md"
_POLICY_FILENAME = "policy.json"


def check(
    markdown: str,
    registry: SkillRegistry,
    *,
    policy: dict | None = None,
    schema_dir: Path | None = None,
) -> QualityReport:
    """Parse, validate and lint a proposed skill file.

    `policy` is the policy document the frontmatter points at; without it a
    procedure that declares a policy file is reported as missing one.
    """
    with TemporaryDirectory() as workspace:
        directory = Path(workspace)
        if policy is not None:
            (directory / _POLICY_FILENAME).write_text(json.dumps(policy), encoding="utf-8")
        path = directory / _SKILL_FILENAME
        try:
            skill = parse_skill_text(markdown, path)
        except SkillParseError as exc:
            message = str(exc).replace(f"{path}: ", "")
            # Nothing else could run, so the file failed every check there is.
            return QualityReport(issues=[f"parse: {message}"], score=_score(LINT_COUNT + 1))
        issues = [str(issue) for issue in validate(skill, registry.skills(), schema_dir=schema_dir)]
        issues.extend(_lints(skill, registry))
    return QualityReport(issues=issues, score=_score(len(issues)))


def passed(report: QualityReport) -> bool:
    """True when nothing blocks publication."""
    return not report.issues


def _lints(skill: Skill, registry: SkillRegistry) -> list[str]:
    frontmatter = skill.frontmatter
    issues: list[str] = []
    length = len(frontmatter.description)
    if not DESCRIPTION_MIN <= length <= DESCRIPTION_MAX:
        issues.append(
            f"description: {length} characters, expected {DESCRIPTION_MIN} to {DESCRIPTION_MAX}"
        )
    triggers = _normalised(frontmatter.triggers)
    if len(set(triggers)) < MIN_TRIGGERS:
        issues.append(f"triggers: at least {MIN_TRIGGERS} distinct triggers are required")
    issues.extend(_write_issues(skill, registry))
    if not skill.sections.steps.strip():
        issues.append("steps: the section is empty")
    if not skill.sections.verification.strip():
        issues.append("verification: the section is empty")
    issues.extend(_reused_triggers(skill, registry, triggers))
    return issues


def _write_issues(skill: Skill, registry: SkillRegistry) -> list[str]:
    declared = {}
    for reference in skill.frontmatter.integrations:
        integration = registry.get(reference)
        if integration is None or integration.type is not SkillType.integration:
            continue
        for spec in integration.frontmatter.operations:
            declared[spec.name] = spec
    issues: list[str] = []
    for name in skill.frontmatter.capabilities.writes:
        spec = declared.get(name)
        if spec is None:
            issues.append(f"writes: {name} is not declared by a listed integration")
        elif not spec.write:
            issues.append(f"writes: {name} is not a write operation")
    return issues


def _reused_triggers(skill: Skill, registry: SkillRegistry, triggers: list[str]) -> list[str]:
    taken: dict[str, str] = {}
    for published in registry.published():
        if published.type is not SkillType.runbook or published.id == skill.id:
            continue
        for trigger in _normalised(published.frontmatter.triggers):
            taken.setdefault(trigger, published.id)
    issues: list[str] = []
    for trigger in triggers:
        owner = taken.get(trigger)
        if owner is not None:
            issues.append(f"triggers: {trigger!r} already belongs to {owner}")
    return issues


def _normalised(triggers: list[str]) -> list[str]:
    return [trigger.strip().lower() for trigger in triggers if trigger.strip()]


def _score(count: int) -> float:
    return max(0.0, min(1.0, 1.0 - count / (LINT_COUNT + 1)))
