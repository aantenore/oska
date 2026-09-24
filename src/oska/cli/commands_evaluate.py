"""The evaluation sets from a terminal: run the catalog, or draft a skill's set.

`catalog` is meant to be run wherever the catalog changes, and leaves a non-zero
status on any failing case, so a procedure that starts taking another's requests
fails the build rather than a conversation. `suggest` writes nothing: it prints
cases drawn from the procedures that sit closest to the one named, for an author
to paste beside it and edit, because which of those requests a procedure must
really refuse is a judgment about the catalog.

Both rank with the keyword index and no model provider. That is what makes this
runnable offline and identical on every machine, and it is the ranking a
deployment without a provider actually serves; a deployment that ranks by
embedding should run the same sets against its own provider before publishing.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from oska.catalog.evaluation import (
    CatalogEvaluation,
    EvaluationSetError,
    cases_from_neighbors,
    evaluate_catalog,
    evaluation_path,
)
from oska.catalog.registry import Catalog, SkillRegistry
from oska.cli.options import JsonOption, SkillsOption, fail, settings_for
from oska.config import Settings, load_runtime_config, state_path
from oska.index.base import LearnedPhrases
from oska.index.discovery import Discovery
from oska.index.keyword import KeywordIndex

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run the evaluation set each skill carries, and draft the cases it is missing.",
)


@app.command("catalog")
def catalog(skills: SkillsOption = None, as_json: JsonOption = False) -> None:
    """Run every evaluation set in the catalog.

    Fails with a non-zero status when precision or recall falls below the
    configured floor, or, where the configuration requires every procedure to
    carry a set, when one does not.
    """
    settings = settings_for(skills)
    registry = SkillRegistry(settings.skills_roots)
    gate = load_runtime_config(settings=settings).evaluation
    try:
        report = evaluate_catalog(
            registry,
            _discovery(settings, registry),
            min_precision=gate.min_precision,
            min_recall=gate.min_recall,
            require_sets=gate.require_sets,
        )
    except EvaluationSetError as exc:
        fail(str(exc))
    typer.echo(report.model_dump_json(indent=2) if as_json else _text(report))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("suggest")
def suggest(skill_id: str, skills: SkillsOption = None, as_json: JsonOption = False) -> None:
    """Print cases drawn from the procedures closest to this one, to paste and edit."""
    settings = settings_for(skills)
    registry = SkillRegistry(settings.skills_roots)
    skill = registry.get(skill_id)
    if skill is None:
        fail(f"unknown skill: {skill_id}")
    proposed = cases_from_neighbors(skill, registry, _discovery(settings, registry))
    if not proposed:
        typer.echo(f"{skill_id}: no neighboring procedure declares a trigger to draw from")
        return
    document = {"cases": [case.model_dump(mode="json") for case in proposed]}
    if not as_json:
        typer.echo(f"{skill_id}: {len(proposed)} proposed case(s) for {evaluation_path(skill)}")
        typer.echo("Keep the ones this procedure must not answer; drop the rest.")
    typer.echo(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True))


def _discovery(settings: Settings, registry: Catalog) -> Discovery:
    """The ranking the runtime serves when no model provider is configured."""
    runtime = load_runtime_config(settings=settings)
    learned = LearnedPhrases(
        state_path(Path(settings.state_dir), runtime.improvement.learned_root)
    )
    return Discovery(registry, KeywordIndex(), runtime.discovery, runtime.enrichment, learned)


def _text(report: CatalogEvaluation) -> str:
    lines = [
        f"{report.cases} case(s) over {len(report.per_skill)} skill(s), "
        f"precision {report.precision}, recall {report.recall}"
    ]
    lines.extend(
        f"  {entry.skill_id}: {entry.passed}/{entry.cases}, "
        f"precision {entry.precision}, recall {entry.recall}"
        for entry in report.per_skill
    )
    lines.extend(
        f"  failed: {result.skill_id} expects {result.expects!r} of "
        f"{result.text!r} -- {result.reason}"
        for result in report.failures
    )
    if report.missing_sets:
        lines.append(f"no evaluation set: {', '.join(report.missing_sets)}")
    return "\n".join(lines)
