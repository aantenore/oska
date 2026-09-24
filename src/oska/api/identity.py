"""Who is calling: the one place a channel turns a request into a principal.

Two modes, chosen by `IdentityConfig.mode`. In `proxy` mode an authenticated
proxy in front of the runtime sets headers and the runtime takes them as given:
it verifies nothing itself, and saying so here is the honest description. In
`token` mode the caller presents a signed session token and the runtime verifies
it before anything else runs.
"""

from __future__ import annotations

import json

from fastapi import Request

from oska.api.errors import ApiError
from oska.config import IdentityConfig
from oska.governance.identity import Principal

__all__ = ["ATTRIBUTES_HEADER", "SUBJECT_HEADER", "resolve_principal"]

SUBJECT_HEADER = "X-Principal-Subject"
ATTRIBUTES_HEADER = "X-Principal-Attributes"


def resolve_principal(
    request: Request, config: IdentityConfig, *, session_id: str = ""
) -> Principal | None:
    """The identity this call carries, or nothing when it carries none."""
    if config.mode == "token":
        raise ApiError(501, "identity_mode_unavailable", "token identity is not wired yet")
    return _from_proxy(request)


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
