"""The turn loop: route, resolve, plan, execute under governance, answer.

One turn is one pass through this module. Everything it decides is written to
the session state and to the audit trail before the reply is returned, so a
turn that fails halfway still leaves a record of what was attempted.

Three things wrap that loop. The guardrail chain runs over what arrives before
anything is decided and over the reply before it is returned, so a promise the
catalog says is kept by code is kept here rather than in a prompt. The trace
sink times the stretches of the turn for whoever has to explain one later, and
records nothing at all unless a deployment asked for it. The model budget
bounds what one turn may spend: past it the model is not called again, the turn
says so and answers from what it already has. That count belongs to the turn and
not to the engine, because one engine serves overlapping turns.

A turn may arrive under an identity. The channel authenticates it and says who
it is; this module records it on the session the first time and refuses any
later turn that arrives as somebody else, because a session is one conversation
with one person and a write bound to an identity halfway through would be bound
to whichever one happened to be on that turn. A procedure that takes a field
from that identity is not offered to a session that has none: the value is not
the person's to give, so the turn is refused with that as the reason instead of
asking a question nobody should answer.

A fourth wraps it where the catalog is served as a revision. The turn opens on
the revision the session was last served by and reads the whole catalog through
it, so a procedure retired while someone holds a pending approval does not
disappear from the turn that offered it. Only a turn that starts a new task
takes the newest revision, and it says so in the audit trail. That pin belongs
to the turn for the same reason the budget does.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from opsdeck.audit.sink import AuditSink
from opsdeck.audit.trace import NullTraceSink, TraceSink, destination
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.config import RuntimeConfig
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.contracts.common import (
    Hydrated,
    Message,
    Outcome,
    PendingApproval,
    PendingGoal,
    PendingInfo,
    PolicyDecision,
    RouteDecision,
    TurnKind,
    TurnResult,
)
from opsdeck.contracts.plan import Plan
from opsdeck.contracts.receipt import WriteReceipt
from opsdeck.contracts.skill import Skill, SkillType
from opsdeck.engine import outcomes as build
from opsdeck.engine.execution import PlanExecutor, StopReason, TurnContext, approvals_required
from opsdeck.engine.goals import UNKNOWN_PROCEDURE, Answer, GoalRunner
from opsdeck.engine.reply import ReplyBuilder, guided
from opsdeck.engine.session_store import SessionStore
from opsdeck.engine.state import SessionState
from opsdeck.governance.approval import ApprovalGate
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.guardrails import (
    GuardrailCatalog,
    GuardrailChain,
    GuardrailOutcome,
)
from opsdeck.governance.identity import (
    Principal,
    PrincipalMismatch,
    declared_inputs,
    requires_principal,
)
from opsdeck.governance.ledger import WriteLedger
from opsdeck.governance.policy_engine import PolicyEngine
from opsdeck.governance.verification import Verifier
from opsdeck.index.discovery import Discovery
from opsdeck.planner.planner import Planner
from opsdeck.planner.provider import ModelProvider, ModelProviderError
from opsdeck.router import RoutingContext, TurnRouter

__all__ = ["BudgetExceeded", "BudgetedProvider", "Engine", "ModelBudget"]

_PAYLOAD_MOVED = "approval payload no longer matches the plan"
_CANCELLED_BY_REQUEST = "the approval waiting for an answer was cancelled by the new request"
_CANCELLED = "the request under way was dropped, and nothing ran"
_WITHDRAWN = "the write is no longer declared, so nothing ran"
_OVER_BUDGET = (
    "this turn reached the model calls it is allowed to make, so nothing further "
    "was planned or run"
)
_NO_PRINCIPAL = (
    "acts on the account of whoever is signed in, and nobody is signed in to this "
    "session, so nothing was planned; that account is not something to ask for"
)
_FIRST_TURN = "first_turn"
_NEW_TASK = "new_task"
_UNPINNED = "unpinned"


class BudgetExceeded(ModelProviderError):
    """Raised instead of calling the model once the turn has spent its budget."""


class _Spend:
    """What the turn under way has spent. One of these per turn, and no wider."""

    __slots__ = ("refused", "spent")

    def __init__(self) -> None:
        self.spent = 0
        self.refused = 0


_SPEND: ContextVar[_Spend | None] = ContextVar("opsdeck_model_spend", default=None)


class ModelBudget:
    """What one turn may spend on model calls, counted per turn and not per engine.

    One process holds one engine and serves many turns through it: a synchronous
    HTTP handler runs in a thread of a pool, and two turns overlap. So the limit
    belongs to the engine, which declares it once, and the count belongs to the
    turn, which is why it is held in a context variable a turn sets for as long
    as it runs. A worker thread, a task or a command each carry their own
    context and count their own turn; one context runs one turn at a time, so
    the wrapper around the provider still counts every call in one place.

    A call made where no turn is under way is charged to no turn. Drafting a
    candidate from a session that already finished is such a call: it is work of
    its own with its own boundary, and counting it against whatever turn last
    ran in this context would refuse it for a reason that has nothing to do
    with it.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(0, int(limit))

    @property
    def limit(self) -> int:
        """How many calls one turn may make."""
        return self._limit

    @property
    def spent(self) -> int:
        """How many calls this turn has made."""
        spend = _SPEND.get()
        return 0 if spend is None else spend.spent

    @property
    def refused(self) -> int:
        """How many calls this turn asked for past its budget."""
        spend = _SPEND.get()
        return 0 if spend is None else spend.refused

    @property
    def exhausted(self) -> bool:
        """Whether the next call of the turn under way would be refused."""
        spend = _SPEND.get()
        return spend is not None and spend.spent >= self._limit

    def start_turn(self) -> None:
        """Begin a turn with the whole budget, in this context and no other."""
        _SPEND.set(_Spend())

    @contextmanager
    def for_turn(self) -> Iterator[None]:
        """Count one turn's calls, and leave the context as the turn found it."""
        token = _SPEND.set(_Spend())
        try:
            yield
        finally:
            _SPEND.reset(token)

    def take(self) -> bool:
        """Book one call, or report that this turn has nothing left to spend."""
        spend = _SPEND.get()
        if spend is None:
            return True
        if spend.spent >= self._limit:
            spend.refused += 1
            return False
        spend.spent += 1
        return True

    def as_event(self) -> dict:
        """What the audit records about this turn's spend."""
        return {"limit": self._limit, "spent": self.spent, "refused": self.refused}


