"""Shared test infrastructure for JWT-authenticated control-plane tests.

Each test process generates **one** RSA keypair (cached by the
:func:`_keypair` singleton). Tests then:

* Build a :class:`StaticJWKSProvider` from the public key
* Sign JWTs with the private key via :func:`make_test_jwt`
* Construct a :class:`JWTVerifier` over the static provider

This keeps unit tests fast and self-contained — there is no Keycloak
container in the inner-loop test run. The C.1 integration suite (not
included in this PR's CI gate) optionally hits a real Keycloak.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from jwt import PyJWK

from control_plane.auth import JWTVerifier, StaticJWKSProvider
from expert_work.protocol import Role

TEST_ISSUER = "http://keycloak.test/realms/expert-work"
TEST_AUDIENCE = "expert-work-api-internal"
TEST_KID = "test-kid-1"


@lru_cache(maxsize=1)
def _keypair() -> tuple[rsa.RSAPrivateKey, str, str]:
    """Generate a deterministic-but-not-fixed RSA keypair for the test process.

    Returns ``(private_key, public_pem, private_pem)``. The PEM strings
    are kept for diagnostic printing in CI logs if a sign / verify ever
    diverges; tests themselves should pass the ``PyJWK`` from
    :func:`build_test_jwks_provider`.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return key, public_pem, private_pem


def build_test_jwks_provider() -> StaticJWKSProvider:
    """Return a :class:`StaticJWKSProvider` seeded with the test public key."""
    _, public_pem, _ = _keypair()
    pyjwk = PyJWK.from_dict(
        {
            "kty": "RSA",
            "use": "sig",
            "kid": TEST_KID,
            "alg": "RS256",
            **_jwk_components(public_pem),
        }
    )
    return StaticJWKSProvider({TEST_KID: pyjwk})


def build_test_jwt_verifier(
    *,
    issuer: str = TEST_ISSUER,
    audience: str | Sequence[str] = TEST_AUDIENCE,
) -> JWTVerifier:
    """Return a verifier wired to the process-wide test keypair."""
    audience_seq: Sequence[str]
    if isinstance(audience, str):
        audience_seq = (audience,)
    else:
        audience_seq = tuple(audience)
    return JWTVerifier(
        jwks_provider=build_test_jwks_provider(),
        issuer=issuer,
        audience=audience_seq,
    )


def make_test_jwt(
    *,
    tenant_id: UUID,
    subject: str = "dev-user",
    sub_type: str = "user",
    roles: Iterable[str] = ("admin",),
    scopes: Iterable[str] = (),
    issuer: str = TEST_ISSUER,
    audience: str | Sequence[str] = TEST_AUDIENCE,
    ttl_s: int = 3600,
    jti: str | None = None,
    extra_claims: Mapping[str, Any] | None = None,
    kid: str = TEST_KID,
    algorithm: str = "RS256",
) -> str:
    """Sign a JWT with the test private key. Use only inside tests."""
    private_key, _, _ = _keypair()
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": list(audience) if not isinstance(audience, str) else audience,
        "iat": now,
        "exp": now + ttl_s,
        "jti": jti or uuid4().hex,
        "tenant_id": str(tenant_id),
        "sub_type": sub_type,
        "roles": list(roles),
        "scopes": list(scopes),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(
        payload,
        private_key,
        algorithm=algorithm,
        headers={"kid": kid},
    )


async def grant_system_admin_on(app: object) -> dict[str, str]:
    """Seed a platform-scope binding; return headers for a system_admin whose
    HOME tenant differs from the tenant under test."""
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    token = make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))
    return {"Authorization": f"Bearer {token}"}


async def grant_system_admin(client: AsyncClient) -> dict[str, str]:
    """:func:`grant_system_admin_on` flavored for an ASGI-transport client."""
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    return await grant_system_admin_on(app)


def _jwk_components(public_pem: str) -> dict[str, str]:
    """Extract the JWK ``n`` / ``e`` components from a PEM-encoded public key."""
    public_key = serialization.load_pem_public_key(public_pem.encode())
    numbers = public_key.public_numbers()  # type: ignore[union-attr]
    return {
        "n": _int_to_b64url(numbers.n),
        "e": _int_to_b64url(numbers.e),
    }


def _int_to_b64url(value: int) -> str:
    import base64

    byte_length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
