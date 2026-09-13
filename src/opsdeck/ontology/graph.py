"""The declared entities, their relations and everything bound to them.

The graph is derived from the published catalog: nothing is inferred and
nothing is added that a skill does not declare. Building it is a read over
skills already in memory, so a caller that holds a registry can rebuild
whenever the registry fingerprint moves.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from opsdeck.contracts.skill import Skill, SkillType

NodeKind = Literal["entity", "integration", "operation", "procedure", "composite"]
EdgeKind = Literal["relates_to", "acts_on", "declares", "uses", "touches", "contains"]

PROCESS_KINDS: tuple[NodeKind, ...] = ("procedure", "composite")


class SkillSource(Protocol):
    """What the graph needs of a catalog: the published skills and a fingerprint.

    The catalog reads the ontology, not the other way round, so what holds the
    skills is named by shape here instead of being imported.
    """

    def published(self) -> list[Skill]:
        """The effective version of every published skill."""

    def fingerprint(self) -> str:
        """Identity of the files the skills were read from."""


class Node(BaseModel):
    """One declared thing: an entity, an integration, an operation or a process."""

    id: str
    kind: NodeKind
    label: str = ""
    attributes: dict = Field(default_factory=dict)


class Edge(BaseModel):
    """One declared link between two nodes."""

    kind: EdgeKind
    source: str
    target: str
    attributes: dict = Field(default_factory=dict)


class OntologyGraph(BaseModel):
    """Nodes and edges derived from one state of the catalog."""

    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    fingerprint: str = ""

    @classmethod
    def from_registry(cls, registry: SkillSource) -> OntologyGraph:
        """Build the graph from the published skills of a registry."""
        return cls.from_skills(registry.published(), fingerprint=registry.fingerprint())

    @classmethod
    def from_skills(cls, skills: Sequence[Skill], *, fingerprint: str = "") -> OntologyGraph:
        """Build the graph from a list of skills, ignoring the unpublished ones."""
        published = [skill for skill in skills if skill.is_published]
        nodes: list[Node] = []
        edges: list[Edge] = []
        known = {skill.id for skill in published}
        for skill in published:
            if skill.type is SkillType.entity:
                nodes.append(_entity_node(skill))
                edges.extend(_relation_edges(skill, known))
            elif skill.type is SkillType.integration:
                nodes.append(_integration_node(skill))
                operations, declares, acts_on = _operation_parts(skill, known)
                nodes.extend(operations)
                edges.extend(declares)
                edges.extend(acts_on)
            elif skill.type in (SkillType.runbook, SkillType.composite):
                nodes.append(_process_node(skill))
                edges.extend(_process_edges(skill, known))
        return cls(
            nodes=sorted(nodes, key=lambda node: (node.kind, node.id)),
            edges=sorted(edges, key=lambda edge: (edge.kind, edge.source, edge.target)),
            fingerprint=fingerprint,
        )

    def node(self, node_id: str) -> Node | None:
        """The node with this identifier, if the graph holds it."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def nodes_of(self, kind: NodeKind) -> list[Node]:
        """Every node of one kind, in identifier order."""
        return [node for node in self.nodes if node.kind == kind]

    def edges_from(self, node_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """Every edge leaving a node, optionally of one kind."""
        return [
            edge
            for edge in self.edges
            if edge.source == node_id and (kind is None or edge.kind == kind)
        ]

    def edges_to(self, node_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """Every edge arriving at a node, optionally of one kind."""
        return [
            edge
            for edge in self.edges
            if edge.target == node_id and (kind is None or edge.kind == kind)
        ]


class OntologyCache:
    """Holds one graph and rebuilds it when the catalog changes."""

    def __init__(self, registry: SkillSource) -> None:
        self._registry = registry
        self._graph: OntologyGraph | None = None

    def graph(self) -> OntologyGraph:
        """The graph for the current state of the catalog."""
        fingerprint = self._registry.fingerprint()
        if self._graph is None or self._graph.fingerprint != fingerprint:
            self._graph = OntologyGraph.from_registry(self._registry)
        return self._graph


def operation_node_id(integration_id: str, operation: str) -> str:
    """Identifier of the node standing for one operation of one integration."""
    return f"{integration_id}:{operation}"


def _entity_node(skill: Skill) -> Node:
    frontmatter = skill.frontmatter
    identifier = next((field.name for field in frontmatter.fields if field.identifier), None)
    return Node(
        id=skill.id,
        kind="entity",
        label=skill.id.split(".", 1)[-1],
        attributes={
            "description": frontmatter.description,
            "identifier": identifier,
            "state_field": frontmatter.state_field,
            "states": list(frontmatter.states),
            "fields": [
                {
                    "name": field.name,
                    "type": field.type,
                    "identifier": field.identifier,
                    "description": field.description,
                }
                for field in frontmatter.fields
            ],
        },
    )


def _relation_edges(skill: Skill, known: set[str]) -> list[Edge]:
    return [
        Edge(
            kind="relates_to",
            source=skill.id,
            target=relation.target,
            attributes={
                "relation": relation.name,
                "cardinality": relation.cardinality,
                "description": relation.description,
            },
        )
        for relation in skill.frontmatter.relations
        if relation.target in known
    ]


def _integration_node(skill: Skill) -> Node:
    return Node(
        id=skill.id,
        kind="integration",
        label=skill.id.split(".", 1)[-1],
        attributes={
            "description": skill.frontmatter.description,
            "entities": list(skill.frontmatter.entities),
        },
    )


def _operation_parts(skill: Skill, known: set[str]) -> tuple[list[Node], list[Edge], list[Edge]]:
    nodes: list[Node] = []
    declares: list[Edge] = []
    acts_on: list[Edge] = []
    for spec in skill.frontmatter.operations:
        node_id = operation_node_id(skill.id, spec.name)
        nodes.append(
            Node(
                id=node_id,
                kind="operation",
                label=spec.name,
                attributes={
                    "operation": spec.name,
                    "integration": skill.id,
                    "write": spec.write,
                    "entity": spec.entity,
                    "transitions": dict(spec.transitions),
                },
            )
        )
        declares.append(Edge(kind="declares", source=skill.id, target=node_id))
        if spec.entity in known:
            acts_on.append(
                Edge(
                    kind="acts_on",
                    source=node_id,
                    target=str(spec.entity),
                    attributes={"write": spec.write, "transitions": dict(spec.transitions)},
                )
            )
    return nodes, declares, acts_on


def _process_node(skill: Skill) -> Node:
    frontmatter = skill.frontmatter
    runtime = frontmatter.runtime
    return Node(
        id=skill.id,
        kind="procedure" if skill.type is SkillType.runbook else "composite",
        label=skill.id.split(".", 1)[-1],
        attributes={
            "description": frontmatter.description,
            "action": runtime.action if runtime else "",
            "order": runtime.order if runtime else None,
            "reads": list(frontmatter.capabilities.reads),
            "writes": list(frontmatter.capabilities.writes),
            "runbooks": list(frontmatter.runbooks),
        },
    )


def _process_edges(skill: Skill, known: set[str]) -> list[Edge]:
    frontmatter = skill.frontmatter
    edges = [
        Edge(kind="uses", source=skill.id, target=target)
        for target in _present(frontmatter.integrations, known)
    ]
    edges.extend(
        Edge(kind="touches", source=skill.id, target=target)
        for target in _present(frontmatter.entities, known)
    )
    edges.extend(
        Edge(kind="contains", source=skill.id, target=target, attributes={"position": position})
        for position, target in enumerate(_present(frontmatter.runbooks, known))
    )
    return edges


def _present(references: Iterable[str], known: set[str]) -> list[str]:
    return [reference for reference in references if reference in known]