class BudgetedProvider:
    """A model provider that refuses a completion once the turn is over budget.

    Embeddings are not counted. They are how the index reads the catalog, they
    cost the same whatever the turn decides, and a turn that could not rank the
    catalog would have nothing to answer from. What the budget bounds is the
    reasoning: classification, planning, tie breaking and phrasing.
    """

    def __init__(self, provider: ModelProvider, budget: ModelBudget) -> None:
        self._provider = provider
        self._budget = budget
        self.name = provider.name

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Answer through the provider, unless this turn has spent its budget."""
        if not self._budget.take():
            raise BudgetExceeded(
                f"a turn may make {self._budget.limit} model call(s) and this one has "
                "made them all"
            )
        return self._provider.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed through the provider; embeddings are not part of the budget."""
        return self._provider.embed(texts)


class _TracedExecutor(PlanExecutor):
    """The plan executor, with a span around every stretch of execution."""

    def __init__(
        self,
        trace: TraceSink,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine,
        approval: ApprovalGate,
        ledger: WriteLedger,
        verifier: Verifier,
        connectors: ConnectorRegistry,
    ) -> None:
        super().__init__(capabilities, policy, approval, ledger, verifier, connectors)
        self._trace = trace

    def run(
        self,
        ctx: TurnContext,
        runbook: Skill,
        hydrated: Hydrated,
        plan: Plan,
        start: int = 0,
    ) -> StopReason:
        """Run the plan, timed."""
        with self._trace.span("execute", runbook=runbook.id, start=start) as span:
            stop = super().run(ctx, runbook, hydrated, plan, start)
            span.set(stop=stop)
            return stop

    def execute_approved(
        self,
        ctx: TurnContext,
        hydrated: Hydrated,
        pending: PendingApproval,
    ) -> Outcome:
        """Run the approved write, timed."""
        with self._trace.span(
            "execute", operation=pending.operation, approved=True
        ) as span:
            outcome = super().execute_approved(ctx, hydrated, pending)
            span.set(outcome=outcome.kind)
            return outcome


