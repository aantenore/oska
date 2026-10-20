"""What a domain's runtime is assembled from, each part checked as it is put together.

Every function here is pure over the packs it is given: which packs a domain
serves and in what order, which integrations they bind and what those bindings
may read, which guardrails they add, which synonyms they declare, which layers
of phrases they lay over the catalog the runtime speaks from, and the
connector registry of one tenant in one domain. The runtime in
`oska.platform.runtime` calls them; nothing here holds state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from oska.catalog.registry import Catalog, SkillRegistry
from oska.catalog.resolver import CapabilityResolver
from oska.config import ResilienceConfig, RuntimeConfig, Settings
from oska.connectors.base import ConnectorError
from oska.connectors.registry import ConnectorRegistry, check_bindings, read_bindings
from oska.contracts.platform import DomainSpec, EnvironmentSpec, HostIdentity
from oska.engine.phrases import load_phrases, read_catalog
from oska.governance.guardrails import Guardrail, GuardrailCatalog, GuardrailError
from oska.platform.errors import ManifestError, PlatformError
from oska.platform.host import HostedResolver
from oska.platform.packs import Pack, resolve_dependencies
from oska.platform.tenants import TenantOverlay, check_tenant_name, overlay_binding

__all__ = [
    "STATE_PATHS",
    "bound_documents",
    "check_domain_bindings",
    "domain_guardrails",
    "domain_integrations",
    "domain_packs",
    "domain_phrases",
    "domain_synonyms",
    "hosted",
    "merge_connectors",
    "refuse_absolute_state_paths",
]

STATE_PATHS: tuple[tuple[str, str], ...] = (
    ("ledger", "path"),
    ("audit", "path"),
    ("sessions", "path"),
    ("tracing", "path"),
    ("learning", "publish_root"),
    ("improvement", "learned_root"),
    ("improvement", "report_path"),
    ("catalog", "revisions_root"),
    ("changes", "root"),
)
"""Runtime configuration paths that land under the state directory of a (tenant, domain)."""

PLATFORM_SOURCE = "the platform"


def refuse_absolute_state_paths(runtime: RuntimeConfig) -> None:
    """Refuse a configuration whose state would land outside a (tenant, domain) directory."""
    for section, field in STATE_PATHS:
        configured = Path(getattr(getattr(runtime, section), field))
        if configured.is_absolute():
            raise PlatformError(
                f"{section}.{field} is absolute ({configured}); a platform keeps every "
                "tenant and domain under its own state directory, so state paths are relative"
            )


def domain_packs(spec: DomainSpec, packs: Mapping[str, Pack]) -> list[Pack]:
    """The packs a domain serves, dependencies first, with skill ids unique across them."""
    if not spec.tenants:
        raise ManifestError(f"domain {spec.name} serves no tenant")
    for tenant in spec.tenants:
        check_tenant_name(tenant)
    ordered = resolve_dependencies(packs[name] for name in spec.packs)
    seen: dict[str, str] = {}
    for pack in ordered:
        for skill in SkillRegistry([pack.skills]).current():
            if skill.id in seen:
                raise PlatformError(
                    f"domain {spec.name}: {skill.id} is declared by packs "
                    f"{seen[skill.id]} and {pack.name}"
                )
            seen[skill.id] = pack.name
    return ordered


def domain_integrations(spec: DomainSpec, packs: Sequence[Pack]) -> set[str]:
    """The integration ids a domain binds, refusing one bound by two of its packs."""
    owner: dict[str, str] = {}
    for pack in packs:
        for integration_id in _bound_ids(pack.connectors):
            if integration_id in owner:
                raise PlatformError(
                    f"domain {spec.name}: {integration_id} is bound by packs "
                    f"{owner[integration_id]} and {pack.name}"
                )
            owner[integration_id] = pack.name
    return set(owner)


def domain_guardrails(
    spec: DomainSpec, packs: Sequence[Pack], platform_catalog: Path
) -> GuardrailCatalog:
    """The platform's guardrails with the entries of each pack of the domain added."""
    entries: list[tuple[str, Guardrail]] = []
    if Path(platform_catalog).is_file():
        loaded = GuardrailCatalog.load(platform_catalog).all()
        entries.extend((PLATFORM_SOURCE, entry) for entry in loaded)
    for pack in packs:
        if pack.guardrails is not None:
            loaded = GuardrailCatalog.load(pack.guardrails).all()
            entries.extend((f"pack {pack.name}", entry) for entry in loaded)
    where: dict[str, str] = {}
    for source, entry in entries:
        if entry.id in where:
            raise PlatformError(
                f"domain {spec.name}: guardrail {entry.id} is defined by "
                f"{where[entry.id]} and by {source}"
            )
        where[entry.id] = source
    try:
        return GuardrailCatalog(entry for _, entry in entries)
    except GuardrailError as exc:
        raise PlatformError(f"domain {spec.name}: {exc}") from exc


def domain_synonyms(spec: DomainSpec, packs: Sequence[Pack]) -> dict[str, str]:
    """The synonyms of a domain's packs merged, refusing a term two packs map apart.

    A term is read the way the message is, in lower case, so two packs that
    spell it differently still declare the same term. Two packs mapping it to
    the same catalog word agree, and that is not a conflict.
    """
    merged: dict[str, str] = {}
    owner: dict[str, str] = {}
    for pack in packs:
        for spoken, named in pack.manifest.synonyms.items():
            term = spoken.strip().lower()
            if not term:
                continue
            held = merged.get(term)
            if held is not None and held.strip().lower() != named.strip().lower():
                raise PlatformError(
                    f"domain {spec.name}: synonym {term!r} is mapped to {held!r} by pack "
                    f"{owner[term]} and to {named!r} by pack {pack.name}"
                )
            merged.setdefault(term, named)
            owner.setdefault(term, pack.name)
    return merged


