"""What a session is bound to: the identity that opened it and the revision serving it.

A turn may arrive under an identity. The channel authenticates it and says who
it is; the session records it on its first turn and refuses any later turn that
arrives as somebody else, because a session is one conversation with one person
and a write bound to an identity halfway through would be bound to whichever
one happened to be on that turn. For the same reason a session opened without
an identity stays without one: an identity arriving once the session holds
anything, a history, a goal waiting on a question or a write waiting on an
approval, would take over what somebody else asked for and answer it as
itself. A procedure that takes a field from that
identity is not offered to a session that has none: the value is not the
person's to give, so the turn is refused with that as the reason instead of
asking a question nobody should answer.

Where the catalog is served as a revision, the turn opens on the revision the
session was last served by and reads the whole catalog through it, so a
procedure retired while someone holds a pending approval does not disappear
from the turn that offered it. Only a turn that starts a new task takes the
newest revision, and it says so in the audit trail. That pin belongs to the
turn for the same reason the model budget does.
"""

from __future__ import annotations

from oska.catalog.registry import Catalog
from oska.contracts.skill import Skill
from oska.engine.context import TurnContext
from oska.engine.state import SessionState
from oska.governance.identity import Principal, PrincipalMismatch, requires_principal

__all__ = ["adopt_newest", "bind", "record_revision", "revision_for", "unsigned"]

_NO_PRINCIPAL = (
    "acts on the account of whoever is signed in, and nobody is signed in to this "
    "session, so nothing was planned; that account is not something to ask for"
)
_FIRST_TURN = "first_turn"
_NEW_TASK = "new_task"
_UNPINNED = "unpinned"


def bind(state: SessionState, principal: Principal | None) -> None:
    """Hold the session to one identity, and refuse a turn that arrives as another.

    The identity a session holds is the one its first turn carried. A session
    opened without one stays without one: a later turn carrying an identity
    is refused, so nobody adopts a conversation or answers an approval
    somebody else was asked. A session that holds one
    is that person's: a turn carrying a different subject, or carrying none at
    all, is not a turn of this conversation and nothing of it is run or
    recorded.
    """
    held = Principal.from_dict(state.principal)
    if held is None:
        if principal is None:
            return
        if _begun(state):
            raise PrincipalMismatch(
                "this session was opened without an identity and stays without one, "
                f"and this turn arrives as {principal.subject}"
            )
        state.principal = principal.as_dict()
        return
    if principal is None:
        raise PrincipalMismatch(
            f"this session was opened by {held.subject} and this turn carries no identity"
        )
    if principal.subject != held.subject:
        raise PrincipalMismatch(
            f"this session was opened by {held.subject} and this turn arrives as "
            f"{principal.subject}"
        )
    state.principal = principal.as_dict()


def unsigned(
    registry: Catalog, state: SessionState, skill: Skill, member_ids: list[str]
) -> str | None:
    """Why this procedure is not for a session with nobody signed in, or nothing.

    The procedures of a composite are read as well as the composite itself,
    because a step that binds a field to the identity binds the whole goal
    to it: the person would be asked for the request and refused at the
    step, which is worse than being refused at the start.
    """
    members = [registry.get(member) for member in member_ids]
    declaring = [held for held in (skill, *members) if held is not None]
    if not requires_principal(declaring):
        return None
    principal = Principal.from_dict(state.principal)
    if principal is not None and principal.authenticated:
        return None
    return f"{skill.id} {_NO_PRINCIPAL}"


def revision_for(
    registry: Catalog, pin_sessions: bool, state: SessionState
) -> tuple[str, str]:
    """The revision this turn opens on, and why it is that one.

    A session that has never been served takes what the deployment serves
    now. A pinned session keeps what it was served by until a turn starts a
    new task, and only a turn that starts one adopts a newer revision.

    A deployment that does not pin its sessions still holds one to its
    revision while something of the previous turn is waiting under it: an
    approval, or a goal that stopped to ask a question. Both were composed
    from that revision — the sentence a person is about to answer, and the
    procedure they are answering it for — and finishing either under a
    newer catalog would finish something other than what they were shown.
    Where pinning is off and nothing is waiting, the turn takes the newest
    revision whether or not it starts a new task.
    """
    served = registry.newest_revision()
    if not state.revision:
        return served, _FIRST_TURN
    if pin_sessions or _waiting(state):
        return state.revision, ""
    return (served or state.revision), _UNPINNED


def record_revision(ctx: TurnContext, revision: str, reason: str) -> None:
    """Note on the session which revision it is served by, when that changes."""
    state = ctx.state
    if not revision or revision == state.revision:
        return
    previous = state.revision
    state.revision = revision
    ctx.emit("revision", {"from": previous, "to": revision, "reason": reason})


def adopt_newest(registry: Catalog, ctx: TurnContext) -> None:
    """Start a new task on the newest catalog this deployment serves.

    The turn was routed through the revision the session held, and the
    procedure that routing named is looked up again below; a revision that
    no longer publishes it answers from the declared scope instead, which is
    what a retired procedure should do to a request that arrives after it.
    """
    newest = registry.newest_revision()
    if not newest or newest == ctx.state.revision:
        return
    record_revision(ctx, newest, _NEW_TASK)
    registry.adopt(newest)


def _begun(state: SessionState) -> bool:
    """Whether this session has had a turn, or holds anything of one."""
    return state.turn_count > 0 or bool(state.history) or _waiting(state)


def _waiting(state: SessionState) -> bool:
    """Whether this session left something of an earlier turn to be answered."""
    return state.pending_approval is not None or state.pending_goal is not None
