"""Abstract :class:`PlatformAgentTemplateStore` — Stream Agent-Templates (M1).

CRUD over the platform-curated Agent template catalog. Every row is
platform-global (``tenant_id`` is NULL), so SQL callers MUST be inside
``bypass_rls_session()`` — there is no per-tenant RLS scope to satisfy, exactly
like :class:`McpConnectorCatalogStore`. The store layer is transparent: it does
not import bypass; the control-plane caller applies it.

Versioned by ``(name, version)`` (mirrors ``agent_spec``) so tenants can pin
``extends: name@1.2.0``. ``name``/``version`` are derived from ``spec.metadata``
on write — the manifest is the single source of truth.
"""

from __future__ import annotations

import abc
import hashlib
import json

from expert_work.protocol import (
    PlatformAgentTemplatePatch,
    PlatformAgentTemplateRecord,
    PlatformAgentTemplateStatus,
    PlatformAgentTemplateUpsert,
)
from expert_work.protocol.agent_spec import RESTORE_LEGACY_MAX_TOKENS_CONTEXT, AgentSpec


def compute_spec_sha256(spec: AgentSpec, *, restore_legacy_max_tokens: bool = False) -> str:
    """Stable content hash of a manifest (canonical JSON: by alias, sorted keys).

    A template's sha is independent of the per-tenant ``agent_spec`` sha — it only
    marks "did this version's base manifest change" for templates.

    ``restore_legacy_max_tokens`` (B-105) re-inserts the legacy ``max_tokens: 4096``
    that loading normalised away on non-Anthropic models, reproducing the hash the
    pre-B-105 version stored. Comparison only — never use it for a hash that is
    written."""
    context = {RESTORE_LEGACY_MAX_TOKENS_CONTEXT: True} if restore_legacy_max_tokens else None
    payload = json.dumps(
        spec.model_dump(by_alias=True, mode="json", context=context),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PlatformAgentTemplateNotFoundError(Exception):
    """No ``platform_agent_template`` row for the requested ``(name, version)``."""

    def __init__(self, *, name: str, version: str) -> None:
        super().__init__(f"platform_agent_template not found: name={name!r} version={version!r}")
        self.name = name
        self.version = version


class PlatformAgentTemplateAlreadyExistsError(Exception):
    """A ``platform_agent_template`` row already exists for ``(name, version)``."""

    def __init__(self, *, name: str, version: str) -> None:
        super().__init__(
            f"platform_agent_template already exists: name={name!r} version={version!r}"
        )
        self.name = name
        self.version = version


class PlatformAgentTemplateStore(abc.ABC):
    """CRUD for platform-curated Agent template versions."""

    @abc.abstractmethod
    async def create(
        self, *, upsert: PlatformAgentTemplateUpsert, created_by: str
    ) -> PlatformAgentTemplateRecord:
        """Insert a new platform (NULL-tenant) template version. ``name`` /
        ``version`` come from ``upsert.spec.metadata``. Raises
        :class:`PlatformAgentTemplateAlreadyExistsError` on ``(name, version)``
        conflict."""

    @abc.abstractmethod
    async def get(self, *, name: str, version: str) -> PlatformAgentTemplateRecord | None:
        """Return one template version, or None if absent."""

    @abc.abstractmethod
    async def get_latest(
        self, *, name: str, status: PlatformAgentTemplateStatus | None = None
    ) -> PlatformAgentTemplateRecord | None:
        """Return the most recently created version of ``name`` (the platform's
        "current" version — what an instance pins by default), optionally
        filtered by ``status``. None if no matching version exists.

        Note: "latest" is by ``created_at`` (newest publish wins), not semver
        ordering — a deliberate M1 simplification (semver-aware latest is M2)."""

    @abc.abstractmethod
    async def list_versions(self, *, name: str) -> list[PlatformAgentTemplateRecord]:
        """All versions of one template, newest first."""

    @abc.abstractmethod
    async def list(
        self,
        *,
        category: str | None = None,
        status: PlatformAgentTemplateStatus | None = None,
    ) -> list[PlatformAgentTemplateRecord]:
        """List template versions, optionally filtered by category / status,
        ordered by ``name`` then newest version first."""

    @abc.abstractmethod
    async def update_spec(
        self,
        *,
        name: str,
        version: str,
        spec: AgentSpec,
        updated_by: str,
    ) -> PlatformAgentTemplateRecord | None:
        """Replace the base manifest of an existing version in place. Returns the
        updated record, or None if no row matched. Use ``create`` with a bumped
        ``metadata.version`` to publish a new pinnable version instead."""

    @abc.abstractmethod
    async def update_meta(
        self, *, name: str, version: str, patch: PlatformAgentTemplatePatch
    ) -> PlatformAgentTemplateRecord | None:
        """Apply a partial marketplace-metadata / status update. Returns the
        updated record, or None if no row matched."""

    @abc.abstractmethod
    async def delete(self, *, name: str, version: str) -> None:
        """Delete one template version. Raises
        :class:`PlatformAgentTemplateNotFoundError` if absent."""
