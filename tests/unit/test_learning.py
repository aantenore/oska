"""Drafting a procedure from a session, checking it and moving it to publication."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.contracts.common import Message
from opsdeck.contracts.proposal import ProposalState, QualityReport, SkillProposal
from opsdeck.contracts.receipt import WriteReceipt
from opsdeck.learning import quality
from opsdeck.learning.lifecycle import ALLOWED, LifecycleError, transition
from opsdeck.learning.proposals import ProposalStore, build_policy, draft_from_session
from opsdeck.planner.scripted import ScriptedModelProvider

SESSION_ID = "session-1"
ACTOR = "reviewer"

BODY = """## Purpose

Records what one session did, so it can be repeated on purpose.

## Steps

1. Call `service.restart` with the recorded inputs.

## Verification

The read-back declared by the integration must agree with what was expected.
"""

SCRIPTED_ANSWER = {
    "description": "Restarts one service and reads its state back to confirm it came up.",
    "triggers": ["bring the service back", "cycle the process", "put it back on its feet"],
    "action": "bring a service back",
    "purpose": "The process stopped answering and a restart cleared it.",
    "preconditions": "The service name is known and the environment is not production.",
    "steps": "1. Call `service.restart` with the service name.",
    "verification": "The read-back shows the service running under the same name.",
    "rollback": "A restart cannot be undone; roll the deployment back instead.",
}


@pytest.fixture
def publish_root(tmp_path: Path) -> Path:
    """Where a published proposal is written."""
    return tmp_path / "published"


@pytest.fixture
def learning_registry(skills_root: Path, tmp_path: Path) -> SkillRegistry:
    """Registry over the example catalog and over what this test publishes."""
    return SkillRegistry([skills_root, tmp_path])


@pytest.fixture
def store(tmp_path: Path) -> ProposalStore:
    """Proposal store in a temporary directory."""
    return ProposalStore(tmp_path / "proposals")


@pytest.fixture
def receipts() -> list[WriteReceipt]:
    """One executed write."""
    return [
        WriteReceipt(
            receipt_id="receipt-1",
            session_id=SESSION_ID,
            ledger_key="key",
            integration_id="integration.container_runtime",
            operation="service.restart",
            payload={"service": "checkout"},
            status="executed",
            created_at=datetime.now(UTC),
        )
    ]


@pytest.fixture
def conversation() -> list[Message]:
    """A short exchange that led to the write."""
    return [
        Message(role="user", content="the checkout service is stuck"),
        Message(role="assistant", content="I restarted it and read the state back."),
    ]


@pytest.fixture
def crafted() -> Callable[..., str]:
    """Build a skill file with the given frontmatter overrides and body."""

    def _build(body: str = BODY, **overrides: object) -> str:
        frontmatter: dict = {
            "id": "runbook.recorded",
            "type": "runbook",
            "version": 1,
            "status": "draft",
            "description": "A recorded procedure, described at a length the checks accept.",
            "triggers": ["first trigger", "second trigger", "third trigger"],
            "integrations": ["integration.container_runtime"],
            "capabilities": {"writes": ["service.restart"]},
            "runtime": {"action": "run the recorded procedure"},
            "policy": "./policy.json",
        }
        frontmatter.update(overrides)
        header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        return f"---\n{header}---\n\n{body}"

    return _build


def _draft(
    registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
    *,
    provider: ScriptedModelProvider | None = None,
    hint: str | None = None,
) -> SkillProposal:
    return draft_from_session(
        SESSION_ID,
        conversation,
        receipts,
        registry,
        provider,
        runbook_id_hint=hint,
        schema_dir=schemas_dir,
    )


def test_draft_without_a_provider_passes_the_checks(
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    assert proposal.quality.issues == []
    assert proposal.quality.score == 1.0
    assert proposal.state is ProposalState.draft
    assert [entry.state for entry in proposal.history] == [ProposalState.draft]
    frontmatter = yaml.safe_load(proposal.skill_markdown.split("---\n")[1])
    assert frontmatter["id"] == "runbook.service_bounce"
    assert frontmatter["status"] == "draft"
    assert frontmatter["capabilities"]["writes"] == ["service.restart"]
    assert frontmatter["integrations"] == ["integration.container_runtime"]
    assert frontmatter["entities"] == ["entity.service"]
    assert frontmatter["policy"] == "./policy.json"
    assert len(frontmatter["triggers"]) >= 3
    assert "service.restart" in proposal.skill_markdown
    assert "service" in proposal.skill_markdown


def test_draft_policy_asks_for_a_second_approval(
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    assert proposal.policy == build_policy("runbook.service_bounce", ["service.restart"])
    assert proposal.policy is not None
    rule = proposal.policy["rules"][0]
    assert rule["effect"] == "require_second_approval"
    assert rule["max_per_session"] == 1


def test_draft_with_a_provider_uses_the_scripted_content(
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
) -> None:
    provider = ScriptedModelProvider(
        [{"purpose": "draft", "expect_contains": "service.restart", "response": SCRIPTED_ANSWER}]
    )
    proposal = _draft(
        learning_registry,
        receipts,
        conversation,
        schemas_dir,
        provider=provider,
        hint="service_bounce",
    )
    assert provider.consumed() == 1
    assert proposal.quality.issues == []
    frontmatter = yaml.safe_load(proposal.skill_markdown.split("---\n")[1])
    assert frontmatter["description"] == SCRIPTED_ANSWER["description"]
    assert frontmatter["triggers"] == SCRIPTED_ANSWER["triggers"]
    assert frontmatter["runtime"]["action"] == SCRIPTED_ANSWER["action"]
    assert SCRIPTED_ANSWER["purpose"] in proposal.skill_markdown
    assert SCRIPTED_ANSWER["verification"] in proposal.skill_markdown
    assert SCRIPTED_ANSWER["rollback"] in proposal.skill_markdown


def test_a_crafted_file_passes_every_lint(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(
        crafted(),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )
    assert report.issues == []
    assert quality.passed(report) is True


def test_a_short_description_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(
        crafted(description="too short"),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )
    assert any("description" in issue for issue in report.issues)
    assert quality.passed(report) is False


def test_repeated_triggers_do_not_count_as_three(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(
        crafted(triggers=["same trigger", "same trigger", "same trigger"]),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )
    assert any("distinct triggers" in issue for issue in report.issues)


def test_an_undeclared_write_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(
        crafted(capabilities={"writes": ["service.evacuate"]}),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.evacuate"]),
        schema_dir=schemas_dir,
    )
    assert any("is not declared by a listed integration" in issue for issue in report.issues)


def test_a_read_declared_as_a_write_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(
        crafted(capabilities={"writes": ["service.status"]}),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.status"]),
        schema_dir=schemas_dir,
    )
    assert any("is not a write operation" in issue for issue in report.issues)


def test_an_empty_steps_section_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    body = "## Purpose\n\nWhy.\n\n## Verification\n\nThe read-back agrees.\n"
    report = quality.check(
        crafted(body),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )
    assert "steps: the section is empty" in report.issues


def test_an_empty_verification_section_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    body = "## Purpose\n\nWhy.\n\n## Steps\n\n1. Call `service.restart`.\n"
    report = quality.check(
        crafted(body),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )
    assert "verification: the section is empty" in report.issues


def test_a_trigger_already_taken_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(
        crafted(triggers=["bounce the api", "second trigger", "third trigger"]),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )
    assert any("already belongs to runbook.service_restart" in issue for issue in report.issues)


def test_a_missing_policy_document_is_reported(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check(crafted(), learning_registry, schema_dir=schemas_dir)
    assert any("missing_policy_file" in issue for issue in report.issues)
    assert 0.0 <= report.score < 1.0


def test_a_file_that_does_not_parse_scores_zero(
    learning_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    report = quality.check("not a skill file", learning_registry, schema_dir=schemas_dir)
    assert report.score == 0.0
    assert report.issues and report.issues[0].startswith("parse: ")


def test_the_store_keeps_one_file_per_proposal(
    store: ProposalStore,
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    path = store.save(proposal)
    assert path.name == f"{proposal.proposal_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["state"] == "draft"
    assert payload["source_session_id"] == SESSION_ID
    assert store.get(proposal.proposal_id).skill_markdown == proposal.skill_markdown
    assert [held.proposal_id for held in store.list()] == [proposal.proposal_id]
    assert store.list(ProposalState.submitted) == []
    store.delete(proposal.proposal_id)
    assert store.list() == []
    with pytest.raises(KeyError):
        store.get(proposal.proposal_id)
    with pytest.raises(KeyError):
        store.delete(proposal.proposal_id)


def test_the_legal_path_publishes_a_new_runbook(
    store: ProposalStore,
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
    publish_root: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    store.save(proposal)
    for target in (ProposalState.submitted, ProposalState.approved, ProposalState.published):
        held = transition(
            store,
            proposal.proposal_id,
            target,
            ACTOR,
            f"moved to {target.value}",
            registry=learning_registry,
            publish_root=publish_root,
            schema_dir=schemas_dir,
        )
    assert held.state is ProposalState.published
    assert [entry.state for entry in held.history] == [
        ProposalState.draft,
        ProposalState.submitted,
        ProposalState.approved,
        ProposalState.published,
    ]
    assert {entry.by for entry in held.history[1:]} == {ACTOR}
    directory = publish_root / "runbook" / "service_bounce" / "v1"
    assert (directory / "skill.md").is_file()
    assert json.loads((directory / "policy.json").read_text(encoding="utf-8")) == proposal.policy
    published = learning_registry.get("runbook.service_bounce")
    assert published is not None
    assert published.is_published
    assert published.version == 1
    assert published.frontmatter.capabilities.writes == ["service.restart"]
    assert "runbook.service_bounce" in {skill.id for skill in learning_registry.published()}
    assert store.get(proposal.proposal_id).state is ProposalState.published


def test_publishing_over_an_existing_id_bumps_the_version(
    store: ProposalStore,
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
    publish_root: Path,
) -> None:
    proposal = _draft(
        learning_registry, receipts, conversation, schemas_dir, hint="runbook.service_restart"
    )
    store.save(proposal)
    for target in (ProposalState.submitted, ProposalState.approved, ProposalState.published):
        transition(
            store,
            proposal.proposal_id,
            target,
            ACTOR,
            "",
            registry=learning_registry,
            publish_root=publish_root,
            schema_dir=schemas_dir,
        )
    assert (publish_root / "runbook" / "service_restart" / "v2" / "skill.md").is_file()
    published = learning_registry.get("runbook.service_restart")
    assert published is not None
    assert published.version == 2


def test_an_illegal_edge_is_refused(
    store: ProposalStore,
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
    publish_root: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    store.save(proposal)
    for target in (ProposalState.approved, ProposalState.published, ProposalState.draft):
        with pytest.raises(LifecycleError):
            transition(
                store,
                proposal.proposal_id,
                target,
                ACTOR,
                "",
                registry=learning_registry,
                publish_root=publish_root,
                schema_dir=schemas_dir,
            )
    assert store.get(proposal.proposal_id).state is ProposalState.draft
    assert not publish_root.exists()


def test_a_rejected_proposal_moves_nowhere(
    store: ProposalStore,
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
    publish_root: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    store.save(proposal)
    transition(
        store,
        proposal.proposal_id,
        ProposalState.rejected,
        ACTOR,
        "not wanted",
        registry=learning_registry,
        publish_root=publish_root,
        schema_dir=schemas_dir,
    )
    assert ALLOWED.get(ProposalState.rejected) is None
    with pytest.raises(LifecycleError):
        transition(
            store,
            proposal.proposal_id,
            ProposalState.submitted,
            ACTOR,
            "",
            registry=learning_registry,
            publish_root=publish_root,
            schema_dir=schemas_dir,
        )


def test_an_empty_actor_is_refused(
    store: ProposalStore,
    learning_registry: SkillRegistry,
    receipts: list[WriteReceipt],
    conversation: list[Message],
    schemas_dir: Path,
    publish_root: Path,
) -> None:
    proposal = _draft(learning_registry, receipts, conversation, schemas_dir, hint="service_bounce")
    store.save(proposal)
    with pytest.raises(LifecycleError):
        transition(
            store,
            proposal.proposal_id,
            ProposalState.submitted,
            "   ",
            "",
            registry=learning_registry,
            publish_root=publish_root,
            schema_dir=schemas_dir,
        )
    assert store.get(proposal.proposal_id).state is ProposalState.draft


def test_publication_is_refused_when_the_checks_fail(
    store: ProposalStore,
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
    publish_root: Path,
) -> None:
    body = "## Purpose\n\nWhy.\n\n## Steps\n\n1. Call `service.restart`.\n"
    proposal = SkillProposal(
        proposal_id="proposal-1",
        source_session_id=SESSION_ID,
        skill_markdown=crafted(body),
        policy=build_policy("runbook.recorded", ["service.restart"]),
        quality=QualityReport(issues=[], score=1.0),
    )
    store.save(proposal)
    for target in (ProposalState.submitted, ProposalState.approved):
        transition(
            store,
            proposal.proposal_id,
            target,
            ACTOR,
            "",
            registry=learning_registry,
            publish_root=publish_root,
            schema_dir=schemas_dir,
        )
    with pytest.raises(LifecycleError) as raised:
        transition(
            store,
            proposal.proposal_id,
            ProposalState.published,
            ACTOR,
            "",
            registry=learning_registry,
            publish_root=publish_root,
            schema_dir=schemas_dir,
        )
    assert "verification" in str(raised.value)
    held = store.get(proposal.proposal_id)
    assert held.state is ProposalState.approved
    assert held.quality.issues
    assert not publish_root.exists()


def test_a_proposal_naming_a_guardrail_nothing_defines_does_not_pass(
    crafted: Callable[..., str],
    learning_registry: SkillRegistry,
    schemas_dir: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate the catalog runs is the gate a proposal goes through.

    A promise is a rule of the installation, so a proposal may claim one only
    where the configured guardrail catalog defines it. The identifier that is
    defined is left alone; the invented one is what stops the publication.
    """
    monkeypatch.setenv("OPSDECK_RUNTIME_CONFIG", str(repo_root / "configs" / "runtime.json"))

    report = quality.check(
        crafted(guardrails=["guardrail.identifiers_are_never_invented", "guardrail.invented"]),
        learning_registry,
        policy=build_policy("runbook.recorded", ["service.restart"]),
        schema_dir=schemas_dir,
    )

    named = [issue for issue in report.issues if "guardrail_unknown_id" in issue]
    assert len(named) == 1
    assert "guardrail.invented" in named[0]
    assert quality.passed(report) is False
