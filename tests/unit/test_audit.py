"""Audit events and the sinks they are written to."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from opsdeck.audit.events import new_event
from opsdeck.audit.sink import AuditSink, JsonlSink, MemorySink, MultiSink


def test_new_event_stamps_the_moment_it_was_raised() -> None:
    before = datetime.now(UTC)
    event = new_event("policy", "session-1", "turn-1", {"effect": "deny"})
    after = datetime.now(UTC)

    assert event.kind == "policy"
    assert event.session_id == "session-1"
    assert event.turn_id == "turn-1"
    assert event.data == {"effect": "deny"}
    assert before <= event.at <= after


def test_new_event_defaults_to_empty_data() -> None:
    event = new_event("turn", "session-1", "turn-1")
    assert event.data == {}


def test_new_event_copies_the_data_it_was_given() -> None:
    data = {"operation": "service.restart"}
    event = new_event("execute", "session-1", "turn-1", data)
    data["operation"] = "service.scale"
    assert event.data == {"operation": "service.restart"}


def test_memory_sink_keeps_what_it_is_given() -> None:
    sink = MemorySink()
    assert sink.events == []

    sink.emit(new_event("turn", "s", "t"))
    sink.emit(new_event("reply", "s", "t"))
    assert [event.kind for event in sink.events] == ["turn", "reply"]

    sink.clear()
    assert sink.events == []


def test_jsonl_sink_appends_one_line_per_event(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "audit.jsonl"
    sink = JsonlSink(path)
    sink.emit(new_event("turn", "s", "t1", {"text": "hello"}))
    sink.emit(new_event("reply", "s", "t1", {"length": 12}))

    assert sink.path == path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "turn"
    assert first["session_id"] == "s"
    assert first["data"] == {"text": "hello"}
    assert json.loads(lines[1])["kind"] == "reply"


def test_jsonl_sink_appends_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    JsonlSink(path).emit(new_event("turn", "s", "t1"))
    JsonlSink(path).emit(new_event("turn", "s", "t2"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_multi_sink_hands_the_event_to_every_sink(tmp_path: Path) -> None:
    memory = MemorySink()
    other = MemorySink()
    path = tmp_path / "audit.jsonl"
    sink = MultiSink(memory, other, JsonlSink(path))

    sink.emit(new_event("verify", "s", "t", {"status": "verified"}))

    assert len(memory.events) == 1
    assert len(other.events) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "verify"


def test_multi_sink_with_no_sinks_is_harmless() -> None:
    MultiSink().emit(new_event("turn", "s", "t"))


def test_the_sinks_satisfy_the_protocol(tmp_path: Path) -> None:
    assert isinstance(MemorySink(), AuditSink)
    assert isinstance(JsonlSink(tmp_path / "audit.jsonl"), AuditSink)
    assert isinstance(MultiSink(), AuditSink)
    assert not isinstance(object(), AuditSink)
