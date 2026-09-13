"""Renderings of the ontology graph."""

from __future__ import annotations

import re

from opsdeck.ontology.graph import EdgeKind, NodeKind, OntologyGraph

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
    """The graph as a mermaid diagram, one subgraph per kind of node."""
    hidden = {node.id for node in graph.nodes_of("operation")} if not include_operations else set()
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
    return "\n".join(lines)


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
