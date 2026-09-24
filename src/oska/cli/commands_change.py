"""Change requests from a terminal: read them, apply one, reject one.

These commands are the other half of the split. The serving process composes a
change request and can do nothing else with it; a person reads it here and
decides. `change apply` is the one place in the system that writes into a
catalog root, and the root is an argument: there is no default, no configured
value it falls back to, and no way to run it without saying where the package
lands.

Applying is not serving. A catalog root that has gained a version is a source,
and the runtime answers from a built revision, so the command says what it
copied and then says what is still to be done.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from oska.cli.options import SkillsOption, fail, settings_for
from oska.learning.changes import (
    REQUEST_FILENAME,
    SUMMARY_FILENAME,
    ChangeRequestError,
    ChangeRequestStore,
    store_for,
)

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read the catalog changes the runtime proposed, and decide on them.",
)

ByOption = Annotated[str, typer.Option("--by", help="Who is deciding.")]
StateOption = Annotated[
    str | None,
    typer.Option("--state", help="Show only change requests in this state."),
]
IntoOption = Annotated[
    Path,
    typer.Option("--into", help="Catalog root the package is copied into."),
]
NoteOption = Annotated[str, typer.Option("--note", help="Why, recorded with the decision.")]

BUILD_REMINDER = (
    "Nothing serves this yet. A catalog root is a source; the runtime answers "
    "from a built revision, so build one with `oska revision build` and point "
    "the runtime at it."
)


@app.command("list")
def list_changes(state: StateOption = None, skills: SkillsOption = None) -> None:
    """Print every change request held, oldest first."""
    store = _store(skills)
    try:
        held = store.list(state)
    except ValueError as exc:
        fail(str(exc))
    for request in held:
        typer.echo(
            "  ".join(
                (
                    request.request_id,
                    request.state.value,
                    f"{request.skill_id} v{request.version}",
                    request.actor,
                )
            )
        )


@app.command("show")
def show(request_id: str, skills: SkillsOption = None) -> None:
    """Print one change request and the files its package holds."""
    store = _store(skills)
    try:
        request = store.get(request_id)
    except (KeyError, ChangeRequestError) as exc:
        fail(str(exc))
    typer.echo(json.dumps(request.model_dump(mode="json"), indent=2, ensure_ascii=False))
    directory = store.directory(request_id)
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name not in (REQUEST_FILENAME, SUMMARY_FILENAME):
            typer.echo(f"  {path.relative_to(directory)}")


@app.command("apply")
def apply(
    request_id: str,
    into: IntoOption,
    by: ByOption,
    skills: SkillsOption = None,
) -> None:
    """Copy the package of one change request into a catalog root."""
    store = _store(skills)
    try:
        copied = store.apply(request_id, into, by=by)
    except (KeyError, ChangeRequestError) as exc:
        fail(str(exc))
    typer.echo(f"applied {request_id} into {into}")
    for path in copied:
        typer.echo(f"  {path}")
    typer.echo(BUILD_REMINDER)


@app.command("reject")
def reject(
    request_id: str,
    by: ByOption,
    note: NoteOption = "",
    skills: SkillsOption = None,
) -> None:
    """Close one change request without copying anything."""
    store = _store(skills)
    try:
        request = store.reject(request_id, by=by, note=note)
    except (KeyError, ChangeRequestError) as exc:
        fail(str(exc))
    typer.echo(f"rejected {request.request_id} by {request.decided_by}")


def _store(skills: list[Path] | None) -> ChangeRequestStore:
    """The change request store these settings configure."""
    return store_for(settings_for(skills))
