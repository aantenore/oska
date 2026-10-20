"""The routes. Each handler reads a request, calls one runtime function, answers.

No handler decides anything the runtime does not already decide: a turn is run
by the engine, a proposal is drafted and moved by the learning package, and the
wording of what the runtime can do comes from the resolver.

There is no endpoint that applies a change request, and there is not going to
be one. Moving a proposal to `published` composes a package under the change
request root and returns it; installing that package into a catalog is
`ChangeRequestStore.apply`, which takes the catalog root from whoever calls it
and is called from the command line by a person. The process that answers
people can propose a change to the catalog it answers from and can never make
one. `GET /changes` and `GET /changes/{id}` read; nothing here writes.

Every call is resolved to an identity once, before its handler runs, the way
`IdentityConfig` says: headers an authenticated proxy sets, taken as given, or
a session token the runtime verifies. With `required`, a call that carries none
is refused with 401; `/health` is the one route here that stays open, and
`/metrics` lives on a router of its own.

A call on a session — a turn, its receipts, a proposal drafted from it — is a
session call. A token issued for another session does not reach it, and when
the session was opened under an identity, a caller with another subject or
with none does not either. A session opened without an identity stays without
one: a turn that arrives with an identity once the session has begun is
refused. Those refusals, and a turn that names the session in a domain it does
not belong to, all answer the same 403, so a caller learns nothing about a
session it cannot use. On a turn, identity is settled first, then the tenant,
then the domain the session belongs to.

Proposals are drawn from people's conversations, so reading them, moving
them and reading the change requests they compose is a reviewer's work: a call
with an identity reaches `/proposals`, `/proposals/{id}`, its transition,
`/changes` and `/changes/{id}` only when the principal carries the role
`IdentityConfig.reviewer_role` names, and is otherwise refused with 403 naming
that role. Drafting a proposal from a session stays open to the session's own
principal and to a reviewer; a reviewer's token is not held to the session it
was issued for there, anyone else's is. A session with no history has nothing
to draft from and answers 404 whether or not it exists, and the draft's model
call is counted against the per-turn model budget like any turn's. A call with
no identity reaches the reviewer's routes, and drafts from a session opened
without one, only in a single-user deployment (`IdentityConfig.single_user`);
anywhere else it is refused with 401, so no identity is never worth more than
an identity without the role. `/domains` names each domain's tenants only to a
caller that may review.

A proposal transition is recorded under the caller's identity. Only a call
with no identity, in a single-user deployment, may name its actor in the body,
and that name is recorded as stated and not verified.

A runtime serving a platform answers in several domains, each for several
tenants. `/domains/{domain}/...` names the domain; the routes without it act on
the default domain. In proxy mode with `IdentityConfig.tenant_header` named,
the tenant is that header and nothing else, since the proxy owns that header;
a tenant in the attributes header is not read. Otherwise it is the principal's
tenant attribute when it carries one, else `default`. A header a client can
set is never read as a tenant unless the configuration names it. A runtime
serving a single catalog is a platform of one domain, `default`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from oska.api.errors import ApiError
from oska.api.identity import (
    ATTRIBUTES_HEADER,
    SUBJECT_HEADER,
    application_runtime,
    check_proxy_secret,
    identity_config,
    identity_required,
    is_reviewer,
    may_review,
    refuse_review,
    resolve_principal,
    session_refused,
)
from oska.config import IdentityConfig, RuntimeConfig
from oska.contracts.common import TurnResult
from oska.contracts.platform import HostIdentity
from oska.contracts.proposal import ActorSource, SkillProposal
from oska.contracts.receipt import WriteReceipt
from oska.contracts.skill import Skill
from oska.governance.identity import Principal, PrincipalMismatch
from oska.learning import (
    ChangeRequest,
    LifecycleError,
    draft_from_session,
    transition,
)
from oska.platform import DomainMismatch, Platform, UnknownDomain, UnknownTenant
from oska.services import Services
from oska.storage import ChangeRequestRecordStore

__all__ = [
    "ATTRIBUTES_HEADER",
    "SUBJECT_HEADER",
    "ApiError",
    "CapabilityList",
    "DomainInfo",
    "ProcessInfo",
    "SkillDetail",
    "SkillSummary",
    "TransitionRequest",
    "TransitionResult",
    "TurnRequest",
    "router",
]

DEFAULT_TENANT = "default"
SINGLE_DOMAIN = "default"
OPEN_PATHS = frozenset({"/health"})
SESSION_PARAMETER = "session_id"
REVIEWER_SESSION_PATHS = frozenset(
    {
        "/proposals/from-session/{session_id}",
        "/domains/{domain}/proposals/from-session/{session_id}",
    }
)


def _identified(request: Request) -> None:
    """Resolve the caller once, before the handler, and refuse one without an identity.

    A route with a session in its path is a session call, so a token is held
    to that session; any other route is a call that acts in no session. On a
    route a reviewer may take up any session from, a reviewer's token is not
    held to its session and every other token is.
    """
    path = getattr(request.scope.get("route"), "path", "")
    config = _identity(request)
    if path in OPEN_PATHS:
        # Open to a call without an identity, never to a proxy header the proxy did
        # not send: the tenant header still picks which catalog answers.
        check_proxy_secret(request, config)
        return
    session_id = request.path_params.get(SESSION_PARAMETER)
    if path in REVIEWER_SESSION_PATHS:
        principal = resolve_principal(request, config, session_id=None)
        if principal is not None and not is_reviewer(principal, config):
            principal = resolve_principal(request, config, session_id=session_id)
    else:
        principal = resolve_principal(request, config, session_id=session_id)
    if principal is None and config.required:
        raise identity_required()
    request.state.principal = principal


def _reviewer(request: Request) -> None:
    """Refuse a caller that may not do a reviewer's work.

    A principal passes with the reviewer role. A call with no identity passes
    only in a single-user deployment, and is otherwise refused as a call that
    needs an identity.
    """
    principal = _caller(request)
    config = _identity(request)
    if not may_review(principal, config):
        raise refuse_review(principal, config)


router = APIRouter(dependencies=[Depends(_identified)])
REVIEWER = [Depends(_reviewer)]


class TurnRequest(BaseModel):
    """One thing a person typed."""

    text: str


class TransitionRequest(BaseModel):
    """A request to move a proposal to another state.

    `by` is read only from a call that carries no identity where none is
    required, and is then recorded as stated; a call with an identity is
    recorded under it, whatever `by` says.
    """

    to: str
    by: str = ""
    note: str = ""


class TransitionResult(BaseModel):
    """A moved proposal and, when the move composed one, its change request."""

    proposal: SkillProposal
    change_request: ChangeRequest | None = None


class SkillSummary(BaseModel):
    """One catalog entry, as a list shows it."""

    id: str
    type: str
    version: int
    status: str
    description: str


class SkillDetail(BaseModel):
    """One catalog entry in full: what it declares and what it says."""

    frontmatter: dict
    sections: dict


class ProcessInfo(BaseModel):
    """A procedure on offer, with the wording the runtime uses for it."""

    id: str
    action: str
    action_on_selection: str | None = None
    description: str


class CapabilityList(BaseModel):
    """What the runtime can do, derived from the catalog."""

    declared_scope: str
    processes: list[ProcessInfo] = Field(default_factory=list)


class DomainInfo(BaseModel):
    """One domain the runtime answers in, and who it answers as there."""

    name: str
    host: HostIdentity | None = None
    packs: list[str] = Field(default_factory=list)
    tenants: list[str] = Field(default_factory=list)
    default: bool = False


class Health(BaseModel):
    """Whether the runtime is up, and which catalog it is holding."""

    status: str
    skills: int
    fingerprint: str


@router.post("/sessions/{session_id}/turns", response_model=TurnResult)
def run_turn(session_id: str, body: TurnRequest, request: Request) -> TurnResult:
    """Run one turn of one session in the default domain, under the caller's identity."""
    return _turn(request, None, session_id, body.text)


