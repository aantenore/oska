"""Proposals kept in PostgreSQL, so every replica reviews one queue.

One row per proposal, holding the whole proposal as JSON beside the columns
retention and a state filter select by. A save replaces the row, as writing the
file replaces it in the directory store. Every row and every query carries the
namespace of the store; `purge` removes the whole of it and refuses the default.
"""

from __future__ import annotations

import builtins

from oska.contracts.proposal import ProposalState, SkillProposal
from oska.storage.postgres import Database, jsonb
from oska.storage.protocols import DEFAULT_NAMESPACE, refuse_default_purge

__all__ = ["PostgresProposalStore"]


class PostgresProposalStore:
    """Reads and writes proposals in a PostgreSQL database, under one namespace."""

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
        """The namespace every proposal of this store is kept under."""
        return self._namespace

    @property
    def location(self) -> str:
        """The database and the namespace, without the address that may carry a credential."""
        return f"postgres {self._database.identity} namespace {self._namespace}"

    def save(self, proposal: SkillProposal) -> str:
        """Write a proposal, replacing any earlier copy. Returns where it is kept."""
        with self._database.connect() as connection:
            connection.execute(
                "INSERT INTO oska_proposals "
                "(namespace, proposal_id, state, source_session_id, proposal) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (namespace, proposal_id) DO UPDATE SET "
                "state = EXCLUDED.state, source_session_id = EXCLUDED.source_session_id, "
                "proposal = EXCLUDED.proposal",
                (
                    self._namespace,
                    proposal.proposal_id,
                    proposal.state.value,
                    proposal.source_session_id,
                    jsonb(proposal.model_dump(mode="json")),
                ),
            )
        return self.location

    def get(self, proposal_id: str) -> SkillProposal:
        """Read one proposal. Raises `KeyError` when it is not there."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT proposal FROM oska_proposals WHERE namespace = %s AND proposal_id = %s",
                (self._namespace, proposal_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown proposal: {proposal_id}")
        return SkillProposal.model_validate(row["proposal"])

    def list(self, state: ProposalState | str | None = None) -> builtins.list[SkillProposal]:
        """Every proposal, filtered by state when one is named, sorted by id."""
        wanted = ProposalState(state) if state is not None else None
        with self._database.connect() as connection:
            if wanted is None:
                rows = connection.execute(
                    "SELECT proposal FROM oska_proposals WHERE namespace = %s "
                    "ORDER BY proposal_id",
                    (self._namespace,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT proposal FROM oska_proposals WHERE namespace = %s AND state = %s "
                    "ORDER BY proposal_id",
                    (self._namespace, wanted.value),
                ).fetchall()
        return [SkillProposal.model_validate(row["proposal"]) for row in rows]

    def delete(self, proposal_id: str) -> None:
        """Remove one proposal. Raises `KeyError` when it is not there."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM oska_proposals WHERE namespace = %s AND proposal_id = %s",
                (self._namespace, proposal_id),
            )
            removed = int(cursor.rowcount)
        if not removed:
            raise KeyError(f"unknown proposal: {proposal_id}")

    def purge(self) -> int:
        """Remove every proposal of this namespace and report how many went."""
        refuse_default_purge(self._namespace)
        with self._database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM oska_proposals WHERE namespace = %s", (self._namespace,)
            )
            return int(cursor.rowcount)
