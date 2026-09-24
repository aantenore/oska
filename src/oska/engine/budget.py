"""What one turn may spend on model calls, and the provider that holds it to that.

The model budget bounds what one turn may spend: past it the model is not
called again, the turn says so and answers from what it already has. That count
belongs to the turn and not to the engine, because one engine serves
overlapping turns.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from oska.contracts.common import Message
from oska.planner.provider import ModelProvider, ModelProviderError

__all__ = ["BudgetExceeded", "BudgetedProvider", "ModelBudget"]


class BudgetExceeded(ModelProviderError):
    """Raised instead of calling the model once the turn has spent its budget."""


class _Spend:
    """What the turn under way has spent. One of these per turn, and no wider."""

    __slots__ = ("refused", "spent")

    def __init__(self) -> None:
        self.spent = 0
        self.refused = 0


_SPEND: ContextVar[_Spend | None] = ContextVar("oska_model_spend", default=None)


class ModelBudget:
    """What one turn may spend on model calls, counted per turn and not per engine.

    One process holds one engine and serves many turns through it: a synchronous
    HTTP handler runs in a thread of a pool, and two turns overlap. So the limit
    belongs to the engine, which declares it once, and the count belongs to the
    turn, which is why it is held in a context variable a turn sets for as long
    as it runs. A worker thread, a task or a command each carry their own
    context and count their own turn; one context runs one turn at a time, so
    the wrapper around the provider still counts every call in one place.

    Drafting a candidate from a session runs as a turn of its own, with a fresh
    budget of its own: it opens its own context rather than spending whatever
    turn last ran in this context, which would refuse it for a reason that has
    nothing to do with it, or let it spend a budget another turn already used.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(0, int(limit))

    @property
    def limit(self) -> int:
        """How many calls one turn may make."""
        return self._limit

    @property
    def spent(self) -> int:
        """How many calls this turn has made."""
        spend = _SPEND.get()
        return 0 if spend is None else spend.spent

    @property
    def refused(self) -> int:
        """How many calls this turn asked for past its budget."""
        spend = _SPEND.get()
        return 0 if spend is None else spend.refused

    @property
    def exhausted(self) -> bool:
        """Whether the next call of the turn under way would be refused."""
        spend = _SPEND.get()
        return spend is not None and spend.spent >= self._limit

    def start_turn(self) -> None:
        """Begin a turn with the whole budget, in this context and no other."""
        _SPEND.set(_Spend())

    @contextmanager
    def for_turn(self) -> Iterator[None]:
        """Count one turn's calls, and leave the context as the turn found it."""
        token = _SPEND.set(_Spend())
        try:
            yield
        finally:
            _SPEND.reset(token)

    def take(self) -> bool:
        """Book one call, or report that this turn has nothing left to spend."""
        spend = _SPEND.get()
        if spend is None:
            return True
        if spend.spent >= self._limit:
            spend.refused += 1
            return False
        spend.spent += 1
        return True

    def as_event(self) -> dict:
        """What the audit records about this turn's spend."""
        return {"limit": self._limit, "spent": self.spent, "refused": self.refused}


class BudgetedProvider:
    """A model provider that refuses a completion once the turn is over budget.

    Embeddings are not counted. They are how the index reads the catalog, they
    cost the same whatever the turn decides, and a turn that could not rank the
    catalog would have nothing to answer from. What the budget bounds is the
    reasoning: classification, planning, tie breaking and phrasing.
    """

    def __init__(self, provider: ModelProvider, budget: ModelBudget) -> None:
        self._provider = provider
        self._budget = budget
        self.name = provider.name

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Answer through the provider, unless this turn has spent its budget."""
        if not self._budget.take():
            raise BudgetExceeded(
                f"a turn may make {self._budget.limit} model call(s) and this one has "
                "made them all"
            )
        return self._provider.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed through the provider; embeddings are not part of the budget."""
        return self._provider.embed(texts)

