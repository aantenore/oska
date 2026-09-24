"""Binding of an integration id to the connector that runs its operations.

The map lives in a JSON file: `{"integrations": {"<id>": {"type": ..., ...}}}`.
Relative fixture paths resolve against that file. `${NAME}` in an address or a
header is replaced by the environment variable of that name; a variable that is
not set becomes an empty string and is reported in `warnings`.

A caller serving bindings someone else wrote narrows both. `readable` says
which variables may be read at all, and a reference to any other is refused
rather than expanded; `confine_to` is the directory a mock fixture must resolve
inside, symbolic links followed. A single catalog passes neither and reads its
own configuration as it always has.

What a call may cost before a capability is called unavailable is configuration
too, and every kind of connector takes it from the same place: the `resilience`
block, given by the caller, overridden by one the file declares for the whole
binding, and overridden again by one an integration declares for itself. A
binding that says nothing takes the defaults.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from oska.config import ResilienceConfig
from oska.connectors.base import Connector, ConnectorError
from oska.connectors.http import HttpConnector
from oska.connectors.mock import MockConnector
from oska.connectors.shell import ShellConnector

__all__ = [
    "ConnectorRegistry",
    "Readable",
    "check_bindings",
    "environment_references",
    "read_bindings",
]

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KINDS = ("mock", "http", "shell")

Readable = Callable[[str], bool]
"""Whether a binding may read the environment variable of that name."""


class ConnectorRegistry:
    """Holds one connector per integration."""

    def __init__(
        self,
        connectors: Mapping[str, Connector] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self._connectors: dict[str, Connector] = dict(connectors or {})
        self.warnings: list[str] = list(warnings or [])

    def register(self, integration_id: str, connector: Connector) -> None:
        """Bind an integration to a connector, replacing any earlier binding."""
        self._connectors[integration_id] = connector

    def get(self, integration_id: str) -> Connector:
        """Return the connector bound to an integration."""
        try:
            return self._connectors[integration_id]
        except KeyError as error:
            raise ConnectorError(f"no connector is bound to {integration_id}") from error

    def has(self, integration_id: str) -> bool:
        """Whether an integration is bound."""
        return integration_id in self._connectors

    def ids(self) -> list[str]:
        """The bound integration ids, sorted."""
        return sorted(self._connectors)

    def close(self) -> None:
        """Release what every connector holds open, such as a connection pool."""
        for connector in self._connectors.values():
            close = getattr(connector, "close", None)
            if callable(close):
                close()

    @classmethod
    def from_config(
        cls,
        path: Path | str,
        *,
        behaviors: dict[str, dict] | None = None,
        env: Mapping[str, str] | None = None,
        resilience: ResilienceConfig | None = None,
        readable: Readable | None = None,
        confine_to: Path | None = None,
    ) -> ConnectorRegistry:
        """Build the registry declared by a configuration file."""
        config_path = Path(path)
        return cls.from_document(
            read_bindings(config_path),
            config_path.parent,
            behaviors=behaviors,
            env=env,
            resilience=resilience,
            readable=readable,
            confine_to=confine_to,
        )

    @classmethod
    def from_document(
        cls,
        config: dict,
        base_dir: Path,
        *,
        behaviors: dict[str, dict] | None = None,
        env: Mapping[str, str] | None = None,
        resilience: ResilienceConfig | None = None,
        readable: Readable | None = None,
        confine_to: Path | None = None,
    ) -> ConnectorRegistry:
        """Build the registry a binding document declares, its paths read from `base_dir`."""
        check_bindings(config, base_dir, readable=readable, confine_to=confine_to)
        source = os.environ if env is None else env
        declared = _resilience(resilience or ResilienceConfig(), config.get("resilience"))
        registry = cls()
        integrations = config.get("integrations") or {}
        for integration_id, spec in integrations.items():
            registry.register(
                integration_id,
                registry._build(
                    integration_id,
                    spec,
                    base_dir,
                    (behaviors or {}).get(integration_id),
                    source,
                    _resilience(declared, spec.get("resilience"), spec.get("timeout")),
                ),
            )
        return registry

    def _build(
        self,
        integration_id: str,
        spec: dict,
        base_dir: Path,
        behaviors: dict | None,
        env: Mapping[str, str],
        resilience: ResilienceConfig,
    ) -> Connector:
        kind = spec.get("type")
        operations = spec.get("operations") or {}
        if kind == "mock":
            return MockConnector(
                id=integration_id,
                fixture_path=_fixture(integration_id, spec, base_dir),
                operations=operations,
                behaviors=behaviors,
                resilience=resilience,
            )
        if kind == "http":
            headers = {
                name: self._expand(value, env, integration_id)
                for name, value in (spec.get("headers") or {}).items()
            }
            return HttpConnector(
                id=integration_id,
                base_url=self._expand(spec.get("base_url", ""), env, integration_id),
                headers=headers,
                operations=operations,
                resilience=resilience,
            )
        if kind == "shell":
            return ShellConnector(
                id=integration_id,
                operations=operations,
                resilience=resilience,
            )
        raise ConnectorError(f"{integration_id}: unknown connector type {kind!r}")

    def _expand(self, value: str, env: Mapping[str, str], integration_id: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = env.get(name)
            if resolved is None:
                self.warnings.append(f"{integration_id}: {name} is not set in the environment")
                return ""
            return resolved

        return _VARIABLE.sub(replace, str(value))


def read_bindings(path: Path | str) -> dict:
    """The binding document of a file, refused when it is not a JSON object."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ConnectorError(f"{config_path}: expected a JSON object")
    return config


