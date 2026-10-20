"""What the evolution layer holds of a conversation, and how retention takes it out.

A proposal drafted from a session carries that session: its identifier, and a
procedure written from what was said and what was written in it. A change
request composed from the proposal carries the same procedure in its package.
Both are the record of a conversation as much as its audit trail is, and a
request to be forgotten, or a window, reaches them too.

Most of them go. A proposal is deleted, and so is every change request composed
from it that is open or was rejected. One kind of record stays: a change request
that was applied is part of the catalog, and its record is the audit of that
decision. Its package, the part written from the conversation, is removed, and
the record keeps who asked, who decided, when, what version of which procedure
and where it was applied. The proposal it was composed from stays for the same
reason, with the session, the text drafted from it and the issues quoting that
text redacted, and its history of decisions kept.

These are the two stores `oska.governance.retention` asks for, each answering
for one session at a time. The change requests are asked first: they are found
through the proposals they were composed from. Both reach the proposals and the
change requests through their store interfaces, so a directory on one machine
and a database shared by several replicas are removed from the same way.
"""

from __future__ import annotations

from oska.contracts.proposal import QualityReport, SkillProposal
from oska.contracts.stores import ChangeRequestRecordStore, ProposalRecordStore
from oska.learning.changes import ChangeRequest, ChangeRequestState

__all__ = ["FORGOTTEN", "ChangeRequestRecords", "ProposalRecords"]

FORGOTTEN = "[forgotten]"
"""What a redacted field holds in place of what came from a forgotten session."""


class ProposalRecords:
    """The proposals drafted from a session, as retention removes them."""

    def __init__(
        self, proposals: ProposalRecordStore, changes: ChangeRequestRecordStore
    ) -> None:
        self._proposals = proposals
        self._changes = changes

    def delete(self, session_id: str) -> int:
        """Remove the proposals drafted from this session, or redact those a change applied."""
        applied = {
            request.proposal_id for request in self._changes.list(ChangeRequestState.applied)
        }
        count = 0
        for proposal in self._proposals.list():
            if proposal.source_session_id != session_id:
                continue
            if proposal.proposal_id in applied:
                self._proposals.save(_redacted(proposal, session_id))
            else:
                self._proposals.delete(proposal.proposal_id)
            count += 1
        return count


class ChangeRequestRecords:
    """The change requests composed from a session's proposals, as retention removes them."""

    def __init__(
        self, proposals: ProposalRecordStore, changes: ChangeRequestRecordStore
    ) -> None:
        self._proposals = proposals
        self._changes = changes

    def delete(self, session_id: str) -> int:
        """Remove the requests composed from this session, or redact an applied one's package."""
        drafted = {
            proposal.proposal_id
            for proposal in self._proposals.list()
            if proposal.source_session_id == session_id
        }
        count = 0
        for request in self._changes.list():
            if request.proposal_id not in drafted:
                continue
            if request.state is ChangeRequestState.applied:
                count += self._redact(request)
                continue
            self._changes.delete(request.request_id)
            count += 1
        return count

    def _redact(self, request: ChangeRequest) -> int:
        """Take the package out of an applied request, keeping the record of the decision."""
        removed = self._changes.drop_package(request.request_id)
        if not removed and not request.quality.issues:
            return 0
        kept = request.model_copy(
            update={"quality": QualityReport(issues=[], score=request.quality.score)}
        )
        self._changes.save(kept)
        return 1


def _redacted(proposal: SkillProposal, session_id: str) -> SkillProposal:
    """The proposal, with what came from the session replaced and its decisions kept."""
    history = [
        entry.model_copy(update={"by": FORGOTTEN}) if entry.by == session_id else entry
        for entry in proposal.history
    ]
    return proposal.model_copy(
        update={
            "source_session_id": FORGOTTEN,
            "skill_markdown": "",
            "quality": QualityReport(issues=[], score=proposal.quality.score),
            "history": history,
        }
    )
