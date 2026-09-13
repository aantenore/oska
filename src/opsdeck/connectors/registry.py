"""Binding of an integration id to the connector that runs its operations.

The map lives in a JSON file: `{"integrations": {"<id>": {"type": ..., ...}}}`.
Relative fixture paths resolve against that file. `${NAME}` in an address or a
header is replaced by the environment variable of that name; a variable that is
not set becomes an empty string and is reported in `warnings`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from opsdeck.connectors.base import Connector, ConnectorError
from opsdeck.connectors.http import DEFAULT_TIMEOUT as HTTP_TIMEOUT
from opsdeck.connectors.http import HttpConnector
from opsdeck.connectors.mock import MockConnector
from opsdeck.connectors.shell import DEFAULT_TIMEOUT as SHELL_TIMEOUT
from opsdeck.connectors.shell import ShellConnector

__all__ = ["ConnectorRegistry"]

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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

    @classmethod
    def from_config(
        cls,
        path: Path | str,
        *,
        behaviours: dict[str, dict] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ConnectorRegistry:
        """Build the registry declared by a configuration file."""
        config_path = Path(path)
        with config_path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ConnectorError(f"{config_path}: expected a JSON object")
        source = os.environ if env is None else env
        registry = cls()
        integrations = config.get("integrations") or {}
        for integration_id, spec in integrations.items():
            registry.register(
                integration_id,
                registry._build(
                    integration_id,
                    spec,
                    config_path.parent,
                    (behaviours or {}).get(integration_id),
                    source,
                ),
            )
        return registry

    def _build(
        self,
        integration_id: str,
        spec: dict,
        base_dir: Path,
        behaviours: dict | None,
        env: Mapping[str, str],
    ) -> Connector:
        kind = spec.get("type")
        operations = spec.get("operations") or {}
        if kind == "mock":
            fixture = spec.get("fixture")
            if not fixture:
                raise ConnectorError(f"{integration_id}: the mock binding declares no fixture")
            return MockConnector(
                id=integration_id,
                fixture_path=_resolve(base_dir, fixture),
                operations=operations,
                behaviours=behaviours,
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
                timeout=float(spec.get("timeout", HTTP_TIMEOUT)),
            )
        if kind == "shell":
            return ShellConnector(
                id=integration_id,
                operations=operations,
                timeout=float(spec.get("timeout", SHELL_TIMEOUT)),
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


def _resolve(base_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (base_dir / candidate).resolve()
