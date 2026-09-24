"""Change requests, and the invariant that the serving process cannot write a catalog.

The store is tested the way a store is. The invariant is not: reading the
source would only say that nothing currently calls a write, and the point is
that nothing can. So the turn, the draft and the whole lifecycle to `published`
run with every write under the example catalog raised as an error, and the tree
is hashed before and after.
"""

from __future__ import annotations

import builtins
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oska.api.app import create_app
from oska.audit.sink import MemorySink
from oska.catalog.registry import SkillRegistry
from oska.cli.commands_change import app as change_app
from oska.config import Settings
from oska.conformance import load_scenarios
from oska.contracts.common import Message
from oska.contracts.proposal import ProposalState, QualityReport
from oska.contracts.receipt import WriteReceipt
from oska.engine.factory import build_engine
from oska.improvement.job import run_job
from oska.learning.changes import (
    ChangeRequestError,
    ChangeRequestState,
    ChangeRequestStore,
    new_request,
    package_of,
    store_for,
)
from oska.learning.lifecycle import transition
from oska.learning.proposals import ProposalStore, draft_from_session
from oska.planner.scripted import ScriptedModelProvider

SESSION = "changes-session"
MARKDOWN = "---\nid: runbook.sample\n---\n\n## Steps\n\n1. Do it.\n"
POLICY = {"skill_id": "runbook.sample", "default": "deny", "rules": []}

# The turns the scripted provider answers, taken from the scenario that records
# them, then one answer for the draft the proposal endpoint asks for.
DRAFT_STEP = {
    "purpose": "proposal draft",
    "response": {
        "description": "Restarts a service and reads its state back, as one session did it.",
        "triggers": ["restart that service", "bounce the service", "run the restart procedure"],
        "action": "restart a service",
        "purpose": "Restarts one service and confirms the result by reading its state back.",
        "preconditions": "- The service name is known.",
        "steps": "1. Call `service.restart` with the service name.\n2. Read the state back.",
        "verification": "The status read back names the service as running.",
        "rollback": "None was recorded.",
    },
}


@pytest.fixture
def store(tmp_path: Path) -> ChangeRequestStore:
    """A change request store in a temporary directory."""
    return ChangeRequestStore(tmp_path / "changes")


@pytest.fixture
def catalog_root(tmp_path: Path) -> Path:
    """A catalog root a person may apply a request into."""
    return tmp_path / "catalog"


def _saved(store: ChangeRequestStore, *, version: int = 1) -> str:
    request = new_request(
        "proposal-1", "runbook.sample", version, "a reviewer", QualityReport(issues=[], score=1.0)
    )
    store.save(request, skill_markdown=MARKDOWN, policy=POLICY)
    return request.request_id


def test_a_request_holds_the_package_the_record_and_the_summary(
    store: ChangeRequestStore,
) -> None:
    request_id = _saved(store)
    directory = store.directory(request_id)

    assert (directory / "request.json").is_file()
    assert (directory / "summary.md").is_file()
    package = directory / "runbook" / "sample" / "v1"
    assert (package / "skill.md").read_text(encoding="utf-8") == MARKDOWN
    assert json.loads((package / "policy.json").read_text(encoding="utf-8")) == POLICY

    held = store.get(request_id)
    assert held.proposal_id == "proposal-1"
    assert held.skill_id == "runbook.sample"
    assert held.version == 1
    assert held.actor == "a reviewer"
    assert held.state is ChangeRequestState.open
    assert held.package == "runbook/sample/v1"
    assert held.created_at.tzinfo is not None
    summary = (directory / "summary.md").read_text(encoding="utf-8")
    assert "a reviewer" in summary
    assert "runbook.sample" in summary
    assert "version 1" in summary


def test_the_package_path_is_read_off_the_identifier() -> None:
    assert package_of("runbook.sample", 3) == "runbook/sample/v3"
    with pytest.raises(ChangeRequestError):
        package_of("nodot", 1)


