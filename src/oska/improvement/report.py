"""What the job did, in one document and in two renderings.

The report is the whole point of level 1. A change nobody can see is not a
reversible change, it is a change that happens to be small. Every phrase kept,
every threshold moved, every gap counted and every draft written is named here,
next to the level of the ladder it belongs to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from oska.improvement.evaluation import EvaluationReport
from oska.improvement.gaps import Gap
from oska.improvement.tuning import Tuning

__all__ = [
    "REPORT_INDENT",
    "DraftedProposal",
    "ImprovementReport",
    "precision_dropped",
    "read_report",
    "render_json",
    "render_text",
    "write_report",
]

REPORT_INDENT = 2


class DraftedProposal(BaseModel):
    """A candidate procedure written for a gap, and left where it was written."""

    proposal_id: str
    skill_id: str
    state: str
    issues: int = 0


class ImprovementReport(BaseModel):
    """One run of the job, level by level."""

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    signals: int = 0
    grounded: int = 0
    judged: int = 0
    judgments: dict[str, int] = Field(default_factory=dict)
    learned: dict[str, int] = Field(default_factory=dict)
    preferences: dict[str, int] = Field(default_factory=dict)
    tuning: Tuning | None = None
    tuning_applied: bool = False
    overrides_path: str | None = None
    evaluation: EvaluationReport = Field(default_factory=EvaluationReport)
    previous_precision: float | None = None
    gaps: list[Gap] = Field(default_factory=list)
    proposals: list[DraftedProposal] = Field(default_factory=list)


def precision_dropped(report: ImprovementReport) -> bool:
    """True when this run answers a smaller share of cases rightly than the last one."""
    if report.previous_precision is None:
        return False
    return report.evaluation.precision < report.previous_precision


def write_report(path: Path, report: ImprovementReport) -> Path:
    """Write the report where the improvement configuration says it goes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_json(report) + "\n", encoding="utf-8")
    return target


def read_report(path: Path) -> ImprovementReport | None:
    """The report of the previous run, or nothing when there is none to read."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        return ImprovementReport.model_validate_json(target.read_text(encoding="utf-8"))
    except ValueError:
        return None


def render_json(report: ImprovementReport) -> str:
    """The report as JSON."""
    return json.dumps(
        report.model_dump(mode="json"), indent=REPORT_INDENT, ensure_ascii=False, sort_keys=True
    )


def render_text(report: ImprovementReport) -> str:
    """The report as lines a person reads, level by level."""
    lines = [
        f"improvement run at {report.at.isoformat()}",
        f"level 0  signals: {report.signals} read, {report.grounded} of them grounded",
        f"level 0  judgments: {report.judged} recorded ({_pairs(report.judgments)})",
        *_learned_lines(report),
        *_preference_lines(report),
        *_tuning_lines(report),
        *_evaluation_lines(report),
        *_gap_lines(report),
        *_proposal_lines(report),
    ]
    return "\n".join(lines)


def _learned_lines(report: ImprovementReport) -> list[str]:
    total = sum(report.learned.values())
    lines = [f"level 1  learned phrasings: {total} across {len(report.learned)} procedure(s)"]
    lines.extend(
        f"           {skill_id}: {count} phrase(s)"
        for skill_id, count in sorted(report.learned.items())
    )
    return lines


def _preference_lines(report: ImprovementReport) -> list[str]:
    total = sum(report.preferences.values())
    lines = [
        f"level 1  presentation preferences: {total} across "
        f"{len(report.preferences)} session(s), reply wording only"
    ]
    lines.extend(
        f"           {session_id}: {count} preference(s)"
        for session_id, count in sorted(report.preferences.items())
    )
    return lines


def _tuning_lines(report: ImprovementReport) -> list[str]:
    tuning = report.tuning
    if tuning is None:
        return ["level 1  tuning: not run"]
    state = "written to the overrides file" if report.tuning_applied else "held, not applied"
    lines = [
        f"level 1  tuning: {_pairs(tuning.before)} -> {_pairs(tuning.after)} ({state})",
        f"           decisive {tuning.decisive_rate}, precision {tuning.precision}",
    ]
    lines.extend(f"           refused: {reason}" for reason in tuning.refused)
    if report.overrides_path:
        lines.append(f"           overrides: {report.overrides_path}")
    return lines


def _evaluation_lines(report: ImprovementReport) -> list[str]:
    evaluation = report.evaluation
    lines = [
        f"level 1  evaluation: {evaluation.cases} case(s), "
        f"precision {evaluation.precision}, decisive {evaluation.decisive_rate}, "
        f"model picks {evaluation.model_pick_rate}",
        f"           per language: {_pairs(evaluation.per_language)}",
    ]
    if report.previous_precision is not None:
        movement = "down from" if precision_dropped(report) else "against"
        lines.append(f"           {movement} {report.previous_precision} on the previous run")
    lines.extend(
        f"           missed: {failure.text} -> {failure.actual} "
        f"(expected {failure.expected})"
        for failure in evaluation.failures
    )
    return lines


def _gap_lines(report: ImprovementReport) -> list[str]:
    lines = [f"level 2  gaps: {len(report.gaps)} cluster(s)"]
    for gap in report.gaps:
        lines.append(f"           {gap.size} request(s): {'; '.join(gap.sample_phrases)}")
        lines.append(
            f"             entities {', '.join(gap.suggested_entities) or 'none'}"
            f" / operations {', '.join(gap.suggested_operations) or 'none'}"
        )
    return lines


def _proposal_lines(report: ImprovementReport) -> list[str]:
    lines = [f"level 2  proposals: {len(report.proposals)}, all left where they were written"]
    lines.extend(
        f"           {proposal.proposal_id} {proposal.skill_id} "
        f"[{proposal.state}] {proposal.issues} issue(s)"
        for proposal in report.proposals
    )
    lines.append("level 3  nothing: a person moves a proposal, a policy or a connector")
    return lines


def _pairs(values: dict[str, float] | dict[str, int]) -> str:
    return ", ".join(f"{name}={value}" for name, value in sorted(values.items())) or "none"
