"""The model budget of a turn, when two turns of one runtime overlap.

One process holds one engine, and the HTTP handlers that drive it are
synchronous, so the framework runs them in a threadpool: two turns are in the
same engine at the same time. A count kept on the engine would be reset by
whichever turn started last, and the limit would stop bounding anything. These
tests run two turns at once and ask each of them what it spent.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from opsdeck.audit.sink import MemorySink
from opsdeck.config import Settings
from opsdeck.contracts.common import Message, TurnResult
from opsdeck.engine.engine import ModelBudget
from opsdeck.engine.factory import build_engine
from opsdeck.planner.scripted import hash_bag_vector

RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
CONTAINER_RUNTIME = "integration.container_runtime"
PATIENCE = 10.0

ROUTE = {
    "kind": "new_task",
    "runbook_id": "runbook.service_restart",
    "confidence": 0.9,
    "language": "en",
}
PLAN = {
    "runbook_id": "runbook.service_restart",
    "operations": [
        {
            "integration_id": CONTAINER_RUNTIME,
            "operation": "service.status",
            "payload": {"service": "search"},
            "write": False,
            "purpose": "read the current state",
        },
        {
            "integration_id": CONTAINER_RUNTIME,
            "operation": "service.restart",
            "payload": {"service": "search"},
            "write": True,
            "purpose": "restart the service",
        },
    ],
    "missing": [],
    "reply_intent": "report the restart",
}


class OverlappingProvider:
    """Answers a route and then a plan, and holds each turn until the other arrives.

    The first call of a turn waits at a gate the two turns share, so both turns
    have begun, and both have been charged for one call, before either asks for
    its second. Whatever the budget counts, it counts it with two turns inside
    the engine at once.
    """

    name = "overlapping"

    def __init__(self, gate: threading.Barrier) -> None:
        self._gate = gate
        self._turn = threading.local()
        self._lock = threading.Lock()
        self.calls = 0

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """The route, then the plan, once per thread."""
        made = getattr(self._turn, "made", 0) + 1
        self._turn.made = made
        with self._lock:
            self.calls += 1
        if made == 1:
            self._gate.wait(timeout=PATIENCE)
            return json.loads(json.dumps(ROUTE))
        return json.loads(json.dumps(PLAN))

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Deterministic vectors, so ranking the catalog needs no network."""
        return [hash_bag_vector(text) for text in texts]


def _settings(repo_root: Path, tmp_path: Path, calls: int) -> Settings:
    """Settings over the example domain, with the turn budget these tests want."""
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document["turn"] = {"max_model_calls": calls}
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(document), encoding="utf-8")
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=runtime,
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )


def _in_threads(work: Mapping[str, Callable[[], object]]) -> dict[str, object]:
    """Run each piece of work in its own thread and return what each returned."""
    answers: dict[str, object] = {}
    failures: dict[str, BaseException] = {}

    def run(name: str) -> None:
        try:
            answers[name] = work[name]()
        except BaseException as exc:  # reported below, so a failure is not a hang
            failures[name] = exc

    threads = [threading.Thread(target=run, args=(name,), name=name) for name in work]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=PATIENCE)
    assert not [thread for thread in threads if thread.is_alive()], "a thread did not finish"
    assert not failures, f"a thread failed: {failures}"
    return answers


def test_one_turn_resetting_its_budget_does_not_refill_the_other() -> None:
    """The sharpest case: a turn starts between two calls of the turn beside it."""
    budget = ModelBudget(1)
    took_one = threading.Event()
    second_began = threading.Event()
    first_finished = threading.Event()

    def first() -> list[bool]:
        budget.start_turn()
        taken = [budget.take()]
        took_one.set()
        assert second_began.wait(PATIENCE)
        taken.append(budget.take())
        first_finished.set()
        return taken

    def second() -> list[bool]:
        assert took_one.wait(PATIENCE)
        budget.start_turn()
        second_began.set()
        assert first_finished.wait(PATIENCE)
        return [budget.take()]

    answers = _in_threads({"first": first, "second": second})

    assert answers["first"] == [True, False]
    assert answers["second"] == [True]


def test_two_turns_at_once_each_spend_their_own_budget(
    repo_root: Path, tmp_path: Path
) -> None:
    """Two turns in one engine, each planning inside the two calls it is allowed."""
    provider = OverlappingProvider(threading.Barrier(2))
    engine = build_engine(
        _settings(repo_root, tmp_path, calls=2), provider=provider, audit_sink=MemorySink()
    )

    answers = _in_threads(
        {
            "left": lambda: engine.turn("left", RESTART_REQUEST),
            "right": lambda: engine.turn("right", RESTART_REQUEST),
        }
    )

    assert provider.calls == 4
    for name in ("left", "right"):
        result = answers[name]
        assert isinstance(result, TurnResult)
        assert result.pending is not None and result.pending.kind == "approval"
        assert [event for event in result.events if event.kind == "budget"] == []


def test_a_turn_that_ends_takes_its_count_with_it(repo_root: Path, tmp_path: Path) -> None:
    """Work that is not a turn is charged to no turn, whatever the last turn spent."""
    budget = ModelBudget(1)

    with budget.for_turn():
        assert budget.take() is True
        assert budget.take() is False
        assert budget.spent == 1

    assert budget.spent == 0
    assert budget.refused == 0
    assert budget.exhausted is False
    assert [budget.take(), budget.take()] == [True, True]


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_a_turn_may_make_the_calls_the_configuration_declares(limit: int) -> None:
    budget = ModelBudget(limit)

    with budget.for_turn():
        taken = [budget.take() for _ in range(limit + 1)]
        event = budget.as_event()

    assert taken == [True] * limit + [False]
    assert event == {"limit": limit, "spent": limit, "refused": 1}
