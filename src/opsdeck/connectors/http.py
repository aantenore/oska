"""A connector that calls an HTTP API.

Each operation declares a method and a path template. Placeholders in the path
are filled from the payload; the fields listed in `query_fields` become the
query string and those in `body_fields` the JSON body. Nothing else of the
payload leaves the process.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from opsdeck.connectors.base import ConnectorError, timed
from opsdeck.contracts.common import ConnectorResult

__all__ = ["HttpConnector"]

DEFAULT_TIMEOUT = 30.0
DEFAULT_ERROR_BODY = 500
_BODYLESS = frozenset({"GET", "HEAD"})
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class HttpConnector:
    """Serves declared operations from an HTTP API."""

    def __init__(
        self,
        id: str,
        base_url: str,
        headers: dict[str, str] | None = None,
        operations: dict[str, dict] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        *,
        error_body_limit: int = DEFAULT_ERROR_BODY,
    ) -> None:
        self.id = id
        self.base_url = base_url
        self._operations = dict(operations or {})
        self._error_body_limit = error_body_limit
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            timeout=timeout,
            transport=transport,
        )

    def operations(self) -> list[str]:
        """Names of the operations this binding declares."""
        return sorted(self._operations)

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    @timed
    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        """Call the endpoint declared for `operation`."""
        spec = self._operations.get(operation)
        if spec is None:
            return ConnectorResult(ok=False, error=f"undeclared operation {operation}")
        method = str(spec.get("method", "GET")).upper()
        try:
            path = _render(str(spec.get("path") or "/"), payload)
        except ConnectorError as error:
            return ConnectorResult(ok=False, error=str(error))
        query = _pick(spec.get("query_fields"), payload)
        body = _pick(spec.get("body_fields"), payload)
        try:
            response = self._client.request(
                method,
                path,
                params=query or None,
                json=None if method in _BODYLESS else body,
            )
        except httpx.HTTPError as error:
            return ConnectorResult(ok=False, error=f"{type(error).__name__}: {error}")
        return self._result(response)

    def _result(self, response: httpx.Response) -> ConnectorResult:
        if response.status_code < 200 or response.status_code >= 300:
            body = response.text[: self._error_body_limit]
            return ConnectorResult(ok=False, error=f"http {response.status_code}: {body}")
        return ConnectorResult(ok=True, data=_payload_of(response))


def _render(template: str, payload: dict) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in payload:
            raise ConnectorError(f"the payload carries no field {name}")
        return str(payload[name])

    return _PLACEHOLDER.sub(replace, template)


def _pick(fields: list[str] | None, payload: dict) -> dict[str, Any]:
    return {name: payload[name] for name in fields or [] if name in payload}


def _payload_of(response: httpx.Response) -> dict:
    try:
        parsed = response.json()
    except ValueError:
        return {"text": response.text}
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed}
