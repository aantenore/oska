"""Building an engine from configuration.

Everything the engine needs is assembled here, so the command line, the API and
the tests all start from the same wiring. With no model provider the engine
still routes by rule and by trigger, still answers what it can do, and declines
to plan; with one, discovery ranks by embedding and the model classifies,
plans, breaks ties and phrases the reply. Whatever the provider is, it is
wrapped in the turn's model budget before anything is given it, so every call
the runtime makes is counted in one place.

Which catalog is in force is decided here as well. When the revision store of
this deployment holds a revision for the configured tenant, and the
configuration names one or names none and one exists, the catalog is served
from that revision and nothing reads a mutable directory while it serves;
otherwise the configured skill roots are read directly, which is what a single
process on its own disk wants and what the example catalogs use. The ledger is
wrapped either way, so every receipt opened in a turn names the revision the
turn was decided under.

The guardrail catalog and the trace sink are built here too, from the runtime
configuration: guardrails on unless the configuration switches them off, traces
off unless it switches them on. The enrichment configuration and the learned
phrasings reach the two layers that rank the catalog, so a deployment that has
derived the phrases of its procedures, or has run the improvement job over its
traffic, routes by them without anything else being told. Both are read from
disk; neither is written here.
"""

from __future__ import annotations

from pathlib import Path

from oska.audit.sink import AuditSink, JsonlSink
from oska.audit.trace import JsonlTraceSink, TraceSink
from oska.catalog.registry import Catalog, ServedCatalog, SkillRegistry, active_revision
from oska.catalog.resolver import CapabilityResolver
from oska.catalog.revision import RevisionStore
from oska.catalog.validator import CatalogError, validate_all
from oska.config import (
    CatalogConfig,
    RuntimeConfig,
    Settings,
    guardrail_catalog_path,
    load_runtime_config,
    state_path,
)
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.receipt import WriteReceipt
from oska.engine.engine import BudgetedProvider, Engine, ModelBudget
from oska.engine.session_store import SessionStore
from oska.governance.approval import ApprovalGate
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.guardrails import GuardrailCatalog
from oska.governance.ledger import WriteLedger
from oska.governance.policy_engine import PolicyEngine
from oska.governance.verification import Verifier
from oska.improvement.preferences import preferences_root
from oska.index.base import LearnedPhrases, SkillIndex
from oska.index.discovery import Discovery
from oska.index.embedding import EmbeddingIndex
from oska.index.keyword import KeywordIndex
from oska.planner.planner import Planner
from oska.planner.provider import ModelProvider
from oska.router import FastPath, ModelRouter, RuleRouter, TurnRouter

__all__ = ["build_engine", "revision_ledger"]


def build_engine(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    audit_sink: AuditSink | None = None,
    trace_sink: TraceSink | None = None,
    connector_behaviors: dict[str, dict] | None = None,
) -> Engine:
    """Assemble an engine from the settings and the configuration files they name."""
    runtime = load_runtime_config(settings=settings)
    budget = ModelBudget(runtime.turn.max_model_calls)
    if provider is not None:
        provider = BudgetedProvider(provider, budget)
    guardrails = _guardrail_catalog(runtime, settings)
    trace = trace_sink or _trace_sink(runtime, settings)
    registry = _catalog(runtime, settings)
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
        settings.connectors_config,
        behaviors=connector_behaviors,
        resilience=runtime.resilience,
    )
    capabilities = CapabilityRegistry(registry)
    ledger = revision_ledger(
        state_path(settings.state_dir, runtime.ledger.path),
        busy_timeout=runtime.storage.busy_timeout_seconds,
    )
    audit = audit_sink or JsonlSink(state_path(settings.state_dir, runtime.audit.path))
    preferences = preferences_root(settings.state_dir)
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
        preferences_root=preferences,
        guardrails=guardrails,
        trace=trace,
        budget=budget,
    )


def revision_ledger(path: Path, *, busy_timeout: float | None = None) -> WriteLedger:
    """A write ledger that names the revision in force on every receipt it opens.

    A receipt answers what was done; under a released catalog it also has to
    answer which catalog said it could be done, because the procedure that
    declared the operation is an artifact with a name and that name is the only
    way back to the text a person approved. That is one field on the way out and
    nothing else, so it is added to a ledger here rather than made a second kind
    of ledger: everything a receipt is, and everything the store does with it,
    stays where it is written.
    """
    ledger = WriteLedger(path, busy_timeout=busy_timeout)
    opened = ledger.new_receipt

    def new_receipt(
        session_id: str,
        key: str,
        integration_id: str,
        operation: str,
        payload: dict,
    ) -> WriteReceipt:
        receipt = opened(session_id, key, integration_id, operation, payload)
        receipt.revision = active_revision() or None
        return receipt

    ledger.new_receipt = new_receipt
    return ledger


def _catalog(runtime: RuntimeConfig, settings: Settings) -> Catalog:
    """The catalog this deployment serves: a built revision, or the skill roots.

    A revision wins whenever the store holds one that answers the configuration:
    the revision it names, or the newest when it names none. A configuration
    that names a revision the store does not hold is not silently ignored; it
    falls back to the roots, and the catalog check below is what then decides
    whether anything is served at all.
    """
    catalog: CatalogConfig = runtime.catalog
    store = RevisionStore(state_path(settings.state_dir, catalog.revisions_root))
    if catalog.revision:
        if store.get(catalog.tenant, catalog.revision) is not None:
            return ServedCatalog(store, catalog.tenant, catalog.revision)
        return SkillRegistry(settings.skills_roots)
    if store.current(catalog.tenant) or store.latest(catalog.tenant):
        return ServedCatalog(store, catalog.tenant)
    return SkillRegistry(settings.skills_roots)


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


def _refuse_a_broken_catalog(registry: Catalog, settings: Settings) -> None:
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
