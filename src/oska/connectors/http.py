"""A connector that calls an HTTP API.

Each operation declares a method and a path template. Placeholders in the path
are filled from the payload; the fields listed in `query_fields` become the
query string and those in `body_fields` the JSON body. Nothing else of the
payload leaves the process.

A value put into the path is one segment and nothing more. It is
percent-encoded whole, `/`, `?`, `#` and `%` included, so an identifier cannot
climb out of its segment, start a query or cut the path short, and reach an
endpoint nobody declared. A value that would still be read as a step up or a
step in place (`.` or `..`), or that is empty, names no segment at all and is
refused before anything is sent.

The timeout comes from the resilience configuration of the binding, the same
value every kind of connector takes it from. What happened to a call decides
how its failure is classified, and the question asked is whether the request
left:

- it never left — the connection was refused, the name did not resolve, no
  connection in the pool was free before the timeout — and it is `retryable`;
- it left and no usable answer came back — a read timeout, a connection reset
  after the request was sent, a server error — and it is `ambiguous`, because
  the API may have applied it;
- the API answered no — any 4xx, a conflict included — and it is `terminal`, and
  so is a path this payload cannot fill, which is never sent at all.

A response body is read as a stream and no further than `max_response_bytes`.
A successful answer larger than that is refused, terminal, with the budget
named in the reason; an error answer is only ever read as far as the part of it
the error message keeps.

Every call with a method that is not safe carries an `Idempotency-Key` header.
The caller hands in the key derived from the ledger key of the write; a call
made without one carries a key derived from what it sends. The key is derived
locally, so it is only as useful as the API behind the binding makes it: an API
that honors the header collapses a repeated request into the first, and one
that ignores it is no worse off.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from oska.config import ResilienceConfig
from oska.connectors.base import (
    BaseConnector,
    CallOptions,
    ConnectorError,
    refused,
    rejected,
    succeeded,
    transport_failure,
    unanswered,
)
from oska.contracts.common import ConnectorResult

__all__ = ["IDEMPOTENCY_HEADER", "HttpConnector"]

DEFAULT_ERROR_BODY = 500
SERVER_ERROR = 500
IDEMPOTENCY_HEADER = "Idempotency-Key"
_BODYLESS = frozenset({"GET", "HEAD"})
_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Segments a server reads as a step rather than a name, whatever they are encoded as.
_NOT_A_SEGMENT = frozenset({"", ".", ".."})
# The request provably did not leave: no connection was made to send it on.
_NEVER_LEFT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# The request could not be sent as addressed at all.
_NOT_SENDABLE = (httpx.UnsupportedProtocol,)


class _TooLarge(Exception):
    """A response body ran past the budget of the binding."""


class HttpConnector(BaseConnector):
    """Serves declared operations from an HTTP API."""

    def __init__(
        self,
        id: str,
        base_url: str,
        headers: dict[str, str] | None = None,
        operations: dict[str, dict] | None = None,
        resilience: ResilienceConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        error_body_limit: int = DEFAULT_ERROR_BODY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(id, resilience, sleep=sleep)
        self.base_url = base_url
        self._operations = dict(operations or {})
        self._error_body_limit = error_body_limit
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            timeout=self.timeout,
            transport=transport,
        )

    def operations(self) -> list[str]:
        """Names of the operations this binding declares."""
        return sorted(self._operations)

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def _is_write(self, operation: str, options: CallOptions) -> bool:
        """A call is a write when the caller says so or its method is not safe."""
        return options.write or self._method(operation) not in _SAFE

    def _method(self, operation: str) -> str:
        spec = self._operations.get(operation) or {}
        return str(spec.get("method", "GET")).upper()

    def _perform_call(
        self, operation: str, payload: dict, options: CallOptions
    ) -> ConnectorResult:
        """Call the endpoint declared for `operation`."""
        spec = self._operations.get(operation)
        if spec is None:
            return rejected(f"undeclared operation {operation}")
        method = self._method(operation)
        try:
            path = _render(str(spec.get("path") or "/"), payload)
        except ConnectorError as error:
            return rejected(str(error))
        query = _pick(spec.get("query_fields"), payload)
        body = _pick(spec.get("body_fields"), payload)
        headers: dict[str, str] = {}
        if method not in _SAFE:
            headers[IDEMPOTENCY_HEADER] = options.idempotency_key or _derived_key(
                self.id, operation, path, query, body
            )
        try:
            request = self._client.build_request(
                method,
                path,
                params=query or None,
                json=None if method in _BODYLESS else body,
                headers=headers or None,
            )
            response = self._client.send(request, stream=True)
        except _NEVER_LEFT as error:
            return transport_failure(f"{type(error).__name__}: {error}")
        except _NOT_SENDABLE as error:
            return rejected(f"{type(error).__name__}: {error}")
        except httpx.RequestError as error:
            return unanswered(f"{type(error).__name__}: {error}")
        except httpx.HTTPError as error:
            return rejected(f"{type(error).__name__}: {error}")
        try:
            return self._result(operation, response)
        except httpx.RequestError as error:
            return unanswered(f"{type(error).__name__}: {error}", response.status_code)
        finally:
            response.close()

    def _result(self, operation: str, response: httpx.Response) -> ConnectorResult:
        status = response.status_code
        encoding = response.charset_encoding
        if 200 <= status < 300:
            budget = self.resilience.max_response_bytes
            try:
                content = _read_within(response, budget)
            except _TooLarge:
                return refused(
                    f"the response to {operation} is larger than the budget of "
                    f"{budget} bytes, so it was not read",
                    status,
                )
            return succeeded(_payload_of(content, encoding)).model_copy(
                update={"status_code": status}
            )
        text = _text(_read_prefix(response, self._error_body_limit), encoding)
        error = f"http {status}: {text[: self._error_body_limit]}"
        if status >= SERVER_ERROR:
            return unanswered(error, status)
        return refused(error, status)


def _read_within(response: httpx.Response, budget: int) -> bytes:
    """The whole body, or `_TooLarge` as soon as it is known to exceed the budget."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > budget:
        raise _TooLarge
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > budget:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _read_prefix(response: httpx.Response, limit: int) -> bytes:
    """At least the first `limit` bytes of the body, and no more than it takes."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    return b"".join(chunks)


def _derived_key(
    integration_id: str, operation: str, path: str, query: dict, body: dict
) -> str:
    """A key for a call the caller gave none: the same request always gets the same key."""
    material = json.dumps(
        [integration_id, operation, path, query, body],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _render(template: str, payload: dict) -> str:
    """The path of a call: the template, each placeholder filled by one encoded segment."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in payload:
            raise ConnectorError(f"the payload carries no field {name}")
        return _segment(name, payload[name])

    return _PLACEHOLDER.sub(replace, template)


def _segment(name: str, value: object) -> str:
    """One payload value as one path segment, percent-encoded, or a refusal."""
    text = str(value)
    if text in _NOT_A_SEGMENT:
        raise ConnectorError(f"the value of {name} is not a path segment: {text!r}")
    return quote(text, safe="")


def _pick(fields: list[str] | None, payload: dict) -> dict[str, Any]:
    return {name: payload[name] for name in fields or [] if name in payload}


def _text(content: bytes, encoding: str | None) -> str:
    try:
        return content.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _payload_of(content: bytes, encoding: str | None) -> dict:
    text = _text(content, encoding)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"text": text}
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed}
