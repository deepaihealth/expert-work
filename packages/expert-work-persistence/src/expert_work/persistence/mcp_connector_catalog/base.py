"""Abstract :class:`McpConnectorCatalogStore` — Stream W (Mini-ADR W-1).

CRUD over the platform-curated MCP connector catalog. Every row is platform-global
(``tenant_id`` is NULL), so SQL callers MUST be inside ``bypass_rls_session()`` —
there is no per-tenant RLS scope to satisfy, exactly like
:class:`PlatformSecretStore`. The store layer itself is transparent: it does not
import bypass; the control-plane caller applies it (W-3/W-4).
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from uuid import UUID

from expert_work.protocol import (
    McpConnectorCatalogPatch,
    McpConnectorCatalogRecord,
    McpConnectorCatalogUpsert,
)


class McpConnectorCatalogNotFoundError(Exception):
    """No ``mcp_connector_catalog`` row for the requested id."""

    def __init__(self, *, catalog_id: UUID) -> None:
        super().__init__(f"mcp_connector_catalog not found: id={catalog_id}")
        self.catalog_id = catalog_id


class McpConnectorCatalogAlreadyExistsError(Exception):
    """An ``mcp_connector_catalog`` row already exists for ``name``."""

    def __init__(self, *, name: str) -> None:
        super().__init__(f"mcp_connector_catalog already exists: name={name!r}")
        self.name = name


class McpConnectorCatalogInUseError(Exception):
    """The catalog row is referenced by a tenant's ``tenant_mcp_server.catalog_id``.

    The FK is ``ON DELETE RESTRICT`` (migration 0056), so the database refuses to
    delete a catalog entry that any tenant has instantiated — cross-tenant and
    RLS-independent. Surfaced as a 409 by the control-plane DELETE handler.
    """

    def __init__(self, *, catalog_id: UUID) -> None:
        super().__init__(f"mcp_connector_catalog in use: id={catalog_id}")
        self.catalog_id = catalog_id


class McpConnectorCatalogStore(abc.ABC):
    """CRUD for platform-curated MCP connector catalog entries."""

    @abc.abstractmethod
    async def create(
        self, *, upsert: McpConnectorCatalogUpsert, actor_id: str
    ) -> McpConnectorCatalogRecord:
        """Insert a new platform (NULL-tenant) catalog entry. Raises
        :class:`McpConnectorCatalogAlreadyExistsError` on ``name`` conflict."""

    @abc.abstractmethod
    async def get_by_id(self, catalog_id: UUID) -> McpConnectorCatalogRecord | None:
        """Return the entry, or None if absent."""

    @abc.abstractmethod
    async def get_by_ids(
        self, catalog_ids: Sequence[UUID]
    ) -> dict[UUID, McpConnectorCatalogRecord]:
        """Batch :meth:`get_by_id` — one round trip for many ids, keyed by id
        (missing ids absent). Replaces a per-id ``get_by_id`` loop (N+1)."""

    @abc.abstractmethod
    async def get_by_name(self, name: str) -> McpConnectorCatalogRecord | None:
        """Return the entry by name, or None if absent."""

    @abc.abstractmethod
    async def list(self, *, category: str | None = None) -> list[McpConnectorCatalogRecord]:
        """Return entries ordered by ``name``, optionally filtered by category."""

    @abc.abstractmethod
    async def update(
        self, *, catalog_id: UUID, patch: McpConnectorCatalogPatch
    ) -> McpConnectorCatalogRecord:
        """Apply a partial update. Raises
        :class:`McpConnectorCatalogNotFoundError` if absent."""

    @abc.abstractmethod
    async def update_with_previous(
        self, *, catalog_id: UUID, patch: McpConnectorCatalogPatch
    ) -> tuple[McpConnectorCatalogRecord, McpConnectorCatalogRecord]:
        """:meth:`update`, returning ``(before, after)``. ``before`` is read in
        the same transaction under a row lock, so two concurrent updates never
        see the same ``before`` (B-248: a switch of ``enabled`` is acted on
        exactly once)."""

    @abc.abstractmethod
    async def delete(self, catalog_id: UUID) -> None:
        """Delete the entry. Raises
        :class:`McpConnectorCatalogNotFoundError` if absent."""
