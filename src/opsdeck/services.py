"""The runtime a channel talks to: one engine, one catalog, one proposal store.

Both channels, the HTTP application and the command line, work against the same
assembly, so what a turn does over HTTP and what it does in a terminal cannot
drift apart. Nothing here decides anything; it reads the settings, builds the
parts and hands them over.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opsdeck.config import Settings, load_runtime_config, load_settings, state_path
from opsdeck.engine.engine import Engine
from opsdeck.engine.factory import build_engine
from opsdeck.learning.proposals import ProposalStore
from opsdeck.planner.provider import ModelProvider

__all__ = ["PROPOSALS_DIRNAME", "Services", "build_services"]

PROPOSALS_DIRNAME = "proposals"


@dataclass(frozen=True)
class Services:
    """Everything a channel needs, assembled once."""

    engine: Engine
    proposals: ProposalStore
    settings: Settings
    publish_root: Path


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
