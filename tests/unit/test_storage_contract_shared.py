"""One behavioral contract for the audit trail, the proposals and the change requests.

These are the stores several replicas share with `storage.backend: postgres`,
and keep per process with the files backend. The same tests run against both:
files always, PostgreSQL when `OSKA_TEST_POSTGRES_URL` names a database the tests
may write to, skipped with the reason otherwise. Every PostgreSQL test works
under a namespace of its own and purges it afterwards.

A factory opens a new store over the same record each time it is called, the
way a second replica would, so a test that writes through one store and reads
through another is the two-replica check.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from oska.audit.events import new_event
from oska.audit.sink import JsonlSink
from oska.config import RuntimeConfig, Settings, StorageConfig
from oska.contracts.change import ChangeRequest, ChangeRequestError, ChangeRequestState
from oska.contracts.proposal import (
    LifecycleError,
    ProposalState,
    QualityReport,
    SkillProposal,
)
from oska.contracts.session import SessionState
from oska.engine.session_store import SessionStore
from oska.governance.ledger import WriteLedger
from oska.governance.retention import RETENTION_KIND, forget
from oska.learning.changes import ChangeRequestStore, new_request
from oska.learning.forgetting import FORGOTTEN, ChangeRequestRecords, ProposalRecords
from oska.learning.proposals import ProposalStore
from oska.observability.metrics import service_report
from oska.services import open_changes, open_proposals
from oska.storage import (
    DEFAULT_NAMESPACE,
    AuditStore,
    ChangeRequestRecordStore,
    LedgerStore,
    ProposalRecordStore,
    SessionStateStore,
    StorageError,
    open_audit,
    storage_namespace,
)

POSTGRES_URL_ENV = "OSKA_TEST_POSTGRES_URL"
SKILL_ID = "runbook.sample_change"
MARKDOWN = "---\nid: runbook.sample_change\n---\n## Steps\n\n1. Do the thing.\n"
POLICY = {"skill_id": SKILL_ID, "default": "deny", "rules": []}


@dataclass
class Backend:
    """Factories for every store of one backend over one record, under one namespace."""

    name: str
    audit: Callable[[], AuditStore]
    proposals: Callable[[], ProposalRecordStore]
    changes: Callable[[], ChangeRequestRecordStore]
    ledger: Callable[[], LedgerStore]
    sessions: Callable[[], SessionStateStore]


def _postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not set, so the postgres backend is not exercised")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg is not installed; install the optional extra oska[postgres]")
    return url


def _postgres(url: str, namespace: str) -> Backend:
    from oska.storage.postgres_audit import PostgresAuditStore
    from oska.storage.postgres_changes import PostgresChangeRequestStore
    from oska.storage.postgres_ledger import PostgresLedger
    from oska.storage.postgres_proposals import PostgresProposalStore
    from oska.storage.postgres_sessions import PostgresSessionStore

    return Backend(
        "postgres",
        lambda: PostgresAuditStore(url, namespace=namespace),
        lambda: PostgresProposalStore(url, namespace=namespace),
        lambda: PostgresChangeRequestStore(url, namespace=namespace),
        lambda: PostgresLedger(url, namespace=namespace),
        lambda: PostgresSessionStore(url, namespace=namespace),
    )


def _files(root: Path) -> Backend:
    return Backend(
        "files",
        lambda: JsonlSink(root / "audit.jsonl"),
        lambda: ProposalStore(root / "proposals"),
        lambda: ChangeRequestStore(root / "changes"),
        lambda: WriteLedger(root / "ledger.sqlite"),
        lambda: SessionStore(root / "sessions.sqlite"),
    )


def _purge(backend: Backend) -> None:
    for open_store in (
        backend.audit,
        backend.proposals,
        backend.changes,
        backend.ledger,
        backend.sessions,
    ):
        open_store().purge()


@pytest.fixture(params=["files", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "files":
        yield _files(tmp_path)
        return
    held = _postgres(_postgres_url(), f"contract/{uuid.uuid4().hex}")
    yield held
    _purge(held)


@pytest.fixture(params=["files", "postgres"])
def spaces(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Callable[[str], Backend]]:
    """A backend under a namespace the test names: a directory, or a database namespace."""
    if request.param == "files":
        yield lambda namespace: _files(tmp_path / namespace)
        return
    url = _postgres_url()
    opened: list[Backend] = []

    def make(namespace: str) -> Backend:
        held = _postgres(url, namespace)
        opened.append(held)
        return held

    yield make
    for held in opened:
        _purge(held)


@pytest.fixture
def session() -> str:
    return f"contract-{uuid.uuid4().hex}"


def proposal(session_id: str, state: ProposalState = ProposalState.draft) -> SkillProposal:
    return SkillProposal(
        proposal_id=str(uuid.uuid4()),
        state=state,
        source_session_id=session_id,
        skill_markdown=MARKDOWN,
        policy=POLICY,
        quality=QualityReport(issues=["quotes the session"], score=0.9),
    )


def composed(store: ChangeRequestRecordStore, proposal_id: str, version: int = 1) -> str:
    request = new_request(proposal_id, SKILL_ID, version, "reviewer", QualityReport(score=1.0))
    store.save(request, skill_markdown=MARKDOWN, policy=POLICY)
    return request.request_id


# The interfaces.


def test_every_store_satisfies_its_interface(backend: Backend) -> None:
    assert isinstance(backend.audit(), AuditStore)
    assert isinstance(backend.proposals(), ProposalRecordStore)
    assert isinstance(backend.changes(), ChangeRequestRecordStore)


# The audit trail.


def test_the_trail_is_read_back_in_the_order_it_was_appended(
    backend: Backend, session: str
) -> None:
    trail = backend.audit()
    assert not trail.exists()
    assert trail.events() == []

    for kind in ("turn", "route", "reply"):
        trail.emit(new_event(kind, session, "t1", {"kind": kind}))

    events = backend.audit().events()
    assert [event.kind for event in events] == ["turn", "route", "reply"]
    assert events[0].data == {"kind": "turn"} and events[0].session_id == session
    assert backend.audit().exists()


def test_deleting_sessions_keeps_the_kinds_named_and_other_sessions(
    backend: Backend, session: str
) -> None:
    other = f"{session}-other"
    trail = backend.audit()
    trail.emit(new_event("turn", session, "t1"))
    trail.emit(new_event("reply", session, "t1"))
    trail.emit(new_event(RETENTION_KIND, session, "r1"))
    trail.emit(new_event("turn", other, "t2"))

    dropped = backend.audit().delete_sessions([session], keep_kinds=[RETENTION_KIND])

    assert dropped == {session: 2}
    left = [(event.session_id, event.kind) for event in backend.audit().events()]
    assert left == [(session, RETENTION_KIND), (other, "turn")]
    assert backend.audit().delete_sessions([]) == {}


def test_the_service_report_reads_a_trail_another_replica_wrote(
    backend: Backend, session: str
) -> None:
    assert service_report(backend.audit()).turns_missing

    backend.audit().emit(new_event("turn", session, "t1"))
    backend.audit().emit(new_event("reply", session, "t1", {"kind": "no_model"}))

    report = service_report(backend.audit())
    assert report.turns == 1 and report.turns_missing == ""


# Proposals.


def test_a_proposal_round_trips_and_is_listed_by_state(backend: Backend, session: str) -> None:
    store = backend.proposals()
    drafted = proposal(session)
    submitted = proposal(session, ProposalState.submitted)
    store.save(drafted)
    store.save(submitted)

    assert store.get(drafted.proposal_id) == drafted
    assert {held.proposal_id for held in store.list()} == {
        drafted.proposal_id,
        submitted.proposal_id,
    }
    assert [held.proposal_id for held in store.list(ProposalState.submitted)] == [
        submitted.proposal_id
    ]
    listed = [held.proposal_id for held in store.list()]
    assert listed == sorted(listed)

    drafted.state = ProposalState.rejected
    store.save(drafted)
    assert store.get(drafted.proposal_id).state is ProposalState.rejected

    store.delete(drafted.proposal_id)
    with pytest.raises(KeyError):
        store.get(drafted.proposal_id)
    with pytest.raises(KeyError):
        store.delete(drafted.proposal_id)


def test_proposals_written_by_one_replica_are_listed_by_another(
    backend: Backend, session: str
) -> None:
    first, second = backend.proposals(), backend.proposals()
    written = proposal(session)

    first.save(written)

    assert [held.proposal_id for held in second.list()] == [written.proposal_id]
    second.delete(written.proposal_id)
    assert first.list() == []


def test_of_two_moves_from_one_state_only_the_first_is_written(
    backend: Backend, session: str
) -> None:
    held = proposal(session, ProposalState.approved)
    backend.proposals().save(held)
    first = backend.proposals().get(held.proposal_id)
    second = backend.proposals().get(held.proposal_id)

    first.state = ProposalState.published
    backend.proposals().transition(first, ProposalState.approved)
    second.state = ProposalState.rejected
    with pytest.raises(LifecycleError, match="another decision came first"):
        backend.proposals().transition(second, ProposalState.approved)

    assert backend.proposals().get(held.proposal_id).state is ProposalState.published
    assert [held.proposal_id for held in backend.proposals().list(ProposalState.rejected)] == []
    with pytest.raises(KeyError):
        backend.proposals().transition(proposal(session), ProposalState.draft)


# Change requests.


def test_a_change_request_keeps_its_package(backend: Backend, session: str) -> None:
    store = backend.changes()
    request_id = composed(store, "p-1")

    held = backend.changes().get(request_id)
    assert held.state is ChangeRequestState.open and held.skill_id == SKILL_ID
    files = backend.changes().files(request_id)
    assert files["skill.md"] == MARKDOWN
    assert json.loads(files["policy.json"]) == POLICY

    backend.changes().add_file(request_id, "generated-examples.json", "{}\n")
    assert set(store.files(request_id)) == {"skill.md", "policy.json", "generated-examples.json"}
    with pytest.raises(ChangeRequestError, match="inside the package"):
        store.add_file(request_id, "../escape.md", "no")
    with pytest.raises(KeyError):
        store.get("missing")


def test_change_requests_are_listed_oldest_first_by_any_replica(backend: Backend) -> None:
    first = composed(backend.changes(), "p-1")
    second = composed(backend.changes(), "p-2")
    backend.changes().reject(second, by="reviewer", note="not now")

    other = backend.changes()
    assert [request.request_id for request in other.list()] == [first, second]
    assert [request.request_id for request in other.list(ChangeRequestState.rejected)] == [
        second
    ]


def test_applying_writes_the_package_into_the_catalog_once(
    backend: Backend, tmp_path: Path
) -> None:
    store = backend.changes()
    request_id = composed(store, "p-1")
    catalog = tmp_path / "catalog"

    with pytest.raises(ChangeRequestError, match="who applied it"):
        store.apply(request_id, catalog, by=" ")
    written = backend.changes().apply(request_id, catalog, by="operator")

    package = catalog / "runbook" / "sample_change" / "v1"
    assert (package / "skill.md").read_text(encoding="utf-8") == MARKDOWN
    assert package / "skill.md" in written
    applied = store.get(request_id)
    assert applied.state is ChangeRequestState.applied
    assert (applied.decided_by, applied.applied_to) == ("operator", str(catalog))
    with pytest.raises(ChangeRequestError, match="not open"):
        store.apply(request_id, catalog, by="operator")
    with pytest.raises(ChangeRequestError, match="not open"):
        store.reject(request_id, by="operator")

    again = composed(store, "p-2")
    with pytest.raises(ChangeRequestError, match="never overwritten"):
        store.apply(again, catalog, by="operator")
    assert store.get(again).state is ChangeRequestState.open


def test_two_requests_for_one_version_applied_at_once_leave_the_winner_only(
    backend: Backend, tmp_path: Path
) -> None:
    texts = {}
    for proposal_id in ("p-1", "p-2"):
        request = new_request(proposal_id, SKILL_ID, 1, "reviewer", QualityReport(score=1.0))
        backend.changes().save(
            request, skill_markdown=f"{MARKDOWN}\n{proposal_id}\n", policy=POLICY
        )
        texts[request.request_id] = proposal_id
    catalog = tmp_path / "catalog"
    barrier = threading.Barrier(2)

    def attempt(request_id: str) -> str:
        store = backend.changes()
        barrier.wait()
        try:
            store.apply(request_id, catalog, by="operator")
        except ChangeRequestError as refusal:
            assert "already there" in str(refusal)
            return "refused"
        return "applied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = dict(zip(texts, pool.map(attempt, texts), strict=True))

    assert sorted(outcomes.values()) == ["applied", "refused"]
    winner = next(request_id for request_id, outcome in outcomes.items() if outcome == "applied")
    package = catalog / "runbook" / "sample_change" / "v1"
    assert sorted(path.name for path in package.iterdir()) == ["policy.json", "skill.md"]
    assert texts[winner] in (package / "skill.md").read_text(encoding="utf-8")
    for request_id, outcome in outcomes.items():
        expected = ChangeRequestState.applied if outcome == "applied" else ChangeRequestState.open
        assert backend.changes().get(request_id).state is expected


def test_a_package_is_written_out_and_dropped_without_the_record(
    backend: Backend, tmp_path: Path
) -> None:
    store = backend.changes()
    request_id = composed(store, "p-1")

    written = store.write_package(request_id, tmp_path / "out")
    assert sorted(path.name for path in written) == ["policy.json", "skill.md"]

    assert store.drop_package(request_id) is True
    assert store.files(request_id) == {}
    assert store.get(request_id).request_id == request_id
    assert store.drop_package(request_id) is False
    with pytest.raises(ChangeRequestError, match="holds no package"):
        store.write_package(request_id, tmp_path / "again")

    store.delete(request_id)
    with pytest.raises(KeyError):
        store.get(request_id)
    with pytest.raises(KeyError):
        store.delete(request_id)


# Namespaces.


def test_two_namespaces_never_see_each_others_records(
    spaces: Callable[[str], Backend], session: str
) -> None:
    suffix = uuid.uuid4().hex
    left = spaces(storage_namespace(f"left{suffix}", "ops"))
    right = spaces(storage_namespace(f"right{suffix}", "ops"))

    left.audit().emit(new_event("turn", session, "t1"))
    left.proposals().save(proposal(session))
    composed(left.changes(), "p-1")

    assert right.audit().events() == [] and not right.audit().exists()
    assert right.proposals().list() == []
    assert right.changes().list() == []


def test_a_purge_removes_its_own_namespace_and_the_default_is_refused(
    spaces: Callable[[str], Backend], session: str
) -> None:
    suffix = uuid.uuid4().hex
    kept = spaces(storage_namespace(f"kept{suffix}", "ops"))
    gone = spaces(storage_namespace(f"gone{suffix}", "ops"))
    for held in (kept, gone):
        held.audit().emit(new_event("turn", session, "t1"))
        held.proposals().save(proposal(session))
        composed(held.changes(), "p-1")

    removed = (gone.audit().purge(), gone.proposals().purge(), gone.changes().purge())

    if gone.name == "files":
        assert removed == (0, 0, 0)
        return
    assert removed == (1, 1, 1)
    assert gone.audit().events() == [] and gone.proposals().list() == []
    assert gone.changes().list() == []
    assert len(kept.audit().events()) == 1 and len(kept.proposals().list()) == 1
    assert len(kept.changes().list()) == 1
    default = _postgres(_postgres_url(), DEFAULT_NAMESPACE)
    for open_store in (default.audit, default.proposals, default.changes):
        with pytest.raises(StorageError, match="never purged"):
            open_store().purge()


# Retention reaches the shared stores.


def test_forget_removes_a_session_from_the_trail_the_proposals_and_the_changes(
    backend: Backend, session: str, tmp_path: Path
) -> None:
    ledger, sessions = backend.ledger(), backend.sessions()
    sessions.save(SessionState(session_id=session))
    trail = backend.audit()
    trail.emit(new_event("turn", session, "t1"))
    trail.emit(new_event("turn", "someone-else", "t2"))
    proposals, changes = backend.proposals(), backend.changes()
    pending = proposal(session)
    applied = proposal(session, ProposalState.published)
    proposals.save(pending)
    proposals.save(applied)
    open_request = composed(changes, pending.proposal_id)
    applied_request = composed(changes, applied.proposal_id)
    changes.apply(applied_request, tmp_path / "catalog", by="operator")

    report = forget(
        session,
        ledger=ledger,
        sessions=sessions,
        audit=backend.audit(),
        proposals=ProposalRecords(backend.proposals(), backend.changes()),
        change_requests=ChangeRequestRecords(backend.proposals(), backend.changes()),
    )

    assert (report.events, report.proposals, report.change_requests) == (1, 2, 2)
    left = [(event.session_id, event.kind) for event in backend.audit().events()]
    assert (session, "turn") not in left
    assert (session, RETENTION_KIND) in left and ("someone-else", "turn") in left
    with pytest.raises(KeyError):
        backend.proposals().get(pending.proposal_id)
    with pytest.raises(KeyError):
        backend.changes().get(open_request)
    kept = backend.proposals().get(applied.proposal_id)
    assert kept.source_session_id == FORGOTTEN and kept.skill_markdown == ""
    assert backend.changes().files(applied_request) == {}
    assert backend.changes().get(applied_request).state is ChangeRequestState.applied


# Opening a backend from configuration.


def test_the_files_backend_opens_what_it_always_opened(tmp_path: Path) -> None:
    runtime = RuntimeConfig()
    settings = Settings(state_dir=tmp_path)

    audit = open_audit(runtime, settings)
    proposals = open_proposals(runtime, settings)
    changes = open_changes(runtime, settings)

    assert isinstance(audit, JsonlSink) and audit.path.parent == tmp_path
    assert isinstance(proposals, ProposalStore) and proposals.directory == tmp_path / "proposals"
    assert isinstance(changes, ChangeRequestStore) and changes.root.parent == tmp_path


def test_postgres_without_an_address_names_the_variable_to_set(tmp_path: Path) -> None:
    runtime = RuntimeConfig(storage=StorageConfig(backend="postgres", url_env="SAMPLE_DB_URL"))
    settings = Settings(state_dir=tmp_path)

    for opener in (open_audit, open_proposals, open_changes):
        with pytest.raises(StorageError, match="set SAMPLE_DB_URL"):
            opener(runtime, settings, env={})


# The PostgreSQL stores name the namespace in every statement, checked offline.


class _Recorded:
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []


class _Recorder:
    """Stands in for the database: records every statement and its parameters."""

    identity = "offline"

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    @contextmanager
    def connect(self) -> Iterator[_Recorder]:
        yield self

    def execute(self, statement: str, parameters: Any = ()) -> _Recorded:
        self.statements.append((" ".join(statement.split()), tuple(parameters)))
        return _Recorded()


def test_every_shared_store_statement_is_scoped_to_the_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import oska.storage.postgres_audit as postgres_audit
    import oska.storage.postgres_changes as postgres_changes
    import oska.storage.postgres_proposals as postgres_proposals

    for module in (postgres_audit, postgres_changes, postgres_proposals):
        monkeypatch.setattr(module, "jsonb", lambda value: value)
    database: Any = _Recorder()
    namespace = storage_namespace("acme", "operations")
    audit = postgres_audit.PostgresAuditStore("unused", database=database, namespace=namespace)
    proposals = postgres_proposals.PostgresProposalStore(
        "unused", database=database, namespace=namespace
    )
    changes = postgres_changes.PostgresChangeRequestStore(
        "unused", database=database, namespace=namespace
    )
    request = new_request("p", SKILL_ID, 1, "reviewer", QualityReport())

    audit.emit(new_event("turn", "s", "t"))
    audit.events()
    audit.exists()
    audit.delete_sessions(["s"], keep_kinds=[RETENTION_KIND])
    audit.purge()
    proposals.save(proposal("s"))
    proposals.list()
    proposals.list(ProposalState.draft)
    proposals.purge()
    changes.save(request, skill_markdown=MARKDOWN, policy=POLICY)
    changes.list()
    changes.list(ChangeRequestState.open)
    changes.drop_package("r")
    changes.purge()
    for refused in (
        lambda: proposals.get("p"),
        lambda: proposals.transition(proposal("s"), ProposalState.draft),
        lambda: proposals.delete("p"),
        lambda: changes.get("r"),
        lambda: changes.files("r"),
        lambda: changes.add_file("r", "x.json", "{}"),
        lambda: changes.apply("r", tmp_path, by="operator"),
        lambda: changes.reject("r", by="operator"),
        lambda: changes.delete("r"),
    ):
        with pytest.raises(KeyError):
            refused()

    assert len(database.statements) >= 28
    for statement, parameters in database.statements:
        assert "namespace" in statement, statement
        assert namespace in parameters, statement


class _Rows:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return self._rows


class _OneOpenRequest:
    """Stands in for a database holding one request; its next `failures` decisions fail."""

    identity = "offline"

    def __init__(self, request: ChangeRequest, files: dict[str, str], failures: int) -> None:
        self.request = request
        self.files = files
        self.failures = failures

    @contextmanager
    def connect(self) -> Iterator[_OneOpenRequest]:
        yield self

    def execute(self, statement: str, parameters: Any = ()) -> _Rows:
        if statement.startswith("SELECT request"):
            return _Rows([{"request": self.request.model_dump(mode="json")}])
        if statement.startswith("SELECT name, content"):
            return _Rows([{"name": name, "content": text} for name, text in self.files.items()])
        if statement.startswith("INSERT INTO oska_change_requests"):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("the record could not be written")
            self.request = ChangeRequest.model_validate(parameters[-1])
        return _Rows([])


def _fail_the_applied_record_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from oska.storage.postgres_changes import PostgresChangeRequestStore

    write = PostgresChangeRequestStore._write
    left = [1]

    def failing(store: Any, connection: Any, request: ChangeRequest) -> None:
        if request.state is ChangeRequestState.applied and left:
            left.pop()
            raise RuntimeError("the record could not be written")
        write(store, connection, request)

    monkeypatch.setattr(PostgresChangeRequestStore, "_write", failing)


def _assert_nothing_left_and_applied_on_retry(
    store: ChangeRequestRecordStore, request_id: str, catalog: Path
) -> None:
    with pytest.raises(RuntimeError, match="could not be written"):
        store.apply(request_id, catalog, by="operator")

    assert not (catalog / "runbook" / "sample_change").exists()
    assert (catalog / "runbook" / "kept.md").is_file()
    assert store.get(request_id).state is ChangeRequestState.open

    written = store.apply(request_id, catalog, by="operator")
    package = catalog / "runbook" / "sample_change" / "v1"
    assert sorted(written) == [package / "policy.json", package / "skill.md"]
    assert store.get(request_id).state is ChangeRequestState.applied


def test_an_apply_whose_record_fails_leaves_no_file_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import oska.storage.postgres_changes as postgres_changes

    monkeypatch.setattr(postgres_changes, "jsonb", lambda value: value)
    request = new_request("p", SKILL_ID, 1, "reviewer", QualityReport())
    files = {"skill.md": MARKDOWN, "policy.json": json.dumps(POLICY)}
    database: Any = _OneOpenRequest(request, files, failures=1)
    store = postgres_changes.PostgresChangeRequestStore("unused", database=database)
    catalog = tmp_path / "catalog"
    (catalog / "runbook").mkdir(parents=True)
    (catalog / "runbook" / "kept.md").write_text("there before\n", encoding="utf-8")

    _assert_nothing_left_and_applied_on_retry(store, request.request_id, catalog)


def test_an_apply_rolled_back_by_postgres_leaves_no_file_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    held = _postgres(_postgres_url(), f"contract/{uuid.uuid4().hex}")
    try:
        request_id = composed(held.changes(), "p-1")
        catalog = tmp_path / "catalog"
        (catalog / "runbook").mkdir(parents=True)
        (catalog / "runbook" / "kept.md").write_text("there before\n", encoding="utf-8")
        _fail_the_applied_record_once(monkeypatch)

        _assert_nothing_left_and_applied_on_retry(held.changes(), request_id, catalog)
    finally:
        _purge(held)


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        (
            "postgresql://u:s3cret@db:5432/x?sslmode=require&password=s3cret",
            "postgresql://u@db:5432/x?sslmode=require",
        ),
        ("host=db user=u password=s3cret dbname=x", "host=db user=u dbname=x"),
        ("host=db password='s3 cret' dbname=x", "host=db dbname=x"),
    ],
)
def test_a_database_name_carries_no_password(url: str, kept: str) -> None:
    """The name a store reports is derived from the address with its password removed."""
    from types import SimpleNamespace

    from oska.storage.postgres import Database, without_password

    def named(address: str) -> str:
        # The name needs no driver, so no database is opened to compute it.
        return Database.identity.fget(SimpleNamespace(_url=address))  # type: ignore[attr-defined]

    assert without_password(url) == kept
    assert named(url) == named(kept)
