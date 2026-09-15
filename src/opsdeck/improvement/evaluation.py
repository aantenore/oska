"""A labeled set of requests, and what routing does with it.

The set is written by hand, in more than one language, in wording that is not a
copy of the declared triggers. Two of its cases are ambiguous on purpose and
expect a tie: a system that resolves a genuine ambiguity silently is wrong in a
way no accuracy number would show.

The report is an alarm, never a trigger. A drop in precision says that
something moved — a new procedure, a changed trigger, a different provider —
and it says it to a person. Nothing in this package reverts anything because a
number fell.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from opsdeck.contracts.common import RankedSkill
from opsdeck.index.discovery import Discovery

__all__ = [
    "DEFAULT_CASES",
    "TIE",
    "EvaluationReport",
    "Judgment",
    "RoutingCase",
    "RoutedCase",
    "decide",
    "judge",
    "load_cases",
    "run_evaluation",
]

DEFAULT_CASES = Path("examples/ops/evaluation/routing.json")
TIE = "tie"
NONE = "none"

Judgment = Literal["right", "wrong", "open"]


class RoutingCase(BaseModel):
    """One request and the procedure it should reach.

    `expected` is a skill identifier, or `tie` when the request is ambiguous and
    the right answer is to say so.
    """

    text: str
    expected: str
    language: str = "en"


class RoutedCase(BaseModel):
    """One case and what routing did with it."""

    text: str
    language: str
    expected: str
    actual: str = NONE
    tie: bool = False
    score: float = 0.0


class EvaluationReport(BaseModel):
    """How routing did over one labeled set.

    `precision` is how often an answer was right, over the cases that were
    answered at all: a case the system declined is an abstention, not a
    mistake. `decisive_rate` is how often it committed to a single procedure.
    `model_pick_rate` is how often it reported a tie and would hand the choice
    to a model.
    """

    cases: int = 0
    answered: int = 0
    precision: float = 0.0
    decisive_rate: float = 0.0
    model_pick_rate: float = 0.0
    per_language: dict[str, float] = Field(default_factory=dict)
    failures: list[RoutedCase] = Field(default_factory=list)


def load_cases(path: Path | None = None) -> list[RoutingCase]:
    """Read a labeled set: `{"cases": [{"text", "expected", "language"}]}`."""
    target = Path(path) if path is not None else DEFAULT_CASES
    document = json.loads(target.read_text(encoding="utf-8"))
    declared = document.get("cases", []) if isinstance(document, dict) else document
    return [RoutingCase.model_validate(entry) for entry in declared]


def decide(
    ranked: Sequence[RankedSkill],
    min_score: float,
    tie_margin: float,
) -> tuple[str | None, bool]:
    """What discovery would answer for this ranking at these thresholds.

    The rule is the one discovery applies: below the floor nothing is chosen,
    and a runner-up within the margin makes the top a tie rather than a choice.
    """
    if not ranked or ranked[0].score < min_score:
        return None, False
    tie = (
        len(ranked) > 1
        and ranked[1].score >= min_score
        and (ranked[0].score - ranked[1].score) < tie_margin
    )
    return ranked[0].skill_id, tie


def judge(expected: str, chosen: str | None, tie: bool) -> Judgment:
    """`right`, `wrong` or `open` for one answer.

    An answer is a commitment: one procedure, or the statement that two are
    equally close. Declining to answer is `open`, and counts against how often
    the system is useful rather than against how often it is correct.
    """
    if expected == TIE:
        if tie:
            return "right"
        return "wrong" if chosen is not None else "open"
    if tie or chosen is None:
        return "open"
    return "right" if chosen == expected else "wrong"


def run_evaluation(cases: Sequence[RoutingCase], discovery: Discovery) -> EvaluationReport:
    """Route every case and report what happened."""
    routed: list[tuple[RoutingCase, RoutedCase, Judgment]] = []
    for case in cases:
        result = discovery.select(case.text)
        score = result.ranked[0].score if result.ranked else 0.0
        verdict = judge(case.expected, result.chosen, result.tie)
        routed.append(
            (
                case,
                RoutedCase(
                    text=case.text,
                    language=case.language,
                    expected=case.expected,
                    actual=result.chosen or NONE,
                    tie=result.tie,
                    score=score,
                ),
                verdict,
            )
        )
    return _report(routed)


def _report(routed: Sequence[tuple[RoutingCase, RoutedCase, Judgment]]) -> EvaluationReport:
    total = len(routed)
    right = sum(1 for _, _, verdict in routed if verdict == "right")
    wrong = sum(1 for _, _, verdict in routed if verdict == "wrong")
    decisive = sum(1 for _, outcome, _ in routed if outcome.actual != NONE and not outcome.tie)
    ties = sum(1 for _, outcome, _ in routed if outcome.tie)
    per_language: dict[str, float] = {}
    for language in sorted({case.language for case, _, _ in routed}):
        subset = [entry for entry in routed if entry[0].language == language]
        per_language[language] = _share(
            sum(1 for _, _, verdict in subset if verdict == "right"),
            sum(1 for _, _, verdict in subset if verdict in ("right", "wrong")),
        )
    return EvaluationReport(
        cases=total,
        answered=right + wrong,
        precision=_share(right, right + wrong),
        decisive_rate=_share(decisive, total),
        model_pick_rate=_share(ties, total),
        per_language=per_language,
        failures=[outcome for _, outcome, verdict in routed if verdict != "right"],
    )


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0
