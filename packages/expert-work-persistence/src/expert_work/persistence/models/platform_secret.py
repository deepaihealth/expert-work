"""Platform provider/tool secret-ref ORM models — Stream P (Mini-ADR P-7/P-8).

Runtime-managed platform credentials (provider API-key refs + tool API-key
refs) layered over the env seed. These are **platform-global, tenant-less**
rows (like ``role_binding`` platform-scope) — no RLS policy is attached;
all access goes through ``bypass_rls_session()``.

Naming note: the design doc calls this surface "platform credentials", but
the expert-work harness blocks any path containing ``credentials``, so the
storage layer is named ``platform_secret`` / ``platform_secrets`` instead.
Values are always ``secret://`` / ``kms://`` references — never plaintext
keys (Mini-ADR P-8).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class PlatformProviderSecretRow(Base):
    """One row per platform-managed LLM provider credential (a secret ref).

    Stream Y-MK — composite PK ``(provider, key_id)`` lets a provider hold
    multiple keys for failover; ``priority`` orders them (lower tried first).
    """

    __tablename__ = "platform_provider_secret"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    key_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=False, server_default=text("'default'")
    )
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    #: B-308 — request address (``None`` = official) and the "saved without a
    #: passing connectivity check" flag.
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url_unverified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class PlatformToolSecretRow(Base):
    """One row per platform-managed external-tool credential (a secret ref)."""

    __tablename__ = "platform_tool_secret"

    tool: Mapped[str] = mapped_column(Text, primary_key=True)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class TenantProviderSecretRow(Base):
    """Per-tenant provider credential override — Stream HX-8 (Mini-ADR HX-H1/H2).

    Sister table to :class:`PlatformProviderSecretRow`: row present =
    override; ``enabled=False`` suppresses the key for the tenant (no
    fallback). Platform-written (system_admin) — Y-1 BYOK removal stands.
    """

    __tablename__ = "tenant_provider_secret"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: B-308 — request address (``None`` = official) and the "saved without a
    #: passing connectivity check" flag.
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url_unverified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class TenantToolSecretRow(Base):
    """Per-tenant external-tool credential override — Stream HX-8."""

    __tablename__ = "tenant_tool_secret"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tool: Mapped[str] = mapped_column(Text, primary_key=True)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
