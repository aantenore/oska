"""Checks a proposed procedure must pass before anyone can publish it.

A proposal is text until it parses, validates against the catalog it names and
survives the lints below. `check` runs all three and reports what failed; it
changes nothing.

The middle of those three is the catalog's own validator, unchanged and not
restated here, which is how a proposal is held to everything a published skill
is held to: the references it makes, the policy it declares, and the guardrails
it names, which have to be defined by the catalog this installation configures.

A fourth check runs when there is something to run it against. A skill that
carries an evaluation set beside it declares, in its author's own words, which
requests must reach it and which must not; a proposal that revises that skill
and cannot answer its own cases is refused. It needs a ranking to run, so a
caller that has none gets the three checks and not the fourth.

A skill with no evaluation set is not refused, and that is a deliberate choice
rather than an oversight. Making a set mandatory here would turn the gate into a
migration blocker: a catalog written before evaluation sets existed would have
every revision of every procedure refused until someone wrote cases for all of
them, which pushes authors to paste in cases that pass rather than cases that
matter. The gate refuses what is there and wrong, not what is absent. Requiring
a set belongs to the catalog validator, once enough of a catalog carries one.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from oska.catalog.evaluation import EvaluationSetError, Ranking, load_cases, run_cases
from oska.catalog.loader import SkillParseError, parse_skill_text
from oska.catalog.registry import SkillRegistry
from oska.catalog.validator import validate
from oska.contracts.proposal import QualityReport
from oska.contracts.skill import MIN_TRIGGERS, Skill, SkillType

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
    ranking: Ranking | None = None,
) -> QualityReport:
    """Parse, validate, lint a proposed skill file, and run the cases it must answer.

    `policy` is the policy document the frontmatter points at; without it a
    procedure that declares a policy file is reported as missing one. `ranking`
    is what the runtime ranks requests with; with one, the evaluation set of the
    skill under review is run and a failing case refuses the proposal.
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
        issues.extend(_evaluation_issues(skill, registry, ranking))
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
    triggers = _normalized(frontmatter.triggers)
    if len(set(triggers)) < MIN_TRIGGERS:
        issues.append(f"triggers: at least {MIN_TRIGGERS} distinct triggers are required")
    issues.extend(_write_issues(skill, registry))
    if not skill.sections.steps.strip():
        issues.append("steps: the section is empty")
    if not skill.sections.verification.strip():
        issues.append("verification: the section is empty")
    issues.extend(_reused_triggers(skill, registry, triggers))
    return issues


def _evaluation_issues(
    skill: Skill,
    registry: SkillRegistry,
    ranking: Ranking | None,
) -> list[str]:
    """The cases the skill under review declares, and the ones it no longer answers.

    The proposal is text in a temporary directory, so the set is read from the
    published skill of the same identifier: what is being asked is whether the
    revision still answers what its own author said it must.
    """
    if ranking is None:
        return []
    published = registry.get(skill.id)
    if published is None:
        return []
    try:
        cases = load_cases(published)
    except EvaluationSetError as exc:
        return [f"evaluation: {exc}"]
    if not cases:
        return []
    return [
        f"evaluation: expected {result.expects!r} of {result.text!r}, {result.reason}"
        for result in run_cases(cases, skill.id, ranking)
        if not result.passed
    ]


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
        for trigger in _normalized(published.frontmatter.triggers):
            taken.setdefault(trigger, published.id)
    issues: list[str] = []
    for trigger in triggers:
        owner = taken.get(trigger)
        if owner is not None:
            issues.append(f"triggers: {trigger!r} already belongs to {owner}")
    return issues


def _normalized(triggers: list[str]) -> list[str]:
    return [trigger.strip().lower() for trigger in triggers if trigger.strip()]


def _score(count: int) -> float:
    return max(0.0, min(1.0, 1.0 - count / (LINT_COUNT + 1)))
