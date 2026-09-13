"""Durable record of every write this system attempted.

The receipt is written before the connector is called, so a write whose outcome
is unknown still leaves a trace and still blocks a second attempt at the same
thing. The file is plain SQLite and every call opens and closes its own
connection, so the ledger survives a restart and can be read by another
process while the runtime is up.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsdeck.contracts.receipt import ReceiptStatus, VerificationStatus, WriteReceipt

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
    error TEXT
);
CREATE INDEX IF NOT EXISTS receipts_session_key ON receipts (session_id, ledger_key);
CREATE INDEX IF NOT EXISTS receipts_session_operation ON receipts (session_id, operation);
"""

def canonical_json(value: Any) -> str:
    """Render a value as JSON that is byte-identical for equal content."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


class WriteLedger:
    """Write receipts kept in a SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._ready = False

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

    def _query(self, statement: str, parameters: tuple) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(statement, parameters).fetchall()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            if not self._ready:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(_SCHEMA)
                self._ready = True
            yield connection
            connection.commit()
        finally:
            connection.close()


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
    )