def environment_references(value: object) -> list[str]:
    """The environment variables a value reads with `${NAME}`, in order."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return [name for item in value for name in environment_references(item)]
    return [] if value is None else _VARIABLE.findall(str(value))


def check_bindings(
    config: dict,
    base_dir: Path,
    *,
    readable: Readable | None = None,
    confine_to: Path | None = None,
) -> None:
    """Refuse a binding document that reads what it may not, without building anything.

    Every integration names a known kind of connector; with `readable`, every
    `${NAME}` in an address or a header is one it admits; with `confine_to`,
    every mock fixture resolves inside that directory.
    """
    integrations = config.get("integrations") or {}
    if not isinstance(integrations, dict):
        raise ConnectorError("expected an object under 'integrations'")
    for integration_id, spec in integrations.items():
        if not isinstance(spec, dict):
            raise ConnectorError(f"{integration_id}: the binding is not an object")
        kind = spec.get("type")
        if kind not in _KINDS:
            raise ConnectorError(f"{integration_id}: unknown connector type {kind!r}")
        if kind == "mock" and confine_to is not None:
            fixture = _fixture(integration_id, spec, base_dir)
            root = Path(confine_to).resolve()
            if not fixture.resolve().is_relative_to(root):
                raise ConnectorError(
                    f"{integration_id}: the fixture {fixture} is outside the pack ({root}); "
                    "a mock binding reads only its own pack's files"
                )
        if kind == "http" and readable is not None:
            for name in environment_references([spec.get("base_url"), spec.get("headers")]):
                if not readable(name):
                    raise ConnectorError(
                        f"{integration_id}: reads {name} from the environment, which is not "
                        "declared for it; name it in the manifest's environment"
                    )


def _fixture(integration_id: str, spec: dict, base_dir: Path) -> Path:
    fixture = spec.get("fixture")
    if not fixture:
        raise ConnectorError(f"{integration_id}: the mock binding declares no fixture")
    return _resolve(base_dir, fixture)


def _resilience(
    base: ResilienceConfig, declared: object = None, timeout: object = None
) -> ResilienceConfig:
    """The inherited resilience settings with what this level declares over them.

    A binding may also carry a bare `timeout`, which says that and nothing
    else: it is what a connector configuration said before availability was
    part of the contract, and it still means the same thing.
    """
    values = base.model_dump()
    if timeout is not None:
        values["timeout_seconds"] = float(timeout)
    if isinstance(declared, dict):
        values.update(declared)
    return ResilienceConfig.model_validate(values)


def _resolve(base_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (base_dir / candidate).resolve()
