"""The guardrail chain at the edges of a turn: over what arrives, and over the reply.

The chain runs over what arrives before anything is decided and over the reply
before it is returned, so a promise the catalog says is kept by code is kept
here rather than in a prompt. A turn the input guardrails refuse ends here,
before anything is routed or planned.
"""

from __future__ import annotations

from oska.catalog.registry import SkillRegistry
from oska.contracts.common import Message, Outcome, PendingInfo, TurnResult
from oska.contracts.skill import Skill
from oska.engine.context import TurnContext
from oska.governance.guardrails import GuardrailChain, GuardrailOutcome

__all__ = ["Boundary"]


class Boundary:
    """Applies the guardrail chain to what a turn receives and to what it answers."""

    def __init__(self, chain: GuardrailChain, registry: SkillRegistry) -> None:
        self._chain = chain
        self._registry = registry

    def guard_input(self, text: str) -> GuardrailOutcome:
        """Run the input guardrails over what arrived, before anything is decided."""
        return self._chain.apply_input(text)

    def refuse(self, ctx: TurnContext, guarded: GuardrailOutcome) -> TurnResult:
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
        reply = self.guard_output(ctx, guarded.description)
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

    def guard_output(self, ctx: TurnContext, reply: str) -> str:
        """Run the output guardrails over the reply, before anyone reads it."""
        guarded = self._chain.apply_output(reply)
        if guarded.action != "passed":
            ctx.emit("guardrail", guarded.as_event("output"))
        return guarded.description if not guarded.ok else guarded.text

    def output_instructions(self, ctx: TurnContext) -> list[str]:
        """What the skills this turn hydrated promise about the reply, as instructions."""
        return self._chain.instructions(self._hydrated(ctx), "output")

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
