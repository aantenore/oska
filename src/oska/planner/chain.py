"""Several model profiles behind one provider, tried in order.

A completion goes to the first chat profile and moves to the next only when the
failure is one another profile might not have: the endpoint could not be
reached, timed out, was busy or down, or answered with something that could not
be read. A request an endpoint refused as it was written raises at once, because
the next endpoint would be handed the same request and the failure would only
be hidden. Embeddings go to the embeddings profiles the same way.

This provider sits under the model budget, so a completion that took two
profiles to answer is still the one call the turn asked for. Which profile
answered each call, and every move from one profile to the next with the
status or the class of the failure that caused it, are recorded for the turn
under way, never the content of a call. The record belongs to the turn, as the
budget's count does, so two overlapping turns never read each other's.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeVar

from oska.contracts.common import Message
from oska.planner.provider import ModelProvider, ModelProviderError

__all__ = [
    "FallbackProvider",
    "ProfileLog",
    "fallback_reason",
    "profile_log",
    "recording_profiles",
    "start_profile_log",
]

CHAT = "chat"
EMBEDDINGS = "embeddings"

_Answer = TypeVar("_Answer")


@dataclass
class ProfileLog:
    """Which profile answered each call of one turn, and every fallback on the way."""

    answered: list[dict[str, str]] = field(default_factory=list)
    fallbacks: list[dict[str, str]] = field(default_factory=list)

    def as_event(self) -> dict:
        """What the audit records: profile names, roles and reasons, never content."""
        return {
            "answered": [dict(entry) for entry in self.answered],
            "fallbacks": [dict(entry) for entry in self.fallbacks],
        }


_LOG: ContextVar[ProfileLog | None] = ContextVar("oska_model_profiles", default=None)


def profile_log() -> ProfileLog | None:
    """The record of the turn under way in this context, if a turn is recording."""
    return _LOG.get()


def start_profile_log() -> None:
    """Begin recording a turn in this context, and no other."""
    _LOG.set(ProfileLog())


@contextmanager
def recording_profiles() -> Iterator[ProfileLog]:
    """Record one turn's profiles, and leave the context as the turn found it."""
    log = ProfileLog()
    token = _LOG.set(log)
    try:
        yield log
    finally:
        _LOG.reset(token)


def fallback_reason(error: ModelProviderError) -> str:
    """Why a profile was left: the HTTP status, else the class of the failure."""
    if error.status is not None:
        return f"HTTP {error.status}"
    cause = error.__cause__
    return type(cause).__name__ if cause is not None else type(error).__name__


class FallbackProvider:
    """Chat and embedding profiles in order, each tried only when the one before it could not.

    `chat` and `embeddings` are (profile name, provider) pairs in the order they
    are tried. `embeds` is true exactly when some profile serves embeddings.
    """

    name = "profiles"

    def __init__(
        self,
        chat: Sequence[tuple[str, ModelProvider]],
        embeddings: Sequence[tuple[str, ModelProvider]] = (),
    ) -> None:
        if not chat:
            raise ModelProviderError("no chat profile is usable")
        self._chat = list(chat)
        self._embeddings = list(embeddings)
        self.embeds = bool(self._embeddings)

    @property
    def profiles(self) -> dict[str, list[str]]:
        """The profile names of each role, in the order they are tried."""
        return {
            CHAT: [name for name, _ in self._chat],
            EMBEDDINGS: [name for name, _ in self._embeddings],
        }

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Answer through the first chat profile that can."""
        return _first_answer(
            CHAT,
            self._chat,
            lambda provider: provider.complete_json(
                system, messages, schema, temperature=temperature
            ),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed through the first embeddings profile that can."""
        if not self._embeddings:
            raise ModelProviderError("no profile serves embeddings")
        return _first_answer(EMBEDDINGS, self._embeddings, lambda provider: provider.embed(texts))

    def close(self) -> None:
        """Release every profile's connections, once each."""
        seen: set[int] = set()
        for _, provider in [*self._chat, *self._embeddings]:
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "close", None)
            if callable(close):
                close()


def _first_answer(
    role: str,
    profiles: list[tuple[str, ModelProvider]],
    call: Callable[[ModelProvider], _Answer],
) -> _Answer:
    log = _LOG.get()
    for index, (name, provider) in enumerate(profiles):
        try:
            answer = call(provider)
        except ModelProviderError as exc:
            following = profiles[index + 1][0] if index + 1 < len(profiles) else None
            if not exc.retryable or following is None:
                raise
            if log is not None:
                log.fallbacks.append(
                    {"role": role, "from": name, "to": following, "reason": fallback_reason(exc)}
                )
            continue
        if log is not None:
            log.answered.append({"role": role, "profile": name})
        return answer
    raise ModelProviderError(f"no {role} profile is usable")
