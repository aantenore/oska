"""The improvement job from a terminal: run it, read it, undo one part of it.

Four commands and one rule. `run` carries out the job and writes the report,
`show` prints the last report, `revert` deletes what was learned for one
procedure, and `evaluate` routes the labelled set without changing anything.

`revert` is a deletion and nothing else. The phrasings it removes are what an
index ranks by, and the index folds their files into the fingerprint it holds,
so the next ranking is built without them and is the ranking that was there
before they were learned. Nothing has to be restarted.

A run whose precision is below the precision of the previous report leaves a
non-zero status. That is an alarm for whoever is watching the job, not an
instruction to the job: nothing is undone, nothing is rolled back, and the
report says which cases were missed so a person can decide.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.cli.options import JsonOption, SkillsOption, fail, settings_for
from opsdeck.config import Settings, load_runtime_config, state_path
from opsdeck.improvement.evaluation import EvaluationReport, load_cases, run_evaluation
from opsdeck.improvement.job import run_job
from opsdeck.improvement.learned import revert as revert_learned
from opsdeck.improvement.report import (
    ImprovementReport,
    precision_dropped,
    read_report,
    render_json,
    render_text,
)
from opsdeck.index.base import LearnedPhrases
from opsdeck.index.discovery import Discovery
from opsdeck.index.keyword import KeywordIndex

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
    typer.Option("--cases", help="Labelled routing set to evaluate against."),
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


@app.command("revert")
def revert(skill_id: str, skills: SkillsOption = None) -> None:
    """Delete the phrasings learned for one procedure, and with them their effect."""
    settings = settings_for(skills)
    runtime = load_runtime_config(settings=settings)
    root = state_path(Path(settings.state_dir), runtime.improvement.learned_root)
    try:
        removed = revert_learned(root, skill_id)
    except (KeyError, OSError) as exc:
        fail(f"nothing could be reverted: {exc}")
    if not removed:
        fail(f"nothing was learned for {skill_id}")
    typer.echo(
        f"{skill_id}: the learned phrasings were deleted; "
        "the next ranking is built without them"
    )


@app.command("evaluate")
def evaluate(
    cases: CasesOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
) -> None:
    """Route the labelled set and report how it went. Nothing is changed."""
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
        fail(f"the labelled set could not be read: {exc}")
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