def test_listing_filters_by_state_and_a_missing_one_is_a_key_error(
    store: ChangeRequestStore, catalog_root: Path
) -> None:
    first = _saved(store)
    second = _saved(store, version=2)
    store.reject(second, by="a reviewer", note="not this one")

    assert [held.request_id for held in store.list()] == [first, second]
    assert [held.request_id for held in store.list(ChangeRequestState.open)] == [first]
    assert [held.request_id for held in store.list("rejected")] == [second]
    assert store.list(ChangeRequestState.applied) == []
    with pytest.raises(KeyError):
        store.get("nothing-like-this")
    with pytest.raises(ChangeRequestError):
        store.get("../escape")


def test_applying_copies_the_package_and_closes_the_request(
    store: ChangeRequestStore, catalog_root: Path
) -> None:
    request_id = _saved(store)

    copied = store.apply(request_id, catalog_root, by="an operator")

    assert [path.name for path in copied] == ["policy.json", "skill.md"]
    assert (catalog_root / "runbook" / "sample" / "v1" / "skill.md").read_text(
        encoding="utf-8"
    ) == MARKDOWN
    held = store.get(request_id)
    assert held.state is ChangeRequestState.applied
    assert held.decided_by == "an operator"
    assert held.applied_to == str(catalog_root)
    assert "Applied into" in (store.directory(request_id) / "summary.md").read_text(
        encoding="utf-8"
    )


def test_a_request_is_decided_once_and_never_overwrites_a_version(
    store: ChangeRequestStore, catalog_root: Path
) -> None:
    first = _saved(store)
    store.apply(first, catalog_root, by="an operator")
    with pytest.raises(ChangeRequestError):
        store.apply(first, catalog_root, by="an operator")
    with pytest.raises(ChangeRequestError):
        store.reject(first, by="an operator", note="too late")

    second = _saved(store)
    with pytest.raises(ChangeRequestError):
        store.apply(second, catalog_root, by="an operator")
    assert store.get(second).state is ChangeRequestState.open


def test_a_decision_records_who_made_it(
    store: ChangeRequestStore, catalog_root: Path
) -> None:
    request_id = _saved(store)
    with pytest.raises(ChangeRequestError):
        store.apply(request_id, catalog_root, by="   ")
    with pytest.raises(ChangeRequestError):
        store.reject(request_id, by="", note="")
    assert store.get(request_id).state is ChangeRequestState.open
    assert not catalog_root.exists()


def test_rejecting_copies_nothing(store: ChangeRequestStore, catalog_root: Path) -> None:
    request_id = _saved(store)

    rejected = store.reject(request_id, by="a reviewer", note="the rollback is missing")

    assert rejected.state is ChangeRequestState.rejected
    assert rejected.note == "the rollback is missing"
    assert isinstance(rejected.decided_at, datetime)
    assert not catalog_root.exists()


