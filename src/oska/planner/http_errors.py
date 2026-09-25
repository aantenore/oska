"""How an HTTP adapter turns an upstream failure into a provider error.

Which failures are worth handing to another provider is decided here once, so
every adapter draws the line in the same place. A failure of transport, a
timeout, a request the upstream timed out on, a rate limit and a server error
are retryable: another endpoint may well answer. A request the upstream refused
as written is not: malformed, unauthorized, forbidden, unknown or unprocessable,
it would be refused again, and sending it on would only hide a misconfiguration
and spend the time of every endpoint after it.
"""

from __future__ import annotations

import json

import httpx

from oska.planner.provider import ModelProviderError

__all__ = ["RETRYABLE_STATUSES", "error_text", "raise_for_status", "send", "status_error"]

RETRYABLE_STATUSES = frozenset({408, 429})
"""Statuses below the server range that another endpoint may answer."""

_SERVER_ERRORS = range(500, 600)
_FAILURE_FROM = 400
_TEXT_LIMIT = 400


def send(client: httpx.Client, path: str, body: dict) -> httpx.Response:
    """Post a body; a failure to reach the endpoint is a retryable provider error."""
    try:
        return client.post(path, json=body)
    except httpx.HTTPError as exc:
        raise ModelProviderError(f"{path}: {exc}", retryable=True) from exc


def raise_for_status(response: httpx.Response) -> None:
    """Raise the provider error an upstream failure amounts to, if it is one."""
    if response.status_code >= _FAILURE_FROM:
        raise status_error(response)


def status_error(response: httpx.Response) -> ModelProviderError:
    """The provider error for a failed response, retryable where another endpoint may answer."""
    status = response.status_code
    return ModelProviderError(
        f"{response.request.url.path}: HTTP {status}: {error_text(response)}",
        retryable=status in RETRYABLE_STATUSES or status in _SERVER_ERRORS,
        status=status,
    )


def error_text(response: httpx.Response) -> str:
    """The shortest useful description of an upstream failure."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:_TEXT_LIMIT]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(error, str):
        return error
    return json.dumps(payload)[:_TEXT_LIMIT]
