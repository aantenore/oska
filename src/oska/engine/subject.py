"""What a conversation is about, derived from what the runtime showed.

A turn that shows records leaves two things on the session. `presented` is what
was shown: the moment of the turn it was shown at, the entity, the read that
returned it, and for each record its identifiers, its display fields and its
comparable fields. `focus` is what the conversation is about from then on: the
object, or the list of objects, a follow-up refers to.

The rules, in the order they apply:

1. What was shown is taken from evidence only. It is the records of the last
   read of the turn that returned records of an entity, or the record the
   response of a verified write carries. A write that was not verified shows
   nothing. Nothing a model wrote is ever taken for a record.
2. A turn that showed records makes them the focus: one record is an object in
   focus, several are a list in focus. A turn that showed nothing but resolved
   a record from the focus narrows the focus to it. A turn that did neither
   leaves `presented` and `focus` as they stand.
3. A reference to a shown record is resolved in fixed precedence. First, a
   literal identifier of a shown record in the message. Else, the best overlap
   between the message and each shown record's display fields, where the
   record in focus sets the score a challenger must beat: a challenger that
   only ties it, or ties another challenger, wins nothing. Else, the standing
   focus. A display value is mentioned when it appears in the message as a
   whole phrase; a field holding several values is mentioned when one of them
   is, by membership and never as a piece of a word.
4. The message is read through the synonyms of the pack that serves the
   domain, which map how people say a thing to how the catalog names it. The
   runtime keeps no word list of its own.
5. A procedure declaring `subject: {entity, cardinality, joins: focus}` receives
   the resolved identifier as a collected input, under the payload field its
   operations name the entity by, so a follow-up that only points back at what
   was shown needs no question. A subject of cardinality one joins only a single record; a list
   in focus is not guessed down to one. The planner is told such an input
   apart from what the person typed: it is the default of the conversation,
   and a request that names another record is planned on that record. The
   selection counts as made only once a governed read of the turn returned
   the selected record.
6. A context the client supplies never overrides any of this. It is a claim,
   not evidence, and it may only be confirmed by a governed read that shows the
   record, after which rule 2 applies to what the read returned.
7. A comparative refines only on what the shown records actually differ on.
   When a list is in focus, the refinements a turn may offer are the fields the
   entity declares comparable, on which the shown records hold more than one
   value, and which the read that showed them accepts as a filter: an enum,
   offered as the shown values the enum allows, or a number, offered with its
   declared bounds. Only those reach the planner, as facts.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from oska.catalog.registry import Catalog
from oska.contracts.common import Outcome
from oska.contracts.receipt import VerificationStatus
from oska.contracts.skill import OperationSpec, Skill, SkillFrontmatter
from oska.engine.state import SessionState
from oska.ontology.naming import (
    identifier_names,
    is_multi_valued,
    records_in,
    subject_field,
)

__all__ = [
    "Referent",
    "Shown",
    "focus_inputs",
    "join",
    "normalize",
    "operation_spec",
    "record",
    "refinements",
    "resolve",
    "shown_in",
]

_NUMERIC = frozenset({"integer", "number"})


@dataclass(frozen=True)
class Shown:
    """The records one turn showed, and the evidence they came from."""

    entity: str
    integration_id: str
    operation: str
    records: list[dict]
    verified_write: bool = False


@dataclass(frozen=True)
class Referent:
    """The record or records a message refers to, and the rule that found them."""

    entity: str
    identifiers: list[dict] = field(default_factory=list)
    source: str = "focus"


def operation_spec(
    registry: Catalog, integration_id: str | None, operation: str | None
) -> OperationSpec | None:
    """The declaration of one operation, or nothing when the catalog holds none."""
    integration = registry.get(integration_id) if integration_id else None
    if integration is None or not operation:
        return None
    return integration.frontmatter.operation(operation)


def shown_in(outcomes: Sequence[Outcome], registry: Catalog) -> Shown | None:
    """The records the turn showed, from reads and verified writes alone (rule 1)."""
    shown: Shown | None = None
    for outcome in outcomes:
        spec = operation_spec(registry, outcome.integration_id, outcome.operation)
        if spec is None or not spec.entity:
            continue
        data: object = None
        verified = False
        if outcome.kind == "read" and outcome.data.get("ok"):
            data = outcome.data.get("data")
        elif outcome.kind == "receipt" and outcome.data.get("verification") == str(
            VerificationStatus.verified
        ):
            data = outcome.data.get("response")
            verified = True
        records = records_in(spec.entity, data) if isinstance(data, dict) else None
        if records:
            shown = Shown(
                entity=spec.entity,
                integration_id=str(outcome.integration_id),
                operation=spec.name,
                records=records,
                verified_write=verified,
            )
    return shown


def record(
    state: SessionState,
    registry: Catalog,
    shown: Shown | None,
    moment: str,
    selection: Referent | None = None,
) -> None:
    """Write what the turn showed and what the conversation is now about (rule 2)."""
    entity = registry.get(shown.entity) if shown is not None else None
    if shown is not None and entity is not None:
        fields = entity.frontmatter
        records = [_shown_record(fields, item) for item in shown.records]
        state.presented = {
            "turn": state.turn_count,
            "moment": moment,
            "entity": shown.entity,
            "integration_id": shown.integration_id,
            "operation": shown.operation,
            "records": records,
        }
        identifiers = [item["identifier"] for item in records if item["identifier"]]
        if identifiers:
            state.focus = _focus(shown.entity, identifiers, "shown", state.turn_count)
        return
    if selection is not None and selection.identifiers:
        state.focus = _focus(
            selection.entity, list(selection.identifiers), selection.source, state.turn_count
        )


def resolve(
    text: str,
    presented: Mapping,
    focus: Mapping,
    entity: Skill,
    synonyms: Mapping[str, str] | None = None,
) -> Referent | None:
    """The record or records a message refers to, in the fixed precedence (rule 3)."""
    standing = _standing(focus, entity.id)
    records = list(presented.get("records") or []) if presented.get("entity") == entity.id else []
    if not records:
        return standing
    said = normalize(text, synonyms)
    literal = [
        item["identifier"]
        for item in records
        if item.get("identifier")
        and any(_mentions(said, value) for value in item["identifier"].values())
    ]
    if literal:
        return Referent(entity=entity.id, identifiers=literal, source="identifier")
    scored = [
        (_score(said, item, entity.frontmatter), item)
        for item in records
        if item.get("identifier")
    ]
    if not scored:
        return standing
    bar = _focus_score(scored, standing)
    best = max(score for score, _ in scored)
    winners = [item for score, item in scored if score == best]
    if best > bar and len(winners) == 1:
        return Referent(entity=entity.id, identifiers=[winners[0]["identifier"]], source="display")
    return standing


def join(
    procedures: Sequence[Skill],
    registry: Catalog,
    state: SessionState,
    text: str,
    synonyms: Mapping[str, str] | None = None,
) -> tuple[dict, Referent | None, list[str]]:
    """The inputs a goal takes from the focus, the referent, and who took them (rule 5)."""
    inputs: dict = {}
    selection: Referent | None = None
    joined: list[str] = []
    for procedure in procedures:
        subject = procedure.frontmatter.subject
        if subject is None or subject.joins != "focus":
            continue
        entity = registry.get(subject.entity)
        if entity is None:
            continue
        referent = resolve(text, state.presented, state.focus, entity, synonyms)
        if referent is None or not referent.identifiers:
            continue
        if subject.cardinality == "one" and len(referent.identifiers) != 1:
            continue
        named = subject_field(entity, _operations(procedure, registry))
        if named is None:
            continue
        payload_field, identifier = named
        values = [ids.get(identifier) for ids in referent.identifiers]
        if any(value is None for value in values):
            continue
        inputs[payload_field] = values[0] if subject.cardinality == "one" else values
        selection = referent
        joined.append(procedure.id)
    return inputs, selection, joined


def focus_inputs(
    procedure: Skill, registry: Catalog, state: SessionState, collected: Mapping
) -> dict:
    """The collected inputs of this procedure that name a shown or focused record.

    They are what the join supplied rather than what the person typed, and the
    planner states them apart for that reason.
    """
    subject = procedure.frontmatter.subject
    if subject is None or subject.joins != "focus":
        return {}
    entity = registry.get(subject.entity)
    if entity is None:
        return {}
    named = subject_field(entity, _operations(procedure, registry))
    if named is None or named[0] not in collected:
        return {}
    payload_field, identifier = named
    held = collected[payload_field]
    known = [
        ids.get(identifier)
        for ids in _known_identifiers(state, entity.id)
        if ids.get(identifier) is not None
    ]
    offered = held if isinstance(held, list) else [held]
    if offered and all(value in known for value in offered):
        return {payload_field: held}
    return {}


def refinements(
    state: SessionState, registry: Catalog, reads: Collection[str]
) -> list[dict]:
    """What a comparative about the list in focus may refine on, and nothing else (rule 7)."""
    presented = state.presented
    records = list(presented.get("records") or [])
    if len(records) < 2 or state.focus.get("cardinality") != "many":
        return []
    if presented.get("operation") not in reads:
        return []
    spec = operation_spec(registry, presented.get("integration_id"), presented.get("operation"))
    entity = registry.get(str(presented.get("entity", "")))
    if spec is None or entity is None:
        return []
    properties = spec.input_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    offered: list[dict] = []
    for name in entity.frontmatter.comparable_fields:
        accepted = properties.get(name)
        if not isinstance(accepted, dict):
            continue
        values = _distinct(item.get("compared", {}).get(name) for item in records)
        if len(values) < 2:
            continue
        refinement = _refinement(name, spec.name, accepted, values)
        if refinement is not None:
            offered.append(refinement)
    return offered


def normalize(text: str, synonyms: Mapping[str, str] | None = None) -> str:
    """The message in lower case, with how people say a thing replaced by the catalog's word."""
    said = f" {text.lower()} "
    for spoken in sorted(synonyms or {}, key=len, reverse=True):
        named = str((synonyms or {})[spoken]).lower()
        if spoken.strip():

            def spell(_found: re.Match[str], word: str = named) -> str:
                return word

            said = _phrase(spoken.lower()).sub(spell, said)
    return said


