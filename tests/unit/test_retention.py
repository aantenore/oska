"""Removing the record of a conversation from every place it is kept.

The account of a session is a row in the ledger, a run of lines in the audit
trail and a document in the session store. It is also the judgments a person
left on its answers, the preferences learned from those judgments and, where
tracing is on, the spans naming it. These tests hold all of them together: a
removal that reaches some of the stores has not removed anything, and a removal
that reaches a session nobody asked about has removed too much.

The last three belong to layers above retention, so they are handed in by the
caller. A deployment that keeps none of them hands in none, and the counts for
them are zero.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opsdeck.audit.events import new_event
from opsdeck.audit.sink import JsonlSink, rewrite_jsonl
from opsdeck.audit.trace import JsonlTraceSink
from opsdeck.config import Settings, load_runtime_config, state_path
from opsdeck.contracts.events import AuditEvent
from opsdeck.engine.factory import build_engine
from opsdeck.engine.session_store import SessionStore
from opsdeck.engine.state import SessionState
from opsdeck.governance.ledger import WriteLedger
from opsdeck.governance.retention import expire, forget
from opsdeck.improvement.feedback import FeedbackStore, feedback_root
from opsdeck.improvement.preferences import (
    Preference,
    PreferenceStore,
    preferences_path,
    preferences_root,
    write_preferences,
)

GONE = "session-to-forget"
KEPT = "session-to-keep"
SECRET = "the sentence somebody typed"
LEAKY = "session-that-typed-a-credential"
CREDENTIAL = "sk-live-4f2b9c8d7e6a5b4c3d2e1f0a"
REDACTED = "[redacted]"


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """An audit trail holding two turns of each of two sessions."""
    path = tmp_path / "audit.jsonl"
    sink = JsonlSink(path)
    for session_id in (GONE, KEPT):
        sink.emit(new_event("turn", session_id, "t1", {"turn": 1, "text": SECRET}))
        sink.emit(new_event("reply", session_id, "t1", {"kind": "outcomes"}))
    return path


@pytest.fixture
def ledger(tmp_path: Path) -> WriteLedger:
    """A ledger holding two receipts for one session and one for the other."""
    held = WriteLedger(tmp_path / "ledger.sqlite")
    for session_id, count in ((GONE, 2), (KEPT, 1)):
        for index in range(count):
            held.record(
                held.new_receipt(
                    session_id,
                    f"key-{session_id}-{index}",
                    "integration.access_control",
                    "membership.grant",
                    {"username": "tnovak", "group": f"group-{index}"},
                )
            )
    return held


@pytest.fixture
def sessions(tmp_path: Path) -> SessionStore:
    """A session store holding the state of both sessions."""
    store = SessionStore(tmp_path / "sessions.sqlite")
    for session_id in (GONE, KEPT):
        store.save(SessionState(session_id=session_id))
    return store


@pytest.fixture
def feedback(tmp_path: Path) -> FeedbackStore:
    """Judgments left on two turns of one session and one turn of the other."""
    store = FeedbackStore(feedback_root(tmp_path))
    store.record(GONE, 1, "down", "keep it shorter")
    store.record(GONE, 2, "up")
    store.record(KEPT, 1, "up")
    return store


@pytest.fixture
def preferences(tmp_path: Path) -> PreferenceStore:
    """A preference file for each of the two sessions."""
    root = preferences_root(tmp_path)
    for session_id in (GONE, KEPT):
        held = [Preference(text="Keep it short.", session_id=session_id)]
        write_preferences(root, session_id, held)
    return PreferenceStore(root)


@pytest.fixture
def traces(tmp_path: Path) -> JsonlTraceSink:
    """A trace file holding one turn of each session, each with a nested span."""
    sink = JsonlTraceSink(tmp_path / "traces.jsonl")
    for session_id in (GONE, KEPT):
        with sink.span("turn", session=session_id, turn=1):
            with sink.span("route"):
                pass
    return sink


def _events(path: Path) -> list[AuditEvent]:
    return [
        AuditEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_forget_removes_one_session_from_all_three_stores(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    report = forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.sessions == [GONE]
    assert (report.receipts, report.events, report.session_states) == (2, 2, 1)
    assert ledger.for_session(GONE) == []
    assert len(ledger.for_session(KEPT)) == 1
    assert sessions.sessions() == [KEPT]
    kinds = [(event.session_id, event.kind) for event in _events(audit_path)]
    assert (KEPT, "turn") in kinds
    assert (GONE, "turn") not in kinds


def test_forget_leaves_no_trace_of_what_was_said(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    trail = audit_path.read_text(encoding="utf-8")
    assert trail.count(SECRET) == 1  # the one the kept session wrote
    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert len(recorded) == 1
    assert recorded[0].session_id == GONE
    assert recorded[0].data == {
        "operation": "forget",
        "receipts": 2,
        "events": 2,
        "session_states": 1,
        "feedback": 0,
        "preferences": 0,
        "trace_spans": 0,
    }


def test_the_record_of_a_removal_survives_the_next_removal(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    again = forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert (again.receipts, again.events, again.session_states) == (0, 0, 0)
    kinds = [event.kind for event in _events(audit_path) if event.session_id == GONE]
    assert kinds == ["retention", "retention"]


def test_forgetting_a_session_nobody_recorded_removes_nothing(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    before = audit_path.read_text(encoding="utf-8")
    report = forget("never-happened", ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.empty
    assert len(ledger.for_session(GONE)) == 2
    assert before in audit_path.read_text(encoding="utf-8")


def test_expire_removes_what_is_past_the_window_and_nothing_else(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=40))
    report = expire(30, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.sessions == [GONE]
    assert report.receipts == 2
    assert sessions.sessions() == [KEPT]
    assert len(ledger.for_session(KEPT)) == 1
    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert [event.data["operation"] for event in recorded] == ["expire"]


def test_a_window_of_zero_days_keeps_everything(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=400))
    report = expire(0, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.empty and report.sessions == []
    assert sorted(sessions.sessions()) == sorted([GONE, KEPT])
    assert [event.kind for event in _events(audit_path) if event.kind == "retention"] == []
    # Deletion on request is not the window, and still works.
    assert forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions).receipts == 2


def test_a_trail_is_replaced_whole_and_keeps_what_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text('not json\n{"session_id": "a"}\n{"session_id": "b"}\n', encoding="utf-8")
    removed = rewrite_jsonl(path, lambda entry: entry.get("session_id") != "a")
    assert removed == 1
    assert path.read_text(encoding="utf-8") == 'not json\n{"session_id": "b"}\n'
    assert [item.name for item in tmp_path.iterdir()] == ["audit.jsonl"]


def test_a_trail_that_was_never_written_is_not_an_error(
    ledger: WriteLedger,
    sessions: SessionStore,
    tmp_path: Path,
) -> None:
    path = tmp_path / "nothing" / "audit.jsonl"
    report = forget(GONE, ledger=ledger, audit_path=path, sessions=sessions)
    assert report.events == 0
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "retention"


# --- the stores a caller hands in --------------------------------------------


def test_forget_reaches_the_judgments_the_preferences_and_the_traces(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
    feedback: FeedbackStore,
    preferences: PreferenceStore,
    traces: JsonlTraceSink,
    tmp_path: Path,
) -> None:
    """A session leaves six trails, and a request to be forgotten answers all six."""
    report = forget(
        GONE,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
    )

    assert (report.receipts, report.events, report.session_states) == (2, 2, 1)
    assert (report.feedback, report.preferences, report.trace_spans) == (2, 1, 2)
    assert feedback.for_session(GONE) == []
    assert len(feedback.for_session(KEPT)) == 1
    assert not preferences_path(preferences_root(tmp_path), GONE).is_file()
    assert preferences_path(preferences_root(tmp_path), KEPT).is_file()
    held = _spans(traces.path)
    assert [span["name"] for span in held] == ["route", "turn"]
    assert held[1]["fields"]["session"] == KEPT


def test_the_event_names_the_counts_of_every_store_and_no_content(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
    feedback: FeedbackStore,
    preferences: PreferenceStore,
    traces: JsonlTraceSink,
) -> None:
    """What was removed is counted; what it said is not in the event or anywhere else."""
    forget(
        GONE,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
    )

    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert len(recorded) == 1
    assert recorded[0].data == {
        "operation": "forget",
        "receipts": 2,
        "events": 2,
        "session_states": 1,
        "feedback": 2,
        "preferences": 1,
        "trace_spans": 2,
    }
    assert "keep it shorter" not in audit_path.read_text(encoding="utf-8")
    assert "Keep it short." not in audit_path.read_text(encoding="utf-8")


def test_a_deployment_that_keeps_none_of_them_hands_in_none(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    """The three stores are optional, and what is not kept is counted zero."""
    report = forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)

    assert (report.feedback, report.preferences, report.trace_spans) == (0, 0, 0)
    assert report.receipts == 2


def test_the_window_reaches_the_same_stores_as_a_request(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
    feedback: FeedbackStore,
    preferences: PreferenceStore,
    traces: JsonlTraceSink,
) -> None:
    """Expiry is the same removal on a schedule, so it removes the same things."""
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=40))

    report = expire(
        30,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
    )

    assert report.sessions == [GONE]
    assert (report.feedback, report.preferences, report.trace_spans) == (2, 1, 2)
    assert feedback.for_session(GONE) == []
    assert len(feedback.for_session(KEPT)) == 1


def test_a_trace_of_a_session_goes_with_the_spans_opened_inside_it(
    tmp_path: Path,
) -> None:
    """A nested span names no session and still belongs to one, by descent."""
    sink = JsonlTraceSink(tmp_path / "traces.jsonl")
    with sink.span("turn", session=GONE, turn=1):
        with sink.span("plan"):
            with sink.span("execute"):
                pass
    with sink.span("turn", session=KEPT, turn=1):
        pass

    removed = sink.delete(GONE)

    assert removed == 3
    assert [span["name"] for span in _spans(sink.path)] == ["turn"]
    assert sink.delete("never-traced") == 0
    assert sink.delete(GONE) == 0


def _spans(path: Path) -> list[dict]:
    """The spans a trace file holds, in the order they were recorded."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- what is never written ---------------------------------------------------


