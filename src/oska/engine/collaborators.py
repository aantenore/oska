"""The executor and the planner as the turn loop uses them: timed, and guided.

The trace sink times the stretches of execution and planning for whoever has to
explain a turn later, and records nothing at all unless a deployment asked for
it. The planner is handed the promises the procedure declares as instructions,
and is not asked to fill a field the runtime fills from the identity.
"""

from __future__ import annotations

from collections.abc import Sequence

from oska.audit.trace import TraceSink
from oska.catalog.registry import Catalog
from oska.catalog.resolver import CapabilityResolver
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.common import Hydrated, Message, Outcome, PendingApproval
from oska.contracts.plan import Plan
from oska.contracts.skill import Skill
from oska.engine.execution import PlanExecutor, StopReason, TurnContext
from oska.governance.approval import ApprovalGate
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.guardrails import GuardrailChain
from oska.governance.identity import declared_inputs
from oska.governance.policy_engine import PolicyEngine
from oska.governance.verification import Verifier
from oska.planner.planner import Planner
from oska.storage.protocols import LedgerStore

__all__ = ["GuidedPlanner", "TracedExecutor"]


class TracedExecutor(PlanExecutor):
    """The plan executor, with a span around every stretch of execution."""

    def __init__(
        self,
        trace: TraceSink,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine,
        approval: ApprovalGate,
        ledger: LedgerStore,
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


class GuidedPlanner(Planner):
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
        registry: Catalog,
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
        *,
        refinements: Sequence[dict] | None = None,
        from_focus: dict | None = None,
        guidance: Sequence[str] = (),
    ) -> Plan:
        """Plan the procedure, with what it promises stated in the prompt.

        Guidance a caller passes comes first, ahead of what the procedure promises.
        """
        prose = self._chain.instructions(_declaring(hydrated), "planning")
        with self._trace.span("plan", runbook=hydrated.runbook.id) as span:
            span.set(guardrails=len(prose))
            return super().plan(
                _without_identity_fields(hydrated),
                list(conversation),
                collected_inputs,
                refinements=refinements,
                from_focus=from_focus,
                guidance=[*guidance, *prose],
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
