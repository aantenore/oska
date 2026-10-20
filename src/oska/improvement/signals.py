"""What one run left behind, read back one turn at a time.

Two sources, both already written for other reasons. The audit trail says what
was asked, where the request was routed, what score discovery gave it and which
operations ran. The write ledger says whether a write was confirmed by its
read-back. Nothing new is recorded to make this package work, and nothing here
writes to either source.

Ground truth for routing is free. A turn whose write was verified, or whose
reads completed, names the procedure the request actually meant, next to the
words the person actually used. That pair is the whole signal.

What the record cannot show is whether the answer was any good, so the one
thing read from outside the run is the judgment a person recorded against a
turn. It is carried on the signal and changes nothing about it: what a turn did
is still read from the trail and the ledger alone.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from oska.contracts.events import AuditEvent
from oska.contracts.receipt import VerificationStatus
from oska.improvement.feedback import TurnFeedback, latest
from oska.storage.protocols import LedgerStore

__all__ = ["TurnSignal", "load_signals", "read_events"]


class TurnSignal(BaseModel):
    """One turn, as the record of the run describes it.

    `chosen_runbook` is the procedure the turn reached, from discovery when it
    chose one and from the route decision otherwise. `score` is what the top
    candidate scored. `outcomes` names the operations that completed. `turn` is
    the position of the turn in its session, which is how a person names it.
    `feedback` is the judgment recorded against it, when there is one.
    """

    session_id: str
    turn_id: str
    turn: int = 0
    text: str = ""
    language: str = ""
    route_source: str = ""
    chosen_runbook: str | None = None
    score: float = 0.0
    tie: bool = False
    outcomes: list[str] = Field(default_factory=list)
    verified_write: bool = False
    feedback: TurnFeedback | None = None
    at: datetime

    @property
    def grounded(self) -> bool:
        """True when the turn shows which procedure the request really meant."""
        if self.chosen_runbook is None:
            return False
        return self.verified_write or bool(self.outcomes)


def read_events(audit_path: Path) -> list[AuditEvent]:
    """Every audit event of a trail, in the order it was appended.

    A line that is not a readable event is skipped: a trail is append-only and
    a half-written last line must not stop the rest from being read.
    """
    path = Path(audit_path)
    if not path.is_file():
        return []
    events: list[AuditEvent] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(AuditEvent.model_validate_json(stripped))
            except (ValueError, json.JSONDecodeError):
                continue
    return events


def load_signals(
    audit_path: Path,
    ledger: LedgerStore | None = None,
    feedback: Sequence[TurnFeedback] | None = None,
) -> list[TurnSignal]:
    """One signal per turn of the trail, oldest first.

    With a ledger, every receipt the turn names is looked up and the turn counts
    as a verified write only when the read-back said so. Without one, the
    verification the audit trail recorded is taken as it stands.

    With judgments, the last one recorded against a turn is carried on its
    signal. A judgment naming a turn the trail does not hold is not a signal of
    anything and is left where it is.
    """
    grouped: dict[tuple[str, str], list[AuditEvent]] = {}
    for event in read_events(audit_path):
        grouped.setdefault((event.session_id, event.turn_id), []).append(event)
    signals = [
        _signal(session_id, turn_id, events, ledger)
        for (session_id, turn_id), events in grouped.items()
        if any(event.kind == "turn" for event in events)
    ]
    judgments = latest(feedback or [])
    for signal in signals:
        signal.feedback = judgments.get((signal.session_id, signal.turn))
    signals.sort(key=lambda signal: signal.at)
    return signals


def _signal(
    session_id: str,
    turn_id: str,
    events: list[AuditEvent],
    ledger: LedgerStore | None,
) -> TurnSignal:
    signal = TurnSignal(session_id=session_id, turn_id=turn_id, at=events[0].at)
    receipts: list[str] = []
    verified = False
    for event in events:
        data = event.data
        if event.kind == "turn":
            signal.text = str(data.get("text", ""))
            signal.turn = _position(data.get("turn"), signal.turn)
            signal.at = event.at
        elif event.kind == "route":
            signal.route_source = str(data.get("source", ""))
            signal.language = str(data.get("language") or signal.language)
            signal.chosen_runbook = _text_or_none(data.get("runbook_id"))
            signal.score = _number(data.get("confidence"), signal.score)
        elif event.kind == "discovery":
            signal.tie = bool(data.get("tie"))
            chosen = _text_or_none(data.get("chosen"))
            if chosen is not None:
                signal.chosen_runbook = chosen
            signal.score = _top_score(data.get("ranked"), signal.score)
        elif event.kind == "execute":
            _record_execution(signal, receipts, data)
        elif event.kind == "verify":
            receipt_id = _text_or_none(data.get("receipt_id"))
            if receipt_id is not None:
                receipts.append(receipt_id)
            verified = verified or str(data.get("verification", "")) == VerificationStatus.verified
        elif event.kind == "ledger_hit":
            receipt_id = _text_or_none(data.get("receipt_id"))
            if receipt_id is not None:
                receipts.append(receipt_id)
    signal.verified_write = _verified(receipts, ledger) if ledger is not None else verified
    return signal


def _record_execution(signal: TurnSignal, receipts: list[str], data: dict) -> None:
    """Note the operations a turn completed, and the receipts it left."""
    receipt_id = _text_or_none(data.get("receipt_id"))
    if receipt_id is not None:
        receipts.append(receipt_id)
    if data.get("stage") != "call" or not data.get("ok"):
        return
    operation = str(data.get("operation", ""))
    if operation and operation not in signal.outcomes:
        signal.outcomes.append(operation)


def _verified(receipt_ids: list[str], ledger: LedgerStore) -> bool:
    """True when the ledger says one of these receipts was read back and matched."""
    for receipt_id in dict.fromkeys(receipt_ids):
        receipt = ledger.get(receipt_id)
        if receipt is not None and receipt.verification is VerificationStatus.verified:
            return True
    return False


def _position(value: object, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _top_score(ranked: object, fallback: float) -> float:
    if isinstance(ranked, list) and ranked and isinstance(ranked[0], dict):
        return _number(ranked[0].get("score"), fallback)
    return fallback


def _number(value: object, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) else fallback


def _text_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
