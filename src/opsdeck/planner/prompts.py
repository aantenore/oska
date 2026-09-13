"""System prompts built from the catalog.

Every sentence about what may be done comes from a declaration, never from a
constant in this file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from opsdeck.contracts.common import Hydrated
from opsdeck.contracts.skill import Skill

PLANNER_RULES = (
    "Use only the operations listed above; nothing else exists.",
    "Every payload must satisfy the input schema shown with its operation.",
    "When a required input is unknown, leave that operation out and list the input"
    " in `missing` with the question to ask.",
    "Never invent an identifier, a name or a value; take them from the conversation"
    " or ask for them.",
    "Order reads before writes.",
    "`reply_intent` is one short line telling the reply writer what this turn is about.",
)

REPLY_RULES = (
    "State only facts present in the outcome list.",
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


def _entities(entities: Sequence[Skill]) -> str:
    lines = []
    for entity in entities:
        fields = []
        for field in entity.frontmatter.fields:
            mark = ", identifier" if field.identifier else ""
            head = f"  {field.name} ({field.type}{mark})"
            fields.append(f"{head}: {field.description}" if field.description else head)
        lines.append(f"{entity.id}\n" + "\n".join(fields))
    return "\n".join(lines)


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
