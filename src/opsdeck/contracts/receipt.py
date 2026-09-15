"""Receipts recorded for every write attempt."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ReceiptStatus = Literal["pending", "executed", "error"]

Classification = Literal["retryable", "terminal"]
"""How a failed write may be treated: worth another attempt, or settled."""


class VerificationStatus(StrEnum):
    """Result of the read-back that follows a write."""

    verified = "verified"
    unverified = "unverified"
    failed = "failed"


class WriteReceipt(BaseModel):
    """Record of one write attempt and of what the read-back showed."""

    receipt_id: str
    session_id: str
    ledger_key: str
    integration_id: str
    operation: str
    payload: dict = Field(default_factory=dict)
    response: dict | None = None
    status: ReceiptStatus = "pending"
    verification: VerificationStatus = VerificationStatus.unverified
    verification_evidence: dict | None = None
    created_at: datetime
    error: str | None = None
    classification: Classification | None = None
    revision: str | None = None
