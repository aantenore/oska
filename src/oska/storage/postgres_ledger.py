"""The write ledger kept in PostgreSQL, so several processes share one record.

It keeps the promises of the SQLite ledger exactly — the same keys, the same
settlement rules, the same order — and one more that a single file never had to
spell out: the claim on an intent holds across processes.

A claim is one transaction. It inserts a row for the session and the ledger key
into `oska_write_claims` with `ON CONFLICT DO NOTHING`, then locks that row with
`SELECT ... FOR UPDATE`. Every claimer of the same intent queues on that lock,
so the check that nothing settles the key and the record of the new receipt
are made by one of them at a time, and the second sees what the first
committed. The claim row names the receipt that holds the intent; the receipts
themselves are the record, and `settled` reads them the way SQLite does.

A ledger reads and writes under one namespace, which is part of the key of
every receipt and every claim and of every query, so two ledgers under
different namespaces over one database never see each other's receipts.
"""

from __future__ import annotations

from typing import Any

from oska.contracts.receipt import (
    Classification,
    ReceiptStatus,
    VerificationStatus,
    WriteReceipt,
)
from oska.governance.ledger import ledger_key, pending_receipt
from oska.storage.postgres import Database, jsonb
from oska.storage.protocols import DEFAULT_NAMESPACE

__all__ = ["PostgresLedger"]

_COLUMNS = (
    "namespace",
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
_INSERT = (
    f"INSERT INTO oska_receipts ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(_COLUMNS))})"
)
_SETTLED = (
    "SELECT * FROM oska_receipts WHERE namespace = %s AND session_id = %s "
    "AND ledger_key = %s "
    "AND NOT (status = 'error' AND COALESCE(classification, '') = 'retryable') "
    "ORDER BY seq DESC LIMIT 1"
)
_JSON_COLUMNS = frozenset({"response", "verification_evidence"})


class PostgresLedger:
    """Write receipts kept in a PostgreSQL database, under one namespace."""

    def __init__(
        self,
        url: str,
        *,
        busy_timeout: float | None = None,
        database: Database | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self._database = database or Database(url, busy_timeout=busy_timeout)
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        """The namespace every receipt of this ledger is kept under."""
        return self._namespace

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
        with self._database.connect() as connection:
            connection.execute(_INSERT, _to_row(self._namespace, receipt))
        return receipt

    def claim(self, receipt: WriteReceipt) -> WriteReceipt:
        """Take the intent for this receipt, or hand back the receipt that already holds it."""
        identity = (self._namespace, receipt.session_id, receipt.ledger_key)
        with self._database.connect() as connection:
            connection.execute(
                "INSERT INTO oska_write_claims (namespace, session_id, ledger_key) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                identity,
            )
            connection.execute(
                "SELECT receipt_id FROM oska_write_claims "
                "WHERE namespace = %s AND session_id = %s AND ledger_key = %s FOR UPDATE",
                identity,
            )
            held = connection.execute(_SETTLED, identity).fetchone()
            if held is not None:
                return _from_row(held)
            connection.execute(_INSERT, _to_row(self._namespace, receipt))
            connection.execute(
                "UPDATE oska_write_claims SET receipt_id = %s, claimed_at = now() "
                "WHERE namespace = %s AND session_id = %s AND ledger_key = %s",
                (receipt.receipt_id, *identity),
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
        """Fill in what the connector and the read-back reported; omitted fields stay."""
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("response", response),
            ("verification", None if verification is None else str(verification)),
            ("verification_evidence", evidence),
            ("error", error),
            ("classification", classification),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                values.append(jsonb(value) if column in _JSON_COLUMNS else value)
        if assignments:
            values.extend([self._namespace, receipt_id])
            with self._database.connect() as connection:
                connection.execute(
                    f"UPDATE oska_receipts SET {', '.join(assignments)} "
                    "WHERE namespace = %s AND receipt_id = %s",
                    values,
                )
        return self.get(receipt_id)

    def get(self, receipt_id: str) -> WriteReceipt | None:
        """One receipt by its identifier."""
        rows = self._query(
            "SELECT * FROM oska_receipts WHERE namespace = %s AND receipt_id = %s",
            (self._namespace, receipt_id),
        )
        return _from_row(rows[0]) if rows else None

    def lookup(self, session_id: str, key: str) -> WriteReceipt | None:
        """The most recent receipt for this intent in this session, whatever its outcome."""
        rows = self._query(
            "SELECT * FROM oska_receipts WHERE namespace = %s AND session_id = %s "
            "AND ledger_key = %s ORDER BY seq DESC LIMIT 1",
            (self._namespace, session_id, key),
        )
        return _from_row(rows[0]) if rows else None

    def settled(self, session_id: str, key: str) -> WriteReceipt | None:
        """The receipt that closes this intent for this session, or nothing."""
        rows = self._query(_SETTLED, (self._namespace, session_id, key))
        return _from_row(rows[0]) if rows else None

    def count(self, session_id: str, operation: str) -> int:
        """How many times this session asked for this operation, in any state."""
        rows = self._query(
            "SELECT COUNT(*) AS total FROM oska_receipts "
            "WHERE namespace = %s AND session_id = %s AND operation = %s",
            (self._namespace, session_id, operation),
        )
        return int(rows[0]["total"]) if rows else 0

    def for_session(self, session_id: str) -> list[WriteReceipt]:
        """Every receipt of a session, oldest first."""
        rows = self._query(
            "SELECT * FROM oska_receipts WHERE namespace = %s AND session_id = %s "
            "ORDER BY seq",
            (self._namespace, session_id),
        )
        return [_from_row(row) for row in rows]

    def delete_session(self, session_id: str) -> int:
        """Remove every receipt and claim of one session; report how many receipts went."""
        with self._database.connect() as connection:
            identity = (self._namespace, session_id)
            connection.execute(
                "DELETE FROM oska_write_claims WHERE namespace = %s AND session_id = %s",
                identity,
            )
            cursor = connection.execute(
                "DELETE FROM oska_receipts WHERE namespace = %s AND session_id = %s", identity
            )
            return int(cursor.rowcount)

    def sessions(self) -> list[str]:
        """Every session this ledger holds a receipt for, oldest receipt first."""
        rows = self._query(
            "SELECT session_id FROM oska_receipts WHERE namespace = %s "
            "GROUP BY session_id ORDER BY MIN(seq)",
            (self._namespace,),
        )
        return [row["session_id"] for row in rows]

    def _query(self, statement: str, parameters: tuple) -> list[dict]:
        with self._database.connect() as connection:
            return list(connection.execute(statement, parameters).fetchall())


def _to_row(namespace: str, receipt: WriteReceipt) -> tuple:
    return (
        namespace,
        receipt.receipt_id,
        receipt.session_id,
        receipt.ledger_key,
        receipt.integration_id,
        receipt.operation,
        jsonb(receipt.payload),
        jsonb(receipt.response),
        receipt.status,
        str(receipt.verification),
        jsonb(receipt.verification_evidence),
        receipt.created_at,
        receipt.error,
        receipt.classification,
        receipt.revision,
    )


def _from_row(row: dict) -> WriteReceipt:
    return WriteReceipt(
        receipt_id=row["receipt_id"],
        session_id=row["session_id"],
        ledger_key=row["ledger_key"],
        integration_id=row["integration_id"],
        operation=row["operation"],
        payload=row["payload"],
        response=row["response"],
        status=row["status"],
        verification=VerificationStatus(row["verification"]),
        verification_evidence=row["verification_evidence"],
        created_at=row["created_at"],
        error=row["error"],
        classification=row["classification"],
        revision=row["revision"],
    )
