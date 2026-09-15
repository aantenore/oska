"""A connector that reads and writes an in-memory copy of a JSON fixture.

Operations are not coded here. Each one is a small declarative mapping onto a
collection in the fixture, so a catalog can be exercised end to end without any
system behind it. The mapping keys are:

`collection`      the array in the fixture to work on.
`match`           the record field an identifier is matched against.
`payload_field`   the payload field carrying that identifier.
`where`           extra equality conditions the record must meet, each a
                  literal or `$field` taken from the payload.
`filter`          payload fields that narrow a list.
`field_map`       record field names for payload fields that are spelled
                  differently.
`project`         return one field of the matched record instead of the record.
`as`              the key the result is returned under.
`limit_field`     payload field carrying how many items to return.
`limit_default`   how many items to return when the payload says nothing.
`contains`        `{"in": <list field>, "from": <payload field>}`, keeps only
                  the entries of that list holding the payload value.
`set`             fields to write: a literal, `$field` or `$payload.field` to
                  copy a payload field, or `+n` to increment.
`append`          list fields to append a resolved value to.

Writes land in the copy held in memory; the file on disk is never touched.
`reset()` reloads it.

A behavior injected per operation makes it answer something other than the
fixture, so a scenario can exercise a path that needs a system to misbehave.
The behavior keys are:

`override`      answer with this data instead of reading the fixture.
`ok`            whether an overridden answer counts as a success. Default true.
`fail`          the system answers no. Terminal: another attempt would be
                answered the same way, and the breaker is not touched, because
                a system that refuses is a system that is up.
`unreachable`   the call does not reach the system at all. A transport failure,
                retryable, and the only injected failure that counts towards
                the breaker. This is how an outage is simulated.
`fail_after`    let this many calls through first, then fail in whichever of
                the two ways the behavior declares.
`error`         the message the failure reports.
"""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from opsdeck.config import ResilienceConfig
from opsdeck.connectors.base import (
    BaseConnector,
    ConnectorError,
    ConnectorOutcome,
    refused,
    rejected,
    succeeded,
    transport_failure,
)
from opsdeck.contracts.common import ConnectorResult

__all__ = ["MockConnector"]

_INCREMENT = re.compile(r"^\+\d+$")
_PAYLOAD_PREFIX = "$payload."
_NOT_FOUND = "not found"


class _RecordMissing(ConnectorError):
    """The mapping matched no record."""


