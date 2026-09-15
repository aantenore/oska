"""Renderings of the ontology graph."""

from __future__ import annotations

import re

from opsdeck.ontology.graph import EdgeKind, NodeKind, OntologyGraph
from opsdeck.ontology.queries import Lifecycle, lifecycle_of

SUBGRAPH_TITLES: tuple[tuple[NodeKind, str], ...] = (
    ("entity", "Entities"),
    ("integration", "Integrations"),
    ("operation", "Operations"),
    ("procedure", "Procedures"),
    ("composite", "Composites"),
)

EDGE_LABELS: dict[EdgeKind, str] = {
    "relates_to": "relates to",
    "acts_on": "acts on",
    "declares": "declares",
    "uses": "uses",
    "touches": "touches",
    "contains": "contains",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def to_json(graph: OntologyGraph) -> dict:
    """The graph as plain data."""
    return graph.model_dump(mode="json")


def from_json(payload: dict) -> OntologyGraph:
    """Read back a graph written by `to_json`."""
    return OntologyGraph.model_validate(payload)


def to_mermaid(graph: OntologyGraph, *, include_operations: bool = True) -> str:
    """The graph as a mermaid diagram, then one state diagram per lifecycle.

    States are left out of the first diagram: a lifecycle is a machine of its
    own and reads as one, so each entity that declares moves gets a
    `stateDiagram-v2` block of its own underneath.
    """
    hidden = {node.id for node in graph.nodes_of("state")}
    if not include_operations:
        hidden |= {node.id for node in graph.nodes_of("operation")}
    lines = ["graph LR"]
    for kind, title in SUBGRAPH_TITLES:
        if kind == "operation" and not include_operations:
            continue
        nodes = graph.nodes_of(kind)
        if not nodes:
            continue
        lines.append(f"  subgraph {title}")
        for node in nodes:
            lines.append(f'    {_ref(node.id)}["{_text(node.label or node.id)}"]')
        lines.append("  end")
    for edge in graph.edges:
        if edge.source in hidden or edge.target in hidden:
            continue
        lines.append(
            f"  {_ref(edge.source)} -->|{_text(_edge_label(edge.kind, edge.attributes))}| "
            f"{_ref(edge.target)}"
        )
    for node in graph.nodes_of("entity"):
        lifecycle = lifecycle_of(graph, node.id)
        if lifecycle is not None:
            lines.extend(_state_diagram(node.id, lifecycle))
    return "\n".join(lines)


def _state_diagram(entity_id: str, lifecycle: Lifecycle) -> list[str]:
    """One `stateDiagram-v2` block for the moves an entity declares."""
    lines = ["", f"%% lifecycle of {entity_id}", "stateDiagram-v2"]
    if lifecycle.initial:
        lines.append(f"  [*] --> {_ref(lifecycle.initial)}")
    for move in lifecycle.moves:
        lines.append(
            f"  {_ref(move.from_state)} --> {_ref(move.to)}: "
            f"{_text(_move_label(move.operation, move.required, move.offered))}"
        )
    for state in lifecycle.final_states:
        lines.append(f"  {_ref(state)} --> [*]")
    return lines


def _move_label(operation: str, required: bool, offered: bool) -> str:
    notes = []
    if required:
        notes.append("required")
    if not offered:
        notes.append("not offered")
    return f"{operation} ({', '.join(notes)})" if notes else operation


def _edge_label(kind: EdgeKind, attributes: dict) -> str:
    label = EDGE_LABELS.get(kind, kind)
    relation = attributes.get("relation")
    cardinality = attributes.get("cardinality")
    if relation:
        return f"{relation} ({cardinality})" if cardinality else str(relation)
    if kind == "acts_on" and attributes.get("write"):
        return f"{label} (write)"
    return label


def _ref(node_id: str) -> str:
    return _UNSAFE.sub("_", node_id)


def _text(value: str) -> str:
    return value.replace('"', "'")
