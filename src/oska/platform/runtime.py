"""One process serving a platform: several domains, each for several tenants.

A `Platform` is built from a manifest and the settings of the process. It holds
one runtime per (tenant, domain), built the first time a turn needs it and kept
in a bounded least-recently-used cache of `runtime.platform.cache_size`
entries; an evicted runtime is closed, releasing the connection pools of its
bindings, and built again from the same declarations when it is next asked for,
finding its state where it left it. A runtime evicted while a turn runs on it is
closed when that turn ends. A cold build holds a lock of its own (tenant,
domain) only, so building one pair never stalls a turn of another. A turn takes
its claim on a runtime in the same step that finds or caches it, so a runtime
is never pushed out between being found and being claimed, however many pairs
are busy at once.

The session stores the platform opens to bind sessions are held in a cache of
the same size. A store a binding is using is never closed under it: the cache
grows past its size while more stores are busy than it holds, and shrinks back
as they are released. Asking whether another pair already holds a new session
uses the store of that pair when it is open, and otherwise opens one for the
question and closes it after, so the check neither evicts the stores that are
in use nor churns the cache.

Binding takes a lock chosen by the session id from a small fixed set, so the
first turns of two different sessions do not wait for each other, while two
first turns of one session always meet on the same lock.

What a domain's runtime is built from:

- its catalog is the `skills/` directories of its packs, and nothing else, so a
  skill of one domain is never ranked, offered or executed in another;
- its connectors are the bindings of its packs merged, where an integration id
  bound by two packs of one domain is refused, then what the tenant's overlay
  changes in them put in: only the `base_url` and `headers` of an HTTP binding
  (see `oska.platform.tenants`). A pack's bindings read from the environment
  only the variables its domain's `environment` declares, and a tenant's
  changes only those its manifest entry declares; a mock fixture resolves
  inside its pack. Each of these is checked when the platform is assembled;
- its guardrail catalog is the platform's catalog with each pack's
  `guardrails.json` entries added, where an id defined twice is refused;
- its synonyms are the `synonyms` of its packs merged, where a term two packs
  map to different catalog words is refused; its presentation is on when the
  domain enables it; its host is handed to the reply writer as well as to the
  catalog wording;
- its runtime configuration is the platform's with the tenant's fragment
  merged over it (see `oska.platform.tenants` for what a tenant may change,
  and which costs it may lower and never raise), checked for every tenant when
  the platform is assembled;
- its state lives under `<state_dir>/<tenant>/<domain>/`: sessions, ledger,
  audit, traces, proposals, revisions and preferences of one tenant in one
  domain never share a file with another. Every state path in the runtime
  configuration must therefore be relative; an absolute one is refused. A
  database backend keeps them apart by the storage namespace of the pair.

A session belongs to one tenant and one domain for its whole life. Its first
turn records both on the session, and a turn naming the same session id in
another domain, or as another tenant, is refused before anything runs. Channels
run turns through `Platform.turn`, which is where that check is made, by asking
the session store of each (tenant, domain) through the storage interface,
whichever backend it is.
"""

from __future__ import annotations

import threading
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from oska.catalog.registry import SkillRegistry
from oska.config import (
    RuntimeConfig,
    Settings,
    guardrail_catalog_path,
    load_runtime_config,
    load_settings,
)
from oska.contracts.common import TurnResult
from oska.contracts.platform import DomainSpec, HostIdentity, PlatformManifest
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.governance.guardrails import GuardrailCatalog
from oska.governance.identity import Principal
from oska.planner.provider import ModelProvider
from oska.platform.assembly import (
    bound_documents,
    check_domain_bindings,
    domain_guardrails,
    domain_integrations,
    domain_packs,
    domain_synonyms,
    hosted,
    merge_connectors,
    refuse_absolute_state_paths,
)
from oska.platform.errors import (
    DomainMismatch,
    ManifestError,
    PlatformError,
    TenantMismatch,
    UnknownDomain,
    UnknownTenant,
)
from oska.platform.manifest import load_platform
from oska.platform.packs import Pack, check_provides, check_requires, load_pack
from oska.platform.tenants import (
    DEFAULT_TENANT,
    TenantOverlay,
    apply_runtime,
    check_tenant_name,
    load_overlay,
)
from oska.platform.versions import satisfies
from oska.services import Services, build_services
from oska.storage import SessionStateStore, open_sessions, storage_namespace

