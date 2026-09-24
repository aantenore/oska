"""Whether a write is a legal lifecycle move from the state its object is in now.

The state is never taken from the conversation. It is what the entity's
declared read reports about the object the write names, taken from what the
plan has already read when that read was about this same object and read again
when it was not.
"""

from __future__ import annotations

from oska.connectors.registry import ConnectorRegistry
from oska.contracts.skill import LifecycleTransition, OperationSpec, SkillFrontmatter
from oska.engine.calls import call
from oska.engine.context import TurnContext
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.verification import read_state

__all__ = ["lifecycle_move"]


def lifecycle_move(
    capabilities: CapabilityRegistry,
    connectors: ConnectorRegistry,
    ctx: TurnContext,
    spec: OperationSpec,
    payload: dict,
) -> tuple[str | None, LifecycleTransition | None]:
    """The move this write makes, or why it is not one the object may make."""
    entity = capabilities.lifecycle_entity(spec)
    if entity is None:
        return None, None
    name = _short_name(entity.id)
    state = _current_state(capabilities, connectors, ctx, entity, payload)
    if state is None:
        return (
            f"the current state of the {name} could not be read through "
            f"{entity.state_read}, so {spec.name} cannot be judged a legal move",
            None,
        )
    move = capabilities.move_for(entity, spec.name, state)
    if move is not None:
        return None, move
    legal = capabilities.legal_operations(entity, state)
    listed = ", ".join(legal) if legal else "none"
    return (
        f"the {name} is {state} and {spec.name} is not a legal move from there; "
        f"the moves declared from {state} are: {listed}",
        None,
    )


def _short_name(entity_id: str) -> str:
    """The record key a read returns an entity under: `entity.alert` is `alert`."""
    return entity_id.split(".", 1)[-1]


def _current_state(
    capabilities: CapabilityRegistry,
    connectors: ConnectorRegistry,
    ctx: TurnContext,
    entity: SkillFrontmatter,
    payload: dict,
) -> str | None:
    """The state the system of record holds the object the write names in.

    What an earlier read of this turn left behind is keyed by the entity's
    short name alone, so it may well be a record of another object of the
    same kind. It is used only when it is the same object: the write's own
    payload names the object, and every field of that name the record
    carries has to agree with it. Anything else — a different object, or a
    record carrying nothing to compare — is not an answer about this
    object, and the declared read is executed for the object the write
    names instead.
    """
    operation = entity.state_read
    integration_id = capabilities.integration_for(operation) if operation else None
    read_spec = (
        capabilities.operation_spec(integration_id, operation)
        if operation and integration_id is not None
        else None
    )
    identity = _identity(entity, read_spec, payload)
    held = ctx.entities.get(_short_name(entity.id))
    if identity and isinstance(held, dict):
        state = read_state(entity, {_short_name(entity.id): held}, identity)
        if state is not None:
            return state
    if not operation or integration_id is None:
        return None
    result = call(connectors, ctx, integration_id, operation, _narrowed(read_spec, payload))
    if not result.ok:
        return None
    return read_state(entity, result.data)


def _narrowed(spec: OperationSpec | None, payload: dict) -> dict:
    """The fields of a write payload the read that follows it accepts."""
    if spec is None:
        return dict(payload)
    properties = spec.input_schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {name: value for name, value in payload.items() if name in properties}


def _identity(
    entity: SkillFrontmatter,
    read_spec: OperationSpec | None,
    payload: dict,
) -> dict:
    """Which object this write payload names, as far as the declarations say.

    Two kinds of field name the object: one the entity declares an identifier,
    and one the declared read accepts, because that is the field the read is
    narrowed by and so the field that decides which object came back. A payload
    that carries neither names no object, and nothing already read may be taken
    for it.
    """
    names = {field.name for field in entity.fields if field.identifier}
    if read_spec is not None:
        names |= set(_narrowed(read_spec, payload))
    return {name: value for name, value in payload.items() if name in names}
