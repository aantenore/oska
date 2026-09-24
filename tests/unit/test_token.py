"""A signed session token: what it must carry, and every way it is refused."""

from __future__ import annotations

import base64
import json

import pytest
from starlette.requests import Request

import oska.governance.identity as identity
from oska.api.errors import ApiError
from oska.api.identity import AUTHORIZATION_HEADER, resolve_principal
from oska.config import IdentityConfig
from oska.governance.identity import (
    TokenError,
    mint_token,
    principal_from_claims,
    verify_token,
)

KEY = "a-key-kept-in-the-environment"
KEY_ENV = "OSKA_TEST_SESSION_KEY"
SESSION = "session-one"
NOW = 1_800_000_000.0
REQUIRED = ("sub", "sid", "exp")


def _claims(**overrides: object) -> dict:
    claims: dict = {"sub": "tnovak", "sid": SESSION, "exp": NOW + 600, "tenant": "north"}
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _verify(token: str, *, key: str = KEY, session_id: str = SESSION, leeway: int = 0) -> dict:
    return verify_token(
        token,
        key,
        session_id=session_id,
        required_claims=REQUIRED,
        leeway_seconds=leeway,
        now=NOW,
    )


def _part(document: dict) -> str:
    raw = json.dumps(document, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _failed(token: str, **kwargs: object) -> str:
    with pytest.raises(TokenError) as caught:
        _verify(token, **kwargs)  # type: ignore[arg-type]
    assert token not in str(caught.value)
    return caught.value.check


def test_a_valid_token_gives_its_claims() -> None:
    claims = _verify(mint_token(_claims(), KEY))
    assert claims["sub"] == "tnovak"
    assert claims["sid"] == SESSION


def test_verification_compares_the_signature_through_hmac_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSKA-GOV-38: the signature check goes through `hmac.compare_digest`, called
    with exactly the computed MAC and the signature the token carries.

    This proves the comparison uses the constant-time primitive and the order
    of its two arguments; it cannot prove the comparison itself runs in
    constant time, which is a property of `hmac.compare_digest`, not of this
    call site.
    """
    calls: list[tuple[bytes, bytes]] = []
    real_compare = identity.hmac.compare_digest

    def _spy(computed: bytes, given: bytes) -> bool:
        calls.append((computed, given))
        return real_compare(computed, given)

    monkeypatch.setattr(identity.hmac, "compare_digest", _spy)

    token = mint_token(_claims(), KEY)
    header_part, payload_part, signature_part = token.split(".")
    claims = _verify(token)

    assert claims["sub"] == "tnovak"
    assert len(calls) == 1
    computed, given = calls[0]
    assert computed == identity._mac(KEY, f"{header_part}.{payload_part}")
    assert given == identity._b64decode(signature_part)


def test_an_expired_token_is_refused_past_the_leeway_only() -> None:
    token = mint_token(_claims(exp=NOW - 10), KEY)
    assert _failed(token) == "exp"
    assert _verify(token, leeway=30)["sub"] == "tnovak"


def test_a_token_not_yet_valid_is_refused() -> None:
    assert _failed(mint_token(_claims(nbf=NOW + 120), KEY)) == "nbf"


def test_a_token_for_another_session_is_refused() -> None:
    token = mint_token(_claims(), KEY)
    assert _failed(token, session_id="session-two") == "sid"
    assert _failed(token, session_id="") == "sid"


def test_a_token_without_a_required_claim_is_refused() -> None:
    assert _failed(mint_token(_claims(exp=None), KEY)) == "claims"


def test_alg_none_is_refused() -> None:
    header = _part({"alg": "none", "typ": "JWT"})
    token = f"{header}.{_part(_claims())}."
    assert _failed(token) == "alg"


def test_another_algorithm_is_refused_even_when_signed_with_the_key() -> None:
    minted = mint_token(_claims(), KEY)
    _, payload, signature = minted.split(".")
    token = f"{_part({'alg': 'HS512', 'typ': 'JWT'})}.{payload}.{signature}"
    assert _failed(token) == "alg"


def test_a_tampered_payload_is_refused() -> None:
    header, _, signature = mint_token(_claims(), KEY).split(".")
    forged = f"{header}.{_part(_claims(sub='someone-else'))}.{signature}"
    assert _failed(forged) == "signature"


def test_a_token_signed_with_another_key_is_refused() -> None:
    assert _failed(mint_token(_claims(), "another-key")) == "signature"


def test_a_malformed_token_is_refused() -> None:
    assert _failed("not-a-token") == "format"
    assert _failed("a.b") == "format"


def test_the_tenant_travels_as_an_attribute() -> None:
    principal = principal_from_claims(_claims(org="north", team="desk"), tenant_claim="org")
    assert principal.subject == "tnovak"
    assert principal.authenticated
    assert principal.attributes["tenant"] == "north"
    assert principal.attributes["team"] == "desk"
    assert "sid" not in principal.attributes
    assert "exp" not in principal.attributes


def _request(authorization: str | None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((AUTHORIZATION_HEADER.lower().encode(), authorization.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def _config() -> IdentityConfig:
    return IdentityConfig(mode="token", key_env=KEY_ENV, leeway_seconds=0)


def test_token_mode_resolves_a_bearer_token_into_a_principal() -> None:
    token = mint_token(_claims(exp=4_000_000_000), KEY)
    principal = resolve_principal(
        _request(f"Bearer {token}"), _config(), session_id=SESSION, env={KEY_ENV: KEY}
    )
    assert principal is not None
    assert principal.subject == "tnovak"
    assert principal.attributes["tenant"] == "north"


def test_token_mode_refuses_with_a_401_that_names_the_check() -> None:
    token = mint_token(_claims(exp=1), KEY)
    with pytest.raises(ApiError) as caught:
        resolve_principal(
            _request(f"Bearer {token}"), _config(), session_id=SESSION, env={KEY_ENV: KEY}
        )
    assert caught.value.status_code == 401
    assert "exp" in caught.value.detail
    assert token not in caught.value.detail


def test_a_token_for_another_session_is_refused_without_naming_either() -> None:
    token = mint_token(_claims(exp=4_000_000_000), KEY)
    with pytest.raises(ApiError) as caught:
        resolve_principal(
            _request(f"Bearer {token}"), _config(), session_id="other", env={KEY_ENV: KEY}
        )
    assert (caught.value.status_code, caught.value.error) == (403, "session_refused")
    assert SESSION not in caught.value.detail and "other" not in caught.value.detail
    assert token not in caught.value.detail


def test_token_mode_without_a_token_carries_no_identity() -> None:
    assert resolve_principal(_request(None), _config(), session_id=SESSION, env={}) is None


def test_token_mode_refuses_a_header_that_is_not_a_bearer_token() -> None:
    with pytest.raises(ApiError) as caught:
        resolve_principal(
            _request("Basic abc"), _config(), session_id=SESSION, env={KEY_ENV: KEY}
        )
    assert caught.value.status_code == 401


def test_token_mode_without_its_key_is_a_deployment_error() -> None:
    token = mint_token(_claims(exp=4_000_000_000), KEY)
    with pytest.raises(ApiError) as caught:
        resolve_principal(_request(f"Bearer {token}"), _config(), session_id=SESSION, env={})
    assert caught.value.status_code == 500
    assert KEY_ENV in caught.value.detail


def test_a_call_that_acts_in_no_session_verifies_the_token_without_comparing_it() -> None:
    """Reading what a catalog offers acts in no session; the token still has to hold up."""
    from oska.governance.identity import TokenError, mint_token, verify_token

    key = "k" * 32
    token = mint_token({"sub": "someone", "sid": "session-a", "exp": 4_102_444_800}, key)
    claims = verify_token(token, key, session_id=None, required_claims=("sub", "sid", "exp"))
    assert claims["sub"] == "someone"
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    try:
        verify_token(tampered, key, session_id=None, required_claims=("sub", "sid", "exp"))
    except TokenError as exc:
        assert exc.check == "signature"
    else:
        raise AssertionError("a tampered token was accepted on a call that acts in no session")
    try:
        verify_token(token, key, session_id="", required_claims=("sub", "sid", "exp"))
    except TokenError as exc:
        assert exc.check == "sid"
    else:
        raise AssertionError("a session call naming no session was accepted")
