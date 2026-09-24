"""Who the write is being made for, and where that value comes from.

A procedure that acts on a person's own account should not ask which account
that is. The channel in front of the runtime authenticated somebody; the
session carries what it said; and a procedure declares, field by field, which
of its payload fields take their value from that identity instead of from the
conversation. Those fields are never asked for, never offered to the model and
never accepted from it: they are filled here, after the plan is made and before
the policy judges it, so the value a write is bound to is one the person on the
other end could not choose for themselves.

Nothing in this module authenticates a person. `authenticated` is what the
channel reported, and a principal that reports otherwise is treated as no
principal at all. What it does do is check a signed session token, when the
channel hands one over instead of a trusted header: a compact HS256 token whose
signature, algorithm, required claims, validity window and session binding are
all checked here, with the standard library, before any claim is believed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from oska.contracts.skill import Skill

__all__ = [
    "IdentityError",
    "Principal",
    "PrincipalMismatch",
    "TokenError",
    "declared_inputs",
    "mint_token",
    "principal_from_claims",
    "refused_fields",
    "requires_principal",
    "resolve_inputs",
    "verify_token",
]

SUBJECT = "subject"
TENANT = "tenant"
TOKEN_ALGORITHM = "HS256"
REGISTERED_CLAIMS = frozenset({"sub", "sid", "exp", "nbf", "iat", "iss", "aud", "jti"})


class IdentityError(Exception):
    """A declared principal input cannot be filled from the identity on the session."""


class TokenError(Exception):
    """A session token that does not hold up. `check` names the check it failed.

    The message never carries the token or any part of it, so it may be shown
    to the caller and written to a log.
    """

    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


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


def mint_token(claims: Mapping[str, object], key: bytes | str) -> str:
    """A compact HS256 token over these claims.

    This is for tests and for an operator's own tooling. The runtime verifies
    tokens and never mints the ones it accepts: whoever issues a token is the
    party that authenticated the person, and that is not the runtime.
    """
    header = _encode_part({"alg": TOKEN_ALGORITHM, "typ": "JWT"})
    payload = _encode_part(dict(claims))
    signed = f"{header}.{payload}"
    return f"{signed}.{_b64encode(_mac(key, signed))}"


def verify_token(
    token: str,
    key: bytes | str,
    *,
    session_id: str | None,
    required_claims: Sequence[str] = ("sub", "sid", "exp"),
    leeway_seconds: int = 0,
    now: float | None = None,
) -> dict:
    """The claims of a token that holds up, or `TokenError` naming the check it failed.

    The signature is compared in constant time before any claim is read. `alg`
    must be HS256; `none` and every other value are refused rather than
    honored. Every required claim must be present, so a token minted without
    an expiry is refused instead of living forever. `exp` and `nbf` are held to
    the clock with `leeway_seconds` of tolerance, and `sid` must name the
    session of the call, so a token is good for one session and no other.

    `session_id` of `None` is a call that acts in no session, such as reading
    what a catalog offers: the token is verified in every other respect and
    the session is not compared. An empty string is a session call that named
    no session, and is refused.
    """
    try:
        header_part, payload_part, signature_part = token.split(".")
    except ValueError:
        header_part = payload_part = signature_part = ""
    if not header_part or not payload_part:
        raise TokenError("format", "a session token is three base64url parts joined by dots")
    header = _decode_part(header_part, "header")
    if header.get("alg") != TOKEN_ALGORITHM:
        raise TokenError("alg", f"only {TOKEN_ALGORITHM} session tokens are accepted")
    try:
        given = _b64decode(signature_part)
    except ValueError:
        given = b""
    if not hmac.compare_digest(_mac(key, f"{header_part}.{payload_part}"), given):
        raise TokenError("signature", "the signature does not match the token")
    claims = _decode_part(payload_part, "payload")
    for name in required_claims:
        if name not in claims:
            raise TokenError("claims", f"the token carries no {name} claim, which is required")
    moment = time.time() if now is None else now
    expires = _instant(claims, "exp")
    if expires is not None and moment > expires + leeway_seconds:
        raise TokenError("exp", "the token has expired")
    begins = _instant(claims, "nbf")
    if begins is not None and moment + leeway_seconds < begins:
        raise TokenError("nbf", "the token is not valid yet")
    if session_id is not None:
        if not session_id:
            raise TokenError("sid", "the token is bound to a session, and this call names none")
        if claims.get("sid") != session_id:
            raise TokenError("sid", "the token was issued for another session")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise TokenError("sub", "the token names no subject")
    return claims


def principal_from_claims(claims: Mapping[str, object], *, tenant_claim: str) -> Principal:
    """The identity a verified token establishes.

    The subject is `sub`. Every claim that is not a registered one travels as
    an attribute, so a procedure can bind a field to it, and the claim the
    deployment names as the tenant is also carried as the `tenant` attribute.
    """
    attributes = {
        name: value for name, value in claims.items() if name not in REGISTERED_CLAIMS
    }
    if tenant_claim and tenant_claim in claims:
        attributes[TENANT] = claims[tenant_claim]
    return Principal(subject=str(claims["sub"]), attributes=attributes, authenticated=True)


def _mac(key: bytes | str, signed: str) -> bytes:
    material = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(material, signed.encode("ascii"), hashlib.sha256).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Unpadded base64url, strictly: a character outside the alphabet is an error."""
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("not base64url") from exc
    for padding in (b"", b"=", b"=="):
        try:
            return base64.b64decode(raw + padding, altchars=b"-_", validate=True)
        except binascii.Error:
            continue
    raise ValueError("not base64url")


def _encode_part(document: Mapping[str, object]) -> str:
    return _b64encode(json.dumps(document, separators=(",", ":"), sort_keys=True).encode())


def _decode_part(text: str, part: str) -> dict:
    try:
        document = json.loads(_b64decode(text))
    except ValueError as exc:
        raise TokenError("format", f"the token {part} is not base64url JSON") from exc
    if not isinstance(document, dict):
        raise TokenError("format", f"the token {part} is not a JSON object")
    return document


def _instant(claims: Mapping[str, object], name: str) -> float | None:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TokenError(name, f"the {name} claim is not a number of seconds")
    return float(value)
