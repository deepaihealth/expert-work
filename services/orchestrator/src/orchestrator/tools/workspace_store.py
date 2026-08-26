"""``WorkspaceStore`` — user persistent-workspace file operations.

Split out of :mod:`orchestrator.tools.sandbox` (sandbox migration wave 1,
Task 4). ``SandboxRuntime`` used to carry two unrelated jobs: sandbox
*lifecycle* (acquire / exec / release / destroy / reap) and per-user
*workspace file* access (read / list / write / delete / mark-deleted). The
five workspace methods never touch a live sandbox — they proxy straight to
the supervisor's workspace-file HTTP API — so bundling them onto the
sandbox-lifecycle Protocol was the wrong shape: a future workspace
implementation (wave 2's NAS-mounted control-plane read path) needs none of
the sandbox machinery.

This module is the first cut of that split: same HTTP wire behaviour, moved
under its own :class:`WorkspaceStore` Protocol. URL / request body / header /
error handling are carried over unchanged from ``HTTPSupervisorRuntime``; the
only change is the method names losing their redundant ``workspace_``
prefix (``read_workspace_file`` → :meth:`WorkspaceStore.read_file`, etc.) —
now that they live on a type called ``WorkspaceStore``, repeating the word
in every method name added nothing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from orchestrator.llm.providers._http import client_for
from orchestrator.tools.sandbox import (
    SandboxSupervisorError,
    WorkspaceFileTooLargeError,
    _traced_headers,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class WorkspaceFileEntry:
    """One file in a user's persistent workspace volume (browse listing)."""

    path: str
    size: int