__all__ = ["EngineBuilder", "Platform"]

EngineBuilder = Callable[..., Engine]
"""Builds a domain's engine from settings; takes `build_engine`'s keyword arguments."""

BINDING_STRIPES = 64
"""How many locks the first turns of sessions are spread over, by session id."""


class Platform:
    """Every domain a manifest declares, served from one process."""

    def __init__(
        self,
        manifest: PlatformManifest,
        settings: Settings | None = None,
        *,
        root: Path | None = None,
        provider: ModelProvider | None = None,
    ) -> None:
        self._manifest = manifest
        self._settings = settings or load_settings()
        self._root = Path(root) if root is not None else Path.cwd()
        self._provider = provider
        self._runtime = load_runtime_config(settings=self._settings)
        refuse_absolute_state_paths(self._runtime)
        self._packs = self._load_packs()
        self._ordered = {
            spec.name: domain_packs(spec, self._packs) for spec in manifest.domains
        }
        integrations = {
            spec.name: domain_integrations(spec, self._ordered[spec.name])
            for spec in manifest.domains
        }
        platform_catalog = guardrail_catalog_path(self._runtime.guardrails, self._settings)
        self._guardrails = {
            spec.name: domain_guardrails(spec, self._ordered[spec.name], platform_catalog)
            for spec in manifest.domains
        }
        self._synonyms = {
            spec.name: domain_synonyms(spec, self._ordered[spec.name])
            for spec in manifest.domains
        }
        self._overlays = self._load_overlays(
            {name for ids in integrations.values() for name in ids}
        )
        self._tenant_runtimes: dict[str, RuntimeConfig] = {}
        for tenant in sorted({t for spec in manifest.domains for t in spec.tenants}):
            self.runtime_for(tenant)
        for spec in manifest.domains:
            check_domain_bindings(spec, self._ordered[spec.name])
            for tenant in spec.tenants:
                bound_documents(
                    self._ordered[spec.name],
                    self._overlays.get(tenant),
                    manifest.tenant(tenant).environment,
                )
        self._cache: OrderedDict[tuple[str, str], Services] = OrderedDict()
        self._size = max(1, self._runtime.platform.cache_size)
        self._lock = threading.Lock()
        self._building: dict[tuple[str, str], threading.Lock] = {}
        self._leases: dict[int, int] = {}
        self._retired: dict[int, Services] = {}
        self._binding = tuple(threading.Lock() for _ in range(BINDING_STRIPES))
        self._stores_lock = threading.Lock()
        self._session_stores: OrderedDict[tuple[str, str], SessionStateStore] = OrderedDict()
        self._store_leases: dict[tuple[str, str], int] = {}

    @classmethod
    def from_file(
        cls,
        path: Path | str,
        settings: Settings | None = None,
        *,
        provider: ModelProvider | None = None,
    ) -> Platform:
        """The platform a manifest file declares, with overlays read beside it."""
        manifest = load_platform(path)
        return cls(manifest, settings, root=Path(path).resolve().parent, provider=provider)

    @property
    def manifest(self) -> PlatformManifest:
        """The manifest this platform was built from."""
        return self._manifest

    @property
    def runtime(self) -> RuntimeConfig:
        """The platform's runtime configuration, before any tenant fragment."""
        return self._runtime

    def domains(self) -> list[DomainSpec]:
        """Every domain, in the order the manifest declares them."""
        return list(self._manifest.domains)

    def pairs(self, domain: str | None = None, tenant: str | None = None) -> list[tuple[str, str]]:
        """Every (tenant, domain) this platform serves, narrowed by what is named.

        Without either, every pair the manifest declares, domain by domain in
        its order and tenant by tenant in each domain's order. A named domain
        limits it to that domain's own tenants; a named tenant limits it to the
        domains that serve it, wherever it appears. Naming both is the one pair,
        checked the way `tenant` checks it. A caller reaching a person's record
        across a platform asks for this with neither named: a request to be
        forgotten is about a person, not about one domain.
        """
        checked = None if tenant is None else check_tenant_name(tenant)
        specs = [self.domain(domain)] if domain is not None else self.domains()
        pairs = [
            (served, spec.name)
            for spec in specs
            for served in spec.tenants
            if checked is None or served == checked
        ]
        if checked is not None and not pairs:
            if domain is not None:
                self.tenant(domain, tenant)  # raises UnknownTenant with the same wording
            raise UnknownTenant(f"no domain of this platform serves tenant {checked!r}")
        return pairs

    def domain(self, name: str | None = None) -> DomainSpec:
        """The named domain, or the default one when no name is given."""
        try:
            return self._manifest.domain(name)
        except KeyError as exc:
            raise UnknownDomain(f"this platform declares no domain {name!r}") from exc

    def host(self, domain: str | None = None) -> HostIdentity:
        """Who the runtime answers as in a domain."""
        return self.domain(domain).host

    def guardrails(self, domain: str | None = None) -> GuardrailCatalog:
        """The guardrail catalog of a domain: the platform's with its packs' entries added."""
        return self._guardrails[self.domain(domain).name]

    def synonyms(self, domain: str | None = None) -> dict[str, str]:
        """How people say a thing in a domain, merged from its packs."""
        return dict(self._synonyms[self.domain(domain).name])

    def packs(self, domain: str | None = None) -> list[Pack]:
        """The packs a domain serves, each after the packs it depends on."""
        return list(self._ordered[self.domain(domain).name])

    def tenant(self, domain: str | None = None, tenant: str | None = None) -> str:
        """The tenant a request is served as in a domain.

        No tenant means `default` when the domain serves it, else the first
        tenant the domain declares. A named tenant must be one the domain serves.
        """
        spec = self.domain(domain)
        if tenant is None:
            chosen = DEFAULT_TENANT if DEFAULT_TENANT in spec.tenants else spec.tenants[0]
        else:
            chosen = check_tenant_name(tenant)
        if chosen not in spec.tenants:
            raise UnknownTenant(f"domain {spec.name} does not serve tenant {chosen!r}")
        return chosen

    def settings_for(self, domain: str | None = None, tenant: str | None = None) -> Settings:
        """The settings of one (tenant, domain): its packs and its own state directory."""
        spec = self.domain(domain)
        chosen = self.tenant(spec.name, tenant)
        if not spec.packs:
            raise PlatformError(
                f"domain {spec.name} serves no packs; name at least one in the platform manifest"
            )
        primary = self._packs[spec.packs[0]]
        return self._settings.model_copy(
            update={
                "skills_roots": [pack.skills for pack in self._ordered[spec.name]],
                "connectors_config": primary.connectors,
                "scenarios_dir": primary.scenarios,
                "state_dir": Path(self._settings.state_dir) / chosen / spec.name,
            }
        )

    def runtime_for(self, tenant: str) -> RuntimeConfig:
        """The runtime configuration of one tenant: the platform's, with its fragment."""
        held = self._tenant_runtimes.get(tenant)
        if held is None:
            overlay = self._overlays.get(tenant)
            merged = apply_runtime(
                self._runtime,
                {} if overlay is None else overlay.runtime,
                where=f"tenant {tenant}",
            )
            held = merged.model_copy(
                update={"catalog": merged.catalog.model_copy(update={"tenant": tenant})}
            )
            self._tenant_runtimes[tenant] = held
        return held

    def builder(self, domain: str | None = None, tenant: str | None = None) -> EngineBuilder:
        """A function building this (tenant, domain)'s engine over whatever settings it is given.

        The conformance runner uses it to build a fresh engine per scenario
        with the scenario's own state directory and the domain's declarations.
        A caller may ask for presentation explicitly; otherwise the domain decides.
        A `namespace` it is given is a scope, and the pair's own namespace is
        kept inside it, so pairs built under one scope still never share a record.
        """
        spec = self.domain(domain)
        chosen = self.tenant(spec.name, tenant)

        def build(settings: Settings, **kwargs: object) -> Engine:
            runtime = self.runtime_for(chosen)
            kwargs.setdefault("presentation", spec.presentation)
            pair = storage_namespace(chosen, spec.name)
            scope = kwargs.pop("namespace", None)
            return build_engine(
                settings,
                runtime=runtime,
                connectors=merge_connectors(
                    self._ordered[spec.name],
                    self._overlays.get(chosen),
                    runtime.resilience,
                    environment=spec.environment,
                    tenant_environment=self._manifest.tenant(chosen).environment,
                ),
                guardrails=self._guardrails[spec.name],
                resolver=hosted(spec.host),
                synonyms=self._synonyms[spec.name],
                host=spec.host,
                namespace=pair if scope is None else f"{scope}/{pair}",
                # The builder's keywords are checked where they are declared; mypy cannot
                # match an open mapping against build_engine's fixed signature.
                **kwargs,  # type: ignore[arg-type]
            )

        return build

    def services(self, domain: str | None = None, tenant: str | None = None) -> Services:
        """The runtime of one (tenant, domain), built on first use and cached.

        A cold build holds the lock of its own pair only: two requests for one
        pair build it once, and a request for another pair is not kept waiting.
        What the build pushes out of the cache is closed, unless a turn still
        runs on it, in which case it is closed when that turn ends. A caller
        holding a runtime outside `turn` holds no such claim on it.
        """
        spec = self.domain(domain)
        chosen = self.tenant(spec.name, tenant)
        return self._obtain((chosen, spec.name), lease=False)

    def _obtain(self, key: tuple[str, str], *, lease: bool) -> Services:
        """The runtime of a pair, found or built, claimed for a turn when `lease` is set.

        The claim is taken under the same lock hold that finds the runtime in
        the cache or puts it there, so nothing can push it out in between.
        """
        tenant, domain = key
        with self._lock:
            held = self._cached(key)
            if held is not None:
                return self._claimed(held, lease)
            building = self._building.setdefault(key, threading.Lock())
        with building:
            with self._lock:
                held = self._cached(key)
                if held is not None:
                    return self._claimed(held, lease)
            settings = self.settings_for(domain, tenant)
            engine = self.builder(domain, tenant)(settings, provider=self._provider)
            built = build_services(settings, engine=engine)
            evicted: list[Services] = []
            with self._lock:
                self._cache[key] = built
                self._claimed(built, lease)
                while len(self._cache) > self._size:
                    _, gone = self._cache.popitem(last=False)
                    if self._leases.get(id(gone)):
                        self._retired[id(gone)] = gone
                    else:
                        evicted.append(gone)
            for gone in evicted:
                _close(gone)
            return built

    def _claimed(self, services: Services, lease: bool) -> Services:
        """Count a turn's claim on a runtime; called with the cache lock held."""
        if lease:
            self._leases[id(services)] = self._leases.get(id(services), 0) + 1
        return services

    def engine(self, domain: str | None = None, tenant: str | None = None) -> Engine:
        """The engine of one (tenant, domain)."""
        return self.services(domain, tenant).engine

    def cached(self) -> list[tuple[str, str]]:
        """The (tenant, domain) runtimes held right now, least recently used first."""
        with self._lock:
            return list(self._cache)

    def close(self) -> None:
        """Close every runtime this platform holds; each is built again when next asked for."""
        with self._lock:
            held = [*self._cache.values(), *self._retired.values()]
            self._cache.clear()
            self._retired.clear()
        for services in held:
            _close(services)

    def _cached(self, key: tuple[str, str]) -> Services | None:
        """The runtime of a pair when it is held, marked as the most recently used."""
        held = self._cache.get(key)
        if held is not None:
            self._cache.move_to_end(key)
        return held

    @contextmanager
    def _lease(self, domain: str, tenant: str) -> Iterator[Services]:
        """The runtime of a pair, kept open while the caller runs a turn on it."""
        services = self._obtain((tenant, domain), lease=True)
        try:
            yield services
        finally:
            with self._lock:
                remaining = self._leases.get(id(services), 1) - 1
                if remaining:
                    self._leases[id(services)] = remaining
                else:
                    self._leases.pop(id(services), None)
                retired = None if remaining else self._retired.pop(id(services), None)
            if retired is not None:
                _close(retired)

    def turn(
        self,
        domain: str | None,
        tenant: str | None,
        session_id: str,
        text: str,
        principal: Principal | None = None,
    ) -> TurnResult:
        """Run one turn of one session in one domain, as one tenant."""
        spec = self.domain(domain)
        chosen = self.tenant(spec.name, tenant)
        with self._lease(spec.name, chosen) as services:
            self._bind(spec.name, chosen, session_id)
            return services.engine.turn(session_id, text, principal)

    def _bind(self, domain: str, tenant: str, session_id: str) -> None:
        """Hold a session to the tenant and domain of its first turn, and refuse any other.

        Both are written on the session before the turn runs, so the engine
        loads them and saves them back with the rest of the state. Whether
        another (tenant, domain) of the platform already holds the session is
        asked of that pair's session store through the storage interface, so
        the check is the same whichever backend keeps the record. A session
        held by another tenant is refused without naming that tenant.

        The lock is the one the session id picks from a fixed set, so only
        turns of sessions that share it wait for each other, and two first
        turns of one session always do. It settles the first turn of a session
        in two places on one of them within this process only: two processes
        sharing one database can still both find the session unbound and bind
        it differently at the same moment.
        """
        with self._binding_lock(session_id), self._session_store(domain, tenant) as store:
            state = store.load(session_id)
            if state.tenant and state.tenant != tenant:
                raise TenantMismatch(_other_tenant(session_id, tenant))
            if state.domain and state.domain != domain:
                raise DomainMismatch(_crossing(session_id, state.domain, domain))
            if state.domain == domain and state.tenant == tenant:
                return
            if not state.domain:
                self._refuse_if_held_elsewhere(domain, tenant, session_id)
            state.domain = domain
            state.tenant = tenant
            store.save(state)

    def _binding_lock(self, session_id: str) -> threading.Lock:
        """The lock the first turns of one session meet on, picked by its id."""
        return self._binding[zlib.crc32(session_id.encode("utf-8")) % len(self._binding)]

    def _refuse_if_held_elsewhere(self, domain: str, tenant: str, session_id: str) -> None:
        """Refuse a new session another (tenant, domain) of the platform already holds.

        Each other pair is asked through its session store: the one already
        open when there is one, otherwise one opened for this question and
        closed after it, so asking never pushes a store out of the cache.
        """
        for other_tenant, other_domain in self.pairs():
            if (other_tenant, other_domain) == (tenant, domain):
                continue
            with self._session_store(other_domain, other_tenant, keep=False) as elsewhere:
                if not elsewhere.exists(session_id):
                    continue
                if other_tenant != tenant:
                    raise TenantMismatch(_other_tenant(session_id, tenant))
                held = elsewhere.load(session_id).domain or other_domain
            raise DomainMismatch(_crossing(session_id, held, domain))

    @contextmanager
    def _session_store(
        self, domain: str, tenant: str, *, keep: bool = True
    ) -> Iterator[SessionStateStore]:
        """The session store of one (tenant, domain), held open while the caller uses it.

        It is opened the way that domain's engine opens its own, so it reads
        the same record without building the engine. With `keep`, a store that
        is not open yet is kept in a cache of `cache_size`; without it, one
        that is not open already is opened for the caller alone and closed
        when the caller is done. A store in use is never closed: the cache
        closes the least recently used stores nobody is using, and holds more
        than its size only while more than that are in use.
        """
        key = (tenant, domain)
        with self._stores_lock:
            held = self._session_stores.get(key)
            if held is not None:
                self._session_stores.move_to_end(key)
                self._store_leases[key] = self._store_leases.get(key, 0) + 1
        if held is None and not keep:
            transient = self._open_sessions(domain, tenant)
            try:
                yield transient
            finally:
                _release(transient)
            return
        if held is None:
            opened = self._open_sessions(domain, tenant)
            with self._stores_lock:
                held = self._session_stores.get(key)
                if held is None:
                    held = opened
                    self._session_stores[key] = held
                self._store_leases[key] = self._store_leases.get(key, 0) + 1
            if held is not opened:
                _release(opened)
        try:
            yield held
        finally:
            with self._stores_lock:
                remaining = self._store_leases.get(key, 1) - 1
                if remaining:
                    self._store_leases[key] = remaining
                else:
                    self._store_leases.pop(key, None)
                evicted = self._evict_idle_stores()
            for gone in evicted:
                _release(gone)

    def _open_sessions(self, domain: str, tenant: str) -> SessionStateStore:
        return open_sessions(
            self.runtime_for(tenant),
            self.settings_for(domain, tenant),
            namespace=storage_namespace(tenant, domain),
        )

    def _evict_idle_stores(self) -> list[SessionStateStore]:
        """Take out the least recently used stores nobody is using, down to the cache size.

        Called with the store lock held; the caller closes what comes back.
        """
        evicted: list[SessionStateStore] = []
        for key in list(self._session_stores):
            if len(self._session_stores) <= self._size:
                break
            if self._store_leases.get(key):
                continue
            evicted.append(self._session_stores.pop(key))
        return evicted

    def _load_packs(self) -> dict[str, Pack]:
        packs: dict[str, Pack] = {}
        for name, ref in self._manifest.packs.items():
            pack = load_pack(ref.path)
            if pack.name != name:
                raise ManifestError(
                    f"the manifest calls {ref.path} {name!r}; its pack.yaml says {pack.name!r}"
                )
            if not satisfies(pack.version, ref.version):
                raise ManifestError(
                    f"the manifest expects pack {name} {ref.version}, and it is {pack.version}"
                )
            check_requires(pack)
            check_provides(pack, SkillRegistry([pack.skills]))
            packs[name] = pack
        return packs

    def _load_overlays(self, bound: set[str]) -> dict[str, TenantOverlay]:
        tenants = {tenant for spec in self._manifest.domains for tenant in spec.tenants}
        overlays: dict[str, TenantOverlay] = {}
        for tenant in sorted(tenants):
            overlay = load_overlay(self._root, tenant)
            stray = sorted(overlay.integration_ids - bound)
            if stray:
                raise PlatformError(
                    f"tenant {tenant} binds {', '.join(stray)}, "
                    "which no pack of the platform declares"
                )
            overlays[tenant] = overlay
        return overlays


def _close(services: Services) -> None:
    """Release what a runtime holds open: the connection pools of its bindings."""
    services.engine.connectors.close()
    _release(services.engine)


def _release(held: object) -> None:
    close = getattr(held, "close", None)
    if callable(close):
        close()


def _other_tenant(session_id: str, asked: str) -> str:
    return f"session {session_id} belongs to another tenant and cannot run a turn as {asked}"


def _crossing(session_id: str, held: str, asked: str) -> str:
    return (
        f"session {session_id} belongs to domain {held} and cannot run a turn in domain {asked}"
    )
