"""The evaluation set a skill carries, and what running the whole catalog says.

A ranking threshold that is a hand-set number is a guess, and a guess ages. What
does not age is a worked example: the words a person would really use, next to
the procedure they meant. So a skill may carry its own evaluation set beside it,
holding the requests that must reach it and the requests that must not, and the
set is run whenever the catalog changes. A new procedure that steals another's
requests then fails the build instead of a conversation.

The cases that matter most are the ones that nearly match. A set written from
the skill's own triggers alone proves very little: those words already reach it
by construction. `cases_from_neighbors` is the helper an author runs to get the
other half: it proposes cases drawn from the declared triggers of the skills
that sit closest, which is where a request is most likely to be taken from. It
proposes and prints; it writes nothing, because what belongs in an evaluation
set is a judgment its author has to make, and some of what it proposes will be
two procedures that genuinely overlap rather than a case to paste.

Nothing here scores anything. The ranking is the one the runtime uses, handed in
as a `Ranking`, so a case can only pass here if the same words would reach the
same procedure in a conversation. The protocol is declared rather than imported
so that reading a skill's evaluation set stays a catalog concern and does not
reach up into the layer that ranks it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from oska.catalog.registry import Catalog
from oska.contracts.common import DiscoveryResult
from oska.contracts.skill import Skill

__all__ = [
    "EVALUATION_FILENAME",
    "CaseResult",
    "CatalogEvaluation",
    "EvaluationCase",
    "EvaluationSetError",
    "Expectation",
    "Ranking",
    "SkillEvaluation",
    "cases_from_neighbors",
    "evaluate_catalog",
    "evaluation_path",
    "load_cases",
    "run_cases",
]

EVALUATION_FILENAME = "evaluation.json"
CASES_KEY = "cases"

Expectation = Literal["this", "not this"]
"""Whether the request must reach the skill the set belongs to, or must not."""

THIS: Expectation = "this"
NOT_THIS: Expectation = "not this"

_NEIGHBORS = 3
_CASES_PER_NEIGHBOR = 2


class EvaluationSetError(ValueError):
    """An evaluation set exists but could not be read."""


@runtime_checkable
class Ranking(Protocol):
    """Whatever ranks the catalog against a request, as the runtime ranks it."""

    def select(self, query: str) -> DiscoveryResult:
        """Rank the catalog against one request."""
        ...


class EvaluationCase(BaseModel):
    """One request, and whether it belongs to the skill the set sits beside.

    `language` is what the request is written in; it is stated rather than
    guessed, and an empty value means the author did not say.
    """

    text: str
    expects: Expectation = THIS
    language: str = ""


class CaseResult(BaseModel):
    """One case and what the ranking did with it."""

    skill_id: str
    text: str
    expects: Expectation
    language: str = ""
    top: str | None = None
    chosen: str | None = None
    score: float = 0.0
    passed: bool = False
    reason: str = ""


class SkillEvaluation(BaseModel):
    """How one skill's own evaluation set went."""

    skill_id: str
    cases: int = 0
    passed: int = 0
    results: list[CaseResult] = Field(default_factory=list)

    @property
    def failed(self) -> int:
        """How many of this skill's cases did not hold."""
        return self.cases - self.passed


class CatalogEvaluation(BaseModel):
    """Every evaluation set in the catalog, run against one ranking.

    `precision` is the share of cases that held, over every case that was run.
    A catalog in which no skill carries a set has no cases and a precision of
    zero, which `per_skill` being empty says plainly enough.
    """

    per_skill: list[SkillEvaluation] = Field(default_factory=list)
    failures: list[CaseResult] = Field(default_factory=list)
    precision: float = 0.0

    @property
    def cases(self) -> int:
        """How many cases were run in total."""
        return sum(entry.cases for entry in self.per_skill)

    @property
    def ok(self) -> bool:
        """True when every case that was run held."""
        return not self.failures


def evaluation_path(skill: Skill) -> Path:
    """Where the evaluation set of a skill lives: beside it, in its version directory."""
    return Path(skill.path).parent / EVALUATION_FILENAME


def load_cases(skill: Skill) -> list[EvaluationCase]:
    """The cases declared beside a skill, or nothing when it carries no set.

    A skill without a set is ordinary and reads as an empty list. A set that is
    there and cannot be read is an error and is raised, because a malformed set
    that quietly evaluated as empty would report a passing catalog while proving
    nothing about the skill it belongs to.
    """
    path = evaluation_path(skill)
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvaluationSetError(f"{path}: the evaluation set could not be read: {exc}") from exc
    declared = document.get(CASES_KEY) if isinstance(document, dict) else document
    if not isinstance(declared, list):
        raise EvaluationSetError(f"{path}: an evaluation set is a list under {CASES_KEY!r}")
    try:
        return [EvaluationCase.model_validate(entry) for entry in declared]
    except ValueError as exc:
        raise EvaluationSetError(f"{path}: {exc}") from exc


def run_cases(
    cases: Sequence[EvaluationCase],
    skill_id: str,
    ranking: Ranking,
) -> list[CaseResult]:
    """Rank every case and say whether it held.

    A case marked `this` holds when the skill it belongs to is the top of the
    ranking and the ranking is convincing enough for the runtime to choose it: a
    request that ranks first below the floor reaches nothing in a conversation,
    so it must not pass here either. A case marked `not this` holds when the
    skill is not the top of the ranking at all, which is stricter on purpose: a
    near-miss that is the closest match is one changed threshold away from
    stealing the request.
    """
    return [_run_case(case, skill_id, ranking) for case in cases]


