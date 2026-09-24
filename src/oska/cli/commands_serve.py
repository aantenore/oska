"""Serving the HTTP application, and running a scenario directory against it.

The host and the port are configuration, not flags with defaults baked in: the
flags override what `runtime.json` says under `api`. `conform` runs the suite a
caller would run to decide whether this installation conforms, and leaves a
non-zero status when a scenario did not pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from oska.catalog.validator import CatalogError
from oska.cli.options import ConnectorsOption, JsonOption, SkillsOption, fail, settings_for
from oska.config import load_runtime_config
from oska.conformance import render_json, render_text, run_suite

__all__ = ["conform", "register", "serve"]

HostOption = Annotated[
    str | None,
    typer.Option("--host", help="Address to listen on. Defaults to the configured one."),
]
PortOption = Annotated[
    int | None,
    typer.Option("--port", help="Port to listen on. Defaults to the configured one."),
]
ScenariosOption = Annotated[
    Path | None,
    typer.Option("--scenarios", help="Directory of scenario files."),
]


def register(app: typer.Typer) -> None:
    """Add these commands to the application."""
    app.command()(serve)
    app.command()(conform)


def serve(host: HostOption = None, port: PortOption = None, skills: SkillsOption = None) -> None:
    """Serve the HTTP application."""
    settings = settings_for(skills)
    try:
        import uvicorn

        from oska.api.app import create_app
    except ImportError as exc:
        fail(f"serving needs the api extra: {exc}")
    configured = load_runtime_config(settings=settings).api
    try:
        application = create_app(settings)
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    uvicorn.run(
        application,
        host=host or configured.host,
        port=port if port is not None else configured.port,
    )


def conform(
    scenarios: ScenariosOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
    connectors: ConnectorsOption = None,
) -> None:
    """Run a scenario directory and report the conformance level it reaches."""
    settings = settings_for(skills, connectors)
    directory = Path(scenarios) if scenarios is not None else settings.scenarios_dir
    if not directory.is_dir():
        fail(f"no scenario directory at {directory}")
    try:
        report = run_suite(directory, settings)
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    except (OSError, ValueError) as exc:
        fail(f"the scenarios could not be read: {exc}")
    if as_json:
        typer.echo(json.dumps(render_json(report), indent=2))
    else:
        typer.echo(render_text(report))
    if not report.passed:
        raise typer.Exit(code=1)
