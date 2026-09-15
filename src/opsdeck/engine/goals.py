"""Working a goal forward until it stops.

A goal is one procedure, or the ordered procedures of a composite. This module
plans the procedure the session is on, runs what the plan declares, and moves
to the next one, until something stops it: a question to ask, a write waiting
for approval, a refusal, or the end of the list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver, SuggestedAction, offer
from opsdeck.contracts.common import Hydrated, PendingInfo
from opsdeck.contracts.plan import Plan
from opsdeck.engine import outcomes as build
from opsdeck.engine.execution import PlanExecutor, StopReason, TurnContext
from opsdeck.engine.reply import ReplyKind
from opsdeck.governance.verification import read_state
from opsdeck.planner.planner import InvalidPlanError, Planner

__all__ = ["Answer", "GoalRunner"]

UNKNOWN_PROCEDURE = "the procedure this session was working on is no longer in the catalog"
_PLAN_FAILED = "the plan could not run"


@dataclass
class Answer:
    """What the turn decided its reply should be made of."""

    kind: ReplyKind
    pending: PendingInfo | None = None
    suggestions: list[SuggestedAction] = field(default_factory=list)
    reply_intent: str = ""


class GoalRunner:
    """Plans and runs the procedures of a goal, one step at a time."""

    def __init__(
        self,
        registry: SkillRegistry,
        resolver: CapabilityResolver,
        planner: Planner | None,
        executor: PlanExecutor,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._planner = planner
        self._executor = executor

    def advance(self, ctx: TurnContext, reply_intent: str = "") -> Answer:
        """Run the goal from where it stands until something stops it."""
        state = ctx.state
        goal = state.pending_goal
        while goal is not None and goal.step_index < len(goal.runbook_ids):
            runbook_id = goal.runbook_ids[goal.step_index]
            runbook = self._registry.get(runbook_id)
            if runbook is None:
                state.pending_goal = None
                ctx.record(build.invalid_plan_outcome(runbook_id, [UNKNOWN_PROCEDURE]))
                return Answer("outcomes", reply_intent=reply_intent)
            hydrated = self._registry.closure(runbook_id)
            ctx.emit(
                "hydrate",
                {
                    "runbook_id": runbook_id,
                    "entities": [skill.id for skill in hydrated.entities],
                    "integrations": [skill.id for skill in hydrated.integrations],
                    "step": goal.step_index,
                    "steps": len(goal.runbook_ids),
                },
            )
            if self._planner is None:
                return Answer("unavailable")
            plan = self._plan(ctx, hydrated, goal.collected_inputs)
            if plan is None:
                state.pending_goal = None
                return Answer("outcomes", reply_intent=_PLAN_FAILED)
            reply_intent = plan.reply_intent or reply_intent
            if plan.missing:
                goal.missing = list(plan.missing)
                return Answer(
                    "input_question",
                    pending=PendingInfo(kind="input", question=plan.missing[0].question),
                    reply_intent=reply_intent,
                )
            stop = self._executor.run(ctx, runbook, hydrated, plan, 0)
            if stop != "completed":
                return self.stopped(ctx, stop, reply_intent)
            goal.step_index += 1
        state.pending_goal = None
        return Answer("outcomes", suggestions=self.suggestions(ctx), reply_intent=reply_intent)

    def stopped(self, ctx: TurnContext, stop: StopReason, reply_intent: str) -> Answer:
        """The answer for a plan that did not run to the end."""
        if stop == "approval":
            return Answer(
                "approval_request",
                pending=PendingInfo(kind="approval", approval=ctx.state.pending_approval),
                reply_intent=reply_intent,
            )
        ctx.state.pending_goal = None
        return Answer("outcomes", reply_intent=reply_intent)

    def suggestions(self, ctx: TurnContext) -> list[SuggestedAction]:
        """What can be done next with what was just read, in the catalog's own words.

        An entity that declares a lifecycle answers this itself: the moves that
        are legal from the state the object was just read in, minus the ones its
        author kept out of the runtime's mouth. Everything else is offered the
        procedures that act on it.

        Each offer carries the key of the procedure behind it, so the wording
        can be returned in the language of the turn without the offer itself
        changing.
        """
        if not ctx.outcomes or any(outcome.kind != "read" for outcome in ctx.outcomes):
            return []
        actions: list[SuggestedAction] = []
        for entity_id in ctx.state.last_entities:
            offered = self._resolver.offered_actions_for_entity(
                entity_id, self._state_read_in_turn(ctx, entity_id)
            )
            if offered is None:
                offered = [
                    self._resolver.action_for_skill(skill, on_selection=True)
                    for skill in self._resolver.actions_for_entity(entity_id)
                ]
            for action in offered:
                offer(actions, action.key, action.label)
        return actions

    def _state_read_in_turn(self, ctx: TurnContext, entity_id: str) -> str | None:
        """The state this turn read the object in, or nothing when it read none."""
        skill = self._registry.get(entity_id)
        if skill is None:
            return None
        record = ctx.entities.get(entity_id.split(".", 1)[-1])
        if not isinstance(record, dict):
            return None
        return read_state(skill.frontmatter, {entity_id.split(".", 1)[-1]: record})

    def _plan(self, ctx: TurnContext, hydrated: Hydrated, collected: dict) -> Plan | None:
        planner = self._planner
        if planner is None:
            return None
        try:
            plan = planner.plan(hydrated, list(ctx.state.history), dict(collected))
        except InvalidPlanError as error:
            ctx.emit(
                "plan",
                {"runbook_id": hydrated.runbook.id, "valid": False, "issues": error.issues},
            )
            ctx.record(build.invalid_plan_outcome(hydrated.runbook.id, error.issues))
            return None
        ctx.emit(
            "plan",
            {
                "runbook_id": plan.runbook_id,
                "valid": True,
                "operations": [
                    {"operation": operation.operation, "write": operation.write}
                    for operation in plan.operations
                ],
                "missing": [item.field for item in plan.missing],
                "reply_intent": plan.reply_intent,
            },
        )
        return plan
