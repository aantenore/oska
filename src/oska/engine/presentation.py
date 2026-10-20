"""What a turn asks a presentation surface to show, bound to verified evidence.

A procedure declares, per moment of a turn, a view and the fields that fill it.
After the turn, every procedure that ran is given the first moment it reached
among those it declares, in this order:

1. `on_verified_write`: one of its writes was performed and its read-back
   confirmed it.
2. `on_selection`: the turn resolved its subject from the focus.
3. `on_results`: one of its reads returned several records.
4. `on_single_result`: one of its reads returned one record.
5. `on_completion`: the goal it belongs to finished in this turn, and every
   write the turn performed or found for it was confirmed by its read-back.

The declared bindings are then answered from the evidence the runtime holds
for that procedure, and from nothing else: the records its reads returned, the
record the turn selected, and a verified receipt. A binding the evidence cannot
answer drops the whole directive, because half a view is a view nobody
declared. No model text is evidence, and neither is what a person typed: a
value somebody gave reaches a view only as the payload field of a receipt whose
read-back confirmed it. A directive is a request to show something; it never
writes, and nothing reads one back into a decision.

A domain that does not enable presentation gets none: the presenter is built
switched off and answers every turn with an empty list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from oska.catalog.registry import Catalog
from oska.contracts.common import Directive, Outcome, PendingGoal
from oska.contracts.receipt import VerificationStatus
from oska.contracts.skill import PresentationMoment, Skill
from oska.engine.execution import TurnContext
from oska.engine.subject import Referent, Shown, operation_spec, record, shown_in
from oska.ontology.naming import RECEIPT_FIELDS, identifier_names, parse_binding, records_in

__all__ = [
    "MOMENTS",
    "Evidence",
    "Presenter",
    "TurnNotes",
    "bind",
    "reply_event_fields",
    "turn_moment",
]

MOMENTS = (
    "on_verified_write",
    "on_selection",
    "on_results",
    "on_single_result",
    "on_completion",
)

_UNANSWERED = object()
_WRITE_KINDS = frozenset({"receipt", "ledger_hit"})


@dataclass
class TurnNotes:
    """What the turn decided on the way that the outcomes alone do not say."""

    goal: PendingGoal | None = None
    approved: str | None = None
    selection: Referent | None = None
    joined: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    """Everything one procedure's directive may be filled from."""

    records: list[dict] = field(default_factory=list)
    receipt: Outcome | None = None
    selection: list = field(default_factory=list)
    completed: bool = False

    def moments(self) -> list[str]:
        """The moments this evidence reaches, in the order they are preferred."""
        reached = {
            "on_verified_write": self.receipt is not None,
            "on_selection": bool(self.selection),
            "on_results": len(self.records) > 1,
            "on_single_result": len(self.records) == 1,
            "on_completion": self.completed,
        }
        return [moment for moment in MOMENTS if reached[moment]]


