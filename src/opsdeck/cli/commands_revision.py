"""Releasing a catalog from a terminal: build one, list what was built, choose one.

`build` is the only thing that writes a revision, and it refuses a catalog that
carries an error, so what reaches the store has already passed the same
validation the runtime would have run. `use` records which revision this
deployment serves. It writes one line into `CURRENT`, beside the revisions of
the tenant under the state directory, and that is the file the engine factory
reads when `catalog.revision` in the runtime configuration is empty: moving a
deployment from one release to another is an operation, not an edit of a file
under review. A runtime configuration that does name a revision wins over the
file, because a deployment that pinned itself deliberately should not be moved
by a command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from opsdeck.catalog.revision import Manifest, RevisionStore, build_revision
from opsdeck.catalog.validator import CatalogError
from opsdeck.cli.options import SkillsOption, fail, settings_for
from opsdeck.config import Settings, load_runtime_config, state_path

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build the catalog as an immutable revision, and choose which one is served.",
)

TenantOption = Annotated[
    str,
    typer.Option("--tenant", help="Catalog this belongs to. Defaults to the configured one."),
]


@app.command()
def build(skills: SkillsOption = None, tenant: TenantOption = "") -> None:
    """Validate the catalog roots and release them as a revision."""
    settings = settings_for(skills)
    store_root, chosen = _store_root(settings, tenant)
    try:
        manifest = build_revision(
            settings.skills_roots, store_root, tenant=chosen, settings=settings
        )
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    except OSError as exc:
        fail(f"the revision could not be written: {exc}")
    typer.echo(f"{manifest.revision}\t{chosen}\t{len(manifest.skills)} skill(s)")
    for line in manifest.report:
        typer.echo(f"  {line}")


@app.command("list")
def list_revisions(skills: SkillsOption = None, tenant: TenantOption = "") -> None:
    """Print every revision of a tenant, oldest build first."""
    settings = settings_for(skills)
    store_root, chosen = _store_root(settings, tenant)
    store = RevisionStore(store_root)
    current = store.current(chosen)
    for revision in store.list(chosen):
        manifest = store.manifest(chosen, revision)
        mark = "*" if revision == current else " "
        typer.echo(
            f"{mark} {revision}\t{manifest.created_at.isoformat()}\t"
            f"{len(manifest.skills)} skill(s)"
        )


@app.command()
def show(revision: str, skills: SkillsOption = None, tenant: TenantOption = "") -> None:
    """Print the manifest of one revision in full."""
    settings = settings_for(skills)
    store_root, chosen = _store_root(settings, tenant)
    typer.echo(json.dumps(_manifest(store_root, chosen, revision), indent=2, sort_keys=True))


@app.command()
def use(revision: str, skills: SkillsOption = None, tenant: TenantOption = "") -> None:
    """Record which revision this deployment serves from now on."""
    settings = settings_for(skills)
    store_root, chosen = _store_root(settings, tenant)
    store = RevisionStore(store_root)
    try:
        store.choose(chosen, revision)
    except KeyError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"the choice could not be written: {exc}")
    named = load_runtime_config(settings=settings).catalog.revision
    typer.echo(f"{chosen}\t{revision}")
    if named and named != revision:
        typer.echo(
            f"the runtime configuration names {named}, which is what will be served",
            err=True,
        )


def _store_root(settings: Settings, tenant: str) -> tuple[Path, str]:
    """Where the revisions of this deployment are kept, and whose they are."""
    try:
        catalog = load_runtime_config(settings=settings).catalog
    except (OSError, ValueError) as exc:
        fail(f"the configuration could not be read: {exc}")
    return (
        state_path(settings.state_dir, catalog.revisions_root),
        tenant.strip() or catalog.tenant,
    )


def _manifest(store_root: Path, tenant: str, revision: str) -> dict:
    """One manifest as a document, or a failure naming what the store does not hold."""
    try:
        manifest: Manifest = RevisionStore(store_root).manifest(tenant, revision)
    except KeyError as exc:
        fail(str(exc))
    return manifest.model_dump(mode="json")
