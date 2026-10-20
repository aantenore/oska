"""Who is calling: the one place a channel turns a request into a principal.

Two modes, chosen by `IdentityConfig.mode`. In `proxy` mode an authenticated
proxy in front of the runtime sets headers and the runtime takes them as given;
what it can verify is that the proxy sent them, when the configuration names a
secret the proxy adds to every call (`proxy_secret_env`). Without one, anyone
who reaches the port around the proxy can set the same headers. In
`token` mode the caller presents a signed session token as
`Authorization: Bearer <token>` and the runtime verifies it before anything else
runs: the signature against the key named by `IdentityConfig.key_env`, the
algorithm, the required claims, the validity window and the session the token
was issued for. A token that fails any of those is a 401 naming the check, and
the token itself is never repeated back, except the session check: a token that
holds up but was issued for another session is a 403, worded as every other
refusal of a session is, so it says nothing about the session it names.

A principal is a reviewer when its `roles` attribute, a list, holds the role
`IdentityConfig.reviewer_role` names. In `token` mode that attribute is the
`roles` claim; in `proxy` mode it is the `roles` entry of the attributes header.
A call without an identity does a reviewer's work only where
`IdentityConfig.single_user` says the one who reaches the runtime without an
identity is its one user; anywhere else it is never worth more than an identity
that lacks the role.

The runtime configuration an application answers by is read once, when it is
first needed, and kept on the application: an identity check on every request
does not read a file on every request.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping

from fastapi import Request

from oska.api.errors import ApiError
from oska.config import IdentityConfig, RuntimeConfig, load_runtime_config
from oska.governance.identity import (
    Principal,
    TokenError,
    principal_from_claims,
    verify_token,
)

__all__ = [
    "ATTRIBUTES_HEADER",
    "AUTHORIZATION_HEADER",
    "ROLES_ATTRIBUTE",
    "SUBJECT_HEADER",
    "application_runtime",
    "check_proxy_secret",
    "identity_config",
    "identity_required",
    "is_reviewer",
    "may_review",
    "refuse_review",
    "resolve_principal",
    "reviewer_required",
    "session_refused",
]

SUBJECT_HEADER = "X-Principal-Subject"
ATTRIBUTES_HEADER = "X-Principal-Attributes"
AUTHORIZATION_HEADER = "Authorization"
BEARER = "bearer"
SID_CHECK = "sid"
ROLES_ATTRIBUTE = "roles"


def session_refused() -> ApiError:
    """The one refusal of a call that may not act in a session.

    It is worded the same whatever the reason: another subject opened the
    session, the token was issued for another session, or the session belongs
    to another domain. A caller learns nothing about a session it cannot use.
    """
    return ApiError(403, "session_refused", "this call may not act in this session")


def identity_required() -> ApiError:
    """The refusal of a call that carries no identity where one is required."""
    return ApiError(401, "identity_required", "this runtime answers only a call with an identity")


def reviewer_required(role: str) -> ApiError:
    """The refusal of a call whose identity lacks the reviewer role. It names the role only."""
    return ApiError(403, "reviewer_required", f"this call needs the {role} role")


def is_reviewer(principal: Principal | None, config: IdentityConfig) -> bool:
    """Whether this principal carries the reviewer role in its `roles` attribute."""
    if principal is None or not config.reviewer_role:
        return False
    roles = principal.attributes.get(ROLES_ATTRIBUTE)
    if not isinstance(roles, list | tuple):
        return False
    return config.reviewer_role in roles


def may_review(principal: Principal | None, config: IdentityConfig) -> bool:
    """Whether this caller may do a reviewer's work.

    A principal may when it carries the reviewer role. A call without an
    identity may only in a single-user deployment, where it is that one user.
    """
    if principal is None:
        return config.single_user
    return is_reviewer(principal, config)


def refuse_review(principal: Principal | None, config: IdentityConfig) -> ApiError:
    """The refusal of a caller that may not do a reviewer's work.

    A call without an identity is told an identity is needed, one with an
    identity that the role is: neither says anything else.
    """
    if principal is None:
        return identity_required()
    return reviewer_required(config.reviewer_role)


def application_runtime(request: Request) -> RuntimeConfig:
    """The runtime configuration this application answers by, read once and kept.

    A platform's is the platform's own, before any tenant fragment; a single
    catalog's is the file its settings name. Either is held on the application
    after the first read.
    """
    state = request.app.state
    held: RuntimeConfig | None = getattr(state, "runtime", None)
    if held is None:
        platform = getattr(state, "platform", None)
        held = (
            platform.runtime
            if platform is not None
            else load_runtime_config(settings=state.services.settings)
        )
        state.runtime = held
    return held


def identity_config(request: Request) -> IdentityConfig:
    """How this application reads a caller's identity. A tenant fragment never changes it."""
    return application_runtime(request).identity


