"""Durable record of every write this system attempted.

The receipt is written before the connector is called, so a write whose outcome
is unknown still leaves a trace and still blocks a second attempt at the same
thing. The file is plain SQLite and every call opens and closes its own
connection, so the ledger survives a restart and can be read by another
process while the runtime is up.

One failure is not like the others. A write the system answered — with a
success, with a refusal, with anything at all — reached it, and an identical
write is never sent a second time on the strength of a later request: `settled`
returns that receipt, and the caller reports it rather than calling again.

A failure the connector classifies `retryable` is the one case the ledger
cannot decide on its own. It covers both a request that never left and one the
system applied without ever acknowledging, and the two are indistinguishable
from the connector. So the classification on the receipt is not a guess about
which of them happened: it is what the read-back of that write established. A
receipt left `error` and `retryable` is one whose read-back showed the write
did not land, and only that receipt leaves the key open for another attempt.
Where the read-back showed the write landed, the receipt says `executed`; where
it could not be reached at all, the write is closed as `terminal` unless the
system of record would itself stop a repeat. The rule is written where the
decision is taken, in `engine/execution.py`.

The receipt of the attempt stays in the record whichever way it went, because
what was tried is part of the account even when nothing came of it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oska.config import DEFAULT_BUSY_TIMEOUT_SECONDS
from oska.contracts.receipt import (
    Classification,
    ReceiptStatus,
    VerificationStatus,
    WriteReceipt,
)

_COLUMNS = (
    "receipt_id",
    "session_id",
    "ledger_key",
    "integration_id",
    "operation",
    "payload",
    "response",
    "status",
    "verification",
    "verification_evidence",
    "created_at",
    "error",
    "classification",
    "revision",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ledger_key TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload TEXT NOT NULL,
    response TEXT,
    status TEXT NOT NULL,
    verification TEXT NOT NULL,
    verification_evidence TEXT,
    created_at TEXT NOT NULL,
    error TEXT,
    classification TEXT,
    revision TEXT
);
CREATE INDEX IF NOT EXISTS receipts_session_key ON receipts (session_id, ledger_key);
CREATE INDEX IF NOT EXISTS receipts_session_operation ON receipts (session_id, operation);
"""