def test_what_a_turn_must_not_keep_is_never_written_to_the_record(
    example_settings: Settings,
) -> None:
    """The other half of retention: the record never holds it, so nothing removes it.

    The input guardrail chain runs before the turn is recorded. What it redacts
    is what reaches the audit trail, the session history and the reply; the
    wording it refused reaches none of the three, and there is no later pass
    that takes it out again.
    """
    engine = build_engine(example_settings)
    runtime = load_runtime_config(settings=example_settings)
    audit_path = state_path(example_settings.state_dir, runtime.audit.path)
    sessions_path = state_path(example_settings.state_dir, runtime.sessions.path)

    result = engine.turn(LEAKY, f"restart the payments service, the api key is {CREDENTIAL}")

    assert [outcome.kind for outcome in result.outcomes] == ["denied"]
    assert CREDENTIAL not in result.reply

    trail = audit_path.read_text(encoding="utf-8")
    assert CREDENTIAL not in trail
    recorded = [event for event in _events(audit_path) if event.kind == "turn"]
    assert len(recorded) == 1
    assert REDACTED in str(recorded[0].data["text"])
    assert [event.kind for event in _events(audit_path) if event.kind == "retention"] == []

    held = engine.state(LEAKY)
    assert [message.content for message in held.history if CREDENTIAL in message.content] == []
    assert REDACTED in held.history[0].content
    assert CREDENTIAL.encode("utf-8") not in sessions_path.read_bytes()


def _backdate(store: SessionStore, session_id: str, moment: datetime) -> None:
    """Move when a session was last written, the way the passage of time would."""
    connection = sqlite3.connect(store.path)
    with connection:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (moment.isoformat(), session_id),
        )
    connection.close()