class Presenter:
    """Builds the directives of a turn, when the serving domain enables presentation."""

    def __init__(self, registry: Catalog, *, enabled: bool = False) -> None:
        self._registry = registry
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Whether this presenter emits anything at all."""
        return self._enabled

    def settle(self, ctx: TurnContext, notes: TurnNotes) -> list[Directive]:
        """Record what the turn showed and is about, and return what it asks to be shown.

        The session's record of what was shown is written whether or not the
        domain enables presentation: it is what the next turn refers back to.
        """
        completed = _completed(notes.goal, ctx.outcomes)
        found = self.directives(ctx, notes)
        shown = shown_in(ctx.outcomes, self._registry)
        record(
            ctx.state,
            self._registry,
            shown,
            turn_moment(shown, notes.selection, completed),
            notes.selection,
        )
        return found

    def directives(self, ctx: TurnContext, notes: TurnNotes) -> list[Directive]:
        """One directive per procedure that ran and declared the moment it reached."""
        if not self._enabled:
            return []
        found: list[Directive] = []
        for procedure_id in _ran(ctx, notes):
            procedure = self._registry.get(procedure_id)
            if procedure is None or not procedure.frontmatter.presentation:
                continue
            evidence = self.evidence(procedure, ctx, notes)
            declared = procedure.frontmatter.presentation
            moment = next((name for name in evidence.moments() if name in declared), None)
            if moment is None:
                continue
            directive = bind(procedure.id, moment, declared[moment], evidence)
            if directive is not None:
                found.append(directive)
        return found

    def evidence(self, procedure: Skill, ctx: TurnContext, notes: TurnNotes) -> Evidence:
        """What the runtime holds for one procedure at the end of the turn."""
        capabilities = procedure.frontmatter.capabilities
        goal = notes.goal
        in_goal = goal is not None and procedure.id in goal.runbook_ids
        records = self._records(procedure, ctx.outcomes, set(capabilities.reads))
        return Evidence(
            records=records,
            receipt=_verified_receipt(ctx.outcomes, set(capabilities.writes)),
            selection=_selected(notes, procedure, self._registry, records),
            completed=in_goal and _completed(goal, ctx.outcomes),
        )

    def _records(self, procedure: Skill, outcomes: Sequence[Outcome], reads: set[str]) -> list:
        """The records of the procedure's last read that returned any.

        A procedure that names its subject is shown records of that entity only.
        """
        subject = procedure.frontmatter.subject
        records: list[dict] = []
        for outcome in outcomes:
            if outcome.kind != "read" or outcome.operation not in reads:
                continue
            if not outcome.data.get("ok"):
                continue
            spec = operation_spec(self._registry, outcome.integration_id, outcome.operation)
            if spec is None or not spec.entity:
                continue
            if subject is not None and spec.entity != subject.entity:
                continue
            data = outcome.data.get("data")
            found = records_in(spec.entity, data) if isinstance(data, dict) else None
            if found:
                records = found
        return records


def bind(
    procedure: str, moment: str, declared: PresentationMoment, evidence: Evidence
) -> Directive | None:
    """The directive a declared moment asks for, or None when evidence cannot fill it."""
    fields = dict(declared.fields)
    for key, raw in declared.bind.items():
        value = _answer(raw, evidence)
        if value is _UNANSWERED:
            return None
        fields[key] = value
    return Directive(procedure=procedure, moment=moment, view=declared.view, fields=fields)


def reply_event_fields(notes: TurnNotes, directives: list[Directive]) -> dict:
    """What the reply event adds about the subject and the directives, when there is any."""
    added: dict = {}
    if notes.selection is not None and notes.joined:
        added["subject"] = {
            "procedures": list(notes.joined),
            "source": notes.selection.source,
            "entity": notes.selection.entity,
            "identifiers": list(notes.selection.identifiers),
        }
    if directives:
        added["directives"] = [directive.model_dump(mode="json") for directive in directives]
    return added


def turn_moment(shown: Shown | None, selection: Referent | None, completed: bool) -> str:
    """The moment the turn as a whole reached, recorded with what it presented.

    A selection counts when what the turn showed holds the selected record, or
    when the turn showed nothing that could contradict it.
    """
    if shown is not None and shown.verified_write:
        return "on_verified_write"
    if selection is not None and selection.identifiers and (
        shown is None or _holds_all(shown.records, selection.identifiers)
    ):
        return "on_selection"
    if shown is not None and shown.records:
        return "on_results" if len(shown.records) > 1 else "on_single_result"
    return "on_completion" if completed else ""


def _answer(raw: str, evidence: Evidence) -> object:
    """One binding answered from evidence, or `_UNANSWERED`."""
    binding = parse_binding(raw)
    if binding is None:
        return raw
    name = binding.field
    match binding.kind:
        case "items":
            if not evidence.records or any(name not in item for item in evidence.records):
                return _UNANSWERED
            return [item[name] for item in evidence.records]
        case "item":
            if len(evidence.records) != 1 or name not in evidence.records[0]:
                return _UNANSWERED
            return evidence.records[0][name]
        case "selection":
            if name or not evidence.selection:
                return _UNANSWERED
            return evidence.selection[0] if len(evidence.selection) == 1 else evidence.selection
        case "receipt":
            return _receipt_field(evidence.receipt, name)
    return _UNANSWERED


def _receipt_field(receipt: Outcome | None, name: str) -> object:
    if receipt is None or not name:
        return _UNANSWERED
    if name in RECEIPT_FIELDS:
        value = {
            "receipt_id": receipt.receipt_id,
            "operation": receipt.operation,
            "integration_id": receipt.integration_id,
            "verification": receipt.data.get("verification"),
            "status": receipt.data.get("status"),
        }[name]
        return _UNANSWERED if value is None else value
    payload = receipt.data.get("payload")
    if isinstance(payload, dict) and name in payload:
        return payload[name]
    return _UNANSWERED


def _completed(goal: PendingGoal | None, outcomes: Sequence[Outcome]) -> bool:
    """Whether the goal finished in this turn with every write of it confirmed.

    A goal that ran to its last step past a write whose read-back did not
    confirm it, or past a write that never left, did not finish: it only ran
    out of steps.
    """
    if goal is None or goal.step_index < len(goal.runbook_ids):
        return False
    verified = str(VerificationStatus.verified)
    for outcome in outcomes:
        if outcome.kind in _WRITE_KINDS and (
            outcome.data.get("status") != "executed"
            or outcome.data.get("verification") != verified
        ):
            return False
        if outcome.kind == "unavailable" and outcome.data.get("write"):
            return False
    return True


def _verified_receipt(outcomes: Sequence[Outcome], writes: set[str]) -> Outcome | None:
    """The last write of this procedure the turn performed and verified."""
    verified = str(VerificationStatus.verified)
    found = [
        outcome
        for outcome in outcomes
        if outcome.kind == "receipt"
        and outcome.operation in writes
        and outcome.data.get("verification") == verified
    ]
    return found[-1] if found else None


def _selected(
    notes: TurnNotes, procedure: Skill, registry: Catalog, records: list[dict]
) -> list:
    """The identifier values of the record this procedure took from the focus.

    A selection is evidence only once a governed read of the turn returned the
    selected record: the focus says what was shown before, and a request that
    named another record was planned on that one instead.
    """
    selection = notes.selection
    if selection is None or procedure.id not in notes.joined:
        return []
    entity = registry.get(selection.entity)
    names = identifier_names(entity.frontmatter) if entity is not None else []
    if not names:
        return []
    if not _holds_all(records, selection.identifiers):
        return []
    return [ids[names[0]] for ids in selection.identifiers if names[0] in ids]


def _holds_all(records: list[dict], identifiers: list[dict]) -> bool:
    return all(
        any(all(item.get(key) == value for key, value in ids.items()) for item in records)
        for ids in identifiers
    )


def _ran(ctx: TurnContext, notes: TurnNotes) -> list[str]:
    """Every procedure that ran in this turn: the one approved, then those hydrated."""
    ran: list[str] = [notes.approved] if notes.approved else []
    for event in ctx.events:
        runbook_id = event.data.get("runbook_id") if event.kind == "hydrate" else None
        if isinstance(runbook_id, str) and runbook_id not in ran:
            ran.append(runbook_id)
    return ran
