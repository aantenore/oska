"""What the catalog declares an operation to be, and who may reach it.

Nothing runs unless an integration declares it and the procedure asking for it
lists it among its capabilities. This module answers both questions and holds
no state of its own beyond the registry it reads.
"""

from __future__ import annotations

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.contracts.skill import (
    LifecycleTransition,
    OperationSpec,
    Skill,
    SkillFrontmatter,
    SkillType,
)


class CapabilityRegistry:
    """Declared operations, read from the catalog."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def operation_spec(self, integration_id: str, operation: str) -> OperationSpec | None:
        """The operation an integration declares under this name, if any."""
        skill = self._registry.get(integration_id)
        if skill is None or skill.type is not SkillType.integration:
            return None
        return skill.frontmatter.operation(operation)

    def is_declared(self, integration_id: str, operation: str) -> bool:
        """True when the integration declares this operation."""
        return self.operation_spec(integration_id, operation) is not None

    def is_write(self, integration_id: str, operation: str) -> bool:
        """True when the declared operation changes state."""
        spec = self.operation_spec(integration_id, operation)
        return bool(spec and spec.write)

    def writes_allowed_by(self, runbook: Skill, operation: str) -> bool:
        """True when the procedure lists this operation among the writes it may perform."""
        return operation in runbook.frontmatter.capabilities.writes

    def entity_spec(self, entity_id: str | None) -> SkillFrontmatter | None:
        """The entity an operation is bound to, as the catalog declares it."""
        if not entity_id:
            return None
        skill = self._registry.get(entity_id)
        if skill is None or skill.type is not SkillType.entity:
            return None
        return skill.frontmatter

    def lifecycle_entity(self, spec: OperationSpec) -> SkillFrontmatter | None:
        """The entity whose lifecycle names this operation, when one does.

        An operation outside every lifecycle is governed by the declarations it
        already had; only a write a lifecycle names is judged against the state
        of the object it is about to change.
        """
        entity = self.entity_spec(spec.entity)
        if entity is None or not entity.lifecycle:
            return None
        if not any(move.operation == spec.name for move in entity.lifecycle):
            return None
        return entity

    @staticmethod
    def move_for(
        entity: SkillFrontmatter,
        operation: str,
        state: str,
    ) -> LifecycleTransition | None:
        """The move this operation makes from this state, or nothing when it is illegal."""
        for move in entity.moves_from(state):
            if move.operation == operation:
                return move
        return None

    @staticmethod
    def legal_operations(entity: SkillFrontmatter, state: str) -> list[str]:
        """The operations that may run against an object held in this state."""
        names: list[str] = []
        for move in entity.moves_from(state):
            if move.operation not in names:
                names.append(move.operation)
        return names

    def integration_for(self, operation: str) -> str | None:
        """The published integration declaring this operation name, if exactly one does."""
        for skill in self._registry.published():
            if skill.type is SkillType.integration and skill.frontmatter.operation(operation):
                return skill.id
        return None

    def spec_for(self, operation: str) -> OperationSpec | None:
        """The declared operation with this name, found by scanning published integrations."""
        integration_id = self.integration_for(operation)
        if integration_id is None:
            return None
        return self.operation_spec(integration_id, operation)
