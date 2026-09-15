"""A catalog served as a built revision: how one is made, and what a session does with it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opsdeck.catalog.registry import ServedCatalog, SkillRegistry
from opsdeck.catalog.revision import (
    CHOICE_FILENAME,
    MANIFEST_FILENAME,
    RevisionStore,
    build_revision,
    revision_id,
)
from opsdeck.catalog.validator import CatalogError
from opsdeck.cli.commands_revision import app
from opsdeck.config import Settings
from opsdeck.engine.factory import build_engine, revision_ledger

TENANT = "default"
RESTART = "runbook.service_restart"
RESTART_PHRASE = "restart a service"
runner = CliRunner()


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    """Where the revisions of these tests are kept."""
    return tmp_path / "revisions"


@pytest.fixture
def revision_settings(repo_root: Path, catalog_copy: Path, tmp_path: Path) -> Settings:
    """Settings over a writable copy of the example catalog, with state of its own."""
    return Settings(
        skills_roots=[catalog_copy],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider=None,
    )


def _retire(root: Path, name: str) -> None:
    """Retire one runbook of a catalog copy, the way a change to it would."""
    path = root / "runbook" / name / "v1" / "skill.md"
    text = path.read_text(encoding="utf-8")
    assert "status: published" in text
    path.write_text(text.replace("status: published", "status: retired", 1), encoding="utf-8")


def _build(settings: Settings, root: Path | None = None) -> str:
    """Release the catalog of these settings and return the revision identifier."""
    manifest = build_revision(
        settings.skills_roots,
        root or Path(settings.state_dir) / "revisions",
        tenant=TENANT,
        settings=settings,
    )
    return manifest.revision


def test_building_the_same_content_twice_leaves_one_revision(
    skills_root: Path, store_root: Path, example_settings: Settings
) -> None:
    first = build_revision(
        [skills_root], store_root, tenant=TENANT, settings=example_settings
    )
    second = build_revision(
        [skills_root], store_root, tenant=TENANT, settings=example_settings
    )
    assert first.revision == second.revision
    assert first.created_at == second.created_at
    assert [path.name for path in (store_root / TENANT).iterdir()] == [first.revision]
    assert RevisionStore(store_root).list(TENANT) == [first.revision]


def test_the_identifier_is_the_content_and_not_the_build(
    skills_root: Path, store_root: Path, example_settings: Settings
) -> None:
    manifest = build_revision([skills_root], store_root, tenant=TENANT, settings=example_settings)
    assert revision_id(manifest) == manifest.revision
    assert manifest.skills
    assert f"{RESTART}@v1" in manifest.skills
    moved = manifest.model_copy(update={"created_at": None, "source_roots": ["elsewhere"]})
    assert revision_id(moved) == manifest.revision
    other_tenant = manifest.model_copy(update={"tenant": "another"})
    assert revision_id(other_tenant) != manifest.revision


def test_a_revision_carries_a_manifest_naming_every_skill_by_content_hash(
    skills_root: Path, store_root: Path, example_settings: Settings
) -> None:
    manifest = build_revision([skills_root], store_root, tenant=TENANT, settings=example_settings)
    document = json.loads(
        (store_root / TENANT / manifest.revision / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert document["revision"] == manifest.revision
    assert document["tenant"] == TENANT
    assert document["skills"] == manifest.skills
    from_disk = SkillRegistry.from_revision(RevisionStore(store_root), TENANT, manifest.revision)
    assert {
        f"{skill.id}@v{skill.version}": skill.fingerprint for skill in from_disk.skills()
    } == manifest.skills


def test_a_catalog_with_an_error_is_not_released(
    store_root: Path,
    example_settings: Settings,
    write_skill,
    runbook_frontmatter,
    tmp_path: Path,
) -> None:
    root = tmp_path / "broken"
    write_skill(runbook_frontmatter(triggers=["only one trigger"]), root=root)
    with pytest.raises(CatalogError) as raised:
        build_revision([root], store_root, tenant=TENANT, settings=example_settings)
    assert raised.value.issues
    assert not (store_root / TENANT).exists()


def test_a_registry_over_a_revision_does_not_follow_the_directory_it_came_from(
    catalog_copy: Path, store_root: Path, example_settings: Settings
) -> None:
    settings = example_settings.model_copy(update={"skills_roots": [catalog_copy]})
    manifest = build_revision([catalog_copy], store_root, tenant=TENANT, settings=settings)
    registry = SkillRegistry.from_revision(RevisionStore(store_root), TENANT, manifest.revision)
    assert registry.fingerprint() == manifest.revision
    offered = [skill.id for skill in registry.published()]
    assert RESTART in offered

    _retire(catalog_copy, "service_restart")
    assert registry.fingerprint() == manifest.revision
    assert [skill.id for skill in registry.published()] == offered
    assert SkillRegistry([catalog_copy]).get(RESTART).is_published is False


def test_a_second_revision_is_a_second_artifact(
    catalog_copy: Path, store_root: Path, example_settings: Settings
) -> None:
    settings = example_settings.model_copy(update={"skills_roots": [catalog_copy]})
    first = build_revision([catalog_copy], store_root, tenant=TENANT, settings=settings)
    _retire(catalog_copy, "service_restart")
    second = build_revision([catalog_copy], store_root, tenant=TENANT, settings=settings)
    store = RevisionStore(store_root)
    assert first.revision != second.revision
    assert store.list(TENANT) == [first.revision, second.revision]
    assert store.latest(TENANT) == second.revision
    assert store.get(TENANT, "not a revision") is None
    with pytest.raises(KeyError):
        store.manifest(TENANT, "not a revision")


def test_a_session_keeps_the_revision_it_started_on_and_a_new_task_adopts_the_newest(
    revision_settings: Settings, catalog_copy: Path
) -> None:
    first = _build(revision_settings)
    engine = build_engine(revision_settings)
    assert isinstance(engine.registry, ServedCatalog)

    opening = engine.turn("pinned", "hello")
    assert RESTART_PHRASE in opening.reply
    assert engine.state("pinned").revision == first
    assert _revisions(opening)[0]["to"] == first

    _retire(catalog_copy, "service_restart")
    second = _build(revision_settings)
    assert second != first

    held = engine.turn("pinned", "hello")
    assert RESTART_PHRASE in held.reply
    assert engine.state("pinned").revision == first
    assert _revisions(held) == []

    started = engine.turn("pinned", "roll back the checkout service")
    assert engine.state("pinned").revision == second
    assert _revisions(started) == [{"from": first, "to": second, "reason": "new_task"}]

    after = engine.turn("pinned", "hello")
    assert RESTART_PHRASE not in after.reply
    assert engine.state("pinned").revision == second


def test_a_session_that_starts_later_starts_on_the_newest_revision(
    revision_settings: Settings, catalog_copy: Path
) -> None:
    first = _build(revision_settings)
    engine = build_engine(revision_settings)
    engine.turn("early", "hello")
    _retire(catalog_copy, "service_restart")
    second = _build(revision_settings)

    opening = engine.turn("late", "hello")
    assert engine.state("late").revision == second
    assert engine.state("early").revision == first
    assert RESTART_PHRASE not in opening.reply
    assert _revisions(opening) == [{"from": "", "to": second, "reason": "first_turn"}]


def test_a_directory_backed_deployment_pins_nothing(example_settings: Settings) -> None:
    engine = build_engine(example_settings)
    assert not isinstance(engine.registry, ServedCatalog)
    result = engine.turn("plain", "hello")
    assert engine.state("plain").revision == ""
    assert _revisions(result) == []


def test_a_receipt_names_the_revision_it_was_opened_under(
    revision_settings: Settings, tmp_path: Path
) -> None:
    revision = _build(revision_settings)
    ledger = revision_ledger(tmp_path / "ledger.db")
    registry = ServedCatalog(RevisionStore(Path(revision_settings.state_dir) / "revisions"), TENANT)
    with registry.pinned(revision):
        opened = ledger.new_receipt(
            "s", "k", "integration.container_runtime", "service.restart", {}
        )
    assert opened.revision == revision
    unpinned = ledger.new_receipt("s", "k2", "integration.container_runtime", "x", {})
    assert unpinned.revision is None


def test_the_revision_commands_build_list_show_and_choose(revision_settings: Settings) -> None:
    """The round trip, run against the command group itself.

    The group is not attached to the root command here: `cli/main.py` is where
    that line belongs, and this test is about what the commands do.
    """
    where = ["--skills", str(revision_settings.skills_roots[0])]
    state = {"OPSDECK_STATE_DIR": str(revision_settings.state_dir)}
    built = runner.invoke(app, ["build", *where], env=state)
    assert built.exit_code == 0, built.output
    revision = built.stdout.split("\t")[0].strip()
    assert len(revision) == 16

    again = runner.invoke(app, ["build", *where], env=state)
    assert again.exit_code == 0
    assert again.stdout.split("\t")[0].strip() == revision

    listed = runner.invoke(app, ["list", *where], env=state)
    assert listed.exit_code == 0
    assert revision in listed.stdout

    shown = runner.invoke(app, ["show", revision, *where], env=state)
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["revision"] == revision

    chosen = runner.invoke(app, ["use", revision, *where], env=state)
    assert chosen.exit_code == 0
    choice = Path(revision_settings.state_dir) / "revisions" / TENANT / CHOICE_FILENAME
    assert choice.read_text(encoding="utf-8").strip() == revision

    missing = runner.invoke(app, ["show", "0" * 16, *where], env=state)
    assert missing.exit_code == 1


def test_the_runtime_serves_the_revision_the_choice_file_names(
    revision_settings: Settings, catalog_copy: Path
) -> None:
    first = _build(revision_settings)
    _retire(catalog_copy, "service_restart")
    second = _build(revision_settings)
    store = RevisionStore(Path(revision_settings.state_dir) / "revisions")
    store.choose(TENANT, first)

    engine = build_engine(revision_settings)
    assert engine.registry.newest_revision() == first
    assert RESTART_PHRASE in engine.turn("chosen", "hello").reply

    store.choose(TENANT, second)
    assert engine.registry.newest_revision() == second


def _revisions(result) -> list[dict]:
    """The revision events of one turn, as they were recorded."""
    return [event.data for event in result.events if event.kind == "revision"]
