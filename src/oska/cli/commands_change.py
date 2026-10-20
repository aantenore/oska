"""Change requests from a terminal: read them, apply one, reject one.

These commands are the other half of the split. The serving process composes a
change request and can do nothing else with it; a person reads it here and
decides. `change apply` is the one place in the system that writes into a
catalog root, and the root is an argument: there is no default, no configured
value it falls back to, and no way to run it without saying where the package
lands.

`change publish` puts a request in front of a reviewer as a branch of a git
repository the operator names: one commit authored by the actor who approved
it, in a worktree of its own, never pushed unless `--push` is given and never
forced. `--open-pr` names a command, split into arguments here and run without
a shell, that opens a pull request for the pushed branch; `{branch}` and
`{title}` are substituted into it. A request approved under a name the approving
call only stated is refused unless `--allow-unverified-actor` is given.

Applying is not serving. A catalog root that has gained a version is a source,
and the runtime answers from a built revision, so the command says what it
copied and then says what is still to be done.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Annotated

import typer

from oska.cli.options import SkillsOption, fail, settings_for
from oska.config import load_runtime_config
from oska.contracts.change import ChangeRequestError
from oska.learning.publishers import DEFAULT_REMOTE, CommandOpener, GitPublisher
from oska.services import open_changes
from oska.storage import ChangeRequestRecordStore

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
RepositoryOption = Annotated[
    Path,
    typer.Option("--repo", help="Git repository the branch is made in."),
]
CatalogPathOption = Annotated[
    str,
    typer.Option("--path", help="Catalog root inside the repository; its top by default."),
]
BaseOption = Annotated[str, typer.Option("--base", help="Commit the branch starts from.")]
PushOption = Annotated[
    bool,
    typer.Option("--push", help="Push the branch. Nothing is pushed without it."),
]
RemoteOption = Annotated[str, typer.Option("--remote", help="Remote the branch is pushed to.")]
OpenOption = Annotated[
    str,
    typer.Option(
        "--open-pr",
        help="Command that opens a pull request, with {branch} and {title} substituted.",
    ),
]
UnverifiedOption = Annotated[
    bool,
    typer.Option(
        "--allow-unverified-actor",
        help="Author the commit as an approver that was stated and not verified.",
    ),
]

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
    for name in sorted(store.files(request_id)):
        typer.echo(f"  {request.package}/{name}")


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


@app.command("publish")
def publish(
    request_id: str,
    repo: RepositoryOption,
    path: CatalogPathOption = "",
    base: BaseOption = "HEAD",
    push: PushOption = False,
    remote: RemoteOption = DEFAULT_REMOTE,
    open_pr: OpenOption = "",
    allow_unverified_actor: UnverifiedOption = False,
    skills: SkillsOption = None,
) -> None:
    """Publish one change request as a branch of a git repository, for review there."""
    store = _store(skills)
    try:
        opener = CommandOpener(shlex.split(open_pr)) if open_pr.strip() else None
        publisher = GitPublisher(
            repo,
            catalog_path=path,
            base=base,
            push=push,
            remote=remote,
            opener=opener,
            allow_unverified_actor=allow_unverified_actor,
        )
        request = publisher.publish(store.get(request_id), store)
    except (KeyError, ValueError, ChangeRequestError) as exc:
        fail(str(exc))
    typer.echo(f"{request.request_id}\t{request.branch}\t{request.commit}")
    if not request.pushed:
        typer.echo(f"not pushed; push {request.branch} when it is ready for review")
    if request.pull_request:
        typer.echo(request.pull_request)


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


def _store(skills: list[Path] | None) -> ChangeRequestRecordStore:
    """The change request store these settings configure, on the configured backend."""
    settings = settings_for(skills)
    return open_changes(load_runtime_config(settings=settings), settings)
