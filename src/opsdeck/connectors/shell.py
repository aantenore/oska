"""A connector that runs a command.

Every operation declares its argument vector up front. Placeholders in it are
filled from the payload, one argument at a time, after the value has been
checked. The command is never assembled into a string and never handed to a
shell, so a payload value can only ever become one argument.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from opsdeck.connectors.base import ConnectorError, timed
from opsdeck.contracts.common import ConnectorResult

__all__ = ["ShellConnector"]

DEFAULT_TIMEOUT = 30.0
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED = (str, int, float, bool)


class ShellConnector:
    """Serves declared operations by running a declared command."""

    def __init__(
        self,
        id: str,
        operations: dict[str, dict] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.id = id
        self._operations = dict(operations or {})
        self._timeout = timeout

    def operations(self) -> list[str]:
        """Names of the operations this binding declares."""
        return sorted(self._operations)

    @timed
    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        """Run the command declared for `operation`."""
        spec = self._operations.get(operation)
        if spec is None:
            return ConnectorResult(ok=False, error=f"undeclared operation {operation}")
        try:
            argv = _render(_argv_of(spec), payload)
        except ConnectorError as error:
            return ConnectorResult(ok=False, error=str(error))
        if not argv:
            return ConnectorResult(ok=False, error=f"{operation} declares no command")
        cwd = spec.get("cwd")
        timeout = float(spec.get("timeout", self._timeout))
        try:
            # argv is a declared list, never a string, so no shell ever sees it.
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=Path(cwd) if cwd else None,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ConnectorResult(ok=False, error=f"{operation} timed out after {timeout}s")
        except OSError as error:
            return ConnectorResult(ok=False, error=f"{argv[0]} could not be run: {error}")
        return ConnectorResult(
            ok=completed.returncode == 0,
            data={
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
            },
            error=None if completed.returncode == 0 else f"exit status {completed.returncode}",
        )


def _argv_of(spec: dict) -> list[str]:
    argv = spec.get("argv") or []
    return [str(part) for part in argv]


def _render(argv: list[str], payload: dict) -> list[str]:
    return [_PLACEHOLDER.sub(lambda match: _value(match.group(1), payload), part) for part in argv]


def _value(name: str, payload: dict) -> str:
    if name not in payload:
        raise ConnectorError(f"the payload carries no field {name}")
    raw = payload[name]
    if not isinstance(raw, _ALLOWED):
        raise ConnectorError(f"{name} is a {type(raw).__name__}, not a single value")
    text = str(raw)
    if "\0" in text:
        raise ConnectorError(f"{name} holds a null byte")
    return text
