"""The turn loop: route, resolve, plan, execute under governance, answer.

One turn is one pass through this module. Everything it decides is written to
the session state and to the audit trail before the reply is returned, so a
turn that fails halfway still leaves a record of what was attempted.

What wraps that loop lives in modules of its own. The guardrail chain runs over
what arrives and over the reply (`boundary`). The trace sink times the stretches
of the turn, and the planner is handed the promises of the procedure
(`collaborators`). The model budget bounds what one turn may spend (`budget`).
The session is held to the identity that opened it and to the catalog revision
that serves it (`binding`). An answer to a held write takes the approval path
(`approvals`).

Every turn ends by recording what it showed and what the conversation is now
about, as the subject module derives them from evidence, and a procedure that
joins the focus starts its goal with the object in focus already collected.
Where the serving domain enables presentation, the directives the procedures
that ran declare for the moment they reached are returned with the reply and
recorded on the reply event. A directive is derived from evidence and never
writes; with presentation off, a turn returns none.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path

from oska.audit.sink import AuditSink
from oska.audit.trace import NullTraceSink, TraceSink, destination
from oska.catalog.registry import Catalog
from oska.catalog.resolver import CapabilityResolver
from oska.config import RuntimeConfig
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.common import (
    Message,
    PendingGoal,
    RouteDecision,
    TurnKind,
    TurnResult,
)
from oska.contracts.platform import HostIdentity
from oska.contracts.receipt import WriteReceipt
from oska.contracts.skill import Skill, SkillType
from oska.engine import binding, subject
from oska.engine import outcomes as build
from oska.engine.approvals import ApprovalPath
from oska.engine.boundary import Boundary
from oska.engine.budget import BudgetedProvider, BudgetExceeded, ModelBudget
from oska.engine.collaborators import GuidedPlanner, TracedExecutor
from oska.engine.execution import TurnContext
from oska.engine.goals import Answer, GoalRunner
from oska.engine.phrases import Phrases
from oska.engine.presentation import Presenter, TurnNotes, reply_event_fields
from oska.engine.reply import ReplyBuilder
from oska.engine.state import SessionState
from oska.governance.approval import ApprovalGate
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.guardrails import GuardrailCatalog, GuardrailChain
from oska.governance.identity import Principal
from oska.governance.policy_engine import PolicyEngine
from oska.governance.verification import Verifier
from oska.index.discovery import Discovery
from oska.planner.planner import Planner
from oska.router import RoutingContext, TurnRouter
from oska.storage.protocols import LedgerStore, SessionStateStore

__all__ = ["BudgetExceeded", "BudgetedProvider", "Engine", "ModelBudget"]

_CANCELLED_BY_REQUEST = "the approval waiting for an answer was cancelled by the new request"
_CANCELLED = "the request under way was dropped, and nothing ran"
_OVER_BUDGET = (
    "this turn reached the model calls it is allowed to make, so nothing further "
    "was planned or run"
)


class Engine:
    """Runs one turn of one session."""

    def __init__(
        self,
        registry: Catalog,
        resolver: CapabilityResolver,
        discovery: Discovery,
        router: TurnRouter,
        planner: Planner | None,
        connectors: ConnectorRegistry,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine,
        approval: ApprovalGate,
        ledger: LedgerStore,
        verifier: Verifier,
        session_store: SessionStateStore,
        audit: AuditSink,
        runtime_config: RuntimeConfig,
        *,
        guardrails: GuardrailCatalog | None = None,
        trace: TraceSink | None = None,
        budget: ModelBudget | None = None,
        preferences_root: Path | None = None,
        presentation: bool = False,
        synonyms: Mapping[str, str] | None = None,
        host: HostIdentity | None = None,
        phrases: Phrases | None = None,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._discovery = discovery
        self._router = router
        self._planner = planner
        self._connectors = connectors
        self._capabilities = capabilities
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
        self._executor = TracedExecutor(
            self._trace, capabilities, policy, approval, ledger, verifier, connectors
        )
        guided_planner = (
            None
            if planner is None
            else GuidedPlanner(planner, self._chain, registry, resolver, self._trace)
        )
        self._goals = GoalRunner(registry, resolver, guided_planner, self._executor)
        self._approvals = ApprovalPath(approval, registry, self._executor, policy, self._goals)
        self._boundary = Boundary(self._chain, registry)
        self._phrases = phrases or Phrases.load(
            default_language=runtime_config.reply.default_language
        )
        self._reply = ReplyBuilder(
            planner,
            resolver,
            runtime_config.reply,
            preferences_root=preferences_root,
            host=host,
            phrases=self._phrases,
        )
        self._presenter = Presenter(registry, enabled=presentation)
        self._synonyms = dict(synonyms or {})

    @property
    def registry(self) -> Catalog:
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
    def phrases(self) -> Phrases:
        """The phrase catalog the runtime says its own sentences from."""
        return self._phrases

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

    @property
    def stores(self) -> tuple[LedgerStore, SessionStateStore]:
        """The write ledger and the session store this engine keeps its record in."""
        return self._ledger, self._sessions

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
        binding.bind(state, principal)
        ctx = TurnContext(state=state, turn_id=uuid.uuid4().hex, audit=self._audit)
        revision, reason = binding.revision_for(
            self._registry, self._runtime.catalog.pin_sessions, state
        )
        with self._budget.for_turn(), self._registry.pinned(revision):
            try:
                with self._trace.span("turn", session=session_id, turn=state.turn_count + 1):
                    return self._run(ctx, text, revision, reason)
            finally:
                self._sessions.save(state)

    def _run(
        self, ctx: TurnContext, text: str, revision: str = "", reason: str = ""
    ) -> TurnResult:
        state = ctx.state
        state.turn_count += 1
        state.language = state.language or self._runtime.reply.default_language
        binding.record_revision(ctx, revision, reason)
        guarded = self._boundary.guard_input(text)
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
            return self._boundary.refuse(ctx, guarded)
        decision = self._route(ctx, guarded.text)
        notes = TurnNotes()
        try:
            answer = self._dispatch(ctx, decision, guarded.text, notes)
        except BudgetExceeded:
            answer = self._over_budget(ctx)
        directives = self._presenter.settle(ctx, notes)
        with self._trace.span("reply", kind=answer.kind):
            reply = self._reply.build(
                answer.kind,
                state,
                ctx.outcomes,
                answer.pending,
                answer.suggestions,
                answer.reply_intent,
                state.language,
                instructions=self._boundary.output_instructions(ctx),
            )
        reply = self._boundary.guard_output(ctx, reply)
        state.history.append(Message(role="assistant", content=reply))
        if self._budget.refused:
            ctx.emit("budget", self._budget.as_event())
        fallbacks = self._budget.fallbacks()
        if fallbacks is not None:
            ctx.emit("model_fallback", fallbacks)
        pending_kind = answer.pending.kind if answer.pending else None
        ctx.emit(
            "reply",
            {"kind": answer.kind, "pending": pending_kind, **reply_event_fields(notes, directives)},
        )
        return TurnResult(
            reply=reply,
            outcomes=ctx.outcomes,
            pending=answer.pending,
            events=ctx.events,
            directives=directives,
        )

    def _over_budget(self, ctx: TurnContext) -> Answer:
        """What a turn answers with once it may not call the model again."""
        ctx.state.pending_goal = None
        ctx.record(build.cancelled_outcome(_OVER_BUDGET, phrase="cancelled_over_budget"))
        return Answer("outcomes", reply_intent=_OVER_BUDGET)

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
        routed = {
            "kind": decision.kind.value,
            "runbook_id": decision.runbook_id,
            "confidence": decision.confidence,
            "source": decision.source,
            "language": decision.language,
        }
        if decision.reason:
            routed["reason"] = decision.reason
        ctx.emit("route", routed)
        return decision

    def _dispatch(
        self, ctx: TurnContext, decision: RouteDecision, text: str, notes: TurnNotes
    ) -> Answer:
        state = ctx.state
        kind = decision.kind
        if kind is TurnKind.greeting:
            return Answer("greeting")
        if kind is TurnKind.off_scope:
            return Answer("off_scope")
        if kind is TurnKind.cancel_task:
            return self._cancel(ctx)
        if kind is TurnKind.approve:
            return self._approvals.approve(ctx, notes)
        if kind is TurnKind.reject:
            return self._approvals.reject(ctx)
        answers_a_question = kind in (TurnKind.provide_input, TurnKind.continue_task)
        if answers_a_question and state.pending_goal is not None and not state.pending_approval:
            return self._continue(ctx, text, notes)
        return self._new_task(ctx, decision, text, notes)

    def _cancel(self, ctx: TurnContext) -> Answer:
        state = ctx.state
        pending = state.pending_approval
        if pending is not None:
            ctx.emit(
                "approval_decided",
                {"approval_id": pending.approval_id, "decision": "cancelled"},
            )
        ctx.record(build.cancelled_outcome(_CANCELLED, pending, phrase="cancelled_dropped"))
        state.pending_approval = None
        state.pending_goal = None
        return Answer("outcomes", reply_intent="the request was cancelled")

    def _new_task(
        self, ctx: TurnContext, decision: RouteDecision, text: str, notes: TurnNotes
    ) -> Answer:
        state = ctx.state
        pending = state.pending_approval
        if pending is not None:
            ctx.emit(
                "approval_decided",
                {"approval_id": pending.approval_id, "decision": "cancelled"},
            )
            ctx.record(
                build.cancelled_outcome(
                    _CANCELLED_BY_REQUEST, pending, phrase="cancelled_by_request"
                )
            )
            state.pending_approval = None
        binding.adopt_newest(self._registry, ctx)
        runbook_id = decision.runbook_id or self._choose(ctx, text)
        skill = self._registry.get(runbook_id) if runbook_id else None
        if skill is None or skill.type not in (SkillType.runbook, SkillType.composite):
            state.pending_goal = None
            return Answer("off_scope")
        unsigned = binding.unsigned(self._registry, state, skill, _members(skill))
        if unsigned is not None:
            state.pending_goal = None
            ctx.record(build.invalid_plan_outcome(skill.id, [unsigned]))
            return Answer("outcomes", reply_intent=unsigned)
        members = [self._registry.get(member) for member in _members(skill)]
        joined, notes.selection, notes.joined = subject.join(
            [member for member in members if member is not None],
            self._registry,
            state,
            text,
            self._synonyms,
        )
        state.pending_goal = PendingGoal(
            runbook_ids=_members(skill),
            step_index=0,
            collected_inputs=joined,
            missing=[],
            request_text=text,
        )
        notes.goal = state.pending_goal
        return self._goals.advance(ctx)

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

    def _continue(self, ctx: TurnContext, text: str, notes: TurnNotes) -> Answer:
        goal = ctx.state.pending_goal
        if goal is None:
            return Answer("off_scope")
        notes.goal = goal
        if goal.missing:
            goal.collected_inputs[goal.missing[0].field] = text
            goal.missing = goal.missing[1:]
        return self._goals.advance(ctx)


def _members(skill: Skill) -> list[str]:
    """A composite runs its runbooks in order; anything else runs itself."""
    if skill.type is SkillType.composite:
        return list(skill.frontmatter.runbooks)
    return [skill.id]