def _refinement(name: str, operation: str, accepted: dict, values: list) -> dict | None:
    enum = accepted.get("enum")
    if isinstance(enum, list):
        allowed = [value for value in enum if value in values]
        if not allowed:
            return None
        return {"field": name, "operation": operation, "values": allowed}
    if accepted.get("type") in _NUMERIC:
        numbers = [value for value in values if _is_number(value)]
        if len(numbers) < 2:
            return None
        offered = {
            "field": name,
            "operation": operation,
            "shown_minimum": min(numbers),
            "shown_maximum": max(numbers),
        }
        for bound in ("minimum", "maximum"):
            if _is_number(accepted.get(bound)):
                offered[bound] = accepted[bound]
        return offered
    return None


def _shown_record(entity: SkillFrontmatter, item: dict) -> dict:
    return {
        "identifier": {
            name: item[name] for name in identifier_names(entity) if item.get(name) is not None
        },
        "display": {name: item[name] for name in entity.display_fields if name in item},
        "compared": {name: item[name] for name in entity.comparable_fields if name in item},
    }


def _focus(entity: str, identifiers: list[dict], source: str, turn: int) -> dict:
    return {
        "entity": entity,
        "cardinality": "one" if len(identifiers) == 1 else "many",
        "identifiers": identifiers,
        "source": source,
        "turn": turn,
    }


