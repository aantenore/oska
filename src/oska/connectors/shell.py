"""A connector that runs a command.

Every operation declares its argument vector up front. Placeholders in it are
filled from the payload, one argument at a time, after the value has been
checked. The command is never assembled into a string and never handed to a
shell, so a payload value can only ever become one argument, and never an
option: a value that would start an argument with `-` is refused unless the
declared command ends its options with `--` first.

The timeout is the one the binding's resilience configuration carries, and an
operation that needs longer than the binding allows declares its own. A command
that cannot be run at all is a transport failure that never left, `retryable`:
nothing was started. A command that ran out of time is a transport failure
too, but it was started and may have done its work before it was stopped, so it
is `ambiguous`. A command that ran and exited non-zero is the system answering
no, and is terminal.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from oska.config import ResilienceConfig
from oska.connectors.base import (
    BaseConnector,
    ConnectorError,
    refused,
    rejected,
    succeeded,
    transport_failure,
    unanswered,
)
from oska.contracts.common import ConnectorResult

__all__ = ["ShellConnector"]

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED = (str, int, float, bool)
_END_OF_OPTIONS = "--"


class ShellConnector(BaseConnector):
    """Serves declared operations by running a declared command."""

    def __init__(
        self,
        id: str,
        operations: dict[str, dict] | None = None,
        *,
        resilience: ResilienceConfig | None = None,
    ) -> None:
        super().__init__(id, resilience)
        self._operations = dict(operations or {})

    def operations(self) -> list[str]:
        """Names of the operations this binding declares."""
        return sorted(self._operations)

    def _perform(self, operation: str, payload: dict) -> ConnectorResult:
        """Run the command declared for `operation`."""
        spec = self._operations.get(operation)
        if spec is None:
            return rejected(f"undeclared operation {operation}")
        try:
            argv = _render(_argv_of(spec), payload)
        except ConnectorError as error:
            return rejected(str(error))
        if not argv:
            return rejected(f"{operation} declares no command")
        cwd = spec.get("cwd")
        timeout = float(spec.get("timeout", self.timeout))
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
            return unanswered(f"{operation} timed out after {timeout}s")
        except OSError as error:
            return transport_failure(f"{argv[0]} could not be run: {error}")
        data = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
        }
        if completed.returncode == 0:
            return succeeded(data)
        return refused(f"exit status {completed.returncode}").model_copy(update={"data": data})


def _argv_of(spec: dict) -> list[str]:
    argv = spec.get("argv") or []
    return [str(part) for part in argv]


def _render(argv: list[str], payload: dict) -> list[str]:
    """Fill the placeholders, one argument at a time.

    A value may not turn an argument into an option. When the declared part does
    not start with `-` and the filled one does, the dash came from the payload,
    and the command would read the value as a flag of its own; that is refused
    unless an earlier argument is `--`, after which the command reads no options.
    """
    rendered: list[str] = []
    options_ended = False
    for part in argv:
        filled = _PLACEHOLDER.sub(lambda match: _value(match.group(1), payload), part)
        if not options_ended and filled.startswith("-") and not part.startswith("-"):
            raise ConnectorError(
                f"a payload value would start the argument {filled!r} with '-', where the "
                "command would read it as an option; declare '--' before it to allow that"
            )
        options_ended = options_ended or part == _END_OF_OPTIONS
        rendered.append(filled)
    return rendered


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
