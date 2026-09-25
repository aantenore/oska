"""System prompts built from the catalog.

Every sentence about what may be done comes from a declaration, never from a
constant in this file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from oska.contracts.common import Hydrated
from oska.contracts.skill import Skill
from oska.ontology.naming import noun_of, plural_of

PLANNER_RULES = (
    "Use only the operations listed above; nothing else exists.",
    "Every payload must satisfy the input schema shown with its operation.",
    "When a required input is unknown, leave that operation out and list the input"
    " in `missing` with the question to ask.",
    "Never invent the value of a record's field, such as a name, an object's identifier"
    " or a quantity: take it from the conversation or from a record the runtime read,"
    " and ask for it when it is unknown. The names of the procedure, the integrations"
    " and the operations are given above: copy them, never ask for them.",
    "`missing` names only an input of a declared operation, a key under `properties` of"
    " its input schema; it never names `runbook_id`, `integration_id` or `operation`.",
    "A value the person has stated, even in passing, is taken as they said it; do not ask"
    " them to confirm it. When it could be wrong, plan a read that checks it first: reads"
    " are always allowed, and every write is shown to the person with its exact payload"
    " and approved before it runs.",
    "Order reads before writes.",
    "A field listed as a human input is never filled in from your own judgment:"
    " leave it out of the payload and list it in `missing` with the question to ask,"
    " unless the person has already given it.",
    "A field listed as coming from the signed-in identity is not yours to fill and not"
    " yours to ask for: leave it out of every payload and out of `missing`. The runtime"
    " fills it once you have planned, and a plan that supplies it is refused.",
    "A comparative about records already shown refines only on a field listed under"
    " `Refinements the shown records allow`, with a value listed there; it never refines"
    " on another field or invents a value.",
    "`reply_intent` is one short line telling the reply writer what this turn is about.",
    "`language` is the ISO 639-1 code of the language the person wrote in, such as `it` or"
    " `en`, so the answer comes back in it.",
)

REFINEMENTS_HEADING = "Refinements the shown records allow"
FOCUS_HEADING = (
    "Inputs taken from the record the conversation is about; use them unless the request"
    " names another record"
)

REPLY_RULES = (
    "State only facts present in the outcome list or in the facts list.",
    "Suggest only what the suggestion list holds, and only when it is not empty.",
    "Do not claim that anything was verified unless an outcome says so.",
    "Keep it to a few plain sentences.",
)

_SECTIONS = ("purpose", "preconditions", "steps", "verification", "rollback")


def planner_system(
    hydrated: Hydrated,
    scope: str,
    *,
    ontology: dict | None = None,
    guidance: Sequence[str] = (),
) -> str:
    """The prompt that turns one procedure into operations.

    `ontology` is vocabulary-shaped data keyed by entity identifier, as the
    ontology layer produces it; the entities of this procedure are the only
    ones named.
    """
    runbook = hydrated.runbook
    parts = [
        "You plan the operations of a declared procedure.",
        f"Scope of this installation: {scope}",
        f"Procedure {runbook.id}: {runbook.frontmatter.description}",
        _runbook_sections(runbook),
        _filling(runbook.id),
    ]
    for member in hydrated.runbooks:
        parts.append(f"Step procedure {member.id}: {member.frontmatter.description}")
    entities = _entities(hydrated.entities)
    if entities:
        parts.append(f"Entities:\n{entities}")
    block = _ontology(hydrated.entities, ontology or {})
    if block:
        parts.append(f"Ontology:\n{block}")
    reserved = _human_inputs(runbook, hydrated.runbooks)
    if reserved:
        parts.append(f"Human inputs, which only the person supplies:\n{reserved}")
    identified = _principal_inputs(runbook, hydrated.runbooks)
    if identified:
        parts.append(
            "Fields the runtime fills from the signed-in identity, which you neither"
            f" supply nor ask for:\n{identified}"
        )
    parts.append(f"Declared operations:\n{_operations(hydrated.integrations)}")
    parts.append(_rules("Rules:", PLANNER_RULES))
    if guidance:
        # Declared promises belong with the other rules, before the conversation: a
        # model reads the last message as the latest instruction, and a promise
        # placed after the person's words outweighs what they asked for.
        parts.append(_rules("Promises this installation has declared, and holds you to:", guidance))
    return "\n\n".join(part for part in parts if part)


def _filling(runbook_id: str) -> str:
    """How the plan's own fields are filled: from this prompt, never from the person."""
    return (
        "How to fill the plan:\n"
        f"- `runbook_id` is `{runbook_id}`.\n"
        "- Each step's `integration_id` and `operation` are copied from the declared"
        " operations below, and `write` matches what is declared there.\n"
        "- `payload` holds only inputs of that operation's input schema, with values taken"
        " from the conversation or from records the runtime read.\n"
        "- `missing` lists an input only when its value cannot be found that way."
    )


