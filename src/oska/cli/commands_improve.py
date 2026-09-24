"""The improvement job from a terminal: run it, read it, undo one part of it.

Five commands and one rule. `run` carries out the job and writes the report,
`show` prints the last report, `feedback` records a judgment on one answer,
`revert` deletes what was learned for one procedure or what was recorded about
how one session likes its answers written, and `evaluate` routes the labeled
set without changing anything.

`feedback` is a recording and nothing else. It writes one line and no procedure
changes, no threshold moves and no wording is learned because of it. What comes
of it, if anything, comes of the next run of the job.

`revert` is a deletion and nothing else. The phrasings it removes are what an
index ranks by, and the index folds their files into the fingerprint it holds,
so the next ranking is built without them and is the ranking that was there
before they were learned. A preference it removes stops reaching the reply
writer of that session, which is the only place a preference ever reached.
Nothing has to be restarted.

A run whose precision is below the precision of the previous report leaves a
non-zero status. That is an alarm for whoever is watching the job, not an
instruction to the job: nothing is undone, nothing is rolled back, and the
report says which cases were missed so a person can decide.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

import typer

from oska.catalog.registry import SkillRegistry
from oska.cli.options import JsonOption, SkillsOption, fail, settings_for
from oska.config import Settings, load_runtime_config, state_path
from oska.improvement.evaluation import EvaluationReport, load_cases, run_evaluation
from oska.improvement.feedback import SCORES, FeedbackStore, Score, feedback_root
from oska.improvement.job import run_job
from oska.improvement.learned import revert as revert_learned
from oska.improvement.preferences import preferences_root, revert_preferences
from oska.improvement.report import (
    ImprovementReport,
    precision_dropped,
    read_report,
    render_json,
    render_text,
)
from oska.index.base import LearnedPhrases
from oska.index.discovery import Discovery
from oska.index.keyword import KeywordIndex

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read the record of what ran, learn what is reversible, and report the rest.",
)

ApplyTuningOption = Annotated[
    bool,
    typer.Option("--apply-tuning", help="Write the tuned thresholds to the overrides file."),
]
CasesOption = Annotated[
    Path | None,
    typer.Option("--cases", help="Labeled routing set to evaluate against."),
]
TurnOption = Annotated[
    int,
    typer.Option("--turn", help="Which answer of the session is being judged, counted from 1."),
]
ScoreOption = Annotated[
    str,
    typer.Option("--score", help="up or down."),
]
CommentOption = Annotated[
    str,
    typer.Option("--comment", help="Why, in the words of the person judging."),
]


@app.command("run")
def run(
    apply_tuning: ApplyTuningOption = False,
    as_json: JsonOption = False,
    cases: CasesOption = None,
    skills: SkillsOption = None,
) -> None:
    """Run the job: read the signals, learn, tune, evaluate, report the gaps."""
    settings = settings_for(skills)
    try:
        report = run_job(settings, apply_tuning=apply_tuning, cases_path=cases)
    except (OSError, ValueError) as exc:
        fail(f"the improvement job could not run: {exc}")
    _print(report, as_json)
    if precision_dropped(report):
        _stop_on_a_drop(report.evaluation.precision, report.previous_precision)


@app.command("show")
def show(as_json: JsonOption = False, skills: SkillsOption = None) -> None:
    """Print the report of the last run."""
    report = read_report(_report_path(settings_for(skills)))
    if report is None:
        fail("no improvement report has been written yet")
    _print(report, as_json)


@app.command("feedback")
def feedback(
    session_id: str,
    turn: TurnOption,
    score: ScoreOption,
    comment: CommentOption = "",
    skills: SkillsOption = None,
) -> None:
    """Record a judgment on one answer. Nothing else changes because of it."""
    if score not in SCORES:
        fail(f"a score is {' or '.join(SCORES)}, not {score!r}")
    settings = settings_for(skills)
    store = FeedbackStore(feedback_root(Path(settings.state_dir)))
    try:
        recorded = store.record(session_id, turn, cast("Score", score), comment)
    except (KeyError, OSError, ValueError) as exc:
        fail(f"the judgment could not be recorded: {exc}")
    typer.echo(
        f"{recorded.session_id} turn {recorded.turn}: {recorded.score} recorded; "
        "nothing else changed"
    )


@app.command("revert")
def revert(target: str, skills: SkillsOption = None) -> None:
    """Delete what was learned for a procedure, or recorded about how a session reads.

    One identifier, two things it may name, because both are files this job
    wrote and both are undone by deleting them.
    """
    settings = settings_for(skills)
    runtime = load_runtime_config(settings=settings)
    state_dir = Path(settings.state_dir)
    learned_root = state_path(state_dir, runtime.improvement.learned_root)
    try:
        removed_phrases = revert_learned(learned_root, target)
        removed_preferences = revert_preferences(preferences_root(state_dir), target)
    except (KeyError, OSError) as exc:
        fail(f"nothing could be reverted: {exc}")
    if not removed_phrases and not removed_preferences:
        fail(f"nothing was learned or recorded for {target}")
    if removed_phrases:
        typer.echo(
            f"{target}: the learned phrasings were deleted; "
            "the next ranking is built without them"
        )
    if removed_preferences:
        typer.echo(
            f"{target}: the recorded preferences were deleted; "
            "the next reply of that session is written without them"
        )


@app.command("evaluate")
def evaluate(
    cases: CasesOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
) -> None:
    """Route the labeled set and report how it went. Nothing is changed."""
    settings = settings_for(skills)
    runtime = load_runtime_config(settings=settings)
    registry = SkillRegistry(settings.skills_roots)
    learned = LearnedPhrases(
        state_path(Path(settings.state_dir), runtime.improvement.learned_root)
    )
    discovery = Discovery(
        registry, KeywordIndex(), runtime.discovery, runtime.enrichment, learned
    )
    try:
        report = run_evaluation(load_cases(cases), discovery)
    except (OSError, ValueError) as exc:
        fail(f"the labeled set could not be read: {exc}")
    typer.echo(report.model_dump_json(indent=2) if as_json else _evaluation_text(report))
    previous = read_report(_report_path(settings))
    if previous is not None and report.precision < previous.evaluation.precision:
        _stop_on_a_drop(report.precision, previous.evaluation.precision)


def _print(report: ImprovementReport, as_json: bool) -> None:
    typer.echo(render_json(report) if as_json else render_text(report))


def _evaluation_text(report: EvaluationReport) -> str:
    lines = [
        f"{report.cases} case(s), {report.answered} answered",
        f"precision {report.precision}, decisive {report.decisive_rate}, "
        f"model picks {report.model_pick_rate}",
    ]
    lines.extend(
        f"  {language}: {value}" for language, value in sorted(report.per_language.items())
    )
    lines.extend(
        f"  missed: {failure.text} -> {failure.actual} (expected {failure.expected})"
        for failure in report.failures
    )
    return "\n".join(lines)


def _report_path(settings: Settings) -> Path:
    runtime = load_runtime_config(settings=settings)
    return state_path(Path(settings.state_dir), runtime.improvement.report_path)


def _stop_on_a_drop(precision: float, previous: float | None) -> None:
    typer.echo(
        f"precision {precision} is below the {previous} of the previous report", err=True
    )
    raise typer.Exit(code=1)
