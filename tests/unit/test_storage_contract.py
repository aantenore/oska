"""One behavioral contract for the stores that keep the record, run against every backend.

SQLite runs always. PostgreSQL runs when `OSKA_TEST_POSTGRES_URL` names a
database the tests may write to, and is skipped with the reason otherwise. The
tests share that database with whatever else it holds, so each one works under
session identifiers of its own and asserts only about them.

Two stores under different namespaces must never see each other's sessions or
receipts. On SQLite a namespace is a directory of its own; on PostgreSQL it is
the namespace the store is opened under, over the same database.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oska.config import RuntimeConfig, Settings, StorageConfig
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.engine.session_store import SessionStore
from oska.engine.state import SessionState
from oska.governance.ledger import WriteLedger
from oska.governance.retention import expire, forget
from oska.storage import (
    DEFAULT_NAMESPACE,
    LedgerStore,
    SessionStateStore,
    StorageError,
    database_url,
    open_ledger,
    open_sessions,
    storage_namespace,
)
from oska.storage.postgres import MIGRATIONS

POSTGRES_URL_ENV = "OSKA_TEST_POSTGRES_URL"
INTEGRATION = "integration.sample"
OPERATION = "thing.change"

Stores = tuple[Callable[[], LedgerStore], Callable[[], SessionStateStore]]
Spaces = tuple[Callable[[str], LedgerStore], Callable[[str], SessionStateStore]]


def _postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not set, so the postgres backend is not exercised")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg is not installed; install the optional extra oska[postgres]")
    return url


@pytest.fixture(params=["sqlite", "postgres"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Stores]:
    """Factories for a ledger and a session store of one backend, sharing one address.

    Each call of a factory opens a new store over the same record, the way a
    second process would.
    """
    if request.param == "sqlite":
        yield (
            lambda: WriteLedger(tmp_path / "ledger.sqlite"),
            lambda: SessionStore(tmp_path / "sessions.sqlite"),
        )
        return
    url = _postgres_url()
    from oska.storage.postgres_ledger import PostgresLedger
    from oska.storage.postgres_sessions import PostgresSessionStore

    yield (lambda: PostgresLedger(url), lambda: PostgresSessionStore(url))


@pytest.fixture(params=["sqlite", "postgres"])
def spaces(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Spaces]:
    """Factories for a ledger and a session store of one backend, under a given namespace."""
    if request.param == "sqlite":
        yield (
            lambda namespace: WriteLedger(tmp_path / namespace / "ledger.sqlite"),
            lambda namespace: SessionStore(tmp_path / namespace / "sessions.sqlite"),
        )
        return
    url = _postgres_url()
    from oska.storage.postgres_ledger import PostgresLedger
    from oska.storage.postgres_sessions import PostgresSessionStore

    yield (
        lambda namespace: PostgresLedger(url, namespace=namespace),
        lambda namespace: PostgresSessionStore(url, namespace=namespace),
    )


@pytest.fixture
def ledger(stores: Stores) -> LedgerStore:
    return stores[0]()


@pytest.fixture
def sessions(stores: Stores) -> SessionStateStore:
    return stores[1]()


@pytest.fixture
def session() -> str:
    """A session identifier no other test uses."""
    return f"contract-{uuid.uuid4().hex}"


def intent(ledger: LedgerStore, session: str, value: str = "one") -> WriteReceipt:
    """An unsent receipt for one intent of this session."""
    payload = {"thing": value, "note": "irrelevant"}
    key = ledger.key(INTEGRATION, OPERATION, payload, ["thing"])
    return ledger.new_receipt(session, key, INTEGRATION, OPERATION, payload)


# The interface.


def test_both_stores_satisfy_the_interfaces(
    ledger: LedgerStore, sessions: SessionStateStore
) -> None:
    assert isinstance(ledger, LedgerStore)
    assert isinstance(sessions, SessionStateStore)


# Receipts.


def test_a_receipt_round_trips(ledger: LedgerStore, session: str) -> None:
    held = ledger.record(intent(ledger, session))
    ledger.update(
        held.receipt_id,
        status="executed",
        response={"thing": {"state": "changed", "count": 2}},
        verification=VerificationStatus.verified,
        evidence={"ok": True},
    )

    read = ledger.get(held.receipt_id)

    assert read is not None
    assert read.payload == {"thing": "one", "note": "irrelevant"}
    assert read.response == {"thing": {"state": "changed", "count": 2}}
    assert (read.status, read.verification) == ("executed", VerificationStatus.verified)
    assert read.verification_evidence == {"ok": True}
    assert read.created_at == held.created_at
    assert ledger.lookup(session, held.ledger_key) == read


def test_an_update_leaves_what_it_does_not_name(ledger: LedgerStore, session: str) -> None:
    held = ledger.record(intent(ledger, session))
    ledger.update(held.receipt_id, evidence={"reason": "first"})

    ledger.update(held.receipt_id, status="error", classification="terminal")

    read = ledger.get(held.receipt_id)
    assert read is not None
    assert read.verification_evidence == {"reason": "first"}
    assert (read.status, read.classification) == ("error", "terminal")


def test_the_key_counts_only_the_declared_fields(ledger: LedgerStore) -> None:
    first = ledger.key(INTEGRATION, OPERATION, {"thing": "a", "note": "x"}, ["thing"])
    second = ledger.key(INTEGRATION, OPERATION, {"thing": "a", "note": "y"}, ["thing"])
    assert first == second
    assert first == WriteLedger(Path("unused")).key(
        INTEGRATION, OPERATION, {"thing": "a"}, ["thing"]
    )


# Settlement.


@pytest.mark.parametrize(
    ("status", "classification", "closed"),
    [
        ("pending", None, True),
        ("executed", None, True),
        ("error", "terminal", True),
        ("error", "ambiguous", True),
        ("error", "retryable", False),
    ],
)
def test_what_settles_a_key(
    ledger: LedgerStore,
    session: str,
    status: str,
    classification: str | None,
    closed: bool,
) -> None:
    held = ledger.record(intent(ledger, session))
    if status != "pending":
        ledger.update(held.receipt_id, status=status, classification=classification)

    settled = ledger.settled(session, held.ledger_key)

    assert (settled is not None) is closed
    assert ledger.lookup(session, held.ledger_key) is not None


def test_a_second_attempt_settles_the_key_the_first_left_open(
    ledger: LedgerStore, session: str
) -> None:
    first = ledger.record(intent(ledger, session))
    ledger.update(first.receipt_id, status="error", classification="retryable")
    second = ledger.record(intent(ledger, session))
    ledger.update(second.receipt_id, status="executed", response={})

    settled = ledger.settled(session, first.ledger_key)

    assert settled is not None and settled.receipt_id == second.receipt_id
    assert [r.receipt_id for r in ledger.for_session(session)] == [
        first.receipt_id,
        second.receipt_id,
    ]


# The claim.


def test_a_claim_takes_an_open_key_and_hands_back_a_settled_one(
    ledger: LedgerStore, session: str
) -> None:
    first = intent(ledger, session)
    assert ledger.claim(first).receipt_id == first.receipt_id

    second = intent(ledger, session)
    assert ledger.claim(second).receipt_id == first.receipt_id
    assert len(ledger.for_session(session)) == 1

    ledger.update(first.receipt_id, status="error", classification="retryable")
    third = intent(ledger, session)
    assert ledger.claim(third).receipt_id == third.receipt_id
    assert len(ledger.for_session(session)) == 2


@pytest.mark.parametrize("count", [2, 4])
def test_a_claim_under_concurrent_writers_is_won_once(
    stores: Stores, session: str, count: int
) -> None:
    """Each writer is a store of its own over the same record, the way a process is."""
    writers = [stores[0]() for _ in range(count)]
    start = threading.Barrier(len(writers))
    won: list[str] = []
    held: list[str] = []
    failures: list[BaseException] = []

    def claim(store: LedgerStore) -> None:
        mine = intent(store, session)
        try:
            start.wait()
            holder = store.claim(mine)
        except BaseException as error:  # reported below, not swallowed
            failures.append(error)
            return
        held.append(holder.receipt_id)
        if holder.receipt_id == mine.receipt_id:
            won.append(mine.receipt_id)

    threads = [threading.Thread(target=claim, args=(store,)) for store in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(won) == 1
    assert held == [won[0]] * count
    assert [r.receipt_id for r in writers[0].for_session(session)] == won


# Counting, listing and removal.


def test_count_for_session_sessions_and_delete(ledger: LedgerStore, session: str) -> None:
    other = f"{session}-other"
    for value in ("one", "two"):
        ledger.record(intent(ledger, session, value))
    ledger.record(intent(ledger, other))

    assert ledger.count(session, OPERATION) == 2
    assert ledger.count(session, "thing.other") == 0
    held = ledger.sessions()
    assert held.index(session) < held.index(other)

    assert ledger.delete_session(session) == 2
    assert ledger.for_session(session) == []
    assert session not in ledger.sessions()
    assert len(ledger.for_session(other)) == 1
    fresh = intent(ledger, session)
    assert ledger.claim(fresh).receipt_id == fresh.receipt_id


# Session state.


def test_a_session_round_trips(sessions: SessionStateStore, session: str) -> None:
    assert sessions.load(session) == SessionState(session_id=session)
    assert sessions.updated_at(session) is None
    state = SessionState(session_id=session, language="it", turn_count=3)

    sessions.save(state)

    assert sessions.load(session) == state
    written = sessions.updated_at(session)
    assert written is not None and written.tzinfo is not None
    assert session in sessions.sessions()


def test_expired_and_delete(sessions: SessionStateStore, session: str) -> None:
    sessions.save(SessionState(session_id=session))
    now = datetime.now(UTC)

    assert session in sessions.expired(now + timedelta(minutes=1))
    assert session not in sessions.expired(now - timedelta(days=1))
    assert sessions.delete(session) == 1
    assert sessions.delete(session) == 0
    assert session not in sessions.sessions()


def test_exists_says_whether_a_session_was_written(
    sessions: SessionStateStore, session: str
) -> None:
    assert sessions.exists(session) is False
    sessions.save(SessionState(session_id=session))
    assert sessions.exists(session) is True
    sessions.delete(session)
    assert sessions.exists(session) is False


# Namespaces.


def test_two_namespaces_never_see_each_others_sessions(spaces: Spaces, session: str) -> None:
    run = uuid.uuid4().hex[:8]
    mine = spaces[1](storage_namespace(f"tenant-{run}", "operations"))
    theirs = spaces[1](storage_namespace(f"tenant-{run}", "access"))
    state = SessionState(session_id=session, language="it", turn_count=2)

    mine.save(state)

    assert theirs.exists(session) is False
    assert theirs.load(session) == SessionState(session_id=session)
    assert theirs.updated_at(session) is None
    assert session not in theirs.sessions()
    assert session not in theirs.expired(datetime.now(UTC) + timedelta(minutes=1))
    assert theirs.delete(session) == 0
    assert mine.load(session) == state

    theirs.save(SessionState(session_id=session, language="en"))
    assert mine.load(session).language == "it"
    assert theirs.load(session).language == "en"


def test_two_namespaces_never_see_each_others_receipts(spaces: Spaces, session: str) -> None:
    run = uuid.uuid4().hex[:8]
    mine = spaces[0](storage_namespace(f"tenant-{run}", "operations"))
    theirs = spaces[0](storage_namespace(f"other-{run}", "operations"))
    held = mine.claim(intent(mine, session))

    assert theirs.get(held.receipt_id) is None
    assert theirs.lookup(session, held.ledger_key) is None
    assert theirs.settled(session, held.ledger_key) is None
    assert theirs.for_session(session) == []
    assert theirs.count(session, OPERATION) == 0
    assert session not in theirs.sessions()
    assert theirs.update(held.receipt_id, status="executed") is None
    assert theirs.delete_session(session) == 0

    own = intent(theirs, session)
    assert theirs.claim(own).receipt_id == own.receipt_id
    assert [r.receipt_id for r in mine.for_session(session)] == [held.receipt_id]
    assert [r.receipt_id for r in theirs.for_session(session)] == [own.receipt_id]
    read = mine.get(held.receipt_id)
    assert read is not None and read.status == "pending"


# Retention, through the interfaces.


def test_forget_reaches_any_backend(
    ledger: LedgerStore, sessions: SessionStateStore, session: str, tmp_path: Path
) -> None:
    ledger.record(intent(ledger, session))
    sessions.save(SessionState(session_id=session))

    report = forget(
        session, ledger=ledger, audit_path=tmp_path / "audit.jsonl", sessions=sessions
    )

    assert (report.receipts, report.session_states) == (1, 1)
    assert ledger.for_session(session) == []
    assert sessions.updated_at(session) is None


def test_expire_reaches_any_backend(
    ledger: LedgerStore, sessions: SessionStateStore, session: str, tmp_path: Path
) -> None:
    ledger.record(intent(ledger, session))
    sessions.save(SessionState(session_id=session))

    report = expire(
        1,
        ledger=ledger,
        audit_path=tmp_path / "audit.jsonl",
        sessions=sessions,
        now=datetime.now(UTC) + timedelta(days=2),
    )

    assert session in report.sessions
    assert ledger.for_session(session) == []
    assert sessions.updated_at(session) is None


# Opening a backend from configuration.


def test_sqlite_is_opened_under_the_state_directory(tmp_path: Path) -> None:
    runtime = RuntimeConfig()
    settings = Settings(state_dir=tmp_path)

    ledger = open_ledger(runtime, settings)
    sessions = open_sessions(runtime, settings)

    assert isinstance(ledger, WriteLedger) and ledger.path.parent == tmp_path
    assert isinstance(sessions, SessionStore) and sessions.path.parent == tmp_path


def test_postgres_without_an_address_names_the_variable_to_set(tmp_path: Path) -> None:
    runtime = RuntimeConfig(storage=StorageConfig(backend="postgres", url_env="SAMPLE_DB_URL"))

    with pytest.raises(StorageError, match="set SAMPLE_DB_URL"):
        open_ledger(runtime, Settings(state_dir=tmp_path), env={})
    with pytest.raises(StorageError, match="SAMPLE_DB_URL"):
        open_sessions(runtime, Settings(state_dir=tmp_path), env={"SAMPLE_DB_URL": " "})


def test_the_address_is_read_from_the_named_variable() -> None:
    storage = StorageConfig(backend="postgres", url_env="SAMPLE_DB_URL")
    assert database_url(storage, {"SAMPLE_DB_URL": "postgresql://h/db"}) == "postgresql://h/db"


def test_postgres_without_the_driver_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real = builtins.__import__

    def without_driver(name: str, *args: object, **kwargs: object) -> object:
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_driver)
    runtime = RuntimeConfig(storage=StorageConfig(backend="postgres", url_env="SAMPLE_DB_URL"))

    with pytest.raises(StorageError, match=r"oska\[postgres\]"):
        open_ledger(runtime, Settings(state_dir=tmp_path), env={"SAMPLE_DB_URL": "postgresql://h"})


# The PostgreSQL stores name the namespace in every statement, checked offline.


class _Recorded:
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []


class _Recorder:
    """Stands in for the database: records every statement and its parameters."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    @contextmanager
    def connect(self) -> Iterator[_Recorder]:
        yield self

    def execute(self, statement: str, parameters: Any = ()) -> _Recorded:
        self.statements.append((" ".join(statement.split()), tuple(parameters)))
        return _Recorded()


