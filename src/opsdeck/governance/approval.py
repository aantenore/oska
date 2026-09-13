"""Holds a write at the exact payload the user was shown.

The approval is frozen on a hash of that payload. If anything about the request
changes between the question and the answer, the hash no longer matches and the
old approval cannot be used to run the new request.
"""

from __future__ import annotations

import hashlib
import uuid

from opsdeck.contracts.common import PendingApproval
from opsdeck.contracts.plan import Plan
from opsdeck.governance.ledger import canonical_json


def payload_hash(payload: dict) -> str:
    """Identity of a payload, stable for equal content and different for anything else."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ApprovalGate:
    """Creates the approval a write waits on and checks it before the write runs."""

    def request(
        self,
        session_id: str,
        runbook_id: str,
        plan: Plan,
        op_index: int,
        integration_id: str,
        operation: str,
        payload: dict,
        approvals_required: int = 1,
    ) -> PendingApproval:
        """Freeze one operation of a plan on its payload and wait for an answer.

        `session_id` names the session the approval belongs to; the session
        state holds the approval itself, so it is not repeated on the record.
        The summary is derived from the payload, so the sentence the user reads
        cannot say anything the hash does not cover.
        """
        exact = dict(payload)
        return PendingApproval(
            approval_id=str(uuid.uuid4()),
            runbook_id=runbook_id,
            plan=plan,
            op_index=op_index,
            exact_payload=exact,
            payload_hash=payload_hash(exact),
            integration_id=integration_id,
            operation=operation,
            approvals_required=approvals_required,
            approvals_given=0,
            summary=self.summarise(integration_id, operation, exact),
        )

    def check(self, pending: PendingApproval, payload: dict) -> bool:
        """True when the payload about to run is the one that was approved."""
        return payload_hash(payload) == pending.payload_hash

    def summarise(self, integration_id: str, operation: str, payload: dict) -> str:
        """The sentence the user answers, carrying the payload exactly as it will be sent."""
        return f"{operation} on {integration_id} with {canonical_json(payload)}"
