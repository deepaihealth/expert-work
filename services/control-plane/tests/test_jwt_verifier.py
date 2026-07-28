"""Unit tests for :class:`control_plane.auth.JWTVerifier`."""

from __future__ import annotations

import time
from uuid import UUID

import jwt as pyjwt
import pytest

from control_plane.auth import (
    InvalidTokenError,
    JWTVerifier,
    StaticJWKSProvider,
    TokenExpiredError,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    TEST_KID,
    _keypair,
    build_test_jwks_provider,
    build_test_jwt_verifier,
    make_test_jwt,
)


def _verifier() -> JWTVerifier:
    return build_test_jwt_verifier()


@pytest.mark.asyncio
async def test_verify_happy_path() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(tenant_id=tenant, subject="alice", roles=("operator",))
    claims = await _verifier().verify(token)
    assert claims.tenant_id == tenant
    assert claims.sub == "alice"
    assert claims.roles == ("operator",)
    assert claims.iss == TEST_ISSUER
    assert TEST_AUDIENCE in claims.aud


@pytest.mark.asyncio
async def test_expired_token_raises_token_expired() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    # Default leeway is 30s — push the expiry beyond that.
    token = make_test_jwt(tenant_id=tenant, ttl_s=-120)
    with pytest.raises(TokenExpiredError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_bad_signature_raises_invalid_token() -> None:
    # Tamper with the signature segment. Flip a character in the middle of
    # the segment (every bit there is signature-significant) and guarantee it
    # differs from the original — replacing the *trailing* chars with a fixed
    # value is flaky: the last base64url char carries only 2 effective bits,
    # so ~1/256 signatures already end in the replacement value.
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(tenant_id=tenant)
    head, body, sig = token.split(".")
    mid = len(sig) // 2
    flipped = "A" if sig[mid] != "A" else "B"
    tampered = ".".join([head, body, sig[:mid] + flipped + sig[mid + 1 :]])
    with pytest.raises(InvalidTokenError):
        await _verifier().verify(tampered)


@pytest.mark.asyncio
async def test_wrong_issuer_raises_invalid_token() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(tenant_id=tenant, issuer="http://impostor.test/realms/foo")
    with pytest.raises(InvalidTokenError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_wrong_audience_raises_invalid_token() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(tenant_id=tenant, audience="other-audience")
    with pytest.raises(InvalidTokenError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_missing_kid_header_raises_invalid_token() -> None:
    private_key, _, _ = _keypair()
    now = int(time.time())
    payload = {
        "iss": TEST_ISSUER,
        "sub": "x",
        "aud": TEST_AUDIENCE,
        "exp": now + 60,
        "tenant_id": "00000000-0000-0000-0000-000000000000",
    }
    token = pyjwt.encode(payload, private_key, algorithm="RS256")  # no kid
    with pytest.raises(InvalidTokenError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_unknown_kid_raises_invalid_token() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(tenant_id=tenant, kid="unknown-kid")
    with pytest.raises(InvalidTokenError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_missing_tenant_id_raises_invalid_token() -> None:
    """Verified-but-meaningless tokens (no tenant claim) are rejected."""
    private_key, _, _ = _keypair()
    now = int(time.time())
    payload = {
        "iss": TEST_ISSUER,
        "sub": "x",
        "aud": TEST_AUDIENCE,
        "exp": now + 60,
    }
    token = pyjwt.encode(payload, private_key, algorithm="RS256", headers={"kid": TEST_KID})
    with pytest.raises(InvalidTokenError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_static_provider_requires_at_least_one_key() -> None:
    with pytest.raises(ValueError):
        StaticJWKSProvider({})


@pytest.mark.asyncio
async def test_verifier_requires_non_empty_audience() -> None:
    provider = build_test_jwks_provider()
    with pytest.raises(ValueError):
        JWTVerifier(jwks_provider=provider, issuer="x", audience=())


@pytest.mark.asyncio
async def test_realm_access_roles_extracted_when_top_level_roles_missing() -> None:
    """Keycloak nests realm roles under ``realm_access.roles``."""
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(
        tenant_id=tenant,
        roles=(),
        extra_claims={"realm_access": {"roles": ["admin", "operator"]}},
    )
    claims = await _verifier().verify(token)
    assert set(claims.roles) == {"admin", "operator"}


@pytest.mark.asyncio
async def test_scope_space_separated_string_is_parsed() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(
        tenant_id=tenant,
        extra_claims={"scope": "manifest:read manifest:write"},
    )
    claims = await _verifier().verify(token)
    assert set(claims.scopes) == {"manifest:read", "manifest:write"}


@pytest.mark.asyncio
async def test_email_and_verified_flag_parsed() -> None:
    """Stream ACCT — email + email_verified flow through for first-login bootstrap."""
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(
        tenant_id=tenant,
        extra_claims={"email": "Admin@Corp.com", "email_verified": True},
    )
    claims = await _verifier().verify(token)
    assert claims.email == "Admin@Corp.com"
    assert claims.email_verified is True


@pytest.mark.asyncio
async def test_email_absent_defaults_to_none_and_unverified() -> None:
    tenant = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    token = make_test_jwt(tenant_id=tenant)
    claims = await _verifier().verify(token)
    assert claims.email is None
    assert claims.email_verified is False
