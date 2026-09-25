"""Model provider for any endpoint that speaks the chat-completions protocol."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import httpx

from oska.config import ModelConfig, ModelProfile
from oska.contracts.common import Message
from oska.planner.http_errors import raise_for_status, send
from oska.planner.provider import ModelProviderError

_JSON_INSTRUCTION = (
    "Answer with a single JSON object and nothing else. No prose, no code fence. "
    "The object must satisfy this JSON schema:\n"
)
_REJECTED_FORMAT = re.compile(r"response_format|json_schema|schema|structured", re.IGNORECASE)
_FORMAT_REFUSALS = frozenset({400, 422})
_CHAT_PATH = "/chat/completions"


class OpenAICompatibleProvider:
    """Chat completions and embeddings over HTTP.

    `complete_json` asks for the answer in three ways, each only when the one
    before it was refused as a request the endpoint cannot serve (HTTP 400 or
    422 naming the response format, the schema or structured output): a
    response constrained to the JSON schema, then a JSON object with the schema
    written into the system message, then no response format at all with the
    schema still in the system message. The first JSON object in the answer is
    taken. `max_tokens` is sent only when it is given, and `headers` go with
    every call.
    """

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        chat_model: str = "",
        embedding_model: str = "",
        timeout: float = 30.0,
        *,
        max_tokens: int | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._max_tokens = max_tokens
        sent = {"content-type": "application/json"}
        if api_key:
            sent["authorization"] = f"Bearer {api_key}"
        sent.update(headers or {})
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=sent,
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_config(
        cls,
        model_config: ModelConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> OpenAICompatibleProvider:
        """Build a provider from the model configuration."""
        if not model_config.base_url:
            raise ModelProviderError("no base_url is configured for the model provider")
        return cls(
            base_url=model_config.base_url,
            api_key=model_config.api_key,
            chat_model=model_config.chat_model,
            embedding_model=model_config.embedding_model,
            timeout=model_config.timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_profile(
        cls,
        profile: ModelProfile,
        api_key: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> OpenAICompatibleProvider:
        """Build a provider from one profile of the model configuration and its key."""
        return cls(
            base_url=profile.base_url,
            api_key=api_key,
            chat_model=profile.chat_model,
            embedding_model=profile.embedding_model,
            timeout=profile.timeout_seconds,
            max_tokens=profile.max_tokens,
            headers=profile.headers,
            transport=transport,
        )

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Return the JSON object the model produced for this exchange."""
        instructed = f"{system}\n\n{_JSON_INSTRUCTION}{json.dumps(schema)}"
        constrained = {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
        response = self._post(_CHAT_PATH, self._body(system, messages, temperature, constrained))
        for prompt, response_format in ((instructed, {"type": "json_object"}), (instructed, None)):
            if not _rejects_response_format(response):
                break
            response = self._post(
                _CHAT_PATH, self._body(prompt, messages, temperature, response_format)
            )
        raise_for_status(response)
        return _first_json_object(_content(response, self._client.base_url))

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per text, in the order they were given."""
        if not texts:
            return []
        response = self._post(
            "/embeddings", {"model": self._embedding_model, "input": list(texts)}
        )
        raise_for_status(response)
        payload = _json_body(response)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ModelProviderError(
                "the embeddings endpoint returned an unexpected payload", retryable=True
            )
        ordered = sorted(rows, key=lambda row: row.get("index", 0))
        vectors: list[list[float]] = []
        for row in ordered:
            vector = row.get("embedding")
            if not isinstance(vector, list):
                raise ModelProviderError(
                    "the embeddings endpoint returned a row without a vector", retryable=True
                )
            vectors.append([float(value) for value in vector])
        return vectors

    def _body(
        self,
        system: str,
        messages: list[Message],
        temperature: float,
        response_format: dict | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._chat_model,
            "temperature": temperature,
            "messages": _chat_messages(system, messages),
        }
        if response_format is not None:
            body["response_format"] = response_format
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        return body

    def _post(self, path: str, body: dict) -> httpx.Response:
        return send(self._client, path, body)


def _chat_messages(system: str, messages: list[Message]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        *[{"role": message.role, "content": message.content} for message in messages],
    ]


def _rejects_response_format(response: httpx.Response) -> bool:
    """Whether the endpoint refused the way the answer was asked for, not the request."""
    return response.status_code in _FORMAT_REFUSALS and bool(
        _REJECTED_FORMAT.search(response.text)
    )


def _json_body(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelProviderError(
            f"the endpoint returned a body that is not JSON: {exc}", retryable=True
        ) from exc
    if not isinstance(payload, dict):
        raise ModelProviderError(
            "the endpoint returned a body that is not a JSON object", retryable=True
        )
    return payload


def _content(response: httpx.Response, base_url: httpx.URL) -> str:
    payload = _json_body(response)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelProviderError(f"{base_url}: the model returned no choice", retryable=True)
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelProviderError(
            f"{base_url}: the model returned an empty message", retryable=True
        )
    return content


def _first_json_object(text: str) -> dict:
    """Extract the first balanced JSON object from a model answer."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : position + 1]
                    try:
                        parsed = json.loads(candidate)
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    raise ModelProviderError(
        f"the model answer holds no JSON object: {text.strip()[:200]}", retryable=True
    )
