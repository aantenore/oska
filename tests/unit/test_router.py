"""Deciding what a turn is, through the rules, the fast path and the model."""

from __future__ import annotations

from pathlib import Path

import pytest

from oska.catalog.registry import SkillRegistry
from oska.catalog.resolver import CapabilityResolver
from oska.config import RuntimeConfig, load_runtime_config
from oska.contracts.common import Message, TurnKind
from oska.index.base import SkillIndex
from oska.index.keyword import KeywordIndex
from oska.router import FastPath, ModelRouter, RoutingContext, RuleRouter, TurnRouter


class RecordingProvider:
    """Answers each classification call with the next prepared object."""

    name = "recording"

    def __init__(self, answers: list[dict]) -> None:
        self._answers = list(answers)
        self.systems: list[str] = []
        self.messages: list[list[Message]] = []

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        if not self._answers:
            raise AssertionError("the router asked for more answers than were prepared")
        self.systems.append(system)
        self.messages.append(list(messages))
        return self._answers.pop(0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("this provider only classifies")


class BrokenIndex:
    """An index that fails, to prove the fast path hands the turn on."""

    def build(self, skills: list) -> None:
        raise RuntimeError("the index could not be built")

    def rank(self, query: str, limit: int) -> list:
        raise RuntimeError("the index could not be searched")


@pytest.fixture
def runtime(repo_root: Path) -> RuntimeConfig:
    """The runtime configuration this installation ships."""
    return load_runtime_config(repo_root / "configs" / "runtime.json")


@pytest.fixture
def rules(runtime: RuntimeConfig) -> RuleRouter:
    """The rule layer over the configured words."""
    return RuleRouter(runtime.router.rules)


@pytest.fixture
def fast_path(registry: SkillRegistry, runtime: RuntimeConfig) -> FastPath:
    """The fast path over the example catalog."""
    return FastPath(KeywordIndex, registry, runtime.router.fast_path)


IDLE = RoutingContext()
GOAL = RoutingContext(pending_goal=True)
APPROVAL = RoutingContext(pending_approval=True)


def test_approval_words_only_count_while_an_approval_is_waiting(rules: RuleRouter) -> None:
    assert rules.route("yes", APPROVAL).kind is TurnKind.approve
    assert rules.route("yes", IDLE) is None
    assert rules.route("yes", GOAL) is None


def test_an_approval_is_matched_whole_after_punctuation_and_case(rules: RuleRouter) -> None:
    assert rules.route("  Confermo!  ", APPROVAL).kind is TurnKind.approve
    assert rules.route("yes, and restart the other one too", APPROVAL) is None


def test_refusal_words_answer_a_waiting_approval(rules: RuleRouter) -> None:
    decision = rules.route("no", APPROVAL)
    assert decision.kind is TurnKind.reject
    assert decision.source == "rule"
    assert decision.language is None


def test_cancel_words_only_count_while_something_is_under_way(rules: RuleRouter) -> None:
    assert rules.route("lascia stare", GOAL).kind is TurnKind.cancel_task
    assert rules.route("never mind", GOAL).kind is TurnKind.cancel_task
    assert rules.route("never mind", IDLE) is None


def test_a_refusal_wins_over_a_cancellation_while_an_approval_waits(rules: RuleRouter) -> None:
    assert rules.route("annulla", APPROVAL).kind is TurnKind.reject
    assert rules.route("annulla", GOAL).kind is TurnKind.cancel_task


def test_an_empty_message_matches_no_rule(rules: RuleRouter) -> None:
    assert rules.route("   ", APPROVAL) is None


def test_the_fast_path_routes_a_trigger_to_its_procedure(fast_path: FastPath) -> None:
    decision = fast_path.route("restart the checkout service", IDLE)
    assert decision.kind is TurnKind.new_task
    assert decision.runbook_id == "runbook.service_restart"
    assert decision.source == "fast_path"
    assert decision.confidence == 1.0


def test_the_fast_path_can_route_a_composite(fast_path: FastPath) -> None:
    decision = fast_path.route("triage the incident on checkout", IDLE)
    assert decision.runbook_id == "composite.incident_triage"


def test_the_fast_path_routes_a_greeting(fast_path: FastPath) -> None:
    decision = fast_path.route("Buongiorno!", IDLE)
    assert decision.kind is TurnKind.greeting
    assert decision.runbook_id is None


def test_the_fast_path_stands_down_while_something_is_pending(fast_path: FastPath) -> None:
    assert fast_path.route("restart the checkout service", GOAL) is None
    assert fast_path.route("restart the checkout service", APPROVAL) is None


def test_the_fast_path_declines_a_loose_match(fast_path: FastPath) -> None:
    assert fast_path.route("could you maybe look at the checkout thing", IDLE) is None


def test_the_fast_path_can_be_switched_off(
    registry: SkillRegistry, runtime: RuntimeConfig
) -> None:
    config = runtime.router.fast_path.model_copy(update={"enabled": False})
    disabled = FastPath(KeywordIndex, registry, config)
    assert disabled.route("restart the checkout service", IDLE) is None


def test_a_failing_index_hands_the_turn_on(
    registry: SkillRegistry, runtime: RuntimeConfig
) -> None:
    broken = FastPath(BrokenIndex, registry, runtime.router.fast_path)
    assert broken.route("restart the checkout service", IDLE) is None


def test_the_fast_path_picks_up_a_new_procedure(
    catalog_copy: Path, runtime: RuntimeConfig, write_skill, runbook_frontmatter
) -> None:
    registry = SkillRegistry([catalog_copy])
    fast_path = FastPath(KeywordIndex, registry, runtime.router.fast_path)
    assert fast_path.route("drain the queue of stuck jobs", IDLE) is None
    write_skill(
        runbook_frontmatter(
            id="runbook.queue_drain",
            description="Drains the queue of stuck jobs, one batch at a time, until it is empty.",
            triggers=[
                "drain the queue of stuck jobs",
                "empty the stuck queue",
                "clear the backlog of jobs",
            ],
        ),
        root=catalog_copy,
    )
    decision = fast_path.route("drain the queue of stuck jobs", IDLE)
    assert decision.runbook_id == "runbook.queue_drain"


def test_the_model_router_returns_what_the_model_classified(
    resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider(
        [
            {
                "kind": "new_task",
                "runbook_id": "runbook.service_restart",
                "confidence": 0.82,
                "language": "it",
            }
        ]
    )
    router = ModelRouter(provider, resolver, runtime.router)
    decision = router.route("riavvia il servizio di checkout", RoutingContext())
    assert decision.kind is TurnKind.new_task
    assert decision.runbook_id == "runbook.service_restart"
    assert decision.confidence == pytest.approx(0.82)
    assert decision.language == "it"
    assert decision.source == "model"


def test_the_model_prompt_states_the_scope_the_procedures_and_the_state(
    resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider([{"kind": "off_scope", "confidence": 0.9}])
    router = ModelRouter(provider, resolver, runtime.router)
    router.route("book me a table", RoutingContext(pending_approval=True))
    system = provider.systems[0]
    assert resolver.declared_scope() in system
    assert "runbook.service_restart" in system
    assert "a write is waiting for approval: yes" in system
    assert "changes the outcome the user asked for is new_task" in system
    assert provider.messages[0][0].content == "book me a table"


def test_an_unknown_kind_becomes_off_scope(
    resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider([{"kind": "page", "runbook_id": None, "confidence": 1}])
    router = ModelRouter(provider, resolver, runtime.router)
    assert router.route("what can you do", RoutingContext()).kind is TurnKind.off_scope


def test_a_procedure_outside_the_catalog_is_dropped(
    resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider(
        [{"kind": "new_task", "runbook_id": "runbook.delete_everything", "confidence": "high"}]
    )
    router = ModelRouter(provider, resolver, runtime.router)
    decision = router.route("delete everything", RoutingContext())
    assert decision.runbook_id is None
    assert decision.confidence == 0.0


def test_the_turn_router_tries_the_rules_first(
    rules: RuleRouter, fast_path: FastPath, resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider([])
    router = TurnRouter(rules, fast_path, ModelRouter(provider, resolver, runtime.router))
    decision = router.route("confirm", RoutingContext(pending_approval=True))
    assert decision.kind is TurnKind.approve
    assert provider.systems == []


def test_the_turn_router_tries_the_fast_path_before_the_model(
    rules: RuleRouter, fast_path: FastPath, resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider([])
    router = TurnRouter(rules, fast_path, ModelRouter(provider, resolver, runtime.router))
    decision = router.route("restart the checkout service", RoutingContext())
    assert decision.source == "fast_path"
    assert provider.systems == []


def test_the_turn_router_falls_through_to_the_model(
    rules: RuleRouter, fast_path: FastPath, resolver: CapabilityResolver, runtime: RuntimeConfig
) -> None:
    provider = RecordingProvider(
        [{"kind": "off_scope", "runbook_id": None, "confidence": 0.9, "language": "en"}]
    )
    router = TurnRouter(rules, fast_path, ModelRouter(provider, resolver, runtime.router))
    decision = router.route("book me a table for two", RoutingContext())
    assert decision.kind is TurnKind.off_scope
    assert decision.source == "model"


def test_without_a_model_an_unmatched_turn_is_a_new_request(
    rules: RuleRouter, fast_path: FastPath
) -> None:
    router = TurnRouter(rules, fast_path)
    decision = router.route("the checkout thing is misbehaving again", RoutingContext())
    assert decision.kind is TurnKind.new_task
    assert decision.runbook_id is None
    assert decision.confidence == 0.0
    assert decision.source == "rule"


def test_the_keyword_index_satisfies_the_index_protocol() -> None:
    assert isinstance(KeywordIndex(), SkillIndex)