@runtime_checkable
class WorkspaceStore(Protocol):
    """A user's persistent workspace volume — file read / list / write / delete."""

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        """Read a file from a user's persistent workspace volume (J.9 artifact download)."""

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        """List the files in a user's persistent workspace volume (browse)."""

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` in a user's persistent workspace volume.

        Backs the document-upload path: a user uploads a file, the
        control-plane proxies here, and the bytes land in the durable
        workspace so a later run's ``read_document`` can read them. Only
        the supervisor can write a per-user docker volume."""

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        """Delete one file from a user's persistent workspace volume.

        Backs the playground workspace cleanup. Only the supervisor can
        mutate a per-user docker volume; the control-plane proxies here."""

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """Soft-delete a user's whole persistent workspace (Phase 3a purge_user).

        The supervisor destroys any warm session, drops the user's
        sandbox_instance rows, and marks the volume deleted (Mini-ADR J-36 —
        the reaper archives it, then the 90-day sweep hard-deletes). Idempotent.
        Only the supervisor can mutate a per-user docker volume; the
        control-plane proxies here as part of the cascade purge."""


@dataclass
class SupervisorWorkspaceStore:
    """Production :class:`WorkspaceStore` — calls the supervisor's HTTP API.

    A non-2xx response raises :class:`SandboxSupervisorError`. Field shape
    mirrors :class:`orchestrator.tools.sandbox.HTTPSupervisorRuntime`.
    """

    base_url: str
    timeout_s: float = _DEFAULT_TIMEOUT_S
    #: Test seam — inject ``httpx.MockTransport`` / ``ASGITransport`` to
    #: exercise the wire layer (e.g. the A.8 traceparent round-trip).
    #: Production leaves it ``None`` (real network transport).
    transport: httpx.AsyncBaseTransport | None = None
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``httpx.AsyncClient(...)`` behaviour (tests / eval
    #: CLI / not-yet-wired production paths). When injected it must NOT be
    #: closed here: it is owned by the control-plane lifespan and outlives
    #: every individual call.
    http: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def _make_client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with client_for(
            self.http, timeout=self.timeout_s, transport=self.transport
        ) as client:
            yield client

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/file"
        async with self._make_client() as client:
            try:
                response = await client.get(
                    url,
                    params={"path": path},
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                f"sandbox supervisor workspace read failed: {response.status_code} {response.text}"
            )
            if response.status_code == 413:
                # supervisor 侧的 WorkspaceFileTooLargeError 映射成 413(见
                # sandbox_supervisor/app.py 的 exception handler)—— 在这里
                # 还原成同名窄类型,和 NasWorkspaceStore 的行为保持一致,
                # 下载端点才能把「太大」与「不存在」分开回 413。
                raise WorkspaceFileTooLargeError(msg)
            raise SandboxSupervisorError(msg)
        return response.content

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/files"
        async with self._make_client() as client:
            try:
                response = await client.get(
                    url,
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                f"sandbox supervisor workspace list failed: {response.status_code} {response.text}"
            )
            raise SandboxSupervisorError(msg)
        body = response.json()
        return [
            WorkspaceFileEntry(path=str(f["path"]), size=int(f["size"]))
            for f in body.get("files", [])
        ]

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/file"
        async with self._make_client() as client:
            try:
                response = await client.put(
                    url,
                    params={"path": path},
                    content=data,
                    headers={**_traced_headers(), "content-type": "application/octet-stream"},
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                f"sandbox supervisor workspace write failed: {response.status_code} {response.text}"
            )
            raise SandboxSupervisorError(msg)

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/file"
        async with self._make_client() as client:
            try:
                response = await client.request(
                    "DELETE",
                    url,
                    params={"path": path},
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                "sandbox supervisor workspace delete failed: "
                f"{response.status_code} {response.text}"
            )
            raise SandboxSupervisorError(msg)

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        await self._post(
            f"/v1/workspaces/{tenant_id}/{user_id}:delete",
            json=None,
            expect_body=False,
        )

    async def _post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None,
        expect_body: bool = True,
    ) -> Mapping[str, Any]:
        async with self._make_client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}{path}",
                    json=json,
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({path}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = f"sandbox supervisor {path} failed: {response.status_code} {response.text}"
            raise SandboxSupervisorError(msg)
        if not expect_body:
            return {}
        data = response.json()
        if not isinstance(data, Mapping):
            msg = f"sandbox supervisor {path} returned a non-object body"
            raise SandboxSupervisorError(msg)
        return data


@dataclass
class RecordingWorkspaceStore:
    """In-memory :class:`WorkspaceStore` for dev / tests.

    Records the read / list / write / delete / mark-deleted calls and
    returns the pre-set fixtures. Set the matching ``*_error`` field to
    drive an error path.
    """

    workspace_file: bytes = b""
    workspace_file_error: Exception | None = None
    workspace_files: list[WorkspaceFileEntry] = field(default_factory=list)
    workspace_list_error: Exception | None = None
    workspace_reads: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    workspace_writes: list[tuple[UUID, UUID, str, bytes]] = field(default_factory=list)
    workspace_write_error: Exception | None = None
    workspace_deletes: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    workspace_delete_error: Exception | None = None
    #: Phase 3a — the ``(tenant_id, user_id)`` of each mark_deleted call.
    workspace_deletions: list[tuple[UUID, UUID]] = field(default_factory=list)
    workspace_deletion_error: Exception | None = None

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        self.workspace_reads.append((tenant_id, user_id, path))
        if self.workspace_file_error is not None:
            raise self.workspace_file_error
        return self.workspace_file

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        self.workspace_reads.append((tenant_id, user_id, ""))
        if self.workspace_list_error is not None:
            raise self.workspace_list_error
        return self.workspace_files

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        if self.workspace_write_error is not None:
            raise self.workspace_write_error
        self.workspace_writes.append((tenant_id, user_id, path, data))

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        if self.workspace_delete_error is not None:
            raise self.workspace_delete_error
        self.workspace_deletes.append((tenant_id, user_id, path))

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        if self.workspace_deletion_error is not None:
            raise self.workspace_deletion_error
        self.workspace_deletions.append((tenant_id, user_id))