def test_the_commands_read_apply_and_reject(
    monkeypatch: pytest.MonkeyPatch,
    example_settings: Settings,
    tmp_path: Path,
    catalog_root: Path,
) -> None:
    """The terminal is the only place a package reaches a catalog."""
    for name, value in (
        ("SKILLS_ROOTS", example_settings.skills_roots[0]),
        ("RUNTIME_CONFIG", example_settings.runtime_config),
        ("MODEL_CONFIG", example_settings.model_config_path),
        ("CONNECTORS_CONFIG", example_settings.connectors_config),
        ("SCHEMAS_DIR", example_settings.schemas_dir),
        ("STATE_DIR", example_settings.state_dir),
    ):
        monkeypatch.setenv(f"OSKA_{name}", str(value))
    monkeypatch.setenv("OSKA_PROVIDER", "none")
    store = store_for(example_settings)
    request_id = _saved(store)
    other = _saved(store, version=2)
    runner = CliRunner()

    listed = runner.invoke(change_app, ["list"])
    assert listed.exit_code == 0
    assert request_id in listed.stdout
    assert "runbook.sample v1" in listed.stdout

    shown = runner.invoke(change_app, ["show", request_id])
    assert shown.exit_code == 0
    assert "runbook/sample/v1/skill.md" in shown.stdout.replace("\\", "/")

    applied = runner.invoke(
        change_app, ["apply", request_id, "--into", str(catalog_root), "--by", "an operator"]
    )
    assert applied.exit_code == 0
    assert "skill.md" in applied.stdout
    assert "oska revision build" in applied.stdout
    assert (catalog_root / "runbook" / "sample" / "v1" / "skill.md").is_file()

    rejected = runner.invoke(
        change_app, ["reject", other, "--by", "a reviewer", "--note", "the rollback is missing"]
    )
    assert rejected.exit_code == 0
    assert store.get(other).state is ChangeRequestState.rejected

    again = runner.invoke(
        change_app, ["apply", request_id, "--into", str(catalog_root), "--by", "an operator"]
    )
    assert again.exit_code == 1

    open_only = runner.invoke(change_app, ["list", "--state", "open"])
    assert open_only.exit_code == 0
    assert open_only.stdout.strip() == ""