def evaluate_catalog(registry: Catalog, discovery: Ranking) -> CatalogEvaluation:
    """Run the evaluation set of every published skill that carries one."""
    per_skill: list[SkillEvaluation] = []
    failures: list[CaseResult] = []
    passed = 0
    total = 0
    for skill in registry.published():
        cases = load_cases(skill)
        if not cases:
            continue
        results = run_cases(cases, skill.id, discovery)
        held = sum(1 for result in results if result.passed)
        per_skill.append(
            SkillEvaluation(
                skill_id=skill.id,
                cases=len(results),
                passed=held,
                results=results,
            )
        )
        failures.extend(result for result in results if not result.passed)
        passed += held
        total += len(results)
    return CatalogEvaluation(
        per_skill=per_skill,
        failures=failures,
        precision=round(passed / total, 4) if total else 0.0,
    )


def cases_from_neighbors(
    skill: Skill,
    registry: Catalog,
    ranking: Ranking | None = None,
    *,
    neighbors: int = _NEIGHBORS,
    per_neighbor: int = _CASES_PER_NEIGHBOR,
) -> list[EvaluationCase]:
    """Cases an author might add, drawn from the skills that sit closest to this one.

    Every proposal is a `not this`: it is a declared trigger of a neighboring
    procedure, which must keep reaching that procedure and not this one. With a
    ranking, the neighbors are the skills that come up when this skill's own
    triggers are ranked, which is the only order that says who could actually
    steal a request. Without one, the neighbors are the skills that touch the
    same entities, integrations and operations, which is the best a catalog can
    say about proximity on its own.

    Nothing is written. What belongs in an evaluation set is a judgment, and an
    author has to make it: some of what this proposes is a genuine near-miss and
    some of it is two procedures that really do overlap, which is a finding
    about the catalog rather than a case to paste.
    """
    others = [
        candidate
        for candidate in registry.published()
        if candidate.id != skill.id and candidate.frontmatter.triggers
    ]
    ordered = (
        _ranked_neighbors(skill, others, ranking)
        if ranking is not None
        else _structural_neighbors(skill, others)
    )
    proposed: list[EvaluationCase] = []
    seen: set[str] = set()
    for neighbor in ordered[: max(neighbors, 0)]:
        taken = 0
        for trigger in neighbor.frontmatter.triggers:
            text = " ".join(trigger.split())
            if not text or text.casefold() in seen or taken >= max(per_neighbor, 0):
                continue
            seen.add(text.casefold())
            proposed.append(EvaluationCase(text=text, expects=NOT_THIS))
            taken += 1
    return proposed


def _run_case(case: EvaluationCase, skill_id: str, ranking: Ranking) -> CaseResult:
    result = ranking.select(case.text)
    top = result.ranked[0].skill_id if result.ranked else None
    score = result.ranked[0].score if result.ranked else 0.0
    passed, reason = _verdict(case.expects, skill_id, top, result.chosen)
    return CaseResult(
        skill_id=skill_id,
        text=case.text,
        expects=case.expects,
        language=case.language,
        top=top,
        chosen=result.chosen,
        score=score,
        passed=passed,
        reason=reason,
    )


def _verdict(
    expects: Expectation,
    skill_id: str,
    top: str | None,
    chosen: str | None,
) -> tuple[bool, str]:
    if expects == NOT_THIS:
        if top == skill_id:
            return False, f"{skill_id} is the closest match and should not be"
        return True, ""
    if top != skill_id:
        return False, f"{top or 'nothing'} ranks above {skill_id}"
    if chosen != skill_id:
        return False, f"{skill_id} ranks first but scores below the floor, so nothing is chosen"
    return True, ""


def _ranked_neighbors(
    skill: Skill,
    others: Sequence[Skill],
    ranking: Ranking,
) -> list[Skill]:
    """The other skills that come up when this skill's own triggers are ranked."""
    best: dict[str, float] = {}
    for trigger in skill.frontmatter.triggers:
        for item in ranking.select(trigger).ranked:
            if item.skill_id == skill.id:
                continue
            best[item.skill_id] = max(best.get(item.skill_id, 0.0), item.score)
    by_id = {candidate.id: candidate for candidate in others}
    ordered = sorted(best, key=lambda skill_id: (-best[skill_id], skill_id))
    return [by_id[skill_id] for skill_id in ordered if skill_id in by_id]


def _structural_neighbors(skill: Skill, others: Sequence[Skill]) -> list[Skill]:
    """The other skills that touch the same things this one does."""
    mine = _touches(skill)
    scored = [
        (len(mine & _touches(candidate)), candidate)
        for candidate in others
        if mine & _touches(candidate)
    ]
    scored.sort(key=lambda entry: (-entry[0], entry[1].id))
    return [candidate for _, candidate in scored]


def _touches(skill: Skill) -> set[str]:
    frontmatter = skill.frontmatter
    return {
        *frontmatter.entities,
        *frontmatter.integrations,
        *frontmatter.capabilities.reads,
        *frontmatter.capabilities.writes,
    }
