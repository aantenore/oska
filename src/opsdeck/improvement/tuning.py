"""Moving two thresholds, inside declared bounds, and never in the configuration.

Discovery decides with two numbers: the floor a candidate has to clear, and the
margin under which a runner-up makes the top a tie. Both are guesses when they
are first written, and a labelled set can say which pair of guesses is better.

Tuning is bounded for one reason. A search that is allowed anywhere will find
that a floor of zero and a margin of zero answer every case decisively, and it
will report a higher decisive rate for a system that has stopped declining. The
bounds are the statement of how far the numbers may drift from what a person
chose; a better value outside them is refused, and the refusal is reported
rather than silently dropped.

The result is written to an overrides file under the state directory. A caller
may apply it. Nothing here writes into the runtime configuration, because a
tuned number that overwrites an authored one leaves nothing to compare against.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from opsdeck.config import DiscoveryConfig
from opsdeck.contracts.common import RankedSkill
from opsdeck.contracts.skill import Skill
from opsdeck.improvement.evaluation import RoutingCase, decide, judge
from opsdeck.index.base import SkillIndex, TextSource

__all__ = [
    "GRID_STEPS",
    "OVERRIDES_FILENAME",
    "TUNABLE",
    "Tuning",
    "rank_cases",
    "tune",
    "write_overrides",
]

GRID_STEPS = 9
OVERRIDES_FILENAME = "discovery-overrides.json"
TUNABLE = ("min_score", "tie_margin")
WIDEST = (0.0, 1.0)


class Tuning(BaseModel):
    """What the search found, and what it refused to take.

    `before` and `after` hold the tunables by name. `refused` names every value
    that scored better than the one taken and lies outside the declared bounds,
    and every tunable the bounds do not cover.
    """

    before: dict[str, float] = Field(default_factory=dict)
    after: dict[str, float] = Field(default_factory=dict)
    decisive_rate: float = 0.0
    precision: float = 0.0
    refused: list[str] = Field(default_factory=list)

    @property
    def moved(self) -> bool:
        """True when the search found something other than what is in force."""
        return self.after != self.before


def rank_cases(
    cases: Sequence[RoutingCase],
    skills: Sequence[Skill],
    index: SkillIndex,
    limit: int,
    texts: TextSource | None = None,
) -> list[list[RankedSkill]]:
    """Rank every case once, so the search moves thresholds and not the index.

    `texts` is what the runtime indexes each procedure under, phrases derived
    and learned included; without it only the declarations are ranked, and the
    thresholds would be tuned against a catalog that is not the one in use.
    """
    index.build(list(skills), texts)
    return [index.rank(case.text, limit) for case in cases]


def tune(
    cases: Sequence[RoutingCase],
    ranked: Sequence[Sequence[RankedSkill]],
    discovery: DiscoveryConfig,
    bounds: Mapping[str, tuple[float, float]],
) -> Tuning:
    """Search the grid for the thresholds that answer most cases without a mistake."""
    before = {name: float(getattr(discovery, name)) for name in TUNABLE}
    refused: list[str] = [
        f"{name} is not bounded, so it was left at {before[name]}"
        for name in TUNABLE
        if name not in bounds
    ]
    inside = _best(cases, ranked, _candidates(before, bounds, widest=False))
    outside = _best(cases, ranked, _candidates(before, bounds, widest=True))
    if inside is None:
        return Tuning(
            before=before,
            after=dict(before),
            refused=[*refused, "no bounded thresholds answer the set without a mistake"],
        )
    values, right, decisive = inside
    if outside is not None and outside[1] > right:
        refused.extend(
            f"{name} {value} answers more cases but lies outside "
            f"{bounds[name][0]}..{bounds[name][1]}"
            for name, value in outside[0].items()
            if name in bounds and not _within(value, bounds[name])
        )
    total = len(cases) or 1
    return Tuning(
        before=before,
        after=values,
        decisive_rate=round(decisive / total, 4),
        precision=1.0 if right else 0.0,
        refused=refused,
    )


def write_overrides(state_dir: Path, tuning: Tuning, *, at: datetime | None = None) -> Path:
    """Write the thresholds a caller may apply. The runtime configuration is not touched."""
    path = Path(state_dir) / OVERRIDES_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "written_at": (at or datetime.now(UTC)).isoformat(),
        "discovery": dict(tuning.after),
        "replaces": dict(tuning.before),
        "precision": tuning.precision,
        "decisive_rate": tuning.decisive_rate,
        "refused": list(tuning.refused),
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _candidates(
    before: Mapping[str, float],
    bounds: Mapping[str, tuple[float, float]],
    *,
    widest: bool,
) -> list[dict[str, float]]:
    """Every pair of thresholds the search may look at."""
    per_name: dict[str, list[float]] = {}
    for name in TUNABLE:
        interval = bounds.get(name)
        if interval is None:
            per_name[name] = [before[name]]
        elif widest:
            per_name[name] = _grid(*WIDEST)
        else:
            per_name[name] = _grid(*interval)
            if _within(before[name], interval) and before[name] not in per_name[name]:
                per_name[name] = sorted([*per_name[name], before[name]])
    return [
        {"min_score": floor, "tie_margin": margin}
        for floor in per_name["min_score"]
        for margin in per_name["tie_margin"]
    ]


def _best(
    cases: Sequence[RoutingCase],
    ranked: Sequence[Sequence[RankedSkill]],
    candidates: Sequence[Mapping[str, float]],
) -> tuple[dict[str, float], int, int] | None:
    """The thresholds that answer most cases rightly and none wrongly."""
    best: tuple[dict[str, float], int, int] | None = None
    for values in candidates:
        right, wrong, decisive = _score(cases, ranked, values["min_score"], values["tie_margin"])
        if wrong:
            continue
        taken = (dict(values), right, decisive)
        if best is None or _rank(taken) > _rank(best):
            best = taken
    return best


def _rank(taken: tuple[dict[str, float], int, int]) -> tuple[int, int, float, float]:
    """Most right, then most decisive, then the least permissive thresholds."""
    values, right, decisive = taken
    return (right, decisive, values["min_score"], values["tie_margin"])


def _score(
    cases: Sequence[RoutingCase],
    ranked: Sequence[Sequence[RankedSkill]],
    min_score: float,
    tie_margin: float,
) -> tuple[int, int, int]:
    right = wrong = decisive = 0
    for case, candidates in zip(cases, ranked, strict=False):
        chosen, tie = decide(candidates, min_score, tie_margin)
        if chosen is not None and not tie:
            decisive += 1
        verdict = judge(case.expected, chosen, tie)
        right += verdict == "right"
        wrong += verdict == "wrong"
    return right, wrong, decisive


def _grid(low: float, high: float, steps: int = GRID_STEPS) -> list[float]:
    if steps < 2 or high <= low:
        return [round(low, 4)]
    width = (high - low) / (steps - 1)
    return [round(low + width * step, 4) for step in range(steps)]


def _within(value: float, interval: tuple[float, float]) -> bool:
    return interval[0] <= value <= interval[1]
