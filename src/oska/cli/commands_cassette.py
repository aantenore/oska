"""Turning a recorded session into something a scenario can replay.

A cassette holds more than a scenario reads: the requests that were sent and the
embeddings that came back. `to-script` keeps the part a scenario spends, so a
session recorded against a live endpoint seeds a scenario file without hand
editing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from oska.cli.options import fail
from oska.planner.provider import ModelProviderError
from oska.planner.recording import cassette_to_script

__all__ = ["cassette"]

cassette = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read a recorded session back as a scenario script.",
)

OutOption = Annotated[
    Path | None,
    typer.Option("--out", help="Write the script here instead of standard output."),
]
VolatileOption = Annotated[
    list[str] | None,
    typer.Option(
        "--volatile",
        help=(
            "Regex a recorded expectation is cut before the first match of; repeatable, "
            "replaces the default uuid patterns."
        ),
    ),
]


@cassette.command("to-script")
def to_script(path: Path, out: OutOption = None, volatile: VolatileOption = None) -> None:
    """Print the completions of a cassette as a scenario `provider_script`."""
    try:
        script = cassette_to_script(path, volatile=volatile)
    except ModelProviderError as exc:
        fail(str(exc))
    document = json.dumps(script, indent=2, ensure_ascii=False)
    if out is None:
        typer.echo(document)
        return
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{document}\n", encoding="utf-8")
    except OSError as exc:
        fail(f"the script could not be written: {exc}")
    typer.echo(f"{out}  {len(script)} step(s)")