@router.post("/domains/{domain}/sessions/{session_id}/turns", response_model=TurnResult)
def run_domain_turn(
    domain: str, session_id: str, body: TurnRequest, request: Request
) -> TurnResult:
    """Run one turn of one session in the named domain, under the caller's identity."""
    return _turn(request, domain, session_id, body.text)


@router.get("/domains", response_model=list[DomainInfo])
def list_domains(request: Request) -> list[DomainInfo]:
    """Every domain the runtime answers in, with its host.

    The tenants a domain serves are named only to a caller that may review:
    who else a deployment answers is not every caller's to learn.
    """
    named = may_review(_caller(request), _identity(request))
    platform = _platform(request)
    if platform is None:
        tenants = [DEFAULT_TENANT] if named else []
        return [DomainInfo(name=SINGLE_DOMAIN, tenants=tenants, default=True)]
    default = platform.domain().name
    return [
        DomainInfo(
            name=spec.name,
            host=spec.host,
            packs=list(spec.packs),
            tenants=list(spec.tenants) if named else [],
            default=spec.name == default,
        )
        for spec in platform.domains()
    ]


@router.get("/domains/{domain}/capabilities", response_model=CapabilityList)
def get_domain_capabilities(domain: str, request: Request) -> CapabilityList:
    """The procedures on offer in one domain, in the words its catalog declares."""
    return _capabilities(_services(request, domain))


