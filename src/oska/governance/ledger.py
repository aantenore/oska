"""Durable record of every write this system attempted.

The receipt is written before the connector is called, so a write whose outcome
is unknown still leaves a trace and still blocks a second attempt at the same
thing. This is the SQLite implementation of the `LedgerStore` interface in
`oska.storage`: the file is plain SQLite and every call opens and closes its own
connection, so the ledger survives a restart and can be read by another process
while the runtime is up.

One failure is not like the others. A write the system answered — with a
success, with a refusal, with anything at all — reached it, and an identical
write is never sent a second time on the strength of a later request: `settled`
returns that receipt, and the caller reports it rather than calling again.

The intent is taken with `claim`, which records the receipt only when nothing
already settles its key and does both in one transaction that holds the write
lock of the file. Two turns, two threads or two processes asking for the same
write at once cannot both win it.

A receipt left `error` and `retryable` is the one that leaves the key open. The
connector classifies a failure `retryable` only when the request provably never
left, and the read-back settlement in `engine/execution.py` leaves a receipt
`retryable` only when it showed the write did not land, or when nothing is known
and the system of record would itself stop a repeat. A failure classified
`ambiguous` — the request left and no usable answer came back — keeps the key
closed until the read-back has settled it, and closed for good when the process
never gets that far.

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
from oska.storage.sqlite import prepare

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

_INSERT = (
    f"INSERT INTO receipts ({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})"
)

_SETTLED = (
    "SELECT * FROM receipts WHERE session_id = ? AND ledger_key = ? "
    "AND NOT (status = 'error' AND COALESCE(classification, '') = 'retryable') "
    "ORDER BY rowid DESC LIMIT 1"
)


def canonical_json(value: Any) -> str:
    """Render a value as JSON that is byte-identical for equal content."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def ledger_key(
    integration_id: str,
    operation: str,
    payload: dict,
    idempotency_fields: list[str] | None = None,
) -> str:
    """Identity of a write: the same key twice is the same intent twice.

    With declared idempotency fields only those fields count, so two requests
    that differ in an irrelevant field still collide. Every ledger backend
    derives its keys here, so a key means the same thing wherever it is kept.
    """
    if idempotency_fields:
        subject: dict = {field: payload.get(field) for field in sorted(idempotency_fields)}
    else:
        subject = dict(payload)
    material = canonical_json([integration_id, operation, subject])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def pending_receipt(
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


class WriteLedger:
    """Write receipts kept in a SQLite file: the `LedgerStore` every install starts with."""

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
        """Identity of a write: the same key twice is the same intent twice."""
        return ledger_key(integration_id, operation, payload, idempotency_fields)

    def new_receipt(
        self,
        session_id: str,
        key: str,
        integration_id: str,
        operation: str,
        payload: dict,
    ) -> WriteReceipt:
        """A receipt for an intent that has not been sent yet."""
        return pending_receipt(session_id, key, integration_id, operation, payload)

    def record(self, receipt: WriteReceipt) -> WriteReceipt:
        """Store a receipt, whatever else the ledger holds for its key."""
        with self._connect() as connection:
            connection.execute(_INSERT, _to_row(receipt))
        return receipt

    def claim(self, receipt: WriteReceipt) -> WriteReceipt:
        """Take the intent for this receipt, or hand back the receipt that already holds it.

        The receipt is recorded only when nothing settles its key for its
        session, and the check and the record are one transaction under the
        write lock of the file, so of two callers claiming the same intent at
        once exactly one wins. The winner gets its own receipt back; the other
        gets the winner's, and does not send the write.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            held = connection.execute(
                _SETTLED, (receipt.session_id, receipt.ledger_key)
            ).fetchone()
            if held is not None:
                return _from_row(held)
            connection.execute(_INSERT, _to_row(receipt))
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
        a write that never left, or whose read-back showed it did not land. A
        receipt left `ambiguous` settles the intent: its outcome is not known.
        """
        rows = self._query(_SETTLED, (session_id, key))
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

    def purge(self) -> int:
        """Remove nothing: the namespace of a file ledger is its file, which is not its to remove.

        Whoever created the file removes it with its directory, as the
        conformance runner removes the state directory of every scenario.
        """
        return 0

    def _query(self, statement: str, parameters: tuple) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(statement, parameters).fetchall()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._opening:
            if not self._ready:
                prepare(self._path, self._busy_timeout, _SCHEMA, _add_missing_columns)
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