def resolve_principal(
    request: Request,
    config: IdentityConfig,
    *,
    session_id: str | None = "",
    env: Mapping[str, str] | None = None,
) -> Principal | None:
    """The identity this call carries, or nothing when it carries none.

    In `token` mode `session_id` is the session the call is for, and a token
    issued for any other session, or a session call that names none, is
    refused. `None` marks a call that acts in no session: the token is still
    verified, its session is not compared. `env`
    is where the verification key is read from; the process environment when
    it is not given.
    """
    source = os.environ if env is None else env
    if config.mode == "token":
        return _from_token(request, config, session_id, source)
    check_proxy_secret(request, config, source)
    return _from_proxy(request)


def check_proxy_secret(
    request: Request, config: IdentityConfig, env: Mapping[str, str] | None = None
) -> None:
    """Refuse identity headers that did not come through the proxy, when a secret is named.

    A call that carries none of the headers the proxy owns is a call without an
    identity, and is judged as one; a call that carries any of them must also
    carry the secret, compared in constant time.
    """
    variable = config.proxy_secret_env.strip()
    if config.mode != "proxy" or not variable:
        return
    env = os.environ if env is None else env
    owned = [SUBJECT_HEADER, ATTRIBUTES_HEADER, config.tenant_header.strip()]
    if not any(name and request.headers.get(name, "").strip() for name in owned):
        return
    secret = str(env.get(variable, ""))
    if not secret:
        raise ApiError(
            500,
            "identity_key_missing",
            f"proxy identity reads its secret from {variable}, which is not set",
        )
    presented = request.headers.get(config.proxy_secret_header, "")
    if not hmac.compare_digest(presented.encode(), secret.encode()):
        raise ApiError(
            401,
            "proxy_unverified",
            "the identity headers of this call did not come with the proxy's secret",
        )


def _from_token(
    request: Request, config: IdentityConfig, session_id: str | None, env: Mapping[str, str]
) -> Principal | None:
    """The identity a verified bearer token establishes, or nothing when none is presented."""
    header = request.headers.get(AUTHORIZATION_HEADER, "").strip()
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != BEARER or not token.strip():
        raise ApiError(401, "invalid_token", f"{AUTHORIZATION_HEADER} is not a bearer token")
    key = str(env.get(config.key_env, ""))
    if not key:
        raise ApiError(
            500,
            "identity_key_missing",
            f"token identity reads its key from {config.key_env}, which is not set",
        )
    try:
        claims = verify_token(
            token.strip(),
            key,
            session_id=session_id,
            required_claims=config.required_claims,
            leeway_seconds=config.leeway_seconds,
        )
    except TokenError as exc:
        if exc.check == SID_CHECK:
            raise session_refused() from None
        raise ApiError(
            401, "invalid_token", f"the session token failed the {exc.check} check: {exc}"
        ) from None
    return principal_from_claims(claims, tenant_claim=config.tenant_claim)


def _from_proxy(request: Request) -> Principal | None:
    """The identity the proxy put on this call.

    A subject alone is an identity; the attributes header is optional and is a
    JSON object when it is there. Both are taken as given: this is the point
    where a value the runtime did not establish becomes the value a write is
    bound to.
    """
    subject = request.headers.get(SUBJECT_HEADER, "").strip()
    if not subject:
        return None
    raw = request.headers.get(ATTRIBUTES_HEADER, "").strip()
    attributes: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ApiError(400, "invalid_principal", f"{ATTRIBUTES_HEADER} is not JSON") from exc
        if not isinstance(parsed, dict):
            raise ApiError(400, "invalid_principal", f"{ATTRIBUTES_HEADER} is not a JSON object")
        attributes = parsed
    return Principal(subject=subject, attributes=attributes, authenticated=True)
