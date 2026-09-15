"""Wording and capability lists derived from the catalog.

An action the runtime offers is two things at once. It is a key, which names
the action for the rest of the turn and never reaches a person, and it is a
label, which is the wording its author declared. The two are kept apart because
the label is the only half that may be rewritten: the reply writer, which
already answers in the language of the conversation, hands the label back
translated and the runtime puts it in place of the declared one. What the
action is stays the key's business, and nothing that rewrites a label can
change it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.contracts.skill import Skill, SkillType
from opsdeck.ontology.graph import OntologyCache, OntologyGraph
from opsdeck.ontology.queries import (
    Move,
    legal_moves,
    lifecycle_of,
    procedures_for,
    related,
    vocabulary,
)

PROCESS_TYPES = (SkillType.runbook, SkillType.composite)
REQUIRED_NOTE = "the next step this {name} needs"


@dataclass
class SuggestedAction:
    """One action a turn leaves on offer.

    `key` names the action and is the only half a translated label comes back
    under. `label` is the wording, declared by the author of the procedure until
    a reply writer returns it in the language of the conversation.
    """

    key: str
    label: str


class CapabilityResolver:
    """Every sentence the runtime says about what it can do comes from here."""

    def __init__(self, registry: SkillRegistry, *, conjunction: str = "and") -> None:
        self._registry = registry
        self._conjunction = conjunction
        self._ontology = OntologyCache(registry)

    def ontology(self) -> OntologyGraph:
        """The ontology graph for the current state of the catalog."""
        return self._ontology.graph()

    def available_processes(self) -> list[Skill]:
        """Published procedures, ordered by their declared order then by id."""
        processes = [skill for skill in self._registry.published() if skill.type in PROCESS_TYPES]
        return sorted(processes, key=_sort_key)

    def actions_for_entity(self, entity_id: str) -> list[Skill]:
        """Published runbooks that act on an entity, excluding read-only ones."""
        nodes = procedures_for(self.ontology(), entity_id, writes_only=True)
        found = [self._registry.get(node.id) for node in nodes]
        return sorted([skill for skill in found if skill is not None], key=_sort_key)

    def moves_for_entity(self, entity_id: str, state: str) -> list[tuple[Move, Skill]]:
        """The moves an object in this state may make, each with the procedure that makes it.

        A move is named by the procedure that declares the write behind it. A
        move no published procedure performs is not a thing the runtime can
        offer, so it is left out.
        """
        graph = self.ontology()
        procedures = [
            skill
            for node in procedures_for(graph, entity_id, writes_only=True)
            if (skill := self._registry.get(node.id)) is not None
        ]
        found = []
        for move in legal_moves(graph, entity_id, state):
            for skill in procedures:
                if move.operation in skill.frontmatter.capabilities.writes:
                    found.append((move, skill))
                    break
        return found

    def offered_actions_for_entity(
        self, entity_id: str, state: str | None
    ) -> list[SuggestedAction] | None:
        """What to offer for an object of this entity, or None when it has no lifecycle.

        A lifecycle answers the question on its own: only what is legal from the
        state the object is actually in, and only what the author allowed to be
        offered. `None` says the entity declares no lifecycle, and the caller
        falls back to the procedures that act on it.
        """
        if lifecycle_of(self.ontology(), entity_id) is None:
            return None
        if state is None:
            return []
        name = entity_id.split(".", 1)[-1]
        actions: list[SuggestedAction] = []
        for move, skill in self.moves_for_entity(entity_id, state):
            if not move.offered:
                continue
            label = self.action_phrase(skill, on_selection=True)
            if move.required:
                label = f"{label} ({REQUIRED_NOTE.format(name=name)})"
            offer(actions, skill.id, label)
        return actions

    def suggestions_for_entity(self, entity_id: str, state: str | None) -> list[str] | None:
        """The wording of what to offer for this entity, without the keys."""
        offered = self.offered_actions_for_entity(entity_id, state)
        return None if offered is None else [action.label for action in offered]

    def action_for_skill(self, skill: Skill, on_selection: bool = False) -> SuggestedAction:
        """The offer one procedure makes, keyed by the procedure itself."""
        return SuggestedAction(key=skill.id, label=self.action_phrase(skill, on_selection))

    def entity_summary(self, entity_id: str) -> str:
        """One sentence naming how an entity is identified, its states and its relations."""
        graph = self.ontology()
        entry = vocabulary(graph).get(entity_id)
        if entry is None:
            return ""
        name = entity_id.split(".", 1)[-1]
        article = "An" if name[:1].lower() in "aeiou" else "A"
        identifier = entry["identifier"]
        parts = [
            f"{article} {name} is identified by {identifier}"
            if identifier
            else f"{article} {name} declares no identifier"
        ]
        if entry["state_field"] and entry["states"]:
            states = join_phrases(list(entry["states"]), "or")
            parts.append(f"its {entry['state_field']} is {states}")
        links = [
            f"{relation} ({cardinality} {target.split('.', 1)[-1]})"
            for relation, target, cardinality in related(graph, entity_id)
        ]
        if links:
            parts.append(f"it relates to {join_phrases(links, self._conjunction)}")
        return f"{'; '.join(parts)}."

    def action_phrase(self, skill: Skill, on_selection: bool = False) -> str:
        """The wording for one procedure, in context or standalone."""
        runtime = skill.frontmatter.runtime
        if runtime is None:
            return skill.id
        if on_selection and runtime.action_on_selection:
            return runtime.action_on_selection
        return runtime.action

    def phrase_list(self, skills: Sequence[Skill], on_selection: bool = False) -> str:
        """Join the wording of several procedures into one readable list."""
        phrases = [self.action_phrase(skill, on_selection) for skill in skills]
        return join_phrases(phrases, self._conjunction)

    def declared_scope(self) -> str:
        """One sentence naming every procedure on offer."""
        processes = self.available_processes()
        if not processes:
            return "No procedure is available."
        return f"I can {self.phrase_list(processes)}."


def offer(actions: list[SuggestedAction], key: str, label: str) -> list[SuggestedAction]:
    """Add one action to what a turn offers, unless the same wording is there already.

    Two actions never share a key. A key is what a translated label comes back
    under, so a repeated one would put the wording of one action on another.
    """
    if any(action.label == label for action in actions):
        return actions
    actions.append(SuggestedAction(key=_free_key(actions, key), label=label))
    return actions


def relabel(actions: Sequence[SuggestedAction], labels: Mapping[str, str]) -> None:
    """Put a returned label in place of the declared one, where one came back.

    An action nothing came back for keeps the wording its author declared, and a
    label under a key the turn never offered is dropped: a reply writer may
    rephrase what is on offer and may not add to it.
    """
    for action in actions:
        label = labels.get(action.key)
        if isinstance(label, str) and label.strip():
            action.label = label.strip()


def _free_key(actions: Sequence[SuggestedAction], key: str) -> str:
    taken = {action.key for action in actions}
    if key not in taken:
        return key
    index = 2
    while f"{key}#{index}" in taken:
        index += 1
    return f"{key}#{index}"


def join_phrases(phrases: Sequence[str], conjunction: str = "and") -> str:
    """Join phrases as a sentence fragment: a, b and c."""
    items = [phrase for phrase in phrases if phrase]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def _sort_key(skill: Skill) -> tuple[int, int, str]:
    runtime = skill.frontmatter.runtime
    order = runtime.order if runtime and runtime.order is not None else None
    return (1, 0, skill.id) if order is None else (0, order, skill.id)
