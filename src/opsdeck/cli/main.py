"""Command line surface.

The commands that only read the catalog live here; running turns, proposals,
serving and the conformance suite live in the sibling modules and are attached
below, so every command shares the same options and the same way of failing.
"""

import json
from enum import StrEnum
from typing import Annotated

import typer

from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.catalog.validator import validate_all
from opsdeck.cli import commands_run, commands_serve
from opsdeck.cli.commands_propose import propose
from opsdeck.cli.options import (
    SchemasOption,
    SkillsOption,
    fail,
    registry_for,
    settings_for,
)
from opsdeck.config import load_model_config, load_runtime_config
from opsdeck.ontology.export import to_json, to_mermaid
from opsdeck.ontology.graph import OntologyGraph
from opsdeck.ontology.queries import operations_on, related, vocabulary

__all__ = ["MASK", "OntologyFormat", "app"]

MASK = "***"
SECRET_PARTS = ("key", "token", "secret", "password")

app = typer.Typer(
    add_completion=False,
    help="Inspect and run declared operational procedures.",
)


class OntologyFormat(StrEnum):
    """How the ontology is printed."""

    json = "json"
    mermaid = "mermaid"
    summary = "summary"


FormatOption = Annotated[
    OntologyFormat,
    typer.Option("--format", help="How to print the ontology."),
]


@app.command()
def validate(skills: SkillsOption = None, schemas: SchemasOption = None) -> None:
    """Check every skill in the catalog and report what is wrong."""
    settings = settings_for(skills)
    registry = registry_for(settings)
    schema_dir = schemas or settings.schemas_dir
    issues = list(registry.parse_errors())
    issues.extend(validate_all(registry.skills(), schema_dir=schema_dir))
    loaded = len(registry.skills())
    for issue in issues:
        typer.echo(str(issue))
    if issues:
        typer.echo(f"{loaded} skills, {len(issues)} issues")
        raise typer.Exit(code=1)
    typer.echo(f"{loaded} skills, no issues")


@app.command()
def config(skills: SkillsOption = None) -> None:
    """Print the settings in force and the configuration files they name, as JSON."""
    settings = settings_for(skills)
    try:
        document = {
            "settings": settings.model_dump(mode="json"),
            "runtime": load_runtime_config(settings=settings).model_dump(mode="json"),
            "model": load_model_config(settings=settings).model_dump(mode="json"),
        }
    except (OSError, ValueError) as exc:
        fail(f"the configuration could not be read: {exc}")
    typer.echo(json.dumps(_masked(document), indent=2, sort_keys=True))


@app.command()
def capabilities(skills: SkillsOption = None) -> None:
    """Print the procedures on offer, in the order the runtime names them."""
    resolver = CapabilityResolver(registry_for(settings_for(skills)))
    processes = resolver.available_processes()
    typer.echo(resolver.declared_scope())
    for skill in processes:
        typer.echo(f"  {skill.id}\t{resolver.action_phrase(skill)}")


@app.command()
def ontology(
    skills: SkillsOption = None,
    format: FormatOption = OntologyFormat.summary,
) -> None:
    """Print the declared entities, what they relate to and what acts on them."""
    graph = OntologyGraph.from_registry(registry_for(settings_for(skills)))
    if format is OntologyFormat.json:
        typer.echo(json.dumps(to_json(graph), indent=2, sort_keys=True))
        return
    if format is OntologyFormat.mermaid:
        typer.echo(to_mermaid(graph))
        return
    for entity_id, entry in vocabulary(graph).items():
        typer.echo(entity_id)
        typer.echo(f"  identifier: {entry['identifier'] or 'none'}")
        typer.echo(f"  fields: {', '.join(entry['fields']) or 'none'}")
        state_field = entry["state_field"]
        if state_field:
            typer.echo(f"  {state_field}: {', '.join(entry['states']) or 'none'}")
        for relation, target, cardinality in related(graph, entity_id):
            typer.echo(f"  relation {relation}: {cardinality} {target}")
        for node in operations_on(graph, entity_id):
            kind = "write" if node.attributes.get("write") else "read"
            leaves = node.attributes.get("transitions") or {}
            left = "".join(f" -> {field}={value}" for field, value in sorted(leaves.items()))
            typer.echo(f"  {kind} {node.attributes['integration']} {node.label}{left}")


def _masked(value: object, name: str = "") -> object:
    """The same document with every secret replaced.

    A single value under a name that reads as a secret is replaced; a list is
    not, so a configured list of words keeps the name it was written under
    without its contents being read as credentials.
    """
    if isinstance(value, dict):
        return {key: _masked(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    if value is None or not _is_secret(name):
        return value
    return MASK


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in SECRET_PARTS)


commands_run.register(app)
commands_serve.register(app)
app.add_typer(propose, name="propose")


if __name__ == "__main__":
    app()