class _GuidedPlanner(Planner):
    """The planner, with the promises of the procedure in its prompt path.

    Prompts are built by the cognition layer out of the catalog and nothing
    here edits one. What is added is the block of instruction guardrails the
    hydrated skills declare, handed over as a system message, so a promise that
    is only ever text reaches the model the same way every time and a promise
    kept by code stays out of the prompt entirely.

    What is taken away is the obligation to fill a field the runtime fills. A
    field the procedure binds to the authenticated identity is not the model's
    to supply, so the closure the planner reads is a copy in which no operation
    requires it: the model plans the operation without it, the plan is judged as
    the model was asked to write it, and the runtime completes the payload
    before anything is decided about it. The catalog itself is not touched, and
    the executor runs against the declarations as they are written.
    """

    def __init__(
        self,
        planner: Planner,
        chain: GuardrailChain,
        registry: SkillRegistry,
        resolver: CapabilityResolver,
        trace: TraceSink,
    ) -> None:
        super().__init__(planner.provider, registry, resolver)
        self._chain = chain
        self._trace = trace

    def plan(
        self,
        hydrated: Hydrated,
        conversation: list[Message],
        collected_inputs: dict | None = None,
    ) -> Plan:
        """Plan the procedure, with what it promises stated in the prompt."""
        prose = self._chain.instructions(_declaring(hydrated), "planning")
        with self._trace.span("plan", runbook=hydrated.runbook.id) as span:
            span.set(guardrails=len(prose))
            return super().plan(
                _without_identity_fields(hydrated),
                guided(conversation, prose),
                collected_inputs,
            )


def _without_identity_fields(hydrated: Hydrated) -> Hydrated:
    """The same closure, with the identity's fields required of nobody.

    A schema that requires a field the model may not write would refuse every
    plan the model can honestly produce. The copy relaxes exactly those fields
    and nothing else: they stay in the schema, so a value the model supplies
    anyway is still a field this operation accepts and is still refused later
    for being the model's.
    """
    names = set(declared_inputs([hydrated.runbook, *hydrated.runbooks]))
    if not names:
        return hydrated
    integrations: list[Skill] = []
    for integration in hydrated.integrations:
        copy = integration.model_copy(deep=True)
        for spec in copy.frontmatter.operations:
            required = spec.input_schema.get("required")
            if isinstance(required, list):
                spec.input_schema["required"] = [
                    field for field in required if field not in names
                ]
        integrations.append(copy)
    return hydrated.model_copy(update={"integrations": integrations})


def _declaring(hydrated: Hydrated) -> list[Skill]:
    """Every skill of a closure, each of which may declare guardrails."""
    return [
        hydrated.runbook,
        *hydrated.runbooks,
        *hydrated.entities,
        *hydrated.integrations,
    ]