@router.get("/sessions/{session_id}/receipts", response_model=list[WriteReceipt])
def list_receipts(session_id: str, request: Request) -> list[WriteReceipt]:
    """Every write receipt of one session of the default domain, oldest first."""
    return _receipts(request, None, session_id)


@router.get(
    "/domains/{domain}/sessions/{session_id}/receipts", response_model=list[WriteReceipt]
)
def list_domain_receipts(domain: str, session_id: str, request: Request) -> list[WriteReceipt]:
    """Every write receipt of one session of the named domain, oldest first."""
    return _receipts(request, domain, session_id)


def _receipts(request: Request, domain: str | None, session_id: str) -> list[WriteReceipt]:
    engine = _services(request, domain).engine
    _hold(engine, session_id, _caller(request))
    return engine.receipts(session_id)


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(request: Request) -> list[SkillSummary]:
    """The effective version of every catalog entry."""
    return [_summary(skill) for skill in _services(request).engine.registry.current()]


@router.get("/skills/{skill_id}", response_model=SkillDetail)
def get_skill(skill_id: str, request: Request) -> SkillDetail:
    """One catalog entry, frontmatter and sections."""
    skill = _services(request).engine.registry.get(skill_id)
    if skill is None:
        raise ApiError(404, "unknown_skill", f"no skill is declared as {skill_id}")
    return SkillDetail(
        frontmatter=skill.frontmatter.model_dump(mode="json"),
        sections=skill.sections.model_dump(mode="json"),
    )


@router.get("/capabilities", response_model=CapabilityList)
def get_capabilities(request: Request) -> CapabilityList:
    """The procedures on offer, in the words the catalog declares."""
    return _capabilities(_services(request))


def _capabilities(services: Services) -> CapabilityList:
    resolver = services.engine.resolver
    return CapabilityList(
        declared_scope=resolver.declared_scope(),
        processes=[
            ProcessInfo(
                id=skill.id,
                action=resolver.action_phrase(skill),
                action_on_selection=resolver.action_phrase(skill, on_selection=True),
                description=skill.frontmatter.description,
            )
            for skill in resolver.available_processes()
        ],
    )


@router.post("/proposals/from-session/{session_id}", response_model=SkillProposal)
def draft_proposal(session_id: str, request: Request) -> SkillProposal:
    """Draft a candidate procedure from what one session of the default domain did.

    Open to the session's own principal and to a reviewer.
    """
    return _draft(request, None, session_id)


@router.post("/domains/{domain}/proposals/from-session/{session_id}", response_model=SkillProposal)
def draft_domain_proposal(domain: str, session_id: str, request: Request) -> SkillProposal:
    """Draft a candidate procedure from what one session of the named domain did."""
    return _draft(request, domain, session_id)


