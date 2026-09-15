"""Routing examples derived from the declared triggers, at publish time only."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opsdeck.audit.sink import MemorySink
from opsdeck.catalog.enrichment import (
    EnrichmentError,
    enrich,
    enrich_catalog,
    examples_path,
    needs_enrichment,
    read_examples,
    source_hash,
    write_examples,
)
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.cli.commands_enrich import app
from opsdeck.config import EnrichmentConfig, Settings, load_runtime_config
from opsdeck.contracts.common import Message
from opsdeck.contracts.proposal import ProposalState
from opsdeck.contracts.receipt import WriteReceipt
from opsdeck.contracts.skill import Skill
from opsdeck.engine.factory import build_engine
from opsdeck.learning.changes import ChangeRequestState, ChangeRequestStore
from opsdeck.learning.lifecycle import transition
from opsdeck.learning.proposals import ProposalStore, draft_from_session
from opsdeck.planner.provider import ModelProviderError
from opsdeck.planner.scripted import ScriptedModelProvider

runner = CliRunner()

LANGUAGES = ["it"]
ITALIAN = [
    "svuota la coda dei lavori bloccati",
    "togli i lavori fermi dalla coda",
    "libera la coda dei processi in attesa",
]
ANSWER = [{"purpose": "derived phrases", "response": {"it": ITALIAN}}]

#: A plan that declares no operation, so a turn reaches its reply having run nothing.
_NOTHING_TO_RUN = {
    "runbook_id": "runbook.queue_drain",
    "operations": [],
    "missing": [],
    "reply_intent": "report that there was nothing to do",
}


@pytest.fixture
def config() -> EnrichmentConfig:
    """Enrichment in force, with room for the phrases these tests use."""
    return EnrichmentConfig(enabled=True, per_language=3, cap_per_route=24)


DECLARED = [
    "drain the queue of stuck jobs",
    "empty the stuck queue",
    "clear the backlog of jobs",
]


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    """Root of the catalog these tests write into."""
    return tmp_path / "skills"


@pytest.fixture
def publish(
    catalog: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> Callable[..., Skill]:
    """Write one procedure with the given triggers and load it back."""

    def _publish(triggers: list[str] | None = None) -> Skill:
        write_skill(
            runbook_frontmatter(
                id="runbook.queue_drain",
                description=(
                    "Drains the queue of stuck jobs, one batch at a time, until it is empty."
                ),
                triggers=list(triggers or DECLARED),
            )
        )
        return SkillRegistry([catalog]).require("runbook.queue_drain")

    return _publish


@pytest.fixture
def queue_drain(publish: Callable[..., Skill]) -> Skill:
    """A procedure whose triggers are written in one language only."""
    return publish()


def test_paraphrases_are_written_and_read_back(
    queue_drain: Skill, config: EnrichmentConfig
) -> None:
    provider = ScriptedModelProvider(ANSWER)
    examples = enrich(queue_drain, provider, config, LANGUAGES)
    assert examples == {"it": ITALIAN}
    write_examples(queue_drain, examples, config)
    assert read_examples(queue_drain, config) == {"it": ITALIAN}


def test_one_call_answers_every_language(queue_drain: Skill, config: EnrichmentConfig) -> None:
    provider = ScriptedModelProvider(
        [{"response": {"it": ITALIAN, "de": ["leere die warteschlange"]}}]
    )
    examples = enrich(queue_drain, provider, config, ["it", "de"])
    assert sorted(examples) == ["de", "it"]
    assert provider.consumed() == 1


def test_a_declared_trigger_is_never_repeated_as_a_derived_phrase(
    queue_drain: Skill, config: EnrichmentConfig
) -> None:
    provider = ScriptedModelProvider(
        [{"response": {"it": ["Empty the stuck queue", *ITALIAN]}}]
    )
    assert enrich(queue_drain, provider, config, LANGUAGES) == {"it": ITALIAN}


def test_the_file_sits_beside_the_skill(queue_drain: Skill, config: EnrichmentConfig) -> None:
    path = write_examples(queue_drain, {"it": ITALIAN}, config)
    assert path == examples_path(queue_drain, config)
    assert path.parent == queue_drain.path.parent
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["source_hash"] == source_hash(queue_drain)
    assert document["languages"] == {"it": ITALIAN}


def test_a_file_written_from_other_triggers_is_ignored(
    queue_drain: Skill, publish: Callable[..., Skill], config: EnrichmentConfig
) -> None:
    write_examples(queue_drain, {"it": ITALIAN}, config)
    edited = publish(["drain the queue of stuck jobs", "empty the stuck queue", "clear the jobs"])
    assert examples_path(edited, config).exists()
    assert read_examples(edited, config) == {}
    assert needs_enrichment(edited, config, LANGUAGES) is True


def test_nothing_is_needed_once_every_language_is_written(
    queue_drain: Skill, config: EnrichmentConfig
) -> None:
    assert needs_enrichment(queue_drain, config, LANGUAGES) is True
    write_examples(queue_drain, {"it": ITALIAN}, config)
    assert needs_enrichment(queue_drain, config, LANGUAGES) is False
    assert needs_enrichment(queue_drain, config, ["it", "de"]) is True


def test_a_pass_reports_what_it_wrote_skipped_and_could_not_do(
    catalog: Path,
    queue_drain: Skill,
    config: EnrichmentConfig,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    write_skill(
        runbook_frontmatter(
            id="runbook.cache_purge",
            description="Clears the cached responses of a service so the next read is fresh.",
            triggers=["purge the cache", "clear the cached responses", "drop the cache"],
        )
    )
    registry = SkillRegistry([catalog])
    write_examples(registry.require("runbook.queue_drain"), {"it": ITALIAN}, config)
    # One answer for two procedures that need one: the second finds no step left.
    report = enrich_catalog(registry, ScriptedModelProvider(ANSWER), config, LANGUAGES)
    assert report.written == ["runbook.cache_purge"]
    assert report.skipped == ["runbook.queue_drain"]
    assert report.failed == {}
    exhausted = enrich_catalog(
        registry, ScriptedModelProvider([]), config, ["it", "de"]
    )
    assert sorted(exhausted.failed) == ["runbook.cache_purge", "runbook.queue_drain"]
    assert "ModelProviderError" in exhausted.failed["runbook.queue_drain"]


def test_a_catalog_served_as_a_revision_is_never_enriched(
    catalog: Path, queue_drain: Skill, config: EnrichmentConfig
) -> None:
    """A revision is immutable, and the derived file would be written inside it.

    The identifier of a revision hashes the skills it holds and nothing else,
    so a file written beside one of them would leave two replicas answering to
    the same identifier and routing the same request differently. The pass is
    refused before a provider is asked for anything.
    """
    served = SkillRegistry([catalog], revision="7f3c1d2e4b5a6c8d")
    provider = ScriptedModelProvider(ANSWER)

    with pytest.raises(EnrichmentError) as raised:
        enrich_catalog(served, provider, config, LANGUAGES)

    assert "7f3c1d2e4b5a6c8d" in str(raised.value)
    assert "before a revision is built" in str(raised.value)
    assert provider.consumed() == 0
    assert not examples_path(queue_drain, config).exists()
    # The same catalog read as a directory is enriched as usual.
    assert enrich_catalog(
        SkillRegistry([catalog]), provider, config, LANGUAGES
    ).written == ["runbook.queue_drain"]


def _published(
    store: ProposalStore,
    registry: SkillRegistry,
    publish_root: Path,
    schemas_dir: Path,
    **extra: object,
):
    """Draft one procedure, publish it, and apply the change request as a reviewer would.

    Publication composes a change request and stops; putting the package into a
    catalog is a person's act, so the helper performs it explicitly.
    """
    changes = ChangeRequestStore(publish_root.parent / "changes")
    proposal = draft_from_session(
        "session-enrich",
        [
            Message(role="user", content="the checkout service is stuck"),
            Message(role="assistant", content="I restarted it and read the state back."),
        ],
        [
            WriteReceipt(
                receipt_id="receipt-1",
                session_id="session-enrich",
                ledger_key="key",
                integration_id="integration.container_runtime",
                operation="service.restart",
                payload={"service": "checkout"},
                status="executed",
                created_at=datetime.now(UTC),
            )
        ],
        registry,
        None,
        runbook_id_hint="service_bounce",
        schema_dir=schemas_dir,
    )
    store.save(proposal)
    for state in (ProposalState.submitted, ProposalState.approved):
        proposal = transition(
            store,
            proposal.proposal_id,
            state,
            "reviewer",
            registry=registry,
            changes=changes,
            schema_dir=schemas_dir,
        )
    published = transition(
        store,
        proposal.proposal_id,
        ProposalState.published,
        "reviewer",
        "shipped",
        registry=registry,
        changes=changes,
        schema_dir=schemas_dir,
        **extra,
    )
    publish_root.mkdir(parents=True, exist_ok=True)
    for request in changes.list(ChangeRequestState.open):
        changes.apply(request.request_id, publish_root, by="reviewer")
    return published


def test_publication_writes_the_derived_phrases(
    tmp_path: Path,
    skills_root: Path,
    schemas_dir: Path,
    config: EnrichmentConfig,
) -> None:
    publish_root = tmp_path / "published"
    registry = SkillRegistry([skills_root, publish_root])
    store = ProposalStore(tmp_path / "proposals")
    proposal = _published(
        store,
        registry,
        publish_root,
        schemas_dir,
        provider=ScriptedModelProvider(ANSWER),
        enrichment=config,
        languages=LANGUAGES,
    )
    assert proposal.state is ProposalState.published
    assert read_examples(registry.require("runbook.service_bounce"), config) == {"it": ITALIAN}
    assert "routing examples written for it" in proposal.history[-1].note


def test_a_provider_that_fails_does_not_block_a_publication(
    tmp_path: Path,
    skills_root: Path,
    schemas_dir: Path,
    config: EnrichmentConfig,
) -> None:
    publish_root = tmp_path / "published"
    registry = SkillRegistry([skills_root, publish_root])
    store = ProposalStore(tmp_path / "proposals")
    proposal = _published(
        store,
        registry,
        publish_root,
        schemas_dir,
        provider=ScriptedModelProvider([]),
        enrichment=config,
        languages=LANGUAGES,
    )
    assert proposal.state is ProposalState.published
    published = registry.require("runbook.service_bounce")
    assert published.is_published
    assert not examples_path(published, config).exists()
    note = proposal.history[-1].note
    assert "shipped" in note
    assert "the routing examples were not written" in note


def test_publication_without_a_provider_writes_nothing(
    tmp_path: Path,
    skills_root: Path,
    schemas_dir: Path,
    config: EnrichmentConfig,
) -> None:
    publish_root = tmp_path / "published"
    registry = SkillRegistry([skills_root, publish_root])
    store = ProposalStore(tmp_path / "proposals")
    proposal = _published(store, registry, publish_root, schemas_dir, enrichment=config)
    note = proposal.history[-1].note
    assert note.startswith("shipped")
    assert "the routing examples were not written" not in note
    assert not examples_path(registry.require("runbook.service_bounce"), config).exists()


@pytest.fixture
def cli_catalog(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    catalog: Path,
    queue_drain: Skill,
) -> Path:
    """The command line pointed at a catalog holding one unenriched procedure."""
    monkeypatch.setenv("OPSDECK_RUNTIME_CONFIG", str(repo_root / "configs" / "runtime.json"))
    monkeypatch.setenv("OPSDECK_MODEL_CONFIG", str(repo_root / "configs" / "model.json"))
    return catalog


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "phrases.json"
    path.write_text(json.dumps(ANSWER), encoding="utf-8")
    return path


def test_the_command_writes_the_phrases_with_a_scripted_provider(
    cli_catalog: Path, tmp_path: Path, config: EnrichmentConfig, queue_drain: Skill
) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--skills",
            str(cli_catalog),
            "--language",
            "it",
            "--provider",
            "scripted",
            "--script",
            str(_script(tmp_path)),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["written"] == ["runbook.queue_drain"]
    assert read_examples(queue_drain, config) == {"it": ITALIAN}


def test_a_dry_run_asks_no_provider_and_writes_nothing(
    cli_catalog: Path, config: EnrichmentConfig, queue_drain: Skill
) -> None:
    result = runner.invoke(
        app, ["run", "--skills", str(cli_catalog), "--language", "it", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["written"] == ["runbook.queue_drain"]
    assert not examples_path(queue_drain, config).exists()


def test_the_command_stops_when_no_provider_is_in_force(cli_catalog: Path) -> None:
    result = runner.invoke(
        app, ["run", "--skills", str(cli_catalog), "--language", "it", "--provider", "none"]
    )
    assert result.exit_code == 1
    assert "needs a model provider" in result.stderr
    assert "Traceback" not in result.stderr


def test_show_prints_the_declared_and_the_derived_phrases(
    cli_catalog: Path, config: EnrichmentConfig, queue_drain: Skill
) -> None:
    bare = runner.invoke(app, ["show", "runbook.queue_drain", "--skills", str(cli_catalog)])
    assert bare.exit_code == 0
    assert "declared  drain the queue of stuck jobs" in bare.stdout
    assert "no derived phrases" in bare.stdout
    write_examples(queue_drain, {"it": ITALIAN}, config)
    enriched = runner.invoke(app, ["show", "runbook.queue_drain", "--skills", str(cli_catalog)])
    assert enriched.exit_code == 0
    assert "it  svuota la coda dei lavori bloccati" in enriched.stdout


def test_show_refuses_an_unknown_procedure(cli_catalog: Path) -> None:
    result = runner.invoke(app, ["show", "runbook.absent", "--skills", str(cli_catalog)])
    assert result.exit_code == 1
    assert "unknown skill" in result.stderr


def test_an_exhausted_script_raises_rather_than_answering(queue_drain: Skill) -> None:
    with pytest.raises(ModelProviderError):
        ScriptedModelProvider([]).complete_json("", [Message(role="user", content="x")], {})


NEARLY_A_DERIVED_PHRASE = "per favore svuota la coda dei lavori che sono rimasti bloccati oggi"

ENGLISH_NEIGHBOR = (
    "runbook.service_restart",
    "Restarts a service that is wedged, after reading what it is doing right now.",
    [
        "restart the checkout service",
        "bounce the api",
        "the service is stuck, restart it",
    ],
)


@pytest.fixture
def runtime_with_enrichment(
    repo_root: Path, tmp_path: Path, config: EnrichmentConfig
) -> Path:
    """The shipped runtime configuration, with enrichment switched on for this run.

    The file this installation ships keeps enrichment off; a deployment that has
    derived its phrases turns it on in its own configuration, which is what this
    writes rather than editing what is shipped.
    """
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document["enrichment"] = config.model_dump(mode="json")
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _first(result: object, kind: str) -> dict:
    """The data of the first event of this kind the turn raised."""
    return next(event.data for event in result.events if event.kind == kind)


def test_an_engine_built_from_the_configuration_ranks_by_the_derived_phrases(
    repo_root: Path,
    tmp_path: Path,
    catalog: Path,
    queue_drain: Skill,
    config: EnrichmentConfig,
    runtime_with_enrichment: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    """Both layers that rank the catalog read the file a publication wrote.

    The request is in a language no trigger of this catalog is written in. The
    fast path answers the phrasing that was derived word for word, without a
    model call; discovery answers a phrasing near it, and answers it clearly
    enough to choose between the two procedures on offer.
    """
    skill_id, description, triggers = ENGLISH_NEIGHBOR
    write_skill(
        runbook_frontmatter(id=skill_id, description=description, triggers=list(triggers))
    )
    write_examples(queue_drain, {"it": ITALIAN}, config)
    provider = ScriptedModelProvider(
        [
            {"purpose": "plan, routed by the derived phrase", "response": _NOTHING_TO_RUN},
            {"purpose": "reply", "response": {"reply": "Nothing to report."}},
            {
                "purpose": "router classification",
                "response": {
                    "kind": "new_task",
                    "runbook_id": None,
                    "confidence": 0.6,
                    "language": "it",
                },
            },
            {"purpose": "plan, chosen by discovery", "response": _NOTHING_TO_RUN},
            {"purpose": "reply", "response": {"reply": "Nothing to report."}},
        ]
    )
    settings = Settings(
        skills_roots=[catalog],
        runtime_config=runtime_with_enrichment,
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )
    engine = build_engine(settings, provider=provider, audit_sink=MemorySink())

    routed = engine.turn("derived", ITALIAN[0])
    chosen = engine.turn("derived", NEARLY_A_DERIVED_PHRASE)

    route = _first(routed, "route")
    assert route["source"] == "fast_path"
    assert route["runbook_id"] == "runbook.queue_drain"
    assert route["confidence"] > 0.99
    discovery = _first(chosen, "discovery")
    assert discovery["chosen"] == "runbook.queue_drain"
    assert discovery["tie"] is False
    assert discovery["ranked"][0]["score"] > 0.5
    assert load_runtime_config(repo_root / "configs" / "runtime.json").enrichment.enabled is False


def test_without_the_derived_file_the_same_request_reaches_neither_layer(
    repo_root: Path,
    tmp_path: Path,
    catalog: Path,
    queue_drain: Skill,
    runtime_with_enrichment: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    """Nothing is derived at turn time: with no file written, the request ranks nowhere."""
    skill_id, description, triggers = ENGLISH_NEIGHBOR
    write_skill(
        runbook_frontmatter(id=skill_id, description=description, triggers=list(triggers))
    )
    provider = ScriptedModelProvider(
        [
            {
                "purpose": "router classification",
                "response": {
                    "kind": "new_task",
                    "runbook_id": None,
                    "confidence": 0.6,
                    "language": "it",
                },
            }
        ]
    )
    settings = Settings(
        skills_roots=[catalog],
        runtime_config=runtime_with_enrichment,
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )
    engine = build_engine(settings, provider=provider, audit_sink=MemorySink())

    result = engine.turn("bare", ITALIAN[0])

    assert _first(result, "route")["source"] == "model"
    assert _first(result, "discovery")["chosen"] is None
    assert provider.consumed() == 1