@contextmanager
def no_writes_under(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Raise on any write under `root`, whichever way it is attempted."""
    forbidden = root.resolve()
    write_text, write_bytes = Path.write_text, Path.write_bytes
    path_open, mkdir, builtin_open = Path.open, Path.mkdir, builtins.open
    attempts: list[str] = []

    def refuse(target: object, what: str) -> None:
        if isinstance(target, int):
            return
        where = Path(str(target)).resolve()
        if where.is_relative_to(forbidden):
            attempts.append(f"{what}: {where}")
            raise AssertionError(f"the serving process {what} inside the catalog: {where}")

    def writing(mode: str) -> bool:
        return any(letter in mode for letter in "wax+")

    def guarded_write_text(self: Path, *args: object, **kwargs: object) -> int:
        refuse(self, "wrote")
        return write_text(self, *args, **kwargs)

    def guarded_write_bytes(self: Path, *args: object, **kwargs: object) -> int:
        refuse(self, "wrote")
        return write_bytes(self, *args, **kwargs)

    def guarded_path_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if writing(mode):
            refuse(self, "opened for writing")
        return path_open(self, mode, *args, **kwargs)

    def guarded_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        refuse(self, "created a directory")
        return mkdir(self, *args, **kwargs)

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        if writing(mode):
            refuse(file, "opened for writing")
        return builtin_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        yield attempts
    finally:
        monkeypatch.undo()


def fingerprint(root: Path) -> str:
    """One hash over every file of a tree, name and bytes."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_nothing_the_runtime_runs_writes_into_the_catalog_it_serves(
    example_settings: Settings,
    repo_root: Path,
    skills_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant, proved by running under it rather than by reading the source.

    A turn, a draft, the whole lifecycle to `published` and the improvement job
    run with every write under the example catalog raised as an error. All of
    them have to finish, and the catalog has to be the same bytes afterwards.
    """
    scenarios = {
        scenario.name: scenario
        for scenario in load_scenarios(repo_root / "examples" / "ops" / "scenarios")
    }
    script = [*scenarios["02_restart_staging"].provider_script[:3], DRAFT_STEP]
    engine = build_engine(
        example_settings,
        provider=ScriptedModelProvider(script),
        audit_sink=MemorySink(),
    )
    application = create_app(
        example_settings,
        engine=engine,
        proposal_store=ProposalStore(tmp_path / "proposals"),
    )
    client = TestClient(application)
    before = fingerprint(skills_root)

    with no_writes_under(skills_root, monkeypatch) as attempts:
        # The guard is worth nothing if it does not catch a write, so it catches
        # one first, and the rest of this block runs with it proven.
        with pytest.raises(AssertionError):
            (skills_root / "runbook" / "proof.md").write_text("no", encoding="utf-8")
        attempts.clear()

        first = client.post(f"/sessions/{SESSION}/turns", json={"text": _request_text()})
        second = client.post(f"/sessions/{SESSION}/turns", json={"text": "yes"})
        drafted = client.post(f"/proposals/from-session/{SESSION}")
        proposal_id = drafted.json()["proposal_id"]
        moves = [
            client.post(
                f"/proposals/{proposal_id}/transition", json={"to": state, "by": "a reviewer"}
            )
            for state in ("submitted", "approved", "published")
        ]
        report = run_job(
            example_settings,
            cases_path=repo_root / "examples" / "ops" / "evaluation" / "routing.json",
            redactions_path=repo_root / "examples" / "ops" / "evaluation" / "redactions.json",
        )

    assert attempts == []
    assert first.status_code == 200
    assert second.status_code == 200
    assert [outcome["kind"] for outcome in second.json()["outcomes"]] == ["receipt"]
    assert drafted.status_code == 200
    assert [move.status_code for move in moves] == [200, 200, 200]
    assert moves[-1].json()["proposal"]["state"] == "published"
    assert report is not None

    request = moves[-1].json()["change_request"]
    assert request is not None
    assert (store_for(example_settings).package(request["request_id"]) / "skill.md").is_file()
    assert fingerprint(skills_root) == before


def _request_text() -> str:
    return "the search service in staging needs a restart, it is wedged again"


class _Recording:
    """A publisher that records what it was handed, or refuses when told to."""

    name = "recording"

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.seen: list[str] = []

    def publish(self, request, store):  # type: ignore[no-untyped-def]
        self.seen.append(request.request_id)
        if self.refuse:
            raise ChangeRequestError("the repository is not there")
        request.branch, request.commit = f"change/{request.request_id}", "abc123"
        store.save(request)
        return request


def _publish(
    tmp_path: Path, skills_root: Path, schemas_dir: Path, publisher: _Recording
) -> tuple[str, ChangeRequestStore]:
    """Draft one procedure and move it to `published` with the given publisher."""
    registry = SkillRegistry([skills_root])
    proposals = ProposalStore(tmp_path / "proposals")
    changes = ChangeRequestStore(tmp_path / "changes")
    receipt = WriteReceipt(
        receipt_id="receipt-1",
        session_id=SESSION,
        ledger_key="key",
        integration_id="integration.container_runtime",
        operation="service.restart",
        payload={"service": "checkout"},
        status="executed",
        created_at=datetime.now(UTC),
    )
    proposal = draft_from_session(
        SESSION,
        [Message(role="user", content="the checkout service is stuck")],
        [receipt],
        registry,
        None,
        runbook_id_hint="service_bounce",
        schema_dir=schemas_dir,
    )
    proposals.save(proposal)
    for state in (ProposalState.submitted, ProposalState.approved, ProposalState.published):
        moved = transition(
            proposals,
            proposal.proposal_id,
            state,
            "a reviewer",
            registry=registry,
            changes=changes,
            schema_dir=schemas_dir,
            publisher=publisher,
        )
    assert moved.state is ProposalState.published
    return moved.history[-1].note, changes


def test_publication_hands_the_request_to_its_publisher(
    tmp_path: Path, skills_root: Path, schemas_dir: Path
) -> None:
    publisher = _Recording()
    note, changes = _publish(tmp_path, skills_root, schemas_dir, publisher)
    (request,) = changes.list()
    assert publisher.seen == [request.request_id]
    assert request.branch == f"change/{request.request_id}"
    assert f"branch change/{request.request_id} at abc123" in note


def test_a_publisher_that_fails_leaves_the_request_open_and_says_so(
    tmp_path: Path, skills_root: Path, schemas_dir: Path
) -> None:
    note, changes = _publish(tmp_path, skills_root, schemas_dir, _Recording(refuse=True))
    (request,) = changes.list()
    assert request.state is ChangeRequestState.open
    assert request.branch == ""
    assert "the recording publisher did not publish it" in note