def domain_phrases(
    spec: DomainSpec, packs: Sequence[Pack], runtime: RuntimeConfig, settings: Settings
) -> tuple[Path, ...]:
    """The phrase layers a domain's packs ship, in pack order, each checked.

    Each layer is checked laid over the packaged and the configured catalog,
    the way the engine will load it: known phrases only, the placeholders of
    each English phrase, and approval words the approval rules accept. A
    phrase two packs of one domain give differently in one language is refused
    with both packs named; two packs giving it the same way agree.
    """
    layers: list[Path] = []
    owner: dict[tuple[str, str], tuple[str, object]] = {}
    for pack in packs:
        path = pack.phrases
        if path is None:
            continue
        try:
            catalog = read_catalog(path)
            load_phrases(runtime, settings, (path,))
        except (OSError, ValueError) as exc:
            raise PlatformError(f"domain {spec.name}: pack {pack.name}: {exc}") from exc
        for language, entries in catalog.items():
            for key, entry in entries.items():
                held = owner.setdefault((language, key), (pack.name, entry))
                if held[1] != entry:
                    raise PlatformError(
                        f"domain {spec.name}: phrase {key} in language {language!r} is "
                        f"given one way by pack {held[0]} and another by pack {pack.name}"
                    )
        layers.append(path)
    return tuple(layers)


def check_domain_bindings(spec: DomainSpec, packs: Sequence[Pack]) -> None:
    """Refuse a pack binding that reads what its domain does not grant it.

    Every `${NAME}` a binding reads must be one the domain's `environment`
    admits, and every mock fixture must resolve inside the pack that declares it.
    """
    for pack in packs:
        try:
            check_bindings(
                _document(pack),
                pack.connectors.parent,
                readable=spec.environment.allows,
                confine_to=pack.root,
            )
        except ConnectorError as exc:
            raise PlatformError(f"domain {spec.name}: pack {pack.name}: {exc}") from exc


def bound_documents(
    packs: Sequence[Pack], overlay: TenantOverlay | None, environment: EnvironmentSpec
) -> list[tuple[Pack, dict]]:
    """Each pack's binding document with what a tenant changes put in, each change checked.

    `environment` is what the tenant's changes may read. A change to an
    integration the pack does not bind is left for the pack that does.
    """
    documents: list[tuple[Pack, dict]] = []
    for pack in packs:
        document = _document(pack)
        integrations = document.get("integrations") or {}
        if overlay is not None:
            for integration_id, change in overlay.bindings.items():
                if integration_id in integrations:
                    integrations[integration_id] = overlay_binding(
                        overlay.tenant,
                        integration_id,
                        integrations[integration_id],
                        change,
                        environment,
                    )
        documents.append((pack, document))
    return documents


def merge_connectors(
    packs: Sequence[Pack],
    overlay: TenantOverlay | None,
    resilience: ResilienceConfig,
    *,
    environment: EnvironmentSpec | None = None,
    tenant_environment: EnvironmentSpec | None = None,
) -> ConnectorRegistry:
    """The merged bindings of a domain's packs, with what the tenant changes put in.

    A fresh registry every call: a connector holds what it has been told, and
    two engines never share one. A pack's own values read what the domain's
    `environment` admits and a tenant's changes what its `tenant_environment`
    admits, each checked apart before anything is built; a fixture is read only
    from inside its pack.
    """
    domain_reads = environment or EnvironmentSpec()
    tenant_reads = tenant_environment or EnvironmentSpec()

    def readable(name: str) -> bool:
        return domain_reads.allows(name) or tenant_reads.allows(name)

    merged = ConnectorRegistry()
    for pack, document in bound_documents(packs, overlay, tenant_reads):
        try:
            built = ConnectorRegistry.from_document(
                document,
                pack.connectors.parent,
                resilience=resilience,
                readable=readable,
                confine_to=pack.root,
            )
        except ConnectorError as exc:
            merged.close()
            raise PlatformError(f"pack {pack.name}: {exc}") from exc
        _take(merged, built)
    return merged


def hosted(host: HostIdentity) -> Callable[[Catalog], CapabilityResolver]:
    """What builds a domain's catalog wording, introduced by its host."""

    def make(registry: Catalog) -> CapabilityResolver:
        return HostedResolver(registry, host)

    return make


def _take(target: ConnectorRegistry, source: ConnectorRegistry) -> None:
    for integration_id in source.ids():
        target.register(integration_id, source.get(integration_id))
    target.warnings.extend(source.warnings)


def _document(pack: Pack) -> dict:
    try:
        return read_bindings(pack.connectors)
    except (OSError, ValueError, ConnectorError) as exc:
        raise PlatformError(f"pack {pack.name}: {pack.connectors} cannot be read: {exc}") from exc


def _bound_ids(path: Path) -> list[str]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlatformError(f"{path}: cannot be read: {exc}") from exc
    integrations = document.get("integrations") if isinstance(document, dict) else None
    return list(integrations or {})