def canonical_json(value: Any) -> str:
    """Render a value as JSON that is byte-identical for equal content."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


class WriteLedger:
    """Write receipts kept in a SQLite file."""

    def __init__(self, path: Path, *, busy_timeout: float | None = None) -> None:
        self._path = Path(path)
        self._ready = False
        self._opening = threading.Lock()
        self._busy_timeout = DEFAULT_BUSY_TIMEOUT_SECONDS if busy_timeout is None else busy_timeout

    @property
    def path(self) -> Path:
        """File this ledger writes to."""
        return self._path

    def key(
        self,
        integration_id: str,
        operation: str,
        payload: dict,
        idempotency_fields: list[str] | None = None,
    ) -> str:
        """Identity of a write: the same key twice is the same intent twice.

        With declared idempotency fields only those fields count, so two
        requests that differ in an irrelevant field still collide.
        """
        if idempotency_fields:
            subject: dict = {field: payload.get(field) for field in sorted(idempotency_fields)}
        else:
            subject = dict(payload)
        material = canonical_json([integration_id, operation, subject])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def new_receipt(
        self,
        session_id: str,
        key: str,
        integration_id: str,
        operation: str,
        payload: dict,
    ) -> WriteReceipt:
        """A receipt for an intent that has not been sent yet."""
        return WriteReceipt(
            receipt_id=str(uuid.uuid4()),
            session_id=session_id,
            ledger_key=key,
            integration_id=integration_id,
            operation=operation,
            payload=dict(payload),
            response=None,
            status="pending",
            verification=VerificationStatus.unverified,
            verification_evidence=None,
            created_at=datetime.now(UTC),
            error=None,
            classification=None,
        )

    def record(self, receipt: WriteReceipt) -> WriteReceipt:
        """Store a receipt. Called before the connector runs."""
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO receipts ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(_COLUMNS))})",
                _to_row(receipt),
            )
        return receipt

    def update(
        self,
        receipt_id: str,
        *,
        status: ReceiptStatus | None = None,
        response: dict | None = None,
        verification: VerificationStatus | str | None = None,
        evidence: dict | None = None,
        error: str | None = None,
        classification: Classification | None = None,
    ) -> WriteReceipt | None:
        """Fill in what the connector and the read-back reported.

        Arguments left out are not touched, so an update can report an outcome
        without clearing the evidence an earlier one recorded.
        """
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("response", _dump(response)),
            ("verification", None if verification is None else str(verification)),
            ("verification_evidence", _dump(evidence)),
            ("error", error),
            ("classification", classification),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if assignments:
            values.append(receipt_id)
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE receipts SET {', '.join(assignments)} WHERE receipt_id = ?",
                    values,
                )
        return self.get(receipt_id)

    def get(self, receipt_id: str) -> WriteReceipt | None:
        """One receipt by its identifier."""
        rows = self._query("SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,))
        return _from_row(rows[0]) if rows else None

    def lookup(self, session_id: str, key: str) -> WriteReceipt | None:
        """The most recent receipt for this intent in this session, whatever its outcome."""
        rows = self._query(
            "SELECT * FROM receipts WHERE session_id = ? AND ledger_key = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (session_id, key),
        )
        return _from_row(rows[0]) if rows else None

    def settled(self, session_id: str, key: str) -> WriteReceipt | None:
        """The receipt that closes this intent for this session, or nothing.

        A write the system answered settles the intent, whatever it answered:
        an identical write is not sent again. A receipt left `error` and
        `retryable` settles nothing, and the intent may be attempted once more
        with the receipt of the attempt left in place — which is the record of
        a write whose read-back showed it did not land, never of one whose
        outcome is merely unknown.
        """
        rows = self._query(
            "SELECT * FROM receipts WHERE session_id = ? AND ledger_key = ? "
            "AND NOT (status = 'error' AND COALESCE(classification, '') = 'retryable') "
            "ORDER BY rowid DESC LIMIT 1",
            (session_id, key),
        )
        return _from_row(rows[0]) if rows else None

    def count(self, session_id: str, operation: str) -> int:
        """How many times this session asked for this operation, in any state."""
        rows = self._query(
            "SELECT COUNT(*) AS total FROM receipts WHERE session_id = ? AND operation = ?",
            (session_id, operation),
        )
        return int(rows[0]["total"]) if rows else 0

    def for_session(self, session_id: str) -> list[WriteReceipt]:
        """Every receipt of a session, oldest first."""
        rows = self._query(
            "SELECT * FROM receipts WHERE session_id = ? ORDER BY rowid", (session_id,)
        )
        return [_from_row(row) for row in rows]

    def delete_session(self, session_id: str) -> int:
        """Remove every receipt of one session and report how many went.

        This is the one thing that takes a receipt out of the record. It is not
        an amendment of history and it is not called by a turn: it exists
        because the account of a conversation is personal data, and a person
        may ask for theirs to stop existing.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM receipts WHERE session_id = ?", (session_id,)
            )
            return int(cursor.rowcount)

    def sessions(self) -> list[str]:
        """Every session this ledger holds a receipt for, oldest receipt first."""
        rows = self._query(
            "SELECT session_id FROM receipts GROUP BY session_id ORDER BY MIN(rowid)", ()
        )
        return [row["session_id"] for row in rows]

    def _query(self, statement: str, parameters: tuple) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(statement, parameters).fetchall()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._opening:
            if not self._ready:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                starting = sqlite3.connect(self._path, timeout=self._busy_timeout)
                starting.row_factory = sqlite3.Row
                try:
                    starting.execute("PRAGMA journal_mode=WAL")
                    starting.executescript(_SCHEMA)
                    _add_missing_columns(starting)
                    starting.commit()
                finally:
                    starting.close()
                self._ready = True
        connection = sqlite3.connect(self._path, timeout=self._busy_timeout)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    """Widen a ledger written before a column existed, rather than refusing to read it."""
    held = {row["name"] for row in connection.execute("PRAGMA table_info(receipts)")}
    for column in _COLUMNS:
        if column not in held:
            connection.execute(f"ALTER TABLE receipts ADD COLUMN {column} TEXT")


def _dump(value: dict | None) -> str | None:
    return None if value is None else canonical_json(value)


def _load(value: str | None) -> dict | None:
    return None if value is None else json.loads(value)


def _to_row(receipt: WriteReceipt) -> tuple:
    return (
        receipt.receipt_id,
        receipt.session_id,
        receipt.ledger_key,
        receipt.integration_id,
        receipt.operation,
        canonical_json(receipt.payload),
        _dump(receipt.response),
        receipt.status,
        str(receipt.verification),
        _dump(receipt.verification_evidence),
        receipt.created_at.isoformat(),
        receipt.error,
        receipt.classification,
        receipt.revision,
    )


def _from_row(row: sqlite3.Row) -> WriteReceipt:
    return WriteReceipt(
        receipt_id=row["receipt_id"],
        session_id=row["session_id"],
        ledger_key=row["ledger_key"],
        integration_id=row["integration_id"],
        operation=row["operation"],
        payload=json.loads(row["payload"]),
        response=_load(row["response"]),
        status=row["status"],
        verification=VerificationStatus(row["verification"]),
        verification_evidence=_load(row["verification_evidence"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        error=row["error"],
        classification=row["classification"],
        revision=row["revision"],
    )
