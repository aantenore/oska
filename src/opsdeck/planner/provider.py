"""Model provider protocol shared by the planner, the router and the embedding index."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from opsdeck.contracts.common import Message


class ModelProviderError(RuntimeError):
    """Raised when a provider cannot produce a usable response."""


@runtime_checkable
class ModelProvider(Protocol):
    """Minimal surface the runtime needs from a language model backend.

    `complete_json` returns a JSON object that conforms to `schema`.
    `embed` returns one vector per input text, all of the same length.
    """

    name: str

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
