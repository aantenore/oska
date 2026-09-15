"""Questions asked of the ontology graph.

Every function reads the graph and returns declared facts. None of them reach
back to the catalog, so a caller can answer from a graph it built once.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

from opsdeck.ontology.graph import Node, OntologyGraph, state_node_id


class Move(BaseModel):
    """One legal move of an entity between two of its states."""

    entity: str
    from_state: str
    to: str
    operation: str
    required: bool = False
    offered: bool = True


class Lifecycle(BaseModel):
    """The state machine one entity declares."""

    entity: str
    state_field: str | None = None
    state_read: str | None = None
    initial: str | None = None
    states: list[str] = Field(default_factory=list)
    final_states: list[str] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)

    def moves_from(self, state: str) -> list[Move]:
        """Every move that is legal from this state."""
        return [move for move in self.moves if move.from_state == state]

    def is_final(self, state: str) -> bool:
        """True when the entity moves no further once it holds this state."""
        return state in self.final_states


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


def lifecycle_of(graph: OntologyGraph, entity_id: str) -> Lifecycle | None:
    """The state machine an entity declares, or None when it declares none."""
    entity = graph.node(entity_id)
    if entity is None or entity.kind != "entity":
        return None
    states = [
        node
        for node in graph.nodes_of("state")
        if node.attributes.get("entity") == entity_id
    ]
    if not states:
        return None
    moves = [
        move
        for node in states
        for move in _moves_out(graph, node)
    ]
    return Lifecycle(
        entity=entity_id,
        state_field=entity.attributes.get("state_field"),
        state_read=entity.attributes.get("state_read"),
        initial=entity.attributes.get("initial_state"),
        states=[str(node.attributes.get("state")) for node in states],
        final_states=[
            str(node.attributes.get("state")) for node in states if node.attributes.get("final")
        ],
        moves=moves,
    )


def legal_moves(graph: OntologyGraph, entity_id: str, state: str) -> list[Move]:
    """The moves an entity in this state may make, offered or not."""
    node = graph.node(state_node_id(entity_id, state))
    if node is None or node.attributes.get("entity") != entity_id:
        return []
    return _moves_out(graph, node)


def required_move(graph: OntologyGraph, entity_id: str, state: str) -> Move | None:
    """The move that completes an entity held in this state, if one is declared."""
    for move in legal_moves(graph, entity_id, state):
        if move.required:
            return move
    return None


def _moves_out(graph: OntologyGraph, node: Node) -> list[Move]:
    moves = []
    for edge in graph.edges_from(node.id, "moves_to"):
        target = graph.node(edge.target)
        if target is None:
            continue
        moves.append(
            Move(
                entity=str(node.attributes.get("entity", "")),
                from_state=str(node.attributes.get("state", "")),
                to=str(target.attributes.get("state", "")),
                operation=str(edge.attributes.get("operation", "")),
                required=bool(edge.attributes.get("required")),
                offered=bool(edge.attributes.get("offered", True)),
            )
        )
    return moves
