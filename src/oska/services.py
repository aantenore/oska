"""The runtime a channel talks to: one engine, one catalog, one review queue.

Both channels, the HTTP application and the command line, work against the same
assembly, so what a turn does over HTTP and what it does in a terminal cannot
drift apart. Nothing here decides anything; it reads the settings, builds the
parts and hands them over.

Which catalog the engine is built over is settled one level down, in the engine
factory: a built revision of the configured tenant when the revision store holds
one, and the configured skill roots otherwise. Whichever it is, it is the same
catalog for every channel, and `Services.revision` names it so a channel can
report what it is serving without reaching into the engine.

The proposals and the change requests are opened on the storage backend the
runtime configuration names, under the namespace the engine keeps its record
under: a directory of this process with the files backend, tables every
replica shares with a database.

A platform is the other assembly: several domains, each for several tenants,
declared by a manifest. `build_platform` reads the manifest and returns a
`Platform`, which hands out one `Services` per (tenant, domain), so a channel
serving a platform works against the same service set as one serving a single
catalog, chosen per request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from oska.config import RuntimeConfig, Settings, load_runtime_config, load_settings, state_path
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.learning.changes import ChangeRequestStore
from oska.learning.proposals import ProposalStore
from oska.planner.provider import ModelProvider
from oska.storage import (
    DEFAULT_NAMESPACE,
    POSTGRES,
    ChangeRequestRecordStore,
    ProposalRecordStore,
    database_url,
)

if TYPE_CHECKING:
    from oska.platform.runtime import Platform

__all__ = [
    "PROPOSALS_DIRNAME",
    "Services",
    "build_platform",
    "build_services",
    "open_changes",
    "open_proposals",
]

PROPOSALS_DIRNAME = "proposals"
"""The directory under the state directory the files backend keeps proposals in."""


@dataclass(frozen=True)
class Services:
    """Everything a channel needs, assembled once."""

    engine: Engine
    proposals: ProposalRecordStore
    settings: Settings
    publish_root: Path
    changes: ChangeRequestRecordStore

    @property
    def revision(self) -> str:
        """The catalog revision in force, or nothing when a directory is served."""
        return self.engine.registry.newest_revision()


def build_services(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    proposal_store: ProposalRecordStore | None = None,
    provider: ModelProvider | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> Services:
    """Assemble the runtime. Anything not passed in is built from the settings.

    `namespace` is the one the proposals and change requests are kept under;
    a platform passes the namespace of the (tenant, domain) it serves.
    """
    resolved = settings or load_settings()
    runtime = load_runtime_config(settings=resolved)
    state_dir = Path(resolved.state_dir)
    return Services(
        engine=engine or build_engine(resolved, provider=provider),
        proposals=proposal_store or open_proposals(runtime, resolved, namespace=namespace),
        settings=resolved,
        publish_root=state_path(state_dir, runtime.learning.publish_root),
        changes=open_changes(runtime, resolved, namespace=namespace),
    )


def open_proposals(
    runtime: RuntimeConfig,
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> ProposalRecordStore:
    """The proposal store this deployment is configured to keep.

    A directory under the state directory with the files backend; a table of
    the configured database, under `namespace`, with `storage.backend: postgres`.
    """
    storage = runtime.storage
    if storage.backend == POSTGRES:
        from oska.storage.postgres_proposals import PostgresProposalStore

        return PostgresProposalStore(
            database_url(storage, env),
            busy_timeout=storage.busy_timeout_seconds,
            namespace=namespace,
        )
    return ProposalStore(Path(settings.state_dir) / PROPOSALS_DIRNAME)


def open_changes(
    runtime: RuntimeConfig,
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> ChangeRequestRecordStore:
    """The change request store this deployment is configured to keep.

    The directory at `changes.root` with the files backend; tables of the
    configured database, packages included, under `namespace`, with
    `storage.backend: postgres`.
    """
    storage = runtime.storage
    if storage.backend == POSTGRES:
        from oska.storage.postgres_changes import PostgresChangeRequestStore

        return PostgresChangeRequestStore(
            database_url(storage, env),
            busy_timeout=storage.busy_timeout_seconds,
            namespace=namespace,
        )
    return ChangeRequestStore(state_path(Path(settings.state_dir), runtime.changes.root))


def build_platform(
    manifest: Path | str,
    settings: Settings | None = None,
    *,
    provider: ModelProvider | None = None,
) -> Platform:
    """Assemble a platform from its manifest. Each domain's runtime is built on first use."""
    from oska.platform.runtime import Platform

    return Platform.from_file(manifest, settings or load_settings(), provider=provider)
