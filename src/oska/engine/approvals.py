"""The approval path: from an answer to a held write, to the write run once, or refused.

An approval is frozen on a hash of what the person was shown, and both the
frozen payload and the plan it came from are checked against that hash before
anything runs. The declarations are read again, so a write withdrawn since the
question was asked does not run on the strength of an old answer. What the plan
read before the write is read again, and the policy is evaluated again against
that current state: a rule that now refuses the write stops it, and so does one
that now asks for more approvals than were given. When any of those reads does
not come back, the state is not confirmed and nothing is sent, because a policy
judging without it would silently stop matching the rules conditioned on it.
The write itself is sent by
the executor, which checks the lifecycle move against the state read now and
takes the ledger's claim before the call goes out. The plan and the goal go on
only past a write that was performed and confirmed by its read-back.
"""

from __future__ import annotations

from oska.catalog.registry import SkillRegistry
from oska.contracts.common import PendingApproval, PendingInfo, PolicyDecision
from oska.engine import outcomes as build
from oska.engine.execution import PlanExecutor, TurnContext, approvals_required
from oska.engine.goals import (
    UNKNOWN_PROCEDURE,
    Answer,
    GoalRunner,
    landed,
    stopped_short,
    unlanded,
)
from oska.engine.presentation import TurnNotes
from oska.governance.approval import ApprovalGate
from oska.governance.policy_engine import PolicyEngine

__all__ = ["ApprovalPath"]

_PAYLOAD_MOVED = "approval payload no longer matches the plan"
_WITHDRAWN = "the write is no longer declared, so nothing ran"
_UNCONFIRMED = "the state the write is judged on could not be confirmed, so nothing was sent"


class ApprovalPath:
    """Answers a turn that approves or rejects the write the session holds."""

    def __init__(
        self,
        approval: ApprovalGate,
        registry: SkillRegistry,
        executor: PlanExecutor,
        policy: PolicyEngine,
        goals: GoalRunner,
    ) -> None:
        self._approval = approval
        self._registry = registry
        self._executor = executor
        self._policy = policy
        self._goals = goals

    def reject(self, ctx: TurnContext) -> Answer:
        """Drop the write waiting for an answer, and say that nothing ran."""
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

    def approve(self, ctx: TurnContext, notes: TurnNotes) -> Answer:
        """Count one approval, and run the write once every approval it needs stands."""
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
        return self._execute_approved(ctx, pending, notes)

    def _execute_approved(
        self, ctx: TurnContext, pending: PendingApproval, notes: TurnNotes
    ) -> Answer:
        """Run the approved write against current state, then finish its plan.

        The plan and the goal go on only past a write that was performed and
        confirmed by its read-back; any other outcome ends the goal there.
        """
        state = ctx.state
        notes.approved = pending.runbook_id
        notes.goal = state.pending_goal
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
        unconfirmed = self._executor.refresh_context(ctx, hydrated, pending)
        if unconfirmed is not None:
            state.pending_approval = None
            state.pending_goal = None
            ctx.record(unconfirmed)
            return Answer("outcomes", reply_intent=_UNCONFIRMED)
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
        written = self._executor.execute_approved(ctx, hydrated, pending)
        intent = pending.plan.reply_intent
        if not landed(written):
            return self._stopped_at_the_write(ctx, pending, intent)
        before = len(ctx.outcomes)
        stop = self._executor.run(ctx, runbook, hydrated, pending.plan, pending.op_index + 1)
        if stop != "completed":
            return self._goals.stopped(ctx, stop, intent)
        if unlanded(ctx.outcomes[before:]):
            return self._stopped_at_the_write(ctx, pending, intent, finished=True)
        goal = state.pending_goal
        if goal is None:
            return Answer("outcomes", reply_intent=intent)
        goal.step_index += 1
        return self._goals.advance(ctx, intent)

    @staticmethod
    def _stopped_at_the_write(
        ctx: TurnContext, pending: PendingApproval, intent: str, *, finished: bool = False
    ) -> Answer:
        """End the goal at an approved write that did not land, confirmed.

        The outcome the write left behind says why: withdrawn, refused, never
        sent, answered no, or not confirmed by its read-back. Nothing planned
        after it runs, and the goal is dropped where it stands rather than
        advanced, so no later procedure is planned and nothing reports the goal
        complete. The reply is told that the rest was not run when there was a
        rest to run. `finished` says the plan itself ran to its end, and only
        the procedures after it in the goal were left.
        """
        state = ctx.state
        goal = state.pending_goal
        left = 0 if finished else len(pending.plan.operations) - pending.op_index - 1
        if goal is not None:
            left += len(goal.runbook_ids) - goal.step_index - 1
        state.pending_goal = None
        return Answer("outcomes", reply_intent=stopped_short(intent) if left > 0 else intent)

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
