"""A revision is whole or it is not there, and an approval runs under the one it was given.

Two promises about a released catalog that hold by construction in the
implementation and were asserted by nothing. The first is about the build: a
revision is assembled under a name no reader looks at and only the rename makes
it one, so a build that stops half way leaves files on disk and no revision. The
tests here stop one, once in the middle of the copy and once at the rename, and
then read the store.

The second is about a turn. A write is described to a person under the catalog
that was in force when it was described, and the sentence they answered was
composed from that catalog; running the approved write under a newer one would
run something other than what they agreed to. That holds whether or not the
deployment pins its sessions, and the two tests differ only in that switch: with
pinning off the session still keeps its revision while the approval waits, and
lets go of it on the turn after.

A question is the same promise in a smaller form. A goal that stopped to ask
which service is a task under way as much as an approval is, and the procedure
the answer completes is the one the person was asked for, so an unpinned
session holds its revision while a question is outstanding too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from oska.audit.sink import MemorySink
from oska.catalog import revision as revision_module
from oska.catalog.registry import ServedCatalog, SkillRegistry
from oska.catalog.revision import MANIFEST_FILENAME, RevisionStore, build_revision
from oska.config import Settings
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.planner.scripted import ScriptedModelProvider

TENANT = "default"
RESTART = "runbook.service_restart"
CONTAINER_RUNTIME = "integration.container_runtime"
RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
SESSION = "approving"
ASKED = "answering"
WHICH_SERVICE = "Which service?"
BUILDING = ".building-"


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    """Where the revisions of these tests are kept."""
    return tmp_path / "revisions"


@pytest.fixture
def pinning_settings(repo_root: Path, catalog_copy: Path, tmp_path: Path) -> Settings:
    """Settings over a writable copy of the example catalog, answered by a script."""
    return Settings(
        skills_roots=[catalog_copy],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )


@pytest.fixture
def unpinned_settings(pinning_settings: Settings, tmp_path: Path) -> Settings:
    """The same deployment, with session pinning switched off."""
    document = json.loads(
        Path(pinning_settings.runtime_config).read_text(encoding="utf-8")
    )
    document["catalog"]["pin_sessions"] = False
    path = tmp_path / "runtime-unpinned.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return pinning_settings.model_copy(update={"runtime_config": path})


# --- a build that did not finish --------------------------------------------


def test_a_build_interrupted_part_way_leaves_nothing_to_read(
    skills_root: Path,
    store_root: Path,
    example_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The files of an unfinished build are on disk and the store holds no revision."""
    copied: list[Path] = []
    copytree = shutil.copytree

    def stop_after_the_first(source: Path, target: Path, **options: object) -> Path:
        if copied:
            raise KeyboardInterrupt("the build was interrupted")
        copied.append(Path(target))
        return copytree(source, target, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(revision_module.shutil, "copytree", stop_after_the_first)

    with pytest.raises(KeyboardInterrupt):
        build_revision([skills_root], store_root, tenant=TENANT, settings=example_settings)

    store = RevisionStore(store_root)
    assert store.list(TENANT) == []
    assert store.latest(TENANT) == ""
    assert store.current(TENANT) == ""

    left = [path.name for path in (store_root / TENANT).iterdir()]
    assert left and all(name.startswith(BUILDING) for name in left)
    unfinished = store_root / TENANT / left[0]
    assert any(unfinished.rglob("skill.md")), "nothing was copied, so nothing was interrupted"
    assert not (unfinished / MANIFEST_FILENAME).exists()

    identifier = left[0].removeprefix(BUILDING)
    assert store.get(TENANT, identifier) is None
    with pytest.raises(KeyError):
        SkillRegistry.from_revision(store, TENANT, identifier)


def test_a_build_whose_rename_never_happened_is_not_a_revision(
    skills_root: Path,
    store_root: Path,
    example_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last step is what makes a revision; without it the store holds nothing."""

    def refuse(self: Path, target: Path) -> Path:
        raise OSError(f"the rename to {target} did not happen")

    monkeypatch.setattr(Path, "rename", refuse)

    with pytest.raises(OSError):
        build_revision([skills_root], store_root, tenant=TENANT, settings=example_settings)

    store = RevisionStore(store_root)
    assert store.list(TENANT) == []
    assert store.latest(TENANT) == ""
    assert list((store_root / TENANT).iterdir()) == []


# --- an approval, and the catalog it was described under --------------------


def test_an_approval_runs_under_the_revision_it_was_requested_under(
    pinning_settings: Settings, catalog_copy: Path
) -> None:
    first = _build(pinning_settings)
    engine = _engine(pinning_settings)
    assert isinstance(engine.registry, ServedCatalog)

    asked = engine.turn(SESSION, RESTART_REQUEST)
    assert [outcome.kind for outcome in asked.outcomes] == ["read", "approval_pending"]
    assert engine.state(SESSION).revision == first

    _retire(catalog_copy, "service_restart")
    second = _build(pinning_settings)
    assert second != first
    assert engine.registry.newest_revision() == second
    assert _published_under(pinning_settings, second) is False

    answered = engine.turn(SESSION, "yes")

    assert [outcome.kind for outcome in answered.outcomes] == ["receipt"]
    assert _revisions(answered) == []
    assert engine.state(SESSION).revision == first
    receipts = engine.receipts(SESSION)
    assert [receipt.operation for receipt in receipts] == ["service.restart"]
    assert receipts[0].revision == first


def test_an_unpinned_session_still_holds_its_revision_while_an_approval_waits(
    unpinned_settings: Settings, catalog_copy: Path
) -> None:
    first = _build(unpinned_settings)
    engine = _engine(unpinned_settings, extra=[_route("greeting", None)])

    engine.turn(SESSION, RESTART_REQUEST)
    assert engine.state(SESSION).pending_approval is not None
    assert engine.state(SESSION).revision == first

    _retire(catalog_copy, "service_restart")
    second = _build(unpinned_settings)
    assert engine.registry.newest_revision() == second

    answered = engine.turn(SESSION, "yes")

    assert [outcome.kind for outcome in answered.outcomes] == ["receipt"]
    assert _revisions(answered) == []
    assert engine.state(SESSION).revision == first
    assert engine.receipts(SESSION)[0].revision == first

    # Nothing is waiting now, and this deployment does not pin: the next turn moves.
    after = engine.turn(SESSION, "hello")
    assert engine.state(SESSION).revision == second
    assert _revisions(after) == [{"from": first, "to": second, "reason": "unpinned"}]


def test_an_unpinned_session_still_holds_its_revision_while_a_question_waits(
    unpinned_settings: Settings, catalog_copy: Path
) -> None:
    """A goal that asked for an input finishes under the revision it started on.

    The procedure changes while the question is outstanding: the revision built
    after it no longer declares the restart among the writes the procedure may
    perform, so an answer resolved through that revision would stop the plan
    the person is completing. It is resolved through the one they were asked
    under, and the plan runs to its approval.
    """
    first = _build(unpinned_settings)
    engine = _engine(
        unpinned_settings,
        script=[
            _route("new_task", RESTART),
            _plan_missing_the_service(),
            _route("provide_input", RESTART),
            _restart_plan(),
            _route("greeting", None),
        ],
    )

    asked = engine.turn(ASKED, "something in staging is wedged, restart it")
    assert asked.pending is not None and asked.pending.question == WHICH_SERVICE
    assert engine.state(ASKED).pending_goal is not None
    assert engine.state(ASKED).pending_approval is None
    assert engine.state(ASKED).revision == first

    _withdraw_the_write(catalog_copy)
    second = _build(unpinned_settings)
    assert second != first
    assert engine.registry.newest_revision() == second
    assert _may_write_under(unpinned_settings, second) is False

    answered = engine.turn(ASKED, "search")

    assert [outcome.kind for outcome in answered.outcomes] == ["read", "approval_pending"]
    assert _revisions(answered) == []
    assert engine.state(ASKED).revision == first

    # The question is answered and the approval it led to is refused, so nothing
    # of the task is left waiting: the next turn moves.
    engine.turn(ASKED, "no")
    after = engine.turn(ASKED, "hello")
    assert engine.state(ASKED).revision == second
    assert _revisions(after) == [{"from": first, "to": second, "reason": "unpinned"}]


# --- helpers ----------------------------------------------------------------


def _engine(
    settings: Settings,
    *,
    extra: list[dict] | None = None,
    script: list[dict] | None = None,
) -> Engine:
    """An engine over these settings, answered by the script this suite needs."""
    answers = script or [_route("new_task", RESTART), _restart_plan(), *(extra or [])]
    return build_engine(
        settings, provider=ScriptedModelProvider(answers), audit_sink=MemorySink()
    )


def _build(settings: Settings) -> str:
    """Release the catalog of these settings and return the revision identifier."""
    manifest = build_revision(
        settings.skills_roots,
        Path(settings.state_dir) / "revisions",
        tenant=TENANT,
        settings=settings,
    )
    return manifest.revision


def _retire(root: Path, name: str) -> None:
    """Retire one runbook of a catalog copy, the way a change to it would."""
    path = root / "runbook" / name / "v1" / "skill.md"
    text = path.read_text(encoding="utf-8")
    assert "status: published" in text
    path.write_text(text.replace("status: published", "status: retired", 1), encoding="utf-8")


def _withdraw_the_write(root: Path) -> None:
    """Take the restart out of the writes the restart procedure may perform.

    The policy of that procedure decides that one operation, so it goes with
    it: a policy naming an operation the procedure does not declare is a
    catalog that would not build.
    """
    version = root / "runbook" / "service_restart" / "v1"
    skill = version / "skill.md"
    text = skill.read_text(encoding="utf-8")
    declared = "  writes:\n    - service.restart\n"
    assert declared in text
    skill.write_text(text.replace(declared, "  writes: []\n", 1), encoding="utf-8")
    (version / "policy.json").write_text(
        json.dumps({"skill_id": RESTART, "default": "deny", "rules": []}, indent=2),
        encoding="utf-8",
    )


def _may_write_under(settings: Settings, revision: str) -> bool:
    """Whether the restart procedure of one built revision may still restart."""
    store = RevisionStore(Path(settings.state_dir) / "revisions")
    registry = SkillRegistry.from_revision(store, TENANT, revision)
    return "service.restart" in registry.require(RESTART).frontmatter.capabilities.writes


def _plan_missing_the_service() -> dict:
    """A plan that runs nothing and asks which service the request is about."""
    return {
        "purpose": "plan",
        "response": {
            "runbook_id": RESTART,
            "operations": [],
            "missing": [{"field": "service", "question": WHICH_SERVICE}],
            "reply_intent": "ask which service",
        },
    }


def _published_under(settings: Settings, revision: str) -> bool:
    """Whether the restart procedure is published by one built revision."""
    store = RevisionStore(Path(settings.state_dir) / "revisions")
    registry = SkillRegistry.from_revision(store, TENANT, revision)
    return any(skill.id == RESTART for skill in registry.published())


def _route(kind: str, runbook_id: str | None) -> dict:
    return {
        "purpose": "route",
        "response": {
            "kind": kind,
            "runbook_id": runbook_id,
            "confidence": 0.9,
            "language": "en",
        },
    }


def _restart_plan(service: str = "search") -> dict:
    return {
        "purpose": "plan",
        "response": {
            "runbook_id": RESTART,
            "operations": [
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.status",
                    "payload": {"service": service},
                    "write": False,
                    "purpose": "read the current state",
                },
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.restart",
                    "payload": {"service": service},
                    "write": True,
                    "purpose": "restart the service",
                },
            ],
            "missing": [],
            "reply_intent": "report the restart",
        },
    }


def _revisions(result) -> list[dict]:
    """The revision events of one turn, as they were recorded."""
    return [event.data for event in result.events if event.kind == "revision"]
