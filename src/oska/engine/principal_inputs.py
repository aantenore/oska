"""Filling the fields a procedure takes from the identity, and refusing a plan that supplies them.

A field a procedure declares as a principal input is filled between the plan
and the policy: after the model has finished proposing operations and before
anything judges or performs one. The plan may not carry such a field at all. A
plan that carries one is refused rather than corrected, because a value the
model chose and a value the channel authenticated are not interchangeable and
the difference is the whole point of declaring the field.
"""

from __future__ import annotations

from oska.contracts.plan import Plan
from oska.contracts.skill import OperationSpec, Skill
from oska.engine import outcomes as build
from oska.engine.context import TurnContext
from oska.governance.identity import (
    IdentityError,
    Principal,
    refused_fields,
    resolve_inputs,
)

__all__ = ["fill", "principal_values"]


def principal_values(
    ctx: TurnContext, runbook: Skill, plan: Plan, start: int
) -> dict | None:
    """What the identity supplies to this plan, or nothing once the plan is refused.

    A refusal is recorded on the turn as an invalid plan before this returns.
    """
    try:
        from_principal = resolve_inputs(runbook, Principal.from_dict(ctx.state.principal))
    except IdentityError as exc:
        ctx.record(build.invalid_plan_outcome(plan.runbook_id, [str(exc)]))
        return None
    supplied = _supplied_by_the_model(runbook, plan, start)
    if supplied:
        ctx.record(build.invalid_plan_outcome(plan.runbook_id, supplied))
        return None
    return from_principal


def fill(spec: OperationSpec, payload: dict, from_principal: dict) -> list[str]:
    """Put the values the identity supplies into one payload, and name what moved.

    Only the fields this operation declares are filled: a procedure binds a
    field to the identity once, and the operations that accept that field take
    it while the rest are left as the plan wrote them.
    """
    properties = spec.input_schema.get("properties")
    names = set(properties) if isinstance(properties, dict) else set()
    filled: list[str] = []
    for name, value in from_principal.items():
        if name in names and payload.get(name) != value:
            payload[name] = value
            filled.append(name)
    return filled


def _supplied_by_the_model(runbook: Skill, plan: Plan, start: int) -> list[str]:
    """Why this plan may not run: it fills in what the identity fills in.

    The whole plan from here on is read before anything of it is performed,
    so a read is not executed on the strength of a plan whose write names an
    identity the person chose.
    """
    return [
        f"{runbook.id} takes {name} from the authenticated session, and the plan "
        f"supplies it to {operation.operation}"
        for operation in plan.operations[start:]
        for name in refused_fields(runbook, operation.payload)
    ]
