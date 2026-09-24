"""Who the write is being made for, and where that value comes from.

A procedure that acts on a person's own account should not ask which account
that is. The channel in front of the runtime authenticated somebody; the
session carries what it said; and a procedure declares, field by field, which
of its payload fields take their value from that identity instead of from the
conversation. Those fields are never asked for, never offered to the model and
never accepted from it: they are filled here, after the plan is made and before
the policy judges it, so the value a write is bound to is one the person on the
other end could not choose for themselves.

Nothing in this module authenticates anything. `authenticated` is what the
channel reported, and a principal that reports otherwise is treated as no
principal at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from oska.contracts.skill import Skill

__all__ = [
    "IdentityError",
    "Principal",
    "PrincipalMismatch",
    "declared_inputs",
    "refused_fields",
    "requires_principal",
    "resolve_inputs",
]

SUBJECT = "subject"


class IdentityError(Exception):
    """A declared principal input cannot be filled from the identity on the session."""


class PrincipalMismatch(Exception):
    """A turn arrived under an identity other than the one the session was opened with."""


@dataclass(frozen=True)
class Principal:
    """The identity the channel authenticated, as the channel reported it.

    `subject` is what that identity is called. `attributes` is whatever else
    traveled with it, and `subject` is readable as an attribute of that name,
    so a procedure names either the same way. `authenticated` is the channel's
    own statement: a principal that carries `False` is not one a write may be
    bound to.
    """

    subject: str
    attributes: dict = field(default_factory=dict)
    authenticated: bool = False

    @property
    def values(self) -> dict:
        """Every attribute this identity offers, the subject among them."""
        return {SUBJECT: self.subject, **self.attributes}

    def as_dict(self) -> dict:
        """The form the session state keeps."""
        return {
            SUBJECT: self.subject,
            "attributes": dict(self.attributes),
            "authenticated": self.authenticated,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> Principal | None:
        """The identity a session state holds, or nothing when it holds none."""
        if not data:
            return None
        attributes = data.get("attributes")
        return cls(
            subject=str(data.get(SUBJECT, "")),
            attributes=dict(attributes) if isinstance(attributes, dict) else {},
            authenticated=bool(data.get("authenticated", False)),
        )


def declared_inputs(skills: Sequence[Skill]) -> dict[str, str]:
    """Every principal input the given procedures declare, field to attribute."""
    declared: dict[str, str] = {}
    for skill in skills:
        for name, attribute in skill.frontmatter.principal_inputs.items():
            declared.setdefault(name, attribute)
    return declared


def requires_principal(skills: Sequence[Skill]) -> bool:
    """Whether any of these procedures binds a field to the authenticated identity."""
    return bool(declared_inputs(skills))


def resolve_inputs(skill: Skill, principal: Principal | None) -> dict:
    """The fields this procedure takes from the identity, with their values.

    A procedure that declares none resolves to nothing, whether or not anybody
    was authenticated. A procedure that declares one and is reached without an
    authenticated identity is refused here rather than asked about, because the
    value is not the person's to give.
    """
    declared = skill.frontmatter.principal_inputs
    if not declared:
        return {}
    if principal is None or not principal.authenticated:
        raise IdentityError(
            f"{skill.id} takes {_listed(declared)} from the authenticated session, "
            "and this session carries no authenticated identity"
        )
    held = principal.values
    resolved: dict = {}
    for name, attribute in declared.items():
        value = held.get(attribute)
        if value is None or value == "":
            raise IdentityError(
                f"{skill.id} takes {name} from the {attribute} of the authenticated "
                "identity, and that identity carries no such attribute"
            )
        resolved[name] = value
    return resolved


def refused_fields(skill: Skill, payload: dict) -> list[str]:
    """The declared principal inputs this payload carries, which it may not.

    The model is told these fields exist and are not its to fill. A payload
    that carries one anyway is not corrected quietly: the value it proposed is
    a value it chose, and a write bound to it would be bound to the wrong
    identity.
    """
    declared = skill.frontmatter.principal_inputs
    return [name for name in payload if name in declared]


def _listed(names: Iterable[str]) -> str:
    listed = sorted(names)
    return ", ".join(listed) if listed else "nothing"