def pick_system(candidates: Sequence[str]) -> str:
    """The prompt that resolves a tie between candidate procedures."""
    listed = "\n".join(f"- {candidate}" for candidate in candidates)
    return (
        "You choose the procedure that answers the request.\n\n"
        f"Candidates:\n{listed}\n\n"
        "Answer with the identifier of exactly one candidate from that list."
    )


def reply_system(language: str) -> str:
    """The prompt that phrases the reply from the outcomes of a turn."""
    return (
        f"You write the reply to the user, in this language: {language}.\n\n"
        f"{_rules('Rules:', REPLY_RULES)}"
    )


def _runbook_sections(runbook: Skill) -> str:
    lines = []
    for name in _SECTIONS:
        body = getattr(runbook.sections, name, "").strip()
        if body:
            lines.append(f"{name.capitalize()}:\n{body}")
    return "\n\n".join(lines)


def _human_inputs(runbook: Skill, members: Sequence[Skill]) -> str:
    """The fields the procedures of this goal reserve to a person, one per line."""
    fields: list[str] = []
    for skill in (runbook, *members):
        for field in skill.frontmatter.human_inputs:
            if field not in fields:
                fields.append(field)
    return "\n".join(f"- {field}" for field in fields)


def _principal_inputs(runbook: Skill, members: Sequence[Skill]) -> str:
    """The fields of this goal that come from whoever is signed in, one per line.

    The field is named so the model knows it exists and can plan the operation
    that carries it; the value is not shown, because the model has no use for a
    value it may not write down.
    """
    lines: list[str] = []
    for skill in (runbook, *members):
        for field in skill.frontmatter.principal_inputs:
            entry = f"- {field}"
            if entry not in lines:
                lines.append(entry)
    return "\n".join(lines)


def _entities(entities: Sequence[Skill]) -> str:
    lines = []
    for entity in entities:
        fields = [f"  {_naming(entity)}"]
        for field in entity.frontmatter.fields:
            mark = ", identifier" if field.identifier else ""
            head = f"  {field.name} ({field.type}{mark})"
            fields.append(f"{head}: {field.description}" if field.description else head)
        lines.append(f"{entity.id}\n" + "\n".join(fields))
    return "\n".join(lines)


def _naming(entity: Skill) -> str:
    """How one record and several records of the entity are named, and shown."""
    frontmatter = entity.frontmatter
    line = f"one is a {noun_of(frontmatter)}, several are {plural_of(frontmatter)}"
    if frontmatter.display_fields:
        line += f"; shown by {', '.join(frontmatter.display_fields)}"
    return line


def _ontology(entities: Sequence[Skill], ontology: dict) -> str:
    lines = []
    for entity in entities:
        entry = ontology.get(entity.id)
        if not isinstance(entry, dict):
            continue
        facts = []
        identifier = entry.get("identifier")
        if identifier:
            facts.append(f"identified by {identifier}")
        state_field = entry.get("state_field")
        states = entry.get("states") or []
        if state_field and states:
            facts.append(f"{state_field} is one of {', '.join(str(state) for state in states)}")
        relations = entry.get("relations") or []
        for relation in relations:
            if isinstance(relation, (list, tuple)) and len(relation) == 3:
                facts.append(f"{relation[0]}: {relation[2]} {relation[1]}")
        if facts:
            lines.append(f"{entity.id}: " + "; ".join(facts))
    return "\n".join(lines)


def _operations(integrations: Sequence[Skill]) -> str:
    lines = []
    for integration in integrations:
        for spec in integration.frontmatter.operations:
            kind = "write" if spec.write else "read"
            head = f"{integration.id} {spec.name} ({kind})"
            if spec.entity:
                head += f" on {spec.entity}"
            lines.append(f"{head}\n  input schema: {json.dumps(spec.input_schema, sort_keys=True)}")
            if spec.write and spec.transitions:
                left = ", ".join(
                    f"{field}={value}" for field, value in sorted(spec.transitions.items())
                )
                lines[-1] += f"\n  leaves the entity with: {left}"
    return "\n".join(lines)


def _rules(title: str, rules: Sequence[str]) -> str:
    return title + "\n" + "\n".join(f"- {rule}" for rule in rules)