def _draft(request: Request, domain: str | None, session_id: str) -> SkillProposal:
    """Draft from a session the caller may draft from, within one turn's model budget.

    A reviewer may draft from any session. Anyone else drafts only from a
    session they opened: a principal from the one it opened, and a call
    without an identity from one opened without one, in a single-user
    deployment only. A session with no history has nothing to draft from, and
    is answered the same whether or not it exists. The draft's model call is
    counted as a turn's would be, so a deployment whose turns may not call the
    model drafts from the recorded operations alone.
    """
    services = _services(request, domain)
    engine = services.engine
    principal = _caller(request)
    config = _identity(request)
    state = engine.state(session_id)
    if not is_reviewer(principal, config):
        _hold(engine, session_id, principal)
        if not state.principal:
            # Opened without an identity: nobody with one drafts from it as its own.
            if principal is not None:
                raise session_refused()
            if not config.single_user:
                raise identity_required()
    history = state.history
    if not history:
        raise ApiError(404, "nothing_to_draft", "there is no session history to draft from")
    planner = engine.planner
    with engine.budget.for_turn():
        provider = None if planner is None or engine.budget.exhausted else planner.provider
        proposal = draft_from_session(
            session_id,
            history,
            engine.receipts(session_id),
            engine.registry,
            provider,
            schema_dir=services.settings.schemas_dir,
        )
    services.proposals.save(proposal)
    return proposal


@router.get("/proposals", response_model=list[SkillProposal], dependencies=REVIEWER)
def list_proposals(request: Request) -> list[SkillProposal]:
    """Every proposal held, oldest file first."""
    return _services(request).proposals.list()


@router.get("/proposals/{proposal_id}", response_model=SkillProposal, dependencies=REVIEWER)
def get_proposal(proposal_id: str, request: Request) -> SkillProposal:
    """One proposal."""
    services = _services(request)
    try:
        return services.proposals.get(proposal_id)
    except KeyError as exc:
        raise ApiError(404, "unknown_proposal", str(exc)) from exc


@router.post(
    "/proposals/{proposal_id}/transition",
    response_model=TransitionResult,
    dependencies=REVIEWER,
)
def transition_proposal(
    proposal_id: str, body: TransitionRequest, request: Request
) -> TransitionResult:
    """Move one proposal to another state, recording who asked.

    A move to `published` composes a change request instead of writing a skill
    file, so the answer carries the request a person is now expected to read.
    """
    actor, source = _actor(request, body.by)
    services = _services(request)
    changes = _changes(request)
    runtime = _runtime(request)
    try:
        proposal = transition(
            services.proposals,
            proposal_id,
            body.to,
            actor,
            body.note,
            actor_source=source,
            registry=services.engine.registry,
            changes=changes,
            provider=(
                services.engine.planner.provider if services.engine.planner is not None else None
            ),
            enrichment=runtime.enrichment,
            languages=runtime.discovery.languages,
            schema_dir=services.settings.schemas_dir,
        )
    except KeyError as exc:
        raise ApiError(404, "unknown_proposal", str(exc)) from exc
    except LifecycleError as exc:
        raise ApiError(409, "illegal_transition", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, "unknown_state", str(exc)) from exc
    composed = [held for held in changes.list() if held.proposal_id == proposal.proposal_id]
    return TransitionResult(proposal=proposal, change_request=composed[-1] if composed else None)


@router.get("/changes", response_model=list[ChangeRequest], dependencies=REVIEWER)
def list_changes(request: Request) -> list[ChangeRequest]:
    """Every change request held, oldest first."""
    return _changes(request).list()


@router.get("/changes/{request_id}", response_model=ChangeRequest, dependencies=REVIEWER)
def get_change(request_id: str, request: Request) -> ChangeRequest:
    """One change request. Reading it is all this surface offers."""
    try:
        return _changes(request).get(request_id)
    except KeyError as exc:
        raise ApiError(404, "unknown_change_request", str(exc)) from exc


@router.get("/health", response_model=Health)
def get_health(request: Request) -> Health:
    """Whether the runtime is up, and which catalog it is holding."""
    registry = _services(request).engine.registry
    return Health(
        status="ok", skills=len(registry.current()), fingerprint=registry.fingerprint()
    )


def _turn(request: Request, domain: str | None, session_id: str, text: str) -> TurnResult:
    """Identity first, then the tenant, then the session's holder, then its domain."""
    principal = _caller(request)
    platform = _platform(request)
    with _refused():
        if platform is None:
            engine = _services(request, domain).engine
            _hold(engine, session_id, principal)
            return engine.turn(session_id, text, principal)
        tenant = _tenant(request, principal)
        _hold(platform.services(domain, tenant).engine, session_id, principal)
        return platform.turn(domain, tenant, session_id, text, principal)


