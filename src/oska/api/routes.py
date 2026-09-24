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

A turn may carry an identity. It arrives in headers an authenticated proxy sets:
the subject of whoever is signed in, and whatever else about them the deployment
puts on the call. The runtime trusts those headers and verifies nothing itself —
it holds no key, checks no signature and knows no directory — so a deployment
that lets anything but its proxy reach this port has handed every session
whatever identity the caller typed. Refusing a turn whose identity contradicts
the session is the only check made here, and it is a check about consistency,
not about authenticity.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from oska.config import load_runtime_config
from oska.contracts.common import TurnResult
from oska.contracts.proposal import SkillProposal
from oska.contracts.receipt import WriteReceipt
from oska.contracts.skill import Skill
from oska.governance.identity import Principal, PrincipalMismatch
from oska.learning import (
    ChangeRequest,
    ChangeRequestStore,
    LifecycleError,
    draft_from_session,
    store_for,
    transition,
)

__all__ = [
    "ATTRIBUTES_HEADER",
    "SUBJECT_HEADER",
    "ApiError",
    "CapabilityList",
    "ProcessInfo",
    "SkillDetail",
    "SkillSummary",
    "TransitionRequest",
    "TransitionResult",
    "TurnRequest",
    "router",
]

router = APIRouter()

SUBJECT_HEADER = "X-Principal-Subject"
ATTRIBUTES_HEADER = "X-Principal-Attributes"


class ApiError(Exception):
    """An error with the status and the wording the response carries."""

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail


class TurnRequest(BaseModel):
    """One thing a person typed."""

    text: str


class TransitionRequest(BaseModel):
    """A request to move a proposal to another state."""

    to: str
    by: str
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


class Health(BaseModel):
    """Whether the runtime is up, and which catalog it is holding."""

    status: str
    skills: int
    fingerprint: str


@router.post("/sessions/{session_id}/turns", response_model=TurnResult)
def run_turn(session_id: str, body: TurnRequest, request: Request) -> TurnResult:
    """Run one turn of one session, under the identity the proxy put on the call."""
    try:
        return _services(request).engine.turn(session_id, body.text, _principal(request))
    except PrincipalMismatch as exc:
        raise ApiError(409, "principal_mismatch", str(exc)) from exc


@router.get("/sessions/{session_id}/receipts", response_model=list[WriteReceipt])
def list_receipts(session_id: str, request: Request) -> list[WriteReceipt]:
    """Every write receipt of one session, oldest first."""
    return _services(request).engine.receipts(session_id)


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
    resolver = _services(request).engine.resolver
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
    """Draft a candidate procedure from what one session did."""
    services = _services(request)
    engine = services.engine
    planner = engine.planner
    proposal = draft_from_session(
        session_id,
        engine.state(session_id).history,
        engine.receipts(session_id),
        engine.registry,
        None if planner is None else planner.provider,
        schema_dir=services.settings.schemas_dir,
    )
    services.proposals.save(proposal)
    return proposal


@router.get("/proposals", response_model=list[SkillProposal])
def list_proposals(request: Request) -> list[SkillProposal]:
    """Every proposal held, oldest file first."""
    return _services(request).proposals.list()


@router.get("/proposals/{proposal_id}", response_model=SkillProposal)
def get_proposal(proposal_id: str, request: Request) -> SkillProposal:
    """One proposal."""
    services = _services(request)
    try:
        return services.proposals.get(proposal_id)
    except KeyError as exc:
        raise ApiError(404, "unknown_proposal", str(exc)) from exc


@router.post("/proposals/{proposal_id}/transition", response_model=TransitionResult)
def transition_proposal(
    proposal_id: str, body: TransitionRequest, request: Request
) -> TransitionResult:
    """Move one proposal to another state, recording who asked.

    A move to `published` composes a change request instead of writing a skill
    file, so the answer carries the request a person is now expected to read.
    """
    services = _services(request)
    changes = _changes(request)
    try:
        proposal = transition(
            services.proposals,
            proposal_id,
            body.to,
            body.by,
            body.note,
            registry=services.engine.registry,
            changes=changes,
            provider=(
                services.engine.planner.provider if services.engine.planner is not None else None
            ),
            enrichment=load_runtime_config(settings=services.settings).enrichment,
            languages=load_runtime_config(settings=services.settings).discovery.languages,
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


@router.get("/changes", response_model=list[ChangeRequest])
def list_changes(request: Request) -> list[ChangeRequest]:
    """Every change request held, oldest first."""
    return _changes(request).list()


@router.get("/changes/{request_id}", response_model=ChangeRequest)
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


def _principal(request: Request) -> Principal | None:
    """The identity the proxy put on this call, or nothing when it put none.

    A subject alone is an identity; the attributes header is optional and is a
    JSON object when it is there. Both are taken as given: this is the point
    where a value the runtime did not establish becomes the value a write is
    bound to, and it is worth saying that in one place rather than implying it
    everywhere.
    """
    subject = request.headers.get(SUBJECT_HEADER, "").strip()
    if not subject:
        return None
    raw = request.headers.get(ATTRIBUTES_HEADER, "").strip()
    attributes: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ApiError(
                400, "invalid_principal", f"{ATTRIBUTES_HEADER} is not JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise ApiError(
                400, "invalid_principal", f"{ATTRIBUTES_HEADER} is not a JSON object"
            )
        attributes = parsed
    return Principal(subject=subject, attributes=attributes, authenticated=True)


def _services(request: Request) -> Any:
    return request.app.state.services


def _changes(request: Request) -> ChangeRequestStore:
    """The change request store these settings configure, read-only from here."""
    return store_for(_services(request).settings)


def _summary(skill: Skill) -> SkillSummary:
    return SkillSummary(
        id=skill.id,
        type=skill.type.value,
        version=skill.version,
        status=skill.frontmatter.status.value,
        description=skill.frontmatter.description,
    )