def _known_identifiers(state: SessionState, entity_id: str) -> list[dict]:
    known: list[dict] = []
    if state.presented.get("entity") == entity_id:
        known.extend(item.get("identifier") or {} for item in state.presented.get("records") or [])
    if state.focus.get("entity") == entity_id:
        known.extend(state.focus.get("identifiers") or [])
    return known


def _standing(focus: Mapping, entity_id: str) -> Referent | None:
    if focus.get("entity") != entity_id or not focus.get("identifiers"):
        return None
    return Referent(entity=entity_id, identifiers=list(focus["identifiers"]), source="focus")


def _focus_score(scored: list[tuple[int, dict]], standing: Referent | None) -> int:
    """The score a challenger has to beat: the record in focus, when one is."""
    if standing is None or len(standing.identifiers) != 1:
        return 0
    held = standing.identifiers[0]
    return next((score for score, item in scored if item["identifier"] == held), 0)


def _score(said: str, item: dict, entity: SkillFrontmatter) -> int:
    """How many display fields of a shown record the message mentions."""
    score = 0
    for name, value in (item.get("display") or {}).items():
        if is_multi_valued(entity, name) or isinstance(value, list):
            values = value if isinstance(value, list) else [value]
            if any(_mentions(said, element) for element in values):
                score += 1
        elif _mentions(said, value):
            score += 1
    return score


def _mentions(said: str, value: object) -> bool:
    """Whether the message holds this value as a whole phrase."""
    if isinstance(value, bool) or value is None:
        return False
    text = str(value).strip().lower()
    return bool(text) and _phrase(text).search(said) is not None


def _phrase(text: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(text)}(?![\w-])")


def _distinct(values: Iterable[object]) -> list:
    seen: list = []
    for value in values:
        if value is None or isinstance(value, (list, dict)) or value in seen:
            continue
        seen.append(value)
    return seen


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _operations(procedure: Skill, registry: Catalog) -> list[OperationSpec]:
    found: list[OperationSpec] = []
    for integration_id in procedure.frontmatter.integrations:
        integration = registry.get(integration_id)
        if integration is not None:
            found.extend(integration.frontmatter.operations)
    return found
