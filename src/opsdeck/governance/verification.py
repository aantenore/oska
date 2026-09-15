"""Reads back what a write claims to have done.

The response to a write says only that the request was accepted. What the
integration declares in its verify spec is the question worth asking
afterwards, and its answer is the only thing the reply may call a result.

The entity the operation is bound to takes part in that question. When it
declares a state field and the closed set of values that field may hold, a
declared transition becomes an expectation the read-back has to satisfy, and a
read-back that returns a value outside the set fails verification.

A state is always the state of one object. Reading it from a record therefore
asks which object the record is about, and a record that answers a different
question than the one asked is not an answer at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from opsdeck.contracts.common import ConnectorResult
from opsdeck.contracts.receipt import VerificationStatus
from opsdeck.contracts.skill import LifecycleTransition, OperationSpec, SkillFrontmatter
from opsdeck.governance.capabilities import CapabilityRegistry

_PAYLOAD_PREFIX = "$payload."
_OUTSIDE_STATES = "state_outside_declared_set"
_NOT_REACHED = "state_the_move_promised"
_MISSING = object()

__all__ = ["ReadBackConnector", "Verifier", "read_state"]


@runtime_checkable
class ReadBackConnector(Protocol):
    """The part of a connector the read-back needs."""

    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        """Run one operation and report what happened."""
        ...


class Verifier:
    """Runs the read-back an integration declares for a write."""

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._capabilities = capabilities

    def verify(
        self,
        spec: OperationSpec,
        payload: dict,
        connector: ReadBackConnector,
        *,
        move: LifecycleTransition | None = None,
    ) -> tuple[VerificationStatus, dict]:
        """Read the state back and say whether it matches what was asked for.

        When the write was a move of a declared lifecycle, the state the move
        promised is part of the question: a read-back that finds the object in
        any other state fails, whatever else matched.
        """
        declared = spec.verify
        if declared is None:
            return VerificationStatus.unverified, {
                "reason": f"{spec.name} declares no read-back",
            }
        entity = self._capabilities.entity_spec(spec.entity)
        expectations = _expectations(spec, entity)
        read_payload = self._read_back_payload(spec, payload)
        evidence: dict[str, Any] = {
            "operation": declared.operation,
            "payload": read_payload,
        }
        try:
            result = connector.execute(declared.operation, read_payload)
        except Exception as exc:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            return VerificationStatus.unverified, evidence
        evidence["ok"] = result.ok
        evidence["data"] = result.data
        if result.error:
            evidence["error"] = result.error
        comparisons: dict[str, dict] = {}
        matched = True
        for key, declared_value in expectations.items():
            expected = _resolve(declared_value, payload)
            actual = _lookup(result.data, key)
            found = actual is not _MISSING
            equal = found and _equal(actual, expected)
            comparisons[key] = {
                "expected": expected,
                "actual": None if not found else actual,
                "found": found,
                "match": equal,
            }
            matched = matched and equal
        evidence["comparisons"] = comparisons
        outside = _state_outside_declared_set(entity, result.data)
        if outside is not None:
            evidence[_OUTSIDE_STATES] = outside
            return VerificationStatus.failed, evidence
        reached = read_state(entity, result.data)
        if move is not None and reached is not None and reached != move.to:
            evidence[_NOT_REACHED] = {"expected": move.to, "actual": reached}
            return VerificationStatus.failed, evidence
        if not result.ok or not matched:
            return VerificationStatus.failed, evidence
        return VerificationStatus.verified, evidence

    def _read_back_payload(self, spec: OperationSpec, payload: dict) -> dict:
        """The fields of the write payload the read operation accepts."""
        declared = spec.verify
        if declared is None:
            return {}
        read_spec = self._read_spec(spec, declared.operation)
        if read_spec is None:
            return dict(payload)
        properties = read_spec.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            return {}
        return {name: value for name, value in payload.items() if name in properties}

    def _read_spec(self, spec: OperationSpec, operation: str) -> OperationSpec | None:
        integration_id = self._capabilities.integration_for(spec.name)
        if integration_id is not None:
            found = self._capabilities.operation_spec(integration_id, operation)
            if found is not None:
                return found
        return self._capabilities.spec_for(operation)


def _expectations(spec: OperationSpec, entity: SkillFrontmatter | None) -> dict[str, str]:
    """The declared expectations, plus the state a declared transition promises.

    A transition is keyed by the state field of the entity the operation is
    bound to, so it says what the field must hold once the write has landed.
    It is added only when the verify spec does not already speak about that
    field, because an explicit expectation is the author's and wins.
    """
    declared = dict(spec.verify.expect) if spec.verify is not None else {}
    if entity is None or not entity.state_field or not spec.transitions:
        return declared
    if _names_field(declared, entity.state_field):
        return declared
    prefix = _short_name(entity.id)
    return {
        **declared,
        **{f"{prefix}.{field}": value for field, value in spec.transitions.items()},
    }


def read_state(
    entity: SkillFrontmatter | None,
    data: dict,
    identity: Mapping[str, Any] | None = None,
) -> str | None:
    """The state one object carries in a read, under either shape it comes in.

    A read returns one record under the entity's own name, or a list of them
    under its plural. Both are read here, so the state of an object is taken
    from the system of record the same way wherever it is needed.

    `identity` names the object whose state is wanted, as field names of the
    record and the values they have to hold. A record is read only when every
    one of those fields it carries holds the value asked for, and a record that
    carries none of them is not an answer about that object. Without an
    identity a list is read only when it holds a single record: the first of
    several names no object in particular, and reading a state off it reports
    one object's state as another's.
    """
    if entity is None or not entity.state_field:
        return None
    short = _short_name(entity.id)
    record = _record(data, short, identity)
    if not isinstance(record, dict):
        return None
    found = record.get(entity.state_field)
    return None if found is None else str(found)


def _record(
    data: dict, short: str, identity: Mapping[str, Any] | None
) -> dict | None:
    """The record of a read that is about the object the identity names."""
    single = data.get(short)
    if isinstance(single, dict):
        return single if _is_about(single, identity) else None
    listed = data.get(f"{short}s")
    if not isinstance(listed, list):
        return None
    records = [entry for entry in listed if isinstance(entry, dict)]
    if identity:
        for entry in records:
            if _is_about(entry, identity):
                return entry
        return None
    return records[0] if len(records) == 1 else None


def _is_about(record: Mapping[str, Any], identity: Mapping[str, Any] | None) -> bool:
    """Whether this record is about the object the identity names.

    Every field of the identity the record carries has to agree with it. A
    record carrying none of them says nothing about which object it is, so it
    is not taken for the one asked about.
    """
    if not identity:
        return True
    compared = [(name, value) for name, value in identity.items() if name in record]
    if not compared:
        return False
    return all(_equal(record[name], value) for name, value in compared)


def _state_outside_declared_set(
    entity: SkillFrontmatter | None, data: dict
) -> dict[str, Any] | None:
    """The read-back state when it is not one the entity declares, or nothing."""
    if entity is None or not entity.state_field or not entity.states:
        return None
    actual = read_state(entity, data)
    if actual is None or actual in entity.states:
        return None
    return {
        "field": f"{_short_name(entity.id)}.{entity.state_field}",
        "actual": actual,
        "declared": list(entity.states),
    }


def _names_field(expectations: dict[str, str], field: str) -> bool:
    """True when an expectation already speaks about this field of the record."""
    return any(key.rsplit(".", 1)[-1] == field for key in expectations)


def _short_name(entity_id: str) -> str:
    """The record key a read returns an entity under: `entity.service` is `service`."""
    return entity_id.split(".", 1)[-1]


def _resolve(declared: str, payload: dict) -> Any:
    """A declared expectation is either a literal or a field of the write payload."""
    if isinstance(declared, str) and declared.startswith(_PAYLOAD_PREFIX):
        found = _lookup(payload, declared[len(_PAYLOAD_PREFIX) :])
        return None if found is _MISSING else found
    return declared


def _equal(actual: Any, expected: Any) -> bool:
    """Equal by value, or by rendering, so a literal "3" matches the number 3."""
    if actual == expected:
        return True
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    return str(actual) == str(expected)


def _lookup(subject: Any, path: str) -> Any:
    current: Any = subject
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return _MISSING
    return current
