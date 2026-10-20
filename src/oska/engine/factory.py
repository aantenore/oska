"""Building an engine from configuration.

Everything the engine needs is assembled here, so the command line, the API and
the tests all start from the same wiring. With no model provider the engine
still routes by rule and by trigger, still answers what it can do, and declines
to plan; with one, the model classifies, plans, breaks ties and phrases the
reply, and discovery ranks by embedding when the provider embeds and by keyword
when it does not. Whatever the provider is, it is
wrapped in the turn's model budget before anything is given it, so every call
the runtime makes is counted in one place.

Which catalog is in force is decided here as well. When the revision store of
this deployment holds a revision for the configured tenant, and the
configuration names one or names none and one exists, the catalog is served
from that revision and nothing reads a mutable directory while it serves;
otherwise the configured skill roots are read directly, which is what a single
process on its own disk wants and what the example catalogs use. The ledger is
wrapped either way, so every receipt opened in a turn names the revision the
engine's own catalog was pinned to for that turn, whatever else the process
has pinned.

The guardrail catalog and the trace sink are built here too, from the runtime
configuration: guardrails on unless the configuration switches them off, traces
off unless it switches them on. The enrichment configuration and the learned
phrasings reach the two layers that rank the catalog, so a deployment that has
derived the phrases of its procedures, or has run the improvement job over its
traffic, routes by them without anything else being told. Both are read from
disk; neither is written here. The phrase catalog the runtime speaks its own
sentences from is loaded here once, and refused when the words an approval
request would tell a person to type are not ones the approval rules accept.

A caller that assembles several engines in one process, one per domain and
tenant, hands over what it has already resolved: the runtime configuration
with the tenant's fragment merged in, the connector bindings merged from the
domain's packs, the guardrail catalog with the packs' entries added, the
resolver that names the domain's host, the host itself for the reply writer,
whether the domain enables presentation, the synonyms of its packs, and the
storage namespace of the (tenant, domain). Whatever it does not hand over is
read from the settings, exactly as for a single catalog: presentation off, no
synonyms, no host, and the default namespace.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from oska.audit.sink import AuditSink
from oska.audit.trace import JsonlTraceSink, TraceSink
from oska.catalog.registry import (
    Catalog,
    active_revision,
    serve_catalog,
)
from oska.catalog.resolver import CapabilityResolver
from oska.catalog.validator import CatalogError, validate, validate_all
from oska.config import (
    RuntimeConfig,
    Settings,
    guardrail_catalog_path,
    load_runtime_config,
    state_path,
)
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.platform import HostIdentity
from oska.contracts.receipt import WriteReceipt
from oska.engine.engine import BudgetedProvider, Engine, ModelBudget
from oska.engine.phrases import load_phrases
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
from oska.planner.provider import ModelProvider, provider_embeds
from oska.router import FastPath, ModelRouter, RuleRouter, TurnRouter
from oska.storage import (
    DEFAULT_NAMESPACE,
    LedgerStore,
    open_audit,
    open_ledger,
    open_sessions,
)

__all__ = ["build_engine", "revision_ledger"]


def build_engine(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    audit_sink: AuditSink | None = None,
    trace_sink: TraceSink | None = None,
    connector_behaviors: dict[str, dict] | None = None,
    runtime: RuntimeConfig | None = None,
    connectors: ConnectorRegistry | None = None,
    guardrails: GuardrailCatalog | None = None,
    resolver: Callable[[Catalog], CapabilityResolver] | None = None,
    presentation: bool = False,
    synonyms: Mapping[str, str] | None = None,
    host: HostIdentity | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    phrase_layers: tuple[Path, ...] = (),
) -> Engine:
    """Assemble an engine from the settings and the configuration files they name.

    `runtime`, `connectors` and `guardrails` replace what the settings would
    have read; `connector_behaviors` applies only to bindings read here.
    `guardrails` is the whole declared catalog: skills are checked against it,
    and the engine keeps it unless the configuration switches guardrails off.
    `resolver` builds the catalog wording over the registry built here.
    `presentation` and `synonyms` are the serving domain's, `host` is who the
    reply writer is told it answers as, and `namespace` is what the session
    store, the ledger and the audit trail are kept under. `phrase_layers` are
    laid over the configured phrase catalog in order: the `phrases.json` of the
    domain's packs.
    """
    runtime = runtime or load_runtime_config(settings=settings)
    budget = ModelBudget(runtime.turn.max_model_calls)
    if provider is not None:
        provider = BudgetedProvider(provider, budget)
    declared = guardrails
    guardrails = _guardrail_catalog(runtime, settings, declared)
    trace = trace_sink or _trace_sink(runtime, settings)
    registry = _catalog(runtime, settings)
    _refuse_a_broken_catalog(
        registry, settings, None if declared is None else frozenset(declared.ids())
    )
    wording = (resolver or CapabilityResolver)(registry)

    def make_index() -> SkillIndex:
        if provider is None or not provider_embeds(provider):
            return KeywordIndex()
        return EmbeddingIndex(provider)

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
        None if provider is None else ModelRouter(provider, wording, runtime.router),
    )
    planner = None if provider is None else Planner(provider, registry, wording)
    if connectors is None:
        connectors = ConnectorRegistry.from_config(
            settings.connectors_config,
            behaviors=connector_behaviors,
            resilience=runtime.resilience,
        )
    capabilities = CapabilityRegistry(registry)
    ledger = revision_ledger(
        open_ledger(runtime, settings, namespace=namespace), catalog=registry
    )
    audit = audit_sink or open_audit(runtime, settings, namespace=namespace)
    preferences = preferences_root(settings.state_dir)
    phrases = load_phrases(runtime, settings, phrase_layers)
    return Engine(
        registry=registry,
        resolver=wording,
        discovery=discovery,
        router=router,
        planner=planner,
        connectors=connectors,
        capabilities=capabilities,
        policy=PolicyEngine(registry, ledger),
        approval=ApprovalGate(),
        ledger=ledger,
        verifier=Verifier(capabilities),
        session_store=open_sessions(runtime, settings, namespace=namespace),
        audit=audit,
        runtime_config=runtime,
        preferences_root=preferences,
        guardrails=guardrails,
        trace=trace,
        budget=budget,
        presentation=presentation,
        synonyms=synonyms,
        host=host,
        phrases=phrases,
    )


def revision_ledger(
    ledger: LedgerStore | Path,
    *,
    catalog: Catalog | None = None,
    busy_timeout: float | None = None,
) -> LedgerStore:
    """A write ledger that names the revision in force on every receipt it opens.

    A receipt answers what was done; under a released catalog it also has to
    answer which catalog said it could be done, because the procedure that
    declared the operation is an artifact with a name and that name is the only
    way back to the text a person approved. That is one field on the way out and
    nothing else, so it is added to a ledger here rather than made a second kind
    of ledger: everything a receipt is, and everything the store does with it,
    stays where it is written.

    With `catalog`, the revision is that catalog's pin and no other, so two
    engines in one process never name each other's revision. Without one it is
    the pin opened last in this context, which is only right where one catalog
    is pinned per turn.
    """
    if not isinstance(ledger, LedgerStore):
        ledger = WriteLedger(Path(ledger), busy_timeout=busy_timeout)
    opened = ledger.new_receipt

    def new_receipt(
        session_id: str,
        key: str,
        integration_id: str,
        operation: str,
        payload: dict,
    ) -> WriteReceipt:
        receipt = opened(session_id, key, integration_id, operation, payload)
        receipt.revision = active_revision(catalog) or None
        return receipt

    # The receipt factory is replaced on this one instance on purpose: a method of a
    # store the caller may have supplied, so no subclass is made to carry the revision.
    ledger.new_receipt = new_receipt  # type: ignore[method-assign]
    return ledger


def _catalog(runtime: RuntimeConfig, settings: Settings) -> Catalog:
    """The catalog this deployment serves, decided by the catalog configuration.

    The choice between the skill roots and a built revision, and the check of a
    revision's signature, live with the catalog; the factory only asks.
    """
    return serve_catalog(runtime.catalog, settings)


def _guardrail_catalog(
    runtime: RuntimeConfig, settings: Settings, declared: GuardrailCatalog | None = None
) -> GuardrailCatalog:
    """The declared guardrails, or an empty catalog when they are switched off."""
    if not runtime.guardrails.enabled:
        return GuardrailCatalog.empty()
    if declared is not None:
        return declared
    return GuardrailCatalog.load(guardrail_catalog_path(runtime.guardrails, settings))


def _trace_sink(runtime: RuntimeConfig, settings: Settings) -> TraceSink | None:
    """The diagnostic trace sink, or nothing at all, which is the default."""
    if not runtime.tracing.enabled:
        return None
    return JsonlTraceSink(state_path(settings.state_dir, runtime.tracing.path))


def _refuse_a_broken_catalog(
    registry: Catalog, settings: Settings, guardrail_ids: frozenset[str] | None = None
) -> None:
    """Nothing is served from a catalog whose files do not hold up.

    The settings travel with the catalog, so what a skill may declare is read
    from the configuration of this deployment and not from wherever the process
    happens to have been started. A caller that resolved the guardrail catalog
    itself names the identifiers it defines, and skills are checked against those.
    """
    skills = registry.skills()
    if guardrail_ids is None:
        checked = validate_all(skills, schema_dir=settings.schemas_dir, settings=settings)
    else:
        checked = [
            issue
            for skill in skills
            for issue in validate(
                skill, skills, schema_dir=settings.schemas_dir, guardrail_ids=guardrail_ids
            )
        ]
    issues = [*registry.parse_errors(), *checked]
    blocking = [issue for issue in issues if issue.severity == "error"]
    if blocking:
        raise CatalogError(blocking)
