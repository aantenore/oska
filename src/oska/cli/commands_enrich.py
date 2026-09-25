"""Writing and reading the routing examples derived from the declared triggers.

Enrichment is publish-time work, so it is also work a person has to be able to
ask for: after a catalog is edited by hand, after a language is added to the
configuration, or on a schedule. `run` performs a pass over the catalog and
`show` reads back what one procedure is indexed under, which is how a routing
surprise is explained without reading any code.

The provider is resolved the way `commands_run` resolves it, the scripted one
included, so the command can be exercised offline against a recorded answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from oska.catalog.enrichment import (
    EnrichmentReport,
    enrich_catalog,
    examples_path,
    needs_enrichment,
    read_examples,
)
from oska.catalog.registry import SkillRegistry
from oska.cli.commands_run import Provider, ProviderOption, ScriptOption
from oska.cli.options import JsonOption, SkillsOption, fail, registry_for, settings_for
from oska.config import (
    EnrichmentConfig,
    ProviderName,
    Settings,
    load_model_config,
    load_runtime_config,
)
from oska.planner.provider import ModelProvider, ModelProviderError
from oska.planner.providers import build_provider
from oska.planner.scripted import ScriptedModelProvider

__all__ = ["app", "run", "show"]

LanguageOption = Annotated[
    list[str] | None,
    typer.Option("--language", help="Language to write examples for. Repeat for more than one."),
]
DryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Report what a pass would write, and write nothing."),
]

app = typer.Typer(
    add_completion=False,
    help="Write and read the routing examples derived from the declared triggers.",
)


@app.command()
def run(
    skills: SkillsOption = None,
    language: LanguageOption = None,
    provider: ProviderOption = None,
    script: ScriptOption = None,
    dry_run: DryRunOption = False,
    as_json: JsonOption = False,
) -> None:
    """Derive the routing examples of every procedure that is missing them."""
    settings = settings_for(skills)
    runtime = load_runtime_config(settings=settings)
    config = runtime.enrichment
    languages = [item for item in (language or runtime.discovery.languages) if item.strip()]
    if not languages:
        fail("no language was asked for and none is configured under discovery.languages")
    registry = registry_for(settings)
    if dry_run:
        _report(_planned(registry, config, languages), as_json)
        return
    report = enrich_catalog(registry, _provider(settings, provider, script), config, languages)
    _report(report, as_json)
    if report.failed:
        raise typer.Exit(code=1)


@app.command()
def show(
    skill_id: str,
    skills: SkillsOption = None,
    as_json: JsonOption = False,
) -> None:
    """Print what one procedure is indexed under: declared first, derived after."""
    settings = settings_for(skills)
    registry = registry_for(settings)
    skill = registry.get(skill_id)
    if skill is None:
        fail(f"unknown skill: {skill_id}")
    config = load_runtime_config(settings=settings).enrichment
    examples = read_examples(skill, config)
    if as_json:
        document = {
            "skill_id": skill.id,
            "declared": list(skill.frontmatter.triggers),
            "derived": examples,
            "path": str(examples_path(skill, config)),
        }
        typer.echo(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(skill.id)
    for trigger in skill.frontmatter.triggers:
        typer.echo(f"  declared  {trigger}")
    if not examples:
        typer.echo("  no derived phrases are in force for this procedure")
        return
    for name in sorted(examples):
        for phrase in examples[name]:
            typer.echo(f"  {name}  {phrase}")


def _planned(
    registry: SkillRegistry,
    config: EnrichmentConfig,
    languages: list[str],
) -> EnrichmentReport:
    """What a pass would do, without asking a provider for anything."""
    report = EnrichmentReport()
    for skill in registry.published():
        if not skill.frontmatter.triggers:
            continue
        if needs_enrichment(skill, config, languages):
            report.written.append(skill.id)
        else:
            report.skipped.append(skill.id)
    return report


def _report(report: EnrichmentReport, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return
    for skill_id in report.written:
        typer.echo(f"written  {skill_id}")
    for skill_id in report.skipped:
        typer.echo(f"skipped  {skill_id}")
    for skill_id in sorted(report.failed):
        typer.echo(f"failed   {skill_id}  {report.failed[skill_id]}")


def _provider(settings: Settings, chosen: Provider | None, script: Path | None) -> ModelProvider:
    """The provider the flags ask for, else the one the settings name.

    Enrichment is the one thing here that needs a model, so unlike a turn there
    is nothing sensible to degrade to: without a provider the command stops.
    """
    name: ProviderName = chosen.value if chosen is not None else (settings.provider or "none")
    if name == "none":
        fail("deriving examples needs a model provider, and the provider in force is none")
    if name == "scripted":
        if script is None:
            fail("the scripted provider needs --script pointing at a recorded script")
        try:
            return ScriptedModelProvider(script)
        except (OSError, ModelProviderError, json.JSONDecodeError) as exc:
            fail(f"the script could not be read: {exc}")
    try:
        model_config = load_model_config(settings=settings)
        built = build_provider(model_config.model_copy(update={"provider": name}))
    except (OSError, ValueError, ModelProviderError) as exc:
        fail(f"the model provider could not be built: {exc}")
    if built is None:
        fail("the model provider in force is none")
    return built
