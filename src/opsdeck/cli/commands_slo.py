"""The service-level report from a terminal.

One command, and one rule it keeps: a number this deployment did not record is
printed as not computed, with the reason, and never as zero. The report is read
back from the audit trail and, where a deployment records them, from the traces
the trail itself names. Nothing is written and nothing is changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from opsdeck.cli.options import JsonOption, SkillsOption, fail, settings_for
from opsdeck.config import Settings, load_runtime_config, state_path
from opsdeck.observability.metrics import ServiceReport, service_report
from opsdeck.observability.report import render_json, render_text

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Report what this deployment can say about how it is serving.",
)

AuditOption = Annotated[
    Path | None,
    typer.Option("--audit", help="Audit trail to read. Defaults to the configured one."),
]
SinceOption = Annotated[
    int | None,
    typer.Option("--since", help="How many days back the window opens. Default: the whole trail."),
]


@app.callback()
def slo() -> None:
    """Keep `report` a named command rather than the whole group."""


@app.command("report")
def report(
    audit: AuditOption = None,
    since: SinceOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
) -> None:
    """Print the service-level numbers, and name the ones that could not be computed."""
    settings = settings_for(skills)
    path = audit or _audit_path(settings)
    if not Path(path).is_file():
        fail(f"no audit trail at {path}")
    try:
        measured = service_report(Path(path), since_days=since, trace_path=_trace_path(settings))
    except (OSError, ValueError) as exc:
        fail(f"the record could not be read: {exc}")
    typer.echo(render_json(measured) if as_json else _text(measured, Path(path)))


def _text(measured: ServiceReport, audit: Path) -> str:
    return "\n".join([f"audit trail {audit}", render_text(measured)])


def _audit_path(settings: Settings) -> Path:
    runtime = load_runtime_config(settings=settings)
    return state_path(Path(settings.state_dir), runtime.audit.path)


def _trace_path(settings: Settings) -> Path | None:
    """Where this deployment records traces, when it records them at all.

    A deployment with tracing switched off names no file here, and the trail is
    then asked instead: a run that did record traces says so in the audit event
    of every turn, so a report over an older window still finds them.
    """
    runtime = load_runtime_config(settings=settings)
    if not runtime.tracing.enabled:
        return None
    return state_path(Path(settings.state_dir), runtime.tracing.path)
