"""The turn loop: route, resolve, plan, execute under governance, answer.

One turn is one pass through this module. Everything it decides is written to
the session state and to the audit trail before the reply is returned, so a
turn that fails halfway still leaves a record of what was attempted.
"""

from __future__ import annotations

import uuid

from opsdeck.audit.sink import AuditSink
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.config import RuntimeConfig
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.contracts.common import (
    Message,
    PendingApproval,
    PendingGoal,
    PendingInfo,
    PolicyDecision,
    RouteDecision,
    TurnKind,
    TurnResult,
)
from opsdeck.contracts.receipt import WriteReceipt
from opsdeck.contracts.skill import Skill, SkillType
from opsdeck.engine import outcomes as build
from opsdeck.engine.execution import PlanExecutor, TurnContext, approvals_required
from opsdeck.engine.goals import UNKNOWN_PROCEDURE, Answer, GoalRunner
from opsdeck.engine.reply import ReplyBuilder
from opsdeck.engine.session_store import SessionStore
from opsdeck.engine.state import SessionState
from opsdeck.governance.approval import ApprovalGate
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.ledger import WriteLedger
from opsdeck.governance.policy_engine import PolicyEngine
from opsdeck.governance.verification import Verifier
from opsdeck.index.discovery import Discovery
from opsdeck.planner.planner import Planner
from opsdeck.router import RoutingContext, TurnRouter

__all__ = ["Engine"]

_PAYLOAD_MOVED = "approval payload no longer matches the plan"
_CANCELLED_BY_REQUEST = "the approval waiting for an answer was cancelled by the new request"
_CANCELLED = "the request under way was dropped, and nothing ran"
_WITHDRAWN = "the write is no longer declared, so nothing ran"


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
        self._executor = PlanExecutor(
            capabilities, policy, approval, ledger, verifier, connectors
        )
        self._goals = GoalRunner(registry, resolver, planner, self._executor)
        self._reply = ReplyBuilder(planner, resolver, runtime_config.reply)

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

    def state(self, session_id: str) -> SessionState:
        """The stored state of a session."""
        return self._sessions.load(session_id)

    def receipts(self, session_id: str) -> list[WriteReceipt]:
        """Every write receipt of a session, oldest first."""
        return self._ledger.for_session(session_id)

    def turn(self, session_id: str, text: str) -> TurnResult:
        """Run one turn and answer it."""
        state = self._sessions.load(session_id)
        ctx = TurnContext(state=state, turn_id=uuid.uuid4().hex, audit=self._audit)
        try:
            return self._run(ctx, text)
        finally:
            self._sessions.save(state)

    def _run(self, ctx: TurnContext, text: str) -> TurnResult:
        state = ctx.state
        state.turn_count += 1
        state.language = state.language or self._runtime.reply.default_language
        ctx.emit("turn", {"turn": state.turn_count, "text": text})
        state.history.append(Message(role="user", content=text))
        decision = self._route(ctx, text)
        answer = self._dispatch(ctx, decision, text)
        reply = self._reply.build(
            answer.kind,
            state,
            ctx.outcomes,
            answer.pending,
            answer.suggestions,
            answer.reply_intent,
            state.language,
        )
        state.history.append(Message(role="assistant", content=reply))
        pending_kind = answer.pending.kind if answer.pending else None
        ctx.emit("reply", {"kind": answer.kind, "pending": pending_kind})
        return TurnResult(
            reply=reply, outcomes=ctx.outcomes, pending=answer.pending, events=ctx.events
        )

    def _route(self, ctx: TurnContext, text: str) -> RouteDecision:
        state = ctx.state
        decision = self._router.route(
            text,
            RoutingContext(
                pending_goal=state.pending_goal is not None,
                pending_approval=state.pending_approval is not None,
                language=state.language,
            ),
        )
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
        runbook_id = decision.runbook_id or self._choose(ctx, text)
        skill = self._registry.get(runbook_id) if runbook_id else None
        if skill is None or skill.type not in (SkillType.runbook, SkillType.composite):
            state.pending_goal = None
            return Answer("off_scope")
        state.pending_goal = PendingGoal(
            runbook_ids=_members(skill),
            step_index=0,
            collected_inputs={},
            missing=[],
            request_text=text,
        )
        return self._goals.advance(ctx)

    def _choose(self, ctx: TurnContext, text: str) -> str | None:
        result = self._discovery.select(text)
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