def _caller(request: Request) -> Principal | None:
    """The identity this call carries, as `_identified` resolved it."""
    return getattr(request.state, "principal", None)


def _hold(engine: Any, session_id: str, principal: Principal | None) -> None:
    """Refuse a caller whose subject is not the one the session was opened by.

    A session opened without an identity is read by whoever arrives, and a
    turn that would bind an identity to it once it has begun is refused by
    the engine; one opened under an identity answers that subject only, and a
    caller with another subject or with none is refused the same way either
    way.
    """
    held = Principal.from_dict(engine.state(session_id).principal)
    if held is None:
        return
    if principal is None or principal.subject != held.subject:
        raise session_refused()


def _actor(request: Request, stated: str) -> tuple[str, ActorSource]:
    """Who a transition is recorded under, and where that name came from.

    A call reaches here without an identity only in a single-user deployment,
    the one place a stated name is taken, and recorded as stated.
    """
    principal = _caller(request)
    if principal is not None:
        return principal.subject, ActorSource.principal
    if not stated.strip():
        raise ApiError(
            422, "invalid_request", "by: a call without an identity names who is asking"
        )
    return stated.strip(), ActorSource.stated


def _identity(request: Request) -> IdentityConfig:
    return identity_config(request)


def _runtime(request: Request, domain: str | None = None) -> RuntimeConfig:
    """The runtime configuration of this call: its tenant's, on a platform.

    The tenant is checked against the ones the domain serves before its
    configuration is read, so a name a caller made up is refused, not kept.
    """
    platform = _platform(request)
    if platform is None:
        return application_runtime(request)
    with _refused():
        tenant = platform.tenant(domain, _tenant(request, _caller(request)))
        return platform.runtime_for(tenant)


def _tenant(request: Request, principal: Principal | None) -> str:
    """The tenant of this call: the proxy's own header when one is named, else the principal's.

    The header is read in proxy mode only, and only when the configuration
    names it: the proxy owns it, and no header a client could set is read
    without being named. When it is named it is the only source, and a tenant
    in the attributes header does not override it.
    """
    config = _identity(request)
    named = config.tenant_header.strip()
    if config.mode == "proxy" and named:
        return request.headers.get(named, "").strip() or DEFAULT_TENANT
    if principal is not None:
        claimed = principal.attributes.get(config.tenant_claim)
        if isinstance(claimed, str) and claimed.strip():
            return claimed.strip()
    return DEFAULT_TENANT


def _platform(request: Request) -> Platform | None:
    return getattr(request.app.state, "platform", None)


def _services(request: Request, domain: str | None = None) -> Any:
    """The service set this call acts on: the single catalog, or one (tenant, domain)."""
    platform = _platform(request)
    if platform is None:
        if domain not in (None, SINGLE_DOMAIN):
            raise ApiError(404, "unknown_domain", f"this runtime declares no domain {domain!r}")
        return request.app.state.services
    with _refused():
        return platform.services(domain, _tenant(request, _caller(request)))


@contextmanager
def _refused() -> Iterator[None]:
    """Turn what a platform refuses into the status and wording a caller reads.

    A session held by another subject and a session of another domain answer
    the same refusal, and neither repeats what the runtime said about it.
    """
    try:
        yield
    except (PrincipalMismatch, DomainMismatch):
        raise session_refused() from None
    except UnknownDomain as exc:
        raise ApiError(404, "unknown_domain", str(exc)) from exc
    except UnknownTenant as exc:
        raise ApiError(404, "unknown_tenant", str(exc)) from exc


def _changes(request: Request) -> ChangeRequestRecordStore:
    """The change request store of the runtime serving this request, read-only from here."""
    store: ChangeRequestRecordStore = _services(request).changes
    return store


def _summary(skill: Skill) -> SkillSummary:
    return SkillSummary(
        id=skill.id,
        type=skill.type.value,
        version=skill.version,
        status=skill.frontmatter.status.value,
        description=skill.frontmatter.description,
    )
