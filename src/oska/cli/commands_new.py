"""`oska new pack` and `oska new platform`: the kit's generators.

Not registered on the main application; see the kit lane's report for the
`cli/main.py` line that attaches `app` under the name `new`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from oska.cli.options import fail
from oska.kit.scaffold import (
    DEFAULT_ENTITY,
    DEFAULT_HOST,
    DEFAULT_SYSTEM,
    enclosing_platform,
    new_pack,
    new_platform,
)

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Generate a pack or a platform from the templates the kit ships.",
)

NameArgument = Annotated[str, typer.Argument(help="Name of the pack or platform to write.")]
IntoOption = Annotated[
    Path | None,
    typer.Option("--into", help="Directory to write into. Defaults to the current directory."),
]
EntityOption = Annotated[str, typer.Option("--entity", help="Singular name of the pack's entity.")]
SystemOption = Annotated[
    str, typer.Option("--system", help="Name of the system the pack's integration binds to.")
]
HostOption = Annotated[str, typer.Option("--host", help="Name the pack answers as.")]
PlatformOption = Annotated[
    Path | None,
    typer.Option(
        "--platform",
        help=(
            "A platform manifest to name the new pack in, adding it to the default domain. "
            "Taken as ./platform.yaml when the pack is written --into packs of the "
            "platform directory this runs in."
        ),
    ),
]
PackOption = Annotated[
    list[Path] | None,
    typer.Option("--pack", help="An existing pack to bundle into the platform. Repeat for more."),
]


@app.command("pack")
def pack(
    name: NameArgument,
    entity: EntityOption = DEFAULT_ENTITY,
    system: SystemOption = DEFAULT_SYSTEM,
    host: HostOption = DEFAULT_HOST,
    into: IntoOption = None,
    platform: PlatformOption = None,
) -> None:
    """Write a new pack: one entity, one integration, one read, one governed write."""
    manifest = platform or enclosing_platform(into)
    try:
        target = new_pack(
            name, entity=entity, system=system, host=host, into=into, platform=manifest
        )
    except (ValueError, FileExistsError) as exc:
        fail(str(exc))
    typer.echo(f"wrote {target}")
    if manifest is not None:
        typer.echo(f"named {name} in {manifest}, in its default domain")


@app.command("platform")
def platform(
    name: NameArgument,
    packs: PackOption = None,
    into: IntoOption = None,
) -> None:
    """Write a new platform repository around one or more existing packs."""
    try:
        target = new_platform(name, packs=packs or [], into=into)
    except (ValueError, FileExistsError) as exc:
        fail(str(exc))
    typer.echo(f"wrote {target}")
