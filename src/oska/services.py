"""The runtime a channel talks to: one engine, one catalog, one proposal store.

Both channels, the HTTP application and the command line, work against the same
assembly, so what a turn does over HTTP and what it does in a terminal cannot
drift apart. Nothing here decides anything; it reads the settings, builds the
parts and hands them over.

Which catalog the engine is built over is settled one level down, in the engine
factory: a built revision of the configured tenant when the revision store holds
one, and the configured skill roots otherwise. Whichever it is, it is the same
catalog for every channel, and `Services.revision` names it so a channel can
report what it is serving without reaching into the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from oska.config import Settings, load_runtime_config, load_settings, state_path
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.learning.proposals import ProposalStore
from oska.planner.provider import ModelProvider

__all__ = ["PROPOSALS_DIRNAME", "Services", "build_services"]

PROPOSALS_DIRNAME = "proposals"


@dataclass(frozen=True)
class Services:
    """Everything a channel needs, assembled once."""

    engine: Engine
    proposals: ProposalStore
    settings: Settings
    publish_root: Path

    @property
    def revision(self) -> str:
        """The catalog revision in force, or nothing when a directory is served."""
        return self.engine.registry.newest_revision()


def build_services(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    proposal_store: ProposalStore | None = None,
    provider: ModelProvider | None = None,
) -> Services:
    """Assemble the runtime. Anything not passed in is built from the settings."""
    resolved = settings or load_settings()
    runtime = load_runtime_config(settings=resolved)
    state_dir = Path(resolved.state_dir)
    return Services(
        engine=engine or build_engine(resolved, provider=provider),
        proposals=proposal_store or ProposalStore(state_dir / PROPOSALS_DIRNAME),
        settings=resolved,
        publish_root=state_path(state_dir, runtime.learning.publish_root),
    )
