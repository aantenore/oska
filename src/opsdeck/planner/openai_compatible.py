"""Model provider for any endpoint that speaks the chat-completions protocol."""

from __future__ import annotations

import json
from typing import Any

import httpx

from opsdeck.config import ModelConfig
from opsdeck.contracts.common import Message
from opsdeck.planner.provider import ModelProviderError

_JSON_INSTRUCTION = (
    "Answer with a single JSON object and nothing else. No prose, no code fence. "
    "The object must satisfy this JSON schema:\n"
)


class OpenAICompatibleProvider:
    """Chat completions and embeddings over HTTP.

    `complete_json` asks for a structured response with `response_format`. An
    endpoint that rejects that field (HTTP 400 naming it) is retried once with
    the schema written into the system message, and the first JSON object in
    the answer is taken.
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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
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
        body: dict[str, Any] = {
            "model": self._chat_model,
            "temperature": temperature,
            "messages": _chat_messages(system, messages),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema},
            },
        }
        response = self._post("/chat/completions", body)
        if _rejects_response_format(response):
            fallback = dict(body)
            fallback.pop("response_format")
            fallback["messages"] = _chat_messages(
                f"{system}\n\n{_JSON_INSTRUCTION}{json.dumps(schema)}", messages
            )
            response = self._post("/chat/completions", fallback)
        self._raise_for_status(response)
        return _first_json_object(_content(response, self._client.base_url))

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per text, in the order they were given."""
        if not texts:
            return []
        response = self._post(
            "/embeddings", {"model": self._embedding_model, "input": list(texts)}
        )
        self._raise_for_status(response)
        payload = _json_body(response)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ModelProviderError("the embeddings endpoint returned an unexpected payload")
        ordered = sorted(rows, key=lambda row: row.get("index", 0))
        vectors: list[list[float]] = []
        for row in ordered:
            vector = row.get("embedding")
            if not isinstance(vector, list):
                raise ModelProviderError("the embeddings endpoint returned a row without a vector")
            vectors.append([float(value) for value in vector])
        return vectors

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            return self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise ModelProviderError(f"{path}: {exc}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise ModelProviderError(
                f"{response.request.url.path}: HTTP {response.status_code}: {_error_text(response)}"
            )


def _chat_messages(system: str, messages: list[Message]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        *[{"role": message.role, "content": message.content} for message in messages],
    ]


def _rejects_response_format(response: httpx.Response) -> bool:
    return response.status_code == 400 and "response_format" in response.text.lower()


def _json_body(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelProviderError(f"the endpoint returned a body that is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelProviderError("the endpoint returned a body that is not a JSON object")
    return payload


def _content(response: httpx.Response, base_url: httpx.URL) -> str:
    payload = _json_body(response)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelProviderError(f"{base_url}: the model returned no choice")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelProviderError(f"{base_url}: the model returned an empty message")
    return content


def _error_text(response: httpx.Response) -> str:
    """The shortest useful description of an upstream failure."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:400]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(error, str):
        return error
    return json.dumps(payload)[:400]


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
    raise ModelProviderError(f"the model answer holds no JSON object: {text.strip()[:200]}")
