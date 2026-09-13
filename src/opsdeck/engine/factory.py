"""Building an engine from configuration.

Everything the engine needs is assembled here, so the command line, the API and
the tests all start from the same wiring. With no model provider the engine
still routes by rule and by trigger, still answers what it can do, and declines
to plan; with one, discovery ranks by embedding and the model classifies,
plans, breaks ties and phrases the reply.
"""

from __future__ import annotations

from pathlib import Path

from opsdeck.audit.sink import AuditSink, JsonlSink
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.catalog.validator import CatalogError, validate_all
from opsdeck.config import Settings, load_runtime_config, state_path
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.engine.engine import Engine
from opsdeck.engine.session_store import SessionStore
from opsdeck.governance.approval import ApprovalGate
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.ledger import WriteLedger
from opsdeck.governance.policy_engine import PolicyEngine
from opsdeck.governance.verification import Verifier
from opsdeck.index.base import SkillIndex
from opsdeck.index.discovery import Discovery
from opsdeck.index.embedding import EmbeddingIndex
from opsdeck.index.keyword import KeywordIndex
from opsdeck.planner.planner import Planner
from opsdeck.planner.provider import ModelProvider
from opsdeck.router import FastPath, ModelRouter, RuleRouter, TurnRouter

__all__ = ["build_engine"]


def build_engine(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    audit_sink: AuditSink | None = None,
    connector_behaviours: dict[str, dict] | None = None,
) -> Engine:
    """Assemble an engine from the settings and the configuration files they name."""
    runtime = load_runtime_config(settings=settings)
    registry = SkillRegistry(settings.skills_roots)
    _refuse_a_broken_catalog(registry, settings.schemas_dir)
    resolver = CapabilityResolver(registry)

    def make_index() -> SkillIndex:
        return KeywordIndex() if provider is None else EmbeddingIndex(provider)

    discovery = Discovery(registry, make_index(), runtime.discovery)
    router = TurnRouter(
        RuleRouter(runtime.router.rules),
        FastPath(make_index, registry, runtime.router.fast_path),
        None if provider is None else ModelRouter(provider, resolver, runtime.router),
    )
    planner = None if provider is None else Planner(provider, registry, resolver)
    connectors = ConnectorRegistry.from_config(
        settings.connectors_config, behaviours=connector_behaviours
    )
    capabilities = CapabilityRegistry(registry)
    ledger = WriteLedger(state_path(settings.state_dir, runtime.ledger.path))
    audit = audit_sink or JsonlSink(state_path(settings.state_dir, runtime.audit.path))
    return Engine(
        registry=registry,
        resolver=resolver,
        discovery=discovery,
        router=router,
        planner=planner,
        connectors=connectors,
        capabilities=capabilities,
        policy=PolicyEngine(registry, ledger),
        approval=ApprovalGate(),
        ledger=ledger,
        verifier=Verifier(capabilities),
        session_store=SessionStore(state_path(settings.state_dir, runtime.sessions.path)),
        audit=audit,
        runtime_config=runtime,
    )


def _refuse_a_broken_catalog(registry: SkillRegistry, schema_dir: Path) -> None:
    """Nothing is served from a catalog whose files do not hold up."""
    issues = [*registry.parse_errors(), *validate_all(registry.skills(), schema_dir=schema_dir)]
    blocking = [issue for issue in issues if issue.severity == "error"]
    if blocking:
        raise CatalogError(blocking)
