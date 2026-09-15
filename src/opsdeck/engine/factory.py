"""Building an engine from configuration.

Everything the engine needs is assembled here, so the command line, the API and
the tests all start from the same wiring. With no model provider the engine
still routes by rule and by trigger, still answers what it can do, and declines
to plan; with one, discovery ranks by embedding and the model classifies,
plans, breaks ties and phrases the reply. Whatever the provider is, it is
wrapped in the turn's model budget before anything is given it, so every call
the runtime makes is counted in one place.

The guardrail catalog and the trace sink are built here too, from the runtime
configuration: guardrails on unless the configuration switches them off, traces
off unless it switches them on. The enrichment configuration and the learned
phrasings reach the two layers that rank the catalog, so a deployment that has
derived the phrases of its procedures, or has run the improvement job over its
traffic, routes by them without anything else being told. Both are read from
disk; neither is written here.
"""

from __future__ import annotations

from opsdeck.audit.sink import AuditSink, JsonlSink
from opsdeck.audit.trace import JsonlTraceSink, TraceSink
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.catalog.validator import CatalogError, validate_all
from opsdeck.config import (
    RuntimeConfig,
    Settings,
    guardrail_catalog_path,
    load_runtime_config,
    state_path,
)
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.engine.engine import BudgetedProvider, Engine, ModelBudget
from opsdeck.engine.session_store import SessionStore
from opsdeck.governance.approval import ApprovalGate
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.guardrails import GuardrailCatalog
from opsdeck.governance.ledger import WriteLedger
from opsdeck.governance.policy_engine import PolicyEngine
from opsdeck.governance.verification import Verifier
from opsdeck.index.base import LearnedPhrases, SkillIndex
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
    trace_sink: TraceSink | None = None,
    connector_behaviours: dict[str, dict] | None = None,
) -> Engine:
    """Assemble an engine from the settings and the configuration files they name."""
    runtime = load_runtime_config(settings=settings)
    budget = ModelBudget(runtime.turn.max_model_calls)
    if provider is not None:
        provider = BudgetedProvider(provider, budget)
    guardrails = _guardrail_catalog(runtime, settings)
    trace = trace_sink or _trace_sink(runtime, settings)
    registry = SkillRegistry(settings.skills_roots)
    _refuse_a_broken_catalog(registry, settings)
    resolver = CapabilityResolver(registry)

    def make_index() -> SkillIndex:
        return KeywordIndex() if provider is None else EmbeddingIndex(provider)

    learned = LearnedPhrases(
        state_path(settings.state_dir, runtime.improvement.learned_root)
    )
    discovery = Discovery(
        registry, make_index(), runtime.discovery, runtime.enrichment, learned
    )
    router = TurnRouter(
        RuleRouter(runtime.router.rules),
        FastPath(
            make_index, registry, runtime.router.fast_path, runtime.enrichment, learned
        ),
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
        guardrails=guardrails,
        trace=trace,
        budget=budget,
    )


def _guardrail_catalog(runtime: RuntimeConfig, settings: Settings) -> GuardrailCatalog:
    """The declared guardrails, or an empty catalog when they are switched off."""
    if not runtime.guardrails.enabled:
        return GuardrailCatalog.empty()
    return GuardrailCatalog.load(guardrail_catalog_path(runtime.guardrails, settings))


def _trace_sink(runtime: RuntimeConfig, settings: Settings) -> TraceSink | None:
    """The diagnostic trace sink, or nothing at all, which is the default."""
    if not runtime.tracing.enabled:
        return None
    return JsonlTraceSink(state_path(settings.state_dir, runtime.tracing.path))


def _refuse_a_broken_catalog(registry: SkillRegistry, settings: Settings) -> None:
    """Nothing is served from a catalog whose files do not hold up.

    The settings travel with the catalog, so what a skill may declare is read
    from the configuration of this deployment and not from wherever the process
    happens to have been started.
    """
    issues = [
        *registry.parse_errors(),
        *validate_all(registry.skills(), schema_dir=settings.schemas_dir, settings=settings),
    ]
    blocking = [issue for issue in issues if issue.severity == "error"]
    if blocking:
        raise CatalogError(blocking)