class Engine:
    """Runs one turn of one session."""

    def __init__(
        self,
        registry: SkillRegistry,
        resolver: CapabilityResolver,
        discovery: Discovery,
        router: TurnRouter,
        planner: Planner | None,
        connectors: ConnectorRegistry,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine,
        approval: ApprovalGate,
        ledger: WriteLedger,
        verifier: Verifier,
        session_store: SessionStore,
        audit: AuditSink,
        runtime_config: RuntimeConfig,
        *,
        guardrails: GuardrailCatalog | None = None,
        trace: TraceSink | None = None,
        budget: ModelBudget | None = None,
        preferences_root: Path | None = None,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._discovery = discovery
        self._router = router
        self._planner = planner
        self._connectors = connectors
        self._capabilities = capabilities
        self._policy = policy
        self._approval = approval
        self._ledger = ledger
        self._sessions = session_store
        self._audit = audit
        self._runtime = runtime_config
        self._trace: TraceSink = trace or NullTraceSink()
        self._traced = trace is not None
        self._guardrails = guardrails or GuardrailCatalog.empty()
        self._chain = GuardrailChain(
            self._guardrails, enabled=runtime_config.guardrails.enabled
        )
        self._budget = budget or ModelBudget(runtime_config.turn.max_model_calls)
        self._executor = _TracedExecutor(
            self._trace, capabilities, policy, approval, ledger, verifier, connectors
        )
        guided_planner = (
            None
            if planner is None
            else _GuidedPlanner(planner, self._chain, registry, resolver, self._trace)
        )
        self._goals = GoalRunner(registry, resolver, guided_planner, self._executor)
        self._reply = ReplyBuilder(
            planner, resolver, runtime_config.reply, preferences_root=preferences_root
        )

    @property
    def registry(self) -> SkillRegistry:
        """The catalog this engine runs from."""
        return self._registry

    @property
    def resolver(self) -> CapabilityResolver:
        """The catalog wording this engine answers from."""
        return self._resolver

    @property
    def planner(self) -> Planner | None:
        """The model-backed planner, when a provider is configured."""
        return self._planner

    @property
    def connectors(self) -> ConnectorRegistry:
        """The connectors this engine runs operations through."""
        return self._connectors

    @property
    def guardrails(self) -> GuardrailCatalog:
        """The guardrail catalog this engine keeps."""
        return self._guardrails

    @property
    def budget(self) -> ModelBudget:
        """What the turn under way may still spend on model calls."""
        return self._budget

    def state(self, session_id: str) -> SessionState:
        """The stored state of a session."""
        return self._sessions.load(session_id)

    def receipts(self, session_id: str) -> list[WriteReceipt]:
        """Every write receipt of a session, oldest first."""
        return self._ledger.for_session(session_id)

    def turn(
        self,
        session_id: str,
        text: str,
        principal: Principal | None = None,
    ) -> TurnResult:
        """Run one turn and answer it, through the catalog revision it is served by.

        `principal` is what the channel authenticated. It is recorded on the
        session the first time one arrives, and a turn that arrives as somebody
        else raises rather than running.
        """
        state = self._sessions.load(session_id)
        self._bind(state, principal)
        ctx = TurnContext(state=state, turn_id=uuid.uuid4().hex, audit=self._audit)
        revision, reason = self._revision_for(state)
        with self._budget.for_turn(), self._registry.pinned(revision):
            try:
                with self._trace.span("turn", session=session_id, turn=state.turn_count + 1):
                    return self._run(ctx, text, revision, reason)
            finally:
                self._sessions.save(state)

    @staticmethod
    def _bind(state: SessionState, principal: Principal | None) -> None:
        """Hold the session to one identity, and refuse a turn that arrives as another.

        A session that was opened without one stays open to whoever arrives
        first. A session that holds one is that person's: a turn carrying a
        different subject, or carrying none at all, is not a turn of this
        conversation and nothing of it is run or recorded.
        """
        held = Principal.from_dict(state.principal)
        if held is None:
            if principal is not None:
                state.principal = principal.as_dict()
            return
        if principal is None:
            raise PrincipalMismatch(
                f"this session was opened by {held.subject} and this turn carries no identity"
            )
        if principal.subject != held.subject:
            raise PrincipalMismatch(
                f"this session was opened by {held.subject} and this turn arrives as "
                f"{principal.subject}"
            )
        state.principal = principal.as_dict()

    def _revision_for(self, state: SessionState) -> tuple[str, str]:
        """The revision this turn opens on, and why it is that one.

        A session that has never been served takes what the deployment serves
        now. A pinned session keeps what it was served by until a turn starts a
        new task, and only a turn that starts one adopts a newer revision.

        A deployment that does not pin its sessions still holds one to its
        revision while something of the previous turn is waiting under it: an
        approval, or a goal that stopped to ask a question. Both were composed
        from that revision — the sentence a person is about to answer, and the
        procedure they are answering it for — and finishing either under a
        newer catalog would finish something other than what they were shown.
        Where pinning is off and nothing is waiting, the turn takes the newest
        revision whether or not it starts a new task.
        """
        served = self._registry.newest_revision()
        if not state.revision:
            return served, _FIRST_TURN
        if self._runtime.catalog.pin_sessions or _waiting(state):
            return state.revision, ""
        return (served or state.revision), _UNPINNED

    def _record_revision(self, ctx: TurnContext, revision: str, reason: str) -> None:
        """Note on the session which revision it is served by, when that changes."""
        state = ctx.state
        if not revision or revision == state.revision:
            return
        previous = state.revision
        state.revision = revision
        ctx.emit("revision", {"from": previous, "to": revision, "reason": reason})

    def _adopt_newest(self, ctx: TurnContext) -> None:
        """Start a new task on the newest catalog this deployment serves.

        The turn was routed through the revision the session held, and the
        procedure that routing named is looked up again below; a revision that
        no longer publishes it answers from the declared scope instead, which is
        what a retired procedure should do to a request that arrives after it.
        """
        newest = self._registry.newest_revision()
        if not newest or newest == ctx.state.revision:
            return
        self._record_revision(ctx, newest, _NEW_TASK)
        self._registry.adopt(newest)

    def _run(
        self, ctx: TurnContext, text: str, revision: str = "", reason: str = ""
    ) -> TurnResult:
        state = ctx.state
        state.turn_count += 1
        state.language = state.language or self._runtime.reply.default_language
        self._record_revision(ctx, revision, reason)
        guarded = self._chain.apply_input(text)
        opened = Principal.from_dict(state.principal)
        ctx.emit(
            "turn",
            {
                "turn": state.turn_count,
                "text": guarded.text,
                **({"principal": opened.subject} if opened is not None else {}),
            },
        )
        if self._traced:
            ctx.emit("trace", {"recording": True, "where": destination(self._trace)})
        state.history.append(Message(role="user", content=guarded.text))
        if not guarded.ok:
            return self._refused_at_the_boundary(ctx, guarded)
        decision = self._route(ctx, guarded.text)
        try:
            answer = self._dispatch(ctx, decision, guarded.text)
        except BudgetExceeded:
            answer = self._over_budget(ctx)
        with self._trace.span("reply", kind=answer.kind):
            reply = self._reply.build(
                answer.kind,
                state,
                ctx.outcomes,
                answer.pending,
                answer.suggestions,
                answer.reply_intent,
                state.language,
                instructions=self._chain.instructions(self._hydrated(ctx), "output"),
            )
        reply = self._guard_output(ctx, reply)
        state.history.append(Message(role="assistant", content=reply))
        if self._budget.refused:
            ctx.emit("budget", self._budget.as_event())
        pending_kind = answer.pending.kind if answer.pending else None
        ctx.emit("reply", {"kind": answer.kind, "pending": pending_kind})
        return TurnResult(
            reply=reply, outcomes=ctx.outcomes, pending=answer.pending, events=ctx.events
        )

    def _refused_at_the_boundary(
        self, ctx: TurnContext, guarded: GuardrailOutcome
    ) -> TurnResult:
        """End a turn a guardrail refused, before anything was routed or planned.

        Nothing was decided, so nothing the session was already waiting for is
        touched; the turn answers with what the guardrail itself says and the
        record names which one acted.
        """
        state = ctx.state
        ctx.emit("guardrail", guarded.as_event("input"))
        ctx.record(
            Outcome(
                kind="denied",
                data={"guardrail_id": guarded.guardrail_id, "point": "input"},
                reason=guarded.reason,
            )
        )
        reply = self._guard_output(ctx, guarded.description)
        state.history.append(Message(role="assistant", content=reply))
        pending = (
            PendingInfo(kind="approval", approval=state.pending_approval)
            if state.pending_approval is not None
            else None
        )
        ctx.emit("reply", {"kind": "guardrail", "pending": pending.kind if pending else None})
        return TurnResult(
            reply=reply, outcomes=ctx.outcomes, pending=pending, events=ctx.events
        )

    def _over_budget(self, ctx: TurnContext) -> Answer:
        """What a turn answers with once it may not call the model again."""
        ctx.state.pending_goal = None
        ctx.record(build.cancelled_outcome(_OVER_BUDGET))
        return Answer("outcomes", reply_intent=_OVER_BUDGET)

    def _guard_output(self, ctx: TurnContext, reply: str) -> str:
        """Run the output guardrails over the reply, before anyone reads it."""
        guarded = self._chain.apply_output(reply)
        if guarded.action != "passed":
            ctx.emit("guardrail", guarded.as_event("output"))
        return guarded.description if not guarded.ok else guarded.text

    def _hydrated(self, ctx: TurnContext) -> list[Skill]:
        """The skills this turn hydrated, read back from the record the turn wrote."""
        ids: list[str] = []
        for event in ctx.events:
            if event.kind != "hydrate":
                continue
            data = event.data
            named = [
                data.get("runbook_id"),
                *(data.get("entities") or []),
                *(data.get("integrations") or []),
            ]
            ids.extend(
                value for value in named if isinstance(value, str) and value not in ids
            )
        found = [self._registry.get(skill_id) for skill_id in ids]
        return [skill for skill in found if skill is not None]

    def _route(self, ctx: TurnContext, text: str) -> RouteDecision:
        state = ctx.state
        with self._trace.span("route") as span:
            decision = self._router.route(
                text,
                RoutingContext(
                    pending_goal=state.pending_goal is not None,
                    pending_approval=state.pending_approval is not None,
                    language=state.language,
                ),
            )
            span.set(kind=decision.kind.value, source=decision.source)
        if decision.language:
            state.language = decision.language
        ctx.emit(
            "route",
            {
                "kind": decision.kind.value,
                "runbook_id": decision.runbook_id,
                "confidence": decision.confidence,
                "source": decision.source,
                "language": decision.language,
            },
        )
        return decision

    def _dispatch(self, ctx: TurnContext, decision: RouteDecision, text: str) -> Answer:
        state = ctx.state
        kind = decision.kind
        if kind is TurnKind.greeting:
            return Answer("greeting")
        if kind is TurnKind.off_scope:
            return Answer("off_scope")
        if kind is TurnKind.cancel_task:
            return self._cancel(ctx)
        if kind is TurnKind.approve:
            return self._approve(ctx)
        if kind is TurnKind.reject:
            return self._reject(ctx)
        answers_a_question = kind in (TurnKind.provide_input, TurnKind.continue_task)
        if answers_a_question and state.pending_goal is not None and not state.pending_approval:
            return self._continue(ctx, text)
        return self._new_task(ctx, decision, text)

    def _cancel(self, ctx: TurnContext) -> Answer:
        state = ctx.state
        pending = state.pending_approval
        if pending is not None:
            ctx.emit(
                "approval_decided",
                {"approval_id": pending.approval_id, "decision": "cancelled"},
            )
        ctx.record(build.cancelled_outcome(_CANCELLED, pending))
        state.pending_approval = None
        state.pending_goal = None
        return Answer("outcomes", reply_intent="the request was cancelled")

    def _reject(self, ctx: TurnContext) -> Answer:
        state = ctx.state
        pending = state.pending_approval
        if pending is None:
            return Answer("off_scope")
        ctx.emit(
            "approval_decided",
            {"approval_id": pending.approval_id, "decision": "rejected"},
        )
        ctx.record(build.rejected_outcome(pending))
        state.pending_approval = None
        state.pending_goal = None
        return Answer("outcomes", reply_intent="the write was rejected and nothing ran")

    def _approve(self, ctx: TurnContext) -> Answer:
        state = ctx.state
        pending = state.pending_approval
        if pending is None:
            return Answer("off_scope")
        planned = _planned_payload(pending)
        if (
            planned is None
            or not self._approval.check(pending, planned)
            or not self._approval.check(pending, pending.exact_payload)
        ):
            ctx.emit(
                "approval_decided",
                {
                    "approval_id": pending.approval_id,
                    "decision": "rejected",
                    "reason": _PAYLOAD_MOVED,
                },
            )
            ctx.record(build.rejected_outcome(pending, _PAYLOAD_MOVED))
            state.pending_approval = None
            state.pending_goal = None
            return Answer("outcomes", reply_intent="the approval no longer matches the request")
        pending.approvals_given += 1
        ctx.emit(
            "approval_decided",
            {
                "approval_id": pending.approval_id,
                "decision": "approved",
                "approvals_given": pending.approvals_given,
                "approvals_required": pending.approvals_required,
            },
        )
        if pending.approvals_given < pending.approvals_required:
            ctx.record(build.approval_pending_outcome(pending))
            return Answer(
                "approval_request", pending=PendingInfo(kind="approval", approval=pending)
            )
        return self._execute_approved(ctx, pending)

    def _execute_approved(self, ctx: TurnContext, pending: PendingApproval) -> Answer:
        """Run the approved write against current state, then finish its plan."""
        state = ctx.state
        runbook = self._registry.get(pending.runbook_id)
        if runbook is None:
            state.pending_approval = None
            state.pending_goal = None
            ctx.record(build.invalid_plan_outcome(pending.runbook_id, [UNKNOWN_PROCEDURE]))
            return Answer("outcomes", reply_intent="the procedure is no longer available")
        hydrated = self._registry.closure(runbook.id)
        withdrawn = self._executor.undeclared_write(runbook, pending)
        if withdrawn is not None:
            state.pending_approval = None
            state.pending_goal = None
            ctx.record(build.invalid_plan_outcome(pending.runbook_id, [withdrawn]))
            return Answer("outcomes", reply_intent=_WITHDRAWN)
        self._executor.refresh_context(ctx, hydrated, pending.plan, pending.op_index)
        decision = self._policy.evaluate(
            runbook,
            pending.operation,
            pending.exact_payload,
            ctx.entities,
            state.session_id,
        )
        ctx.emit(
            "policy",
            {
                "skill_id": runbook.id,
                "operation": pending.operation,
                "effect": decision.effect,
                "reason": decision.reason,
                "stage": "approval",
            },
        )
        state.pending_approval = None
        refusal = self._refusal(decision, pending)
        if refusal is not None:
            ctx.record(
                build.denied_outcome(
                    pending.integration_id, pending.operation, decision, refusal
                )
            )
            state.pending_goal = None
            return Answer("outcomes", reply_intent="the write was refused by the policy")
        self._executor.execute_approved(ctx, hydrated, pending)
        stop = self._executor.run(ctx, runbook, hydrated, pending.plan, pending.op_index + 1)
        intent = pending.plan.reply_intent
        if stop != "completed":
            return self._goals.stopped(ctx, stop, intent)
        goal = state.pending_goal
        if goal is None:
            return Answer("outcomes", reply_intent=intent)
        goal.step_index += 1
        return self._goals.advance(ctx, intent)

    def _refusal(self, decision: PolicyDecision, pending: PendingApproval) -> str | None:
        """Why the write may not run now, or nothing when the approval still covers it.

        The state read before the write can carry the operation under a rule
        that was not the one the approval was asked under. A rule that refuses
        it stops it; so does a rule that now asks for more approvals than were
        given, because what stands was given for a lighter decision.
        """
        if decision.effect == "deny":
            return self._policy.explain(decision)
        required = approvals_required(decision.effect)
        if pending.approvals_given < required:
            return (
                f"{self._policy.explain(decision)} The state read before the write asks "
                f"for {required} approval(s) and {pending.approvals_given} stand, so "
                "nothing ran."
            )
        return None

    def _new_task(self, ctx: TurnContext, decision: RouteDecision, text: str) -> Answer:
        state = ctx.state
        pending = state.pending_approval
        if pending is not None:
            ctx.emit(
                "approval_decided",
                {"approval_id": pending.approval_id, "decision": "cancelled"},
            )
            ctx.record(build.cancelled_outcome(_CANCELLED_BY_REQUEST, pending))
            state.pending_approval = None
        self._adopt_newest(ctx)
        runbook_id = decision.runbook_id or self._choose(ctx, text)
        skill = self._registry.get(runbook_id) if runbook_id else None
        if skill is None or skill.type not in (SkillType.runbook, SkillType.composite):
            state.pending_goal = None
            return Answer("off_scope")
        unsigned = self._unsigned(state, skill)
        if unsigned is not None:
            state.pending_goal = None
            ctx.record(build.invalid_plan_outcome(skill.id, [unsigned]))
            return Answer("outcomes", reply_intent=unsigned)
        state.pending_goal = PendingGoal(
            runbook_ids=_members(skill),
            step_index=0,
            collected_inputs={},
            missing=[],
            request_text=text,
        )
        return self._goals.advance(ctx)

    def _unsigned(self, state: SessionState, skill: Skill) -> str | None:
        """Why this procedure is not for a session with nobody signed in, or nothing.

        The procedures of a composite are read as well as the composite itself,
        because a step that binds a field to the identity binds the whole goal
        to it: the person would be asked for the request and refused at the
        step, which is worse than being refused at the start.
        """
        members = [self._registry.get(member) for member in _members(skill)]
        declaring = [held for held in (skill, *members) if held is not None]
        if not requires_principal(declaring):
            return None
        principal = Principal.from_dict(state.principal)
        if principal is not None and principal.authenticated:
            return None
        return f"{skill.id} {_NO_PRINCIPAL}"

    def _choose(self, ctx: TurnContext, text: str) -> str | None:
        with self._trace.span("discovery") as span:
            result = self._discovery.select(text)
            span.set(chosen=result.chosen, tie=result.tie)
        ctx.emit(
            "discovery",
            {
                "ranked": [item.model_dump(mode="json") for item in result.ranked],
                "chosen": result.chosen,
                "tie": result.tie,
            },
        )
        if result.tie and self._planner is not None and len(result.ranked) > 1:
            return self._planner.pick(text, result.ranked)
        return result.chosen

    def _continue(self, ctx: TurnContext, text: str) -> Answer:
        goal = ctx.state.pending_goal
        if goal is None:
            return Answer("off_scope")
        if goal.missing:
            goal.collected_inputs[goal.missing[0].field] = text
            goal.missing = goal.missing[1:]
        return self._goals.advance(ctx)


def _waiting(state: SessionState) -> bool:
    """Whether this session left something of an earlier turn to be answered."""
    return state.pending_approval is not None or state.pending_goal is not None


def _planned_payload(pending: PendingApproval) -> dict | None:
    """The payload the held plan carries for the operation the approval covers.

    The approval is frozen on a hash of what the user was shown, and the plan
    it was taken from is stored with the session. Both the frozen copy and the
    plan are checked against that hash before the write runs, so the two have
    to still agree with each other and with the sentence that was approved.
    """
    operations = pending.plan.operations
    if not 0 <= pending.op_index < len(operations):
        return None
    return operations[pending.op_index].payload


def _members(skill: Skill) -> list[str]:
    """A composite runs its runbooks in order; anything else runs itself."""
    if skill.type is SkillType.composite:
        return list(skill.frontmatter.runbooks)
    return [skill.id]
