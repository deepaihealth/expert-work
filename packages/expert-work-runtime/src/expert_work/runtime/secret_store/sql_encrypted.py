"""Encrypted-secret vault — Stream Q (Mini-ADR Q-1/Q-2/Q-7).

A :class:`~expert_work.runtime.secret_store.base.SecretStore` backend that
stores secret **values** AES-256-GCM-encrypted at rest in the
``encrypted_secret`` table, so a platform admin can paste a raw provider key in
the web UI and have it usable by agents without ever writing plaintext to the
DB or a file.

B-286 —— moved here from ``control_plane.encrypted_secret_store`` (which now
re-exports it) so the retention-cleanup job resolves the same ``secret://``
object-store key refs as the control plane without importing control-plane or
duplicating the crypto. The ORM row + migration live in expert-work-persistence.

Security (Mini-ADR Q-7): values are handled as :class:`pydantic.SecretStr` at
the API boundary and passed to :meth:`put` as plain ``str`` only at the encrypt
call site; this module never logs a value. The KEK is held once as ``bytes``.
A fresh 96-bit nonce is drawn per encryption (``os.urandom(12)``); the AAD binds
the row ``name`` so ciphertext cannot be replayed under a different name.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import EncryptedSecretRow
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.runtime.secret_store.base import SecretNotFoundError, SecretStoreError

#: The single env-sourced KEK version tag stamped on rows it encrypts. A real
#: KMS-wrapped KEK (prod follow-up) bumps this so the decrypt path can dispatch
#: on it during rotation (Mini-ADR Q-2).
ENV_KEK_VERSION = "env-v1"

_NONCE_BYTES = 12
_KEK_BYTES = 32


@asynccontextmanager
async def bypass_rls_session() -> AsyncIterator[None]:
    """Platform rows (``tenant_id IS NULL``) have no tenant scope: flip ``bypass_rls_var`` for the
    body (same contract as ``control_plane.tenant_scope.bypass_rls_session``)."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


def build_kek_from_b64(value: str) -> bytes:
    """Decode a base64 KEK and assert it is exactly 32 bytes (AES-256).

    Raises :class:`ValueError` (fail-loud at boot) on a malformed or
    wrong-length key — a silently-truncated KEK would be a security hole.
    """
    try:
        kek = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        msg = "EXPERT_WORK_SECRET_ENCRYPTION_KEY is not valid base64"
        raise ValueError(msg) from exc
    if len(kek) != _KEK_BYTES:
        msg = f"secret encryption KEK must be {_KEK_BYTES} bytes (got {len(kek)})"
        raise ValueError(msg)
    return kek


class EnvelopeCipher:
    """AES-256-GCM encrypt/decrypt with the row ``name`` bound as AAD."""

    def __init__(self, kek: bytes) -> None:
        if len(kek) != _KEK_BYTES:
            msg = f"KEK must be {_KEK_BYTES} bytes (got {len(kek)})"
            raise ValueError(msg)
        self._aead = AESGCM(kek)

    def encrypt(self, *, name: str, plaintext: str) -> tuple[bytes, bytes]:
        """Return ``(nonce, ciphertext)``. Nonce is fresh per call."""
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode("utf-8"), name.encode("utf-8"))
        return nonce, ciphertext

    def decrypt(self, *, name: str, nonce: bytes, ciphertext: bytes) -> str:
        """Inverse of :meth:`encrypt`. Raises on a wrong KEK / tampered row."""
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, name.encode("utf-8"))
        except InvalidTag as exc:
            msg = f"failed to decrypt secret {name!r} — wrong KEK or tampered ciphertext"
            raise SecretStoreError(msg) from exc
        return plaintext.decode("utf-8")


class SqlEncryptedSecretStore:
    """Postgres-backed envelope-encrypted ``SecretStore`` (platform-scoped).

    This iteration only writes platform-global secrets (``tenant_id IS NULL``),
    so every operation runs inside ``bypass_rls_session()`` — exactly like
    ``SqlPlatformSecretStore`` (the NULL-tenant rows have no RLS scope to
    satisfy). Tenant-scoped secrets are a later iteration (Mini-ADR Q-3).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        kek: bytes,
        kek_version: str = ENV_KEK_VERSION,
    ) -> None:
        self._sf = session_factory
        self._cipher = EnvelopeCipher(kek)
        self._kek_version = kek_version

    async def get(self, name: str, *, version: str | None = None) -> str:
        async with bypass_rls_session(), self._sf() as session:
            stmt = select(EncryptedSecretRow).where(
                EncryptedSecretRow.tenant_id.is_(None),
                EncryptedSecretRow.name == name,
            )
            stmt = (
                stmt.where(EncryptedSecretRow.version == version)
                if version is not None
                else stmt.where(EncryptedSecretRow.is_current.is_(True))
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise SecretNotFoundError(name if version is None else f"{name}@{version}")
        return self._cipher.decrypt(name=name, nonce=row.nonce, ciphertext=row.ciphertext)

    async def put(self, name: str, value: str) -> None:
        nonce, ciphertext = self._cipher.encrypt(name=name, plaintext=value)
        async with bypass_rls_session(), self._sf() as session:
            # Demote the prior current version (if any) before inserting the new
            # one — the partial unique index permits only one current per name.
            await session.execute(
                update(EncryptedSecretRow)
                .where(
                    EncryptedSecretRow.tenant_id.is_(None),
                    EncryptedSecretRow.name == name,
                    EncryptedSecretRow.is_current.is_(True),
                )
                .values(is_current=False)
            )
            session.add(
                EncryptedSecretRow(
                    id=uuid4(),
                    tenant_id=None,
                    name=name,
                    version=uuid4().hex,
                    ciphertext=ciphertext,
                    nonce=nonce,
                    kek_version=self._kek_version,
                    is_current=True,
                    created_by="platform",
                )
            )
            await session.commit()

    async def list_versions(self, name: str) -> list[str]:
        async with bypass_rls_session(), self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(EncryptedSecretRow.version)
                        .where(
                            EncryptedSecretRow.tenant_id.is_(None),
                            EncryptedSecretRow.name == name,
                        )
                        .order_by(EncryptedSecretRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            raise SecretNotFoundError(name)
        return list(rows)

    async def delete(self, name: str) -> None:
        """Hard-delete every version row of the secret ``name``.

        Idempotent — deleting an absent name does NOT raise.
        """
        async with bypass_rls_session(), self._sf() as session:
            await session.execute(
                delete(EncryptedSecretRow).where(
                    EncryptedSecretRow.tenant_id.is_(None),
                    EncryptedSecretRow.name == name,
                )
            )
            await session.commit()
