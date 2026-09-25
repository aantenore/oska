"""Model provider protocol shared by the planner, the router and the embedding index."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from oska.contracts.common import Message


class ModelProviderError(RuntimeError):
    """Raised when a provider cannot produce a usable response.

    `retryable` says whether another provider might answer the same call: true
    for a failure of transport, a timeout, an upstream that is busy or down, or
    an answer that could not be read; false for a request the upstream refused
    as it was written, which another provider would only be handed unchanged.
    `status` is the HTTP status of the failure, when there was one.
    """

    def __init__(
        self, message: str = "", *, retryable: bool = False, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


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


def provider_embeds(provider: object) -> bool:
    """Whether a provider can embed. A provider that does not say is taken to embed.

    A provider that cannot sets `embeds` to false, and the runtime then ranks
    the catalog by keyword instead of asking it for vectors.
    """
    return bool(getattr(provider, "embeds", True))
