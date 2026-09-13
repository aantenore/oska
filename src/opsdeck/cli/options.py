"""Options every command shares, and the way a command reports a failure.

`--skills` is read the same way everywhere: it replaces the catalog roots of
the settings, so a command that builds a registry and a command that builds a
whole runtime read the same catalog. Expected failures leave one line on
standard error and a non-zero status; nothing prints a traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.validator import CatalogError
from opsdeck.config import Settings, load_settings
from opsdeck.planner.provider import ModelProvider
from opsdeck.services import Services, build_services

__all__ = [
    "JsonOption",
    "SchemasOption",
    "SessionOption",
    "SkillsOption",
    "fail",
    "registry_for",
    "services_for",
    "settings_for",
]

SkillsOption = Annotated[
    list[Path] | None,
    typer.Option("--skills", help="Catalog root. Repeat for more than one."),
]
SchemasOption = Annotated[
    Path | None,
    typer.Option("--schemas", help="Directory holding the JSON schemas."),
]
SessionOption = Annotated[
    str,
    typer.Option("--session", help="Session the turns belong to."),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print the answer as JSON."),
]


def settings_for(skills: list[Path] | None = None) -> Settings:
    """Settings from the environment, with the catalog roots the caller named."""
    settings = load_settings()
    if skills:
        return settings.model_copy(update={"skills_roots": list(skills)})
    return settings


def registry_for(settings: Settings) -> SkillRegistry:
    """Registry over the catalog roots of these settings."""
    return SkillRegistry(settings.skills_roots)


def services_for(settings: Settings, *, provider: ModelProvider | None = None) -> Services:
    """The runtime these settings assemble; a catalog that does not hold up stops here."""
    try:
        return build_services(settings, provider=provider)
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")


def fail(message: str) -> NoReturn:
    """Report an expected failure on one line and stop with a non-zero status."""
    typer.echo(message, err=True)
    raise typer.Exit(code=1)
