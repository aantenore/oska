"""How long the record of a conversation is kept, and how it is removed.

A conversation is personal data. The account this system keeps of one is spread
over several stores — the receipts of what it wrote, the audit trail of how it
decided, the state the session carried between turns, the judgments a person
left on its answers, the preferences learned from those judgments, and, where
tracing is on, the spans naming it — and a window or a request to be forgotten
has to reach every one of them or it has reached none. That is what this module
does.

What it cannot do is know which of them a deployment keeps. The receipts, the
trail and the session state are the account itself and are always asked for.
The other three belong to layers above this one, so they are handed in by the
caller as stores that answer to a session, the way the session store already
is; a store nobody hands in is one this deployment does not keep. Nothing else
in the system removes a record of a conversation, though a person may separately
undo one learned thing — reverting a preference is not retention and reports
nothing here.

Removal leaves a line behind. The retention event names the session and the
counts, and nothing else: not what was said, not what was read, not what was
written. It survives the removal it records, because a deletion nobody can see
having happened is indistinguishable from a file somebody edited.

The window is not the whole answer and is not meant to be. What must never be
stored is kept out of the record in the first place; what is stored because the
account needs it is kept for as long as the window says and no longer. A window
of zero days keeps everything, and a request to be forgotten is answered
whatever the window is.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from oska.audit.events import new_event
from oska.audit.sink import JsonlSink, rewrite_jsonl
from oska.governance.ledger import WriteLedger

__all__ = ["RetentionReport", "SessionFiles", "SessionRecords", "expire", "forget"]

RETENTION_KIND = "retention"


@runtime_checkable
class SessionRecords(Protocol):
    """Whatever holds the state of sessions between turns, as far as removal cares.

    The store itself belongs to the layer that runs turns, which is above this
    one. What removal needs of it is these two answers, so it asks for them and
    not for the store.
    """

    def expired(self, before: datetime) -> list[str]:
        """The sessions last written before this moment."""
        ...

    def delete(self, session_id: str) -> int:
        """Remove the stored state of one session and report whether there was any."""
        ...


@runtime_checkable
class SessionFiles(Protocol):
    """Whatever else a deployment keeps under a session identifier.

    The judgments of a session, the preferences learned from them and the
    spans of its traces are each kept by a layer above this one. Removal needs
    one answer from each: take this session out, and say how much went.
    """

    def delete(self, session_id: str) -> int:
        """Remove what is held for one session and report how much went."""
        ...


class RetentionReport(BaseModel):
    """What a removal took out, counted per store.

    A store this deployment does not keep is counted zero, which is the same
    thing it is: nothing of that kind was there to remove.
    """

    operation: Literal["forget", "expire"]
    sessions: list[str] = Field(default_factory=list)
    receipts: int = 0
    events: int = 0
    session_states: int = 0
    feedback: int = 0
    preferences: int = 0
    trace_spans: int = 0

    @property
    def empty(self) -> bool:
        """Whether nothing at all was removed."""
        return not (
            self.receipts
            or self.events
            or self.session_states
            or self.feedback
            or self.preferences
            or self.trace_spans
        )


def forget(
    session_id: str,
    *,
    ledger: WriteLedger,
    audit_path: Path,
    sessions: SessionRecords,
    feedback: SessionFiles | None = None,
    preferences: SessionFiles | None = None,
    traces: SessionFiles | None = None,
) -> RetentionReport:
    """Remove everything recorded for one session, and report what went.

    This answers a request, so it runs whatever the configured window is and
    whether or not the session is past it. A caller that keeps judgments,
    preferences or traces hands them in; one that keeps none hands in nothing
    and the counts for those stores are zero.
    """
    return _remove(
        [session_id],
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
        operation="forget",
    )


def expire(
    older_than_days: int,
    *,
    ledger: WriteLedger,
    audit_path: Path,
    sessions: SessionRecords,
    feedback: SessionFiles | None = None,
    preferences: SessionFiles | None = None,
    traces: SessionFiles | None = None,
    now: datetime | None = None,
) -> RetentionReport:
    """Remove everything recorded for sessions past the window, and report what went.

    A window of zero days, which is the default, keeps everything: nothing is
    removed and nothing is recorded as having been. The window is measured from
    the moment a session was last written, because that is when the
    conversation it holds last happened.
    """
    if older_than_days <= 0:
        return RetentionReport(operation="expire")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    return _remove(
        sessions.expired(cutoff),
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
        operation="expire",
    )


def _remove(
    session_ids: list[str],
    *,
    ledger: WriteLedger,
    audit_path: Path,
    sessions: SessionRecords,
    feedback: SessionFiles | None,
    preferences: SessionFiles | None,
    traces: SessionFiles | None,
    operation: Literal["forget", "expire"],
) -> RetentionReport:
    """Take these sessions out of every store that holds them, and record that.

    The audit trail is rewritten once for all the sessions named rather than
    once per session, so a window that catches many conversations does not
    rewrite the file many times. The events it records are written afterwards,
    into the trail that replaced it. The stores the caller hands in are asked
    one session at a time, because each of them answers for one session.
    """
    targets = set(session_ids)
    if not targets:
        return RetentionReport(operation=operation)
    receipts = {name: ledger.delete_session(name) for name in session_ids}
    states = {name: sessions.delete(name) for name in session_ids}
    judged = _counted(feedback, session_ids)
    preferred = _counted(preferences, session_ids)
    spans = _counted(traces, session_ids)
    dropped: dict[str, int] = {}

    def keep(entry: dict) -> bool:
        held = entry.get("session_id")
        if held in targets and entry.get("kind") != RETENTION_KIND:
            dropped[held] = dropped.get(held, 0) + 1
            return False
        return True

    rewrite_jsonl(Path(audit_path), keep)
    sink = JsonlSink(Path(audit_path))
    for name in session_ids:
        sink.emit(
            new_event(
                RETENTION_KIND,
                name,
                uuid.uuid4().hex,
                {
                    "operation": operation,
                    "receipts": receipts.get(name, 0),
                    "events": dropped.get(name, 0),
                    "session_states": states.get(name, 0),
                    "feedback": judged.get(name, 0),
                    "preferences": preferred.get(name, 0),
                    "trace_spans": spans.get(name, 0),
                },
            )
        )
    return RetentionReport(
        operation=operation,
        sessions=list(session_ids),
        receipts=sum(receipts.values()),
        events=sum(dropped.values()),
        session_states=sum(states.values()),
        feedback=sum(judged.values()),
        preferences=sum(preferred.values()),
        trace_spans=sum(spans.values()),
    )


def _counted(store: SessionFiles | None, session_ids: list[str]) -> dict[str, int]:
    """Remove these sessions from one optional store, counted by session."""
    if store is None:
        return {}
    return {name: store.delete(name) for name in session_ids}
