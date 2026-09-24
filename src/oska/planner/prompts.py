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
    "Never invent an identifier, a name or a value; take them from the conversation"
    " or ask for them.",
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
    return "\n\n".join(part for part in parts if part)


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
