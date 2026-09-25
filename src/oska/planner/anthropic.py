"""Model provider for any endpoint that speaks the messages protocol with tool use.

The protocol has no response format to constrain an answer to a schema, but it
lets a call require one tool: the schema is declared as the input of a single
tool the model must call, and the input it calls it with is the answer. The
protocol serves no embeddings, so this provider says it does not embed and the
runtime ranks the catalog another way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from oska.config import ModelConfig, ModelProfile
from oska.contracts.common import Message
from oska.planner.http_errors import raise_for_status, send
from oska.planner.provider import ModelProviderError

__all__ = ["DEFAULT_MAX_TOKENS", "PROTOCOL_VERSION", "AnthropicProvider"]

PROTOCOL_VERSION = "2023-06-01"
"""The protocol version the request and answer shapes here are written against."""
DEFAULT_MAX_TOKENS = 4096
"""The bound on an answer when the configuration sets none; the protocol requires one."""

_MESSAGES_PATH = "/v1/messages"
_TOOL_NAME = "respond"
_TOOL_DESCRIPTION = "Give the answer, as an object that satisfies the input schema."
_OPENING_TURN = "Begin."


class AnthropicProvider:
    """Completions over the messages protocol, answered through one required tool.

    System messages inside the conversation are folded into the system prompt
    in the order they came, consecutive messages of one role are merged, and a
    conversation that would open with the assistant is opened with a minimal
    user turn, because the protocol requires turns to alternate and the first
    to be the user's. Headers given with the profile go with every call and may
    replace the protocol version sent by default.
    """

    name = "anthropic"
    embeds = False

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        chat_model: str = "",
        timeout: float = 30.0,
        *,
        max_tokens: int | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._chat_model = chat_model
        self._max_tokens = max_tokens or DEFAULT_MAX_TOKENS
        sent = {"content-type": "application/json", "anthropic-version": PROTOCOL_VERSION}
        if api_key:
            sent["x-api-key"] = api_key
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
    ) -> AnthropicProvider:
        """Build a provider from the flat model configuration."""
        if not model_config.base_url:
            raise ModelProviderError("no base_url is configured for the model provider")
        return cls(
            base_url=model_config.base_url,
            api_key=model_config.api_key,
            chat_model=model_config.chat_model,
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
    ) -> AnthropicProvider:
        """Build a provider from one profile of the model configuration and its key."""
        return cls(
            base_url=profile.base_url,
            api_key=api_key,
            chat_model=profile.chat_model,
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
        """Return the input the model called the answering tool with."""
        prompt, turns = _conversation(system, messages)
        body: dict[str, Any] = {
            "model": self._chat_model,
            "max_tokens": self._max_tokens,
            "temperature": temperature,
            "messages": turns,
            "tools": [
                {"name": _TOOL_NAME, "description": _TOOL_DESCRIPTION, "input_schema": schema}
            ],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }
        if prompt:
            body["system"] = prompt
        response = send(self._client, _MESSAGES_PATH, body)
        raise_for_status(response)
        return _tool_input(response, self._client.base_url)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """The protocol serves no embeddings; asking for them is a configuration error."""
        raise ModelProviderError("the messages protocol serves no embeddings")


def _conversation(system: str, messages: list[Message]) -> tuple[str, list[dict[str, str]]]:
    """The system prompt with every system message folded in, and alternating turns."""
    prompts = [system] if system else []
    turns: list[dict[str, str]] = []
    for message in messages:
        if message.role == "system":
            prompts.append(message.content)
            continue
        if turns and turns[-1]["role"] == message.role:
            turns[-1]["content"] = f"{turns[-1]['content']}\n\n{message.content}"
            continue
        turns.append({"role": message.role, "content": message.content})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": _OPENING_TURN})
    return "\n\n".join(prompts), turns


def _tool_input(response: httpx.Response, base_url: httpx.URL) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelProviderError(
            f"the endpoint returned a body that is not JSON: {exc}", retryable=True
        ) from exc
    blocks = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                answer = block.get("input")
                if isinstance(answer, dict):
                    return answer
    raise ModelProviderError(f"{base_url}: the model called no tool with an answer", retryable=True)