def test_every_postgres_statement_is_scoped_to_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oska.storage.postgres_ledger as postgres_ledger
    import oska.storage.postgres_sessions as postgres_sessions

    monkeypatch.setattr(postgres_ledger, "jsonb", lambda value: value)
    monkeypatch.setattr(postgres_sessions, "jsonb", lambda value: value)
    database = _Recorder()
    namespace = storage_namespace("acme", "operations")
    ledger = postgres_ledger.PostgresLedger(
        "unused", database=database, namespace=namespace  # type: ignore[arg-type]
    )
    sessions = postgres_sessions.PostgresSessionStore(
        "unused", database=database, namespace=namespace  # type: ignore[arg-type]
    )
    receipt = intent(ledger, "s")

    ledger.record(receipt)
    ledger.claim(receipt)
    ledger.update(receipt.receipt_id, status="executed", response={}, evidence={})
    ledger.lookup("s", receipt.ledger_key)
    ledger.settled("s", receipt.ledger_key)
    ledger.count("s", OPERATION)
    ledger.for_session("s")
    ledger.delete_session("s")
    ledger.sessions()
    sessions.save(SessionState(session_id="s"))
    sessions.load("s")
    sessions.sessions()
    sessions.updated_at("s")
    sessions.exists("s")
    sessions.expired(datetime.now(UTC))
    sessions.delete("s")

    assert len(database.statements) >= 19
    for statement, parameters in database.statements:
        assert "namespace" in statement, statement
        assert namespace in parameters, statement


def test_the_namespace_migration_gives_existing_rows_the_default_namespace() -> None:
    added = next(migration for migration in MIGRATIONS if migration.version == 2)
    assert [migration.version for migration in MIGRATIONS] == [1, 2]
    defaults = [statement for statement in added.statements if "ADD COLUMN" in statement]
    assert len(defaults) == 3
    assert all(f"DEFAULT '{DEFAULT_NAMESPACE}'" in statement for statement in defaults)
    keys = [statement for statement in added.statements if "ADD PRIMARY KEY" in statement]
    assert len(keys) == 3 and all("(namespace," in statement for statement in keys)