class MockConnector(BaseConnector):
    """Serves declared operations from a JSON fixture held in memory."""

    def __init__(
        self,
        id: str,
        fixture_path: Path | str,
        operations: dict[str, dict],
        behaviors: dict[str, dict] | None = None,
        resilience: ResilienceConfig | None = None,
    ) -> None:
        super().__init__(id, resilience)
        self.fixture_path = Path(fixture_path)
        self._operations = dict(operations)
        self._behaviors: dict[str, dict] = dict(behaviors or {})
        self._calls: dict[str, int] = defaultdict(int)
        self._source = self._load()
        self._data = copy.deepcopy(self._source)

    def operations(self) -> list[str]:
        """Names of the operations this binding declares."""
        return sorted(self._operations)

    def snapshot(self) -> dict:
        """A copy of the current state, for assertions and debugging."""
        return copy.deepcopy(self._data)

    def reset(self) -> None:
        """Reload the fixture, forget every injected behavior count and close the breaker."""
        self._source = self._load()
        self._data = copy.deepcopy(self._source)
        self._calls.clear()
        self._breaker.reset()

    def set_behavior(self, operation: str, behavior: dict | None) -> None:
        """Install, replace or (with `None`) remove an injected behavior."""
        if behavior is None:
            self._behaviors.pop(operation, None)
            self._calls.pop(operation, None)
            return
        self._behaviors[operation] = dict(behavior)
        self._calls[operation] = 0

    def _perform(self, operation: str, payload: dict) -> ConnectorResult:
        """Run a declared operation against the in-memory fixture."""
        self._calls[operation] += 1
        injected = self._injected(operation)
        if injected is not None:
            return injected
        mapping = self._operations.get(operation)
        if mapping is None:
            return rejected(f"undeclared operation {operation}")
        try:
            data = self._run(mapping, payload)
        except _RecordMissing:
            # The system answered and the record is not there: that is not an outage.
            return refused(_NOT_FOUND)
        except ConnectorError as error:
            return rejected(str(error))
        return succeeded(data)

    def _load(self) -> dict:
        with self.fixture_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ConnectorError(f"{self.fixture_path}: expected a JSON object")
        return data

    def _injected(self, operation: str) -> ConnectorResult | None:
        behavior = self._behaviors.get(operation)
        if not behavior:
            return None
        fail_after = behavior.get("fail_after")
        if fail_after is not None:
            # `fail_after` says when, the other keys say in which way.
            if self._calls[operation] > int(fail_after):
                return self._failure(behavior, f"{operation} failed after {fail_after} calls")
        elif behavior.get("unreachable") or behavior.get("fail"):
            return self._failure(behavior, f"{operation} failed")
        override = behavior.get("override")
        if override is not None:
            if behavior.get("ok", True):
                return succeeded(dict(override))
            return refused(str(behavior.get("error") or f"{operation} was refused")).model_copy(
                update={"data": dict(override)}
            )
        return None

    @staticmethod
    def _failure(behavior: dict, default: str) -> ConnectorOutcome:
        """The injected failure, as the kind of failure the behavior declares."""
        message = str(behavior.get("error") or default)
        if behavior.get("unreachable"):
            return transport_failure(message)
        return refused(message)

    def _run(self, mapping: dict, payload: dict) -> dict:
        records = self._collection(mapping)
        key = mapping.get("as") or mapping["collection"]
        if "match" in mapping:
            record = self._match(records, mapping, payload)
            if mapping.get("set") or mapping.get("append"):
                self._write(record, mapping, payload)
                return {key: copy.deepcopy(record)}
            return {key: self._shape(record, mapping, payload)}
        return {key: self._select(records, mapping, payload)}

    def _collection(self, mapping: dict) -> list[dict]:
        name = mapping.get("collection")
        if not name:
            raise ConnectorError("the mapping declares no collection")
        records = self._data.get(name)
        if not isinstance(records, list):
            raise ConnectorError(f"the fixture holds no collection {name}")
        return records

    def _match(self, records: list[dict], mapping: dict, payload: dict) -> dict:
        field = mapping["match"]
        source = mapping.get("payload_field", field)
        if source not in payload:
            raise ConnectorError(f"the payload carries no field {source}")
        wanted = payload[source]
        where = _conditions(mapping, payload)
        for record in records:
            if record.get(field) == wanted and _matches(record, where):
                return record
        raise _RecordMissing(_NOT_FOUND)

    def _select(self, records: list[dict], mapping: dict, payload: dict) -> list[dict]:
        where = _conditions(mapping, payload)
        field_map = mapping.get("field_map") or {}
        wanted = {
            field_map.get(name, name): payload[name]
            for name in mapping.get("filter") or []
            if name in payload
        }
        selected = [
            record
            for record in records
            if _matches(record, where) and _matches(record, wanted)
        ]
        return copy.deepcopy(_limit(selected, mapping, payload))

    def _shape(self, record: dict, mapping: dict, payload: dict) -> Any:
        shaped = copy.deepcopy(record)
        contains = mapping.get("contains")
        if contains:
            needle = payload.get(contains["from"])
            if needle not in (None, ""):
                items = shaped.get(contains["in"]) or []
                lowered = str(needle).lower()
                shaped[contains["in"]] = [
                    item for item in items if lowered in str(item).lower()
                ]
        project = mapping.get("project")
        value = shaped.get(project) if project else shaped
        if isinstance(value, list):
            return _limit(value, mapping, payload)
        return value

    def _write(self, record: dict, mapping: dict, payload: dict) -> None:
        for field, raw in (mapping.get("append") or {}).items():
            existing = record.get(field)
            if existing is None:
                existing = []
                record[field] = existing
            if not isinstance(existing, list):
                raise ConnectorError(f"{field} is not a list, nothing can be appended to it")
            existing.append(_resolve(raw, payload))
        for field, raw in (mapping.get("set") or {}).items():
            if isinstance(raw, str) and _INCREMENT.match(raw):
                record[field] = _number(record.get(field, 0)) + int(raw)
            else:
                record[field] = _resolve(raw, payload)


def _conditions(mapping: dict, payload: dict) -> dict:
    """The `where` conditions of a mapping, with `$field` read from the payload.

    A binding that has to pin a record on more than the one field it matches on
    says so here, the way `set` and `append` name a payload field.
    """
    return {field: _resolve(raw, payload) for field, raw in (mapping.get("where") or {}).items()}


def _matches(record: dict, conditions: dict) -> bool:
    return all(record.get(field) == value for field, value in conditions.items())


def _limit(items: list, mapping: dict, payload: dict) -> list:
    field = mapping.get("limit_field")
    default = mapping.get("limit_default")
    limit = payload.get(field) if field else None
    if limit is None:
        limit = default
    if limit is None:
        return list(items)
    try:
        count = int(limit)
    except (TypeError, ValueError) as error:
        raise ConnectorError(f"{field} is not a whole number") from error
    if count <= 0:
        return []
    # The most recent entries sit at the end of an append-ordered collection.
    return list(items[-count:])


def _resolve(raw: Any, payload: dict) -> Any:
    if not isinstance(raw, str) or not raw.startswith("$"):
        return raw
    name = raw[len(_PAYLOAD_PREFIX) :] if raw.startswith(_PAYLOAD_PREFIX) else raw[1:]
    if name not in payload:
        raise ConnectorError(f"the payload carries no field {name}")
    return payload[name]


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConnectorError(f"{value!r} cannot be incremented")
    return value
