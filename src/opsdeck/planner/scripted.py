"""Model provider that replays recorded answers, so tests run offline."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from opsdeck.contracts.common import Message
from opsdeck.planner.provider import ModelProviderError

EMBEDDING_DIMENSIONS = 256

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ScriptedModelProvider:
    """Answers taken in order from a script.

    Each step is `{"response": {...}, "expect_contains": "...", "purpose": "..."}`.
    `expect_contains` is a substring the last user message must hold; it turns a
    script into an assertion about the prompt that reached the provider.
    Embeddings are a deterministic bag of hashed tokens, so cosine similarity
    behaves like token overlap and needs no network.
    """

    name = "scripted"

    def __init__(
        self,
        script: list[dict] | Path | str,
        *,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ) -> None:
        self._script = _read_script(script)
        self._dimensions = dimensions
        self._index = 0
        self._last_purpose = ""

    def remaining(self) -> int:
        """How many steps have not been consumed."""
        return len(self._script) - self._index

    def consumed(self) -> int:
        """How many steps have been consumed."""
        return self._index

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Return the next recorded answer."""
        last_user = _last_user_message(messages)
        if self._index >= len(self._script):
            raise ModelProviderError(
                f"the script is exhausted after {self._index} step(s); "
                f"the last one was {self._last_purpose or 'unlabelled'!r} and "
                f"the request that found no answer was: {_excerpt(last_user)}"
            )
        step = self._script[self._index]
        expected = step.get("expect_contains")
        purpose = str(step.get("purpose", "")) or f"step {self._index + 1}"
        if expected and expected not in last_user:
            raise ModelProviderError(
                f"script step {self._index + 1} ({purpose}) expected the last user message to "
                f"contain {expected!r}, but it was: {_excerpt(last_user)}"
            )
        self._index += 1
        self._last_purpose = purpose
        response = step.get("response")
        if not isinstance(response, dict):
            raise ModelProviderError(
                f"script step {self._index} ({purpose}) has no response object"
            )
        return json.loads(json.dumps(response))

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one deterministic vector per text."""
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        return hash_bag_vector(text, self._dimensions)


def hash_bag_vector(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """A deterministic unit vector: hashed tokens counted into fixed buckets.

    Cosine similarity over these vectors behaves like token overlap, which is
    enough to rank a catalog offline. Shared with the replay provider, so a text
    a cassette never saw is embedded the same way everywhere.
    """
    vector = [0.0] * dimensions
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).digest()
        vector[int.from_bytes(digest, "big") % dimensions] += 1.0
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def _read_script(script: list[dict] | Path | str) -> list[dict]:
    if isinstance(script, (Path, str)):
        with Path(script).open(encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = script
    if isinstance(data, dict):
        for key in ("provider_script", "script"):
            if key in data:
                data = data[key]
                break
    if not isinstance(data, list):
        raise ModelProviderError("a script is a list of steps")
    steps: list[dict] = []
    for position, step in enumerate(data, start=1):
        if not isinstance(step, dict):
            raise ModelProviderError(f"script step {position} is not an object")
        steps.append(step)
    return steps


def _last_user_message(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _excerpt(text: str, limit: int = 200) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return repr(stripped)
    return repr(f"{stripped[:limit]}...")
