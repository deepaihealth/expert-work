"""Platform provider/tool secret-ref records — Stream P (Mini-ADR P-7/P-8).

Runtime-managed platform credential refs (the DB overlay over the env seed).
Values are **always** ``secret://`` / ``kms://`` references — never plaintext
keys; :func:`validate_secret_ref` enforces this so plaintext can never reach
the DB, the audit log, or a response body (Mini-ADR P-8).

Naming: the harness blocks ``credentials`` paths, so this is ``platform_secret``
rather than the design's ``platform_credential`` — same surface.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from expert_work.protocol.provider_catalog import Provider, Tool

_REF_PREFIXES = ("secret://", "kms://")


def validate_secret_ref(value: str) -> str:
    """Reject anything that is not a ``secret://`` / ``kms://`` reference."""
    if not value.startswith(_REF_PREFIXES):
        msg = "secret_ref must be a secret:// or kms:// reference, never a plaintext key"
        raise ValueError(msg)
    return value


class PlatformProviderSecretRecord(BaseModel):
    """A platform-managed LLM provider credential (secret ref), as stored.

    Stream Y-MK — a provider may hold **multiple keys** (separate billing
    accounts / rate-limit pools). ``key_id`` disambiguates siblings within a
    provider (primary key is ``(provider, key_id)``); ``priority`` orders
    failover (lower = tried first). Legacy single-key rows carry
    ``key_id="default"``.
    """

    model_config = ConfigDict(extra="forbid")
    provider: Provider
    key_id: str = "default"
    secret_ref: str
    enabled: bool = True
    priority: int = 100
    #: B-308 — the request address for this key (API root, version segment
    #: included). ``None`` = the provider's official address.
    base_url: str | None = None
    #: B-308 — saved although the connectivity check failed ("save anyway").
    base_url_unverified: bool = False
    created_at: datetime
    updated_at: datetime
    updated_by: str

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return validate_secret_ref(value)


class PlatformToolSecretRecord(BaseModel):
    """A platform-managed external-tool credential (secret ref), as stored."""

    model_config = ConfigDict(extra="forbid")
    tool: Tool
    secret_ref: str
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    updated_by: str

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return validate_secret_ref(value)


class TenantProviderSecretRecord(BaseModel):
    """A per-tenant provider credential override (secret ref) — Stream HX-8.

    Platform-managed (system_admin writes; tenants cannot see or self-serve
    these — Stream Y-1 BYOK removal stands). Row exists = this tenant resolves
    the provider through its own ref; ``enabled=False`` suppresses the key for
    the tenant entirely (mirrors the platform-row P-12 semantics — no fallback).
    """

    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    provider: Provider
    secret_ref: str
    enabled: bool = True
    #: B-308 — see :class:`PlatformProviderSecretRecord`. An override without
    #: an address uses the official one; it never inherits a platform key's.
    base_url: str | None = None
    base_url_unverified: bool = False
    created_at: datetime
    updated_at: datetime
    updated_by: str

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return validate_secret_ref(value)


class TenantToolSecretRecord(BaseModel):
    """A per-tenant external-tool credential override (secret ref) — Stream HX-8."""

    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    tool: Tool
    secret_ref: str
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    updated_by: str

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return validate_secret_ref(value)


class PlatformSecretUpsert(BaseModel):
    """Validated upsert payload — shared by the API layer and the store.

    Carries only the mutable fields; ``secret_ref`` is ref-validated so a
    plaintext key is rejected before it can be persisted (Mini-ADR P-8).
    """

    model_config = ConfigDict(extra="forbid")
    secret_ref: str
    enabled: bool = True

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return validate_secret_ref(value)
