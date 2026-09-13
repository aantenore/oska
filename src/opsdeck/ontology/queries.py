"""Questions asked of the ontology graph.

Every function reads the graph and returns declared facts. None of them reach
back to the catalog, so a caller can answer from a graph it built once.
"""

from __future__ import annotations

from collections import deque

from opsdeck.ontology.graph import Node, OntologyGraph


def operations_on(
    graph: OntologyGraph,
    entity_id: str,
    *,
    writes_only: bool = False,
) -> list[Node]:
    """Operation nodes bound to an entity, writes only when asked."""
    nodes = []
    for edge in graph.edges_to(entity_id, "acts_on"):
        node = graph.node(edge.source)
        if node is None:
            continue
        if writes_only and not node.attributes.get("write"):
            continue
        nodes.append(node)
    return sorted(nodes, key=lambda node: node.id)


def writers_of(graph: OntologyGraph, entity_id: str) -> list[Node]:
    """Integration nodes that declare at least one write on an entity."""
    integrations: dict[str, Node] = {}
    for operation in operations_on(graph, entity_id, writes_only=True):
        owner = graph.node(str(operation.attributes.get("integration", "")))
        if owner is not None:
            integrations.setdefault(owner.id, owner)
    return [integrations[key] for key in sorted(integrations)]


def procedures_for(
    graph: OntologyGraph,
    entity_id: str,
    *,
    writes_only: bool = False,
) -> list[Node]:
    """Procedure nodes that touch an entity; composites are not procedures."""
    nodes = []
    for edge in graph.edges_to(entity_id, "touches"):
        node = graph.node(edge.source)
        if node is None or node.kind != "procedure":
            continue
        if writes_only and not node.attributes.get("writes"):
            continue
        nodes.append(node)
    return sorted(nodes, key=_process_order)


def related(graph: OntologyGraph, entity_id: str) -> list[tuple[str, str, str]]:
    """The relations an entity declares, as (relation, target, cardinality)."""
    return [
        (
            str(edge.attributes.get("relation", "")),
            edge.target,
            str(edge.attributes.get("cardinality", "")),
        )
        for edge in graph.edges_from(entity_id, "relates_to")
    ]


def path(graph: OntologyGraph, from_entity: str, to_entity: str) -> list[str] | None:
    """The shortest chain of entities linked by relations, or None when none exists."""
    if graph.node(from_entity) is None or graph.node(to_entity) is None:
        return None
    if from_entity == to_entity:
        return [from_entity]
    queue: deque[list[str]] = deque([[from_entity]])
    seen = {from_entity}
    while queue:
        trail = queue.popleft()
        for edge in graph.edges_from(trail[-1], "relates_to"):
            if edge.target in seen:
                continue
            extended = [*trail, edge.target]
            if edge.target == to_entity:
                return extended
            seen.add(edge.target)
            queue.append(extended)
    return None


def vocabulary(graph: OntologyGraph) -> dict:
    """The names an entity is spoken about with, keyed by entity identifier."""
    return {
        node.id: {
            "description": node.attributes.get("description", ""),
            "identifier": node.attributes.get("identifier"),
            "state_field": node.attributes.get("state_field"),
            "states": list(node.attributes.get("states", [])),
            "fields": [field["name"] for field in node.attributes.get("fields", [])],
        }
        for node in graph.nodes_of("entity")
    }


def _process_order(node: Node) -> tuple[int, int, str]:
    order = node.attributes.get("order")
    if not isinstance(order, int):
        return (1, 0, node.id)
    return (0, order, node.id)
