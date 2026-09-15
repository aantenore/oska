"""Skill frontmatter, parsed sections and the loaded skill document."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SKILL_ID_PATTERN = r"^(runbook|entity|integration|composite)\.[a-z0-9_]+$"
_SKILL_ID_RE = re.compile(SKILL_ID_PATTERN)

MIN_TRIGGERS = 3


class SkillType(StrEnum):
    """Kind of catalog entry."""

    runbook = "runbook"
    entity = "entity"
    integration = "integration"
    composite = "composite"


class SkillStatus(StrEnum):
    """Publication state of a catalog entry."""

    draft = "draft"
    published = "published"
    retired = "retired"


class RuntimeCopy(BaseModel):
    """Wording the runtime uses when it names the procedure to the user."""

    action: str
    action_on_selection: str | None = None
    order: int | None = None


class Capabilities(BaseModel):
    """Operations a procedure is allowed to reach."""

    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)


class VerifySpec(BaseModel):
    """Read-back declared by the integration for a write operation."""

    operation: str
    expect: dict[str, str] = Field(default_factory=dict)


class OperationSpec(BaseModel):
    """A single operation exposed by an integration."""

    name: str
    write: bool = False
    entity: str | None = None
    transitions: dict[str, str] = Field(default_factory=dict)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    verify: VerifySpec | None = None
    idempotency_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _write_needs_verify(self) -> OperationSpec:
        if self.write and self.verify is None:
            raise ValueError(f"operation {self.name}: a write operation requires a verify spec")
        return self


class EntityField(BaseModel):
    """A field of an entity record."""

    name: str
    type: str
    identifier: bool = False
    description: str = ""


class Relation(BaseModel):
    """A typed edge from one entity to another."""

    name: str
    target: str
    cardinality: Literal["one", "many"]
    description: str = ""


class LifecycleTransition(BaseModel):
    """A legal move of an entity between states, and the operation that causes it.

    `required` marks the move that completes the object: until it is made, the
    object is not yet what the person asked for. `offered` says whether the
    runtime may suggest the move; a legal move that is not offered is still
    planned and executed when the person asks for it.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_states: list[str] = Field(alias="from", default_factory=list)
    to: str
    operation: str
    required: bool = False
    offered: bool = True


class SkillFrontmatter(BaseModel):
    """Declarative header of a skill file."""

    id: str
    type: SkillType
    version: int = Field(ge=1)
    status: SkillStatus = SkillStatus.published
    description: str = Field(min_length=20, max_length=200)
    triggers: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    runtime: RuntimeCopy | None = None
    operations: list[OperationSpec] = Field(default_factory=list)
    fields: list[EntityField] = Field(default_factory=list)
    state_field: str | None = None
    states: list[str] = Field(default_factory=list)
    initial_state: str | None = None
    final_states: list[str] = Field(default_factory=list)
    lifecycle: list[LifecycleTransition] = Field(default_factory=list)
    state_read: str | None = None
    relations: list[Relation] = Field(default_factory=list)
    human_inputs: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    policy: str | None = None

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, value: str) -> str:
        if not _SKILL_ID_RE.match(value):
            raise ValueError(f"id {value!r} does not match {SKILL_ID_PATTERN}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> SkillFrontmatter:
        kind = self.type.value
        if not self.id.startswith(f"{kind}."):
            raise ValueError(f"id {self.id!r} does not match type {kind!r}")
        if self.type in (SkillType.runbook, SkillType.composite):
            if len(self.triggers) < MIN_TRIGGERS:
                raise ValueError(f"{self.id}: a {kind} needs at least {MIN_TRIGGERS} triggers")
            if self.runtime is None:
                raise ValueError(f"{self.id}: a {kind} needs a runtime section")
        elif self.triggers:
            raise ValueError(f"{self.id}: a {kind} must not declare triggers")
        if self.operations and self.type is not SkillType.integration:
            raise ValueError(f"{self.id}: only an integration declares operations")
        if self.fields and self.type is not SkillType.entity:
            raise ValueError(f"{self.id}: only an entity declares fields")
        if self.type is not SkillType.entity:
            for name, value in (
                ("a state field", self.state_field),
                ("states", self.states),
                ("an initial state", self.initial_state),
                ("final states", self.final_states),
                ("a lifecycle", self.lifecycle),
                ("a state read", self.state_read),
                ("relations", self.relations),
            ):
                if value:
                    raise ValueError(f"{self.id}: only an entity declares {name}")
        if self.human_inputs and self.type not in (SkillType.runbook, SkillType.composite):
            raise ValueError(f"{self.id}: only a procedure declares human inputs")
        if self.runbooks and self.type is not SkillType.composite:
            raise ValueError(f"{self.id}: only a composite declares runbooks")
        if self.type is SkillType.composite and not self.runbooks:
            raise ValueError(f"{self.id}: a composite needs at least one runbook")
        return self

    def field(self, name: str) -> EntityField | None:
        """Return the declared field with this name, if any."""
        for entry in self.fields:
            if entry.name == name:
                return entry
        return None

    def moves_from(self, state: str | None) -> list[LifecycleTransition]:
        """Lifecycle moves that are legal from this state.

        A state nobody reported is not a state every move is legal from. When
        the state is unknown no move is legal, because a move is legal only out
        of a state the system of record was found holding the object in.
        """
        if state is None:
            return []
        return [move for move in self.lifecycle if state in move.from_states]

    def operation(self, name: str) -> OperationSpec | None:
        """Return the declared operation with this name, if any."""
        for spec in self.operations:
            if spec.name == name:
                return spec
        return None


class SkillSections(BaseModel):
    """Body of a skill file, keyed by lowercased H2 title."""

    purpose: str = ""
    preconditions: str = ""
    steps: str = ""
    verification: str = ""
    rollback: str = ""
    extra: dict[str, str] = Field(default_factory=dict)


class Skill(BaseModel):
    """A skill file as loaded from disk."""

    frontmatter: SkillFrontmatter
    sections: SkillSections
    path: Path
    fingerprint: str

    @property
    def id(self) -> str:
        """Skill identifier."""
        return self.frontmatter.id

    @property
    def type(self) -> SkillType:
        """Skill type."""
        return self.frontmatter.type

    @property
    def version(self) -> int:
        """Skill version."""
        return self.frontmatter.version

    @property
    def is_published(self) -> bool:
        """True when the skill is published."""
        return self.frontmatter.status is SkillStatus.published
