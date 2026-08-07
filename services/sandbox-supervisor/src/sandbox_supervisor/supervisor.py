"""``SandboxSupervisor`` — the F.1 sandbox lifecycle core.

M0 cold-start (Mini-ADR F-4): ``acquire`` is a fresh ``docker run``,
``release`` / ``destroy`` a ``docker rm -f``. No warm pool.

Transport is the held-pipe (option C): ``acquire`` launches the
container with ``docker run -i`` and keeps the subprocess; the
supervisor holds a :class:`RunnerLink` per sandbox and ``exec`` drives
the runner protocol over it. All dependencies are injected so the logic
is unit-testable with fakes (test matrix #40 / #41 / #42 + exec).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal, Protocol
from uuid import UUID, uuid4

from expert_work.common.egress_token import mint_egress_token
from expert_work.common.observability import expert_work_histogram
from expert_work.persistence import (
    UserWorkspaceStore,
    is_reserved_workspace_path,
    workspace_volume_name,
)
from expert_work.protocol import AuditEntry, UserWorkspace
from expert_work.protocol.audit import AuditAction, AuditResult
from expert_work.runtime.sandbox import SandboxResourceLimits, SandboxRuntimeProvider
from sandbox_supervisor.docker_client import DockerClient, DockerError
from sandbox_supervisor.domain import (
    DESTROY_REASON_RELEASE,
    InvalidSeedFilesError,
    QuotaExceededError,
    SandboxNotFoundError,
    SandboxRecord,
    SandboxState,
    SupervisorError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    container_name,
)
from sandbox_supervisor.pool import (
    DESTROY_REASON_POOL_CLAIM_FAILED,
    PooledSandbox,
    SandboxPool,
    discard_pooled,
    observe_pool_event,
)
from sandbox_supervisor.quota_enforcer import QuotaEnforcer
from sandbox_supervisor.runner_link import ExecResult, RunnerLink, RunnerLinkError
from sandbox_supervisor.schemas import AcquireRequest, AcquireResponse, SeedFile
from sandbox_supervisor.settings import SandboxSupervisorSettings
from sandbox_supervisor.store import SandboxStore

DESTROY_REASON_WORKSPACE_SOFT_DELETE = "workspace_soft_delete"
#: Stream OFFICE-1a — a warm session is torn down because the new acquire
#: asks for a different image variant than the session was built from.
DESTROY_REASON_VARIANT_CHANGED = "image_variant_changed"

logger = logging.getLogger(__name__)


# Stream K.K10 — Sandbox cold-start duration. Measured from the moment
# ``acquire`` decides to launch (after quota + workspace resolution) to
# the moment ``wait_ready`` returns. Warm-session reuse (Stream J.15
# warm path) does not observe — those acquires never run docker. SLO #4
# (slo.md): P95 < 3s (M0) / < 500ms (M1 with a warm pool).
_sandbox_cold_start_seconds = expert_work_histogram(
    "expert_work_sandbox_cold_start_seconds",
    "Seconds from launch decision to ``wait_ready`` success.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)


class AuditSink(Protocol):
    """The audit surface the supervisor needs — :class:`AuditLogger` satisfies it."""

    async def write(self, entry: AuditEntry) -> None:
        """Persist one audit entry."""


#: The deterministic ``--name`` for a sandbox's container — the helper
#: moved to ``domain.py`` (HX-6) so the pool replenisher can share it.
_container_name = container_name


#: Per-file download cap for the J.9 workspace-file read. Artifacts are
#: documents / code / data — small; the cap bounds the supervisor's
#: in-memory buffer against a pathological file.
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

#: skill-runtime §5.1 — caps on ``seed_files`` (mirror the .skill package caps,
#: asset-store tier: 64 MiB total / 16384 entries — asset-library skills like
#: ppt-master materialize thousands of small template files). The orchestrator
#: already bounds this, but the supervisor re-checks at its trust boundary
#: (the request round-trips untrusted).
_MAX_SEED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_SEED_FILES = 16384

#: Document-upload write cap. A user uploads a PDF / office doc into their
#: workspace; the supervisor re-checks the size at its trust boundary (the
#: control-plane already bounds it, but the request round-trips untrusted).
_MAX_WORKSPACE_WRITE_BYTES = 25 * 1024 * 1024

#: Workspace-browse listing cap. Bounds the inspector's file list (and the
#: supervisor's buffer) against a volume with a pathological number of files.
_MAX_WORKSPACE_LIST_ENTRIES = 2000


def _validate_workspace_path(path: str) -> str:
    """Reject a non-relative or ``..``-bearing workspace path (J.9).

    ``save_artifact`` already validates this, but the path round-trips
    through the control-plane untrusted — re-check at this boundary.
    """
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        msg = f"workspace path must be relative and free of '..': {path!r}"
        raise WorkspaceFileNotFoundError(msg)
    return cleaned


class SandboxSupervisor:
    """Owns the ``acquire`` / ``exec`` / ``release`` / ``destroy`` lifecycle."""

    def __init__(
        self,
        *,
        store: SandboxStore,
        docker: DockerClient,
        audit: AuditSink,
        runtime_provider: SandboxRuntimeProvider,
        workspace_store: UserWorkspaceStore,
        settings: SandboxSupervisorSettings,
        quota_enforcer: QuotaEnforcer | None = None,
        pool: SandboxPool | None = None,
    ) -> None:
        self._store = store
        self._docker = docker
        self._audit = audit
        self._runtime = runtime_provider
        self._workspaces = workspace_store
        self._settings = settings
        # Stream HX-6 — the warm READY pool. ``None`` (legacy callers /
        # pool disabled) keeps the cold-start path byte-identical.
        self._pool = pool
        # Stream J.15-补强-1 — per-workspace volume quota gate. ``None``
        # means quota enforcement is disabled (legacy callers + the
        # ephemeral-tmpfs path; the J.15 warm-session path always
        # constructs one in ``create_app``).
        self._quota_enforcer = quota_enforcer
        # Held runner links, keyed by sandbox id — the option-C transport.
        self._links: dict[UUID, RunnerLink] = {}
        # Stream J.15 — warm per-user sandbox sessions: ``(tenant, user)``
        # → the live sandbox id. An ``acquire`` with a ``user_id`` reuses
        # the session here; the idle reaper / destroy clears the entry.
        self._sessions: dict[tuple[UUID, UUID], UUID] = {}
        # Per-sandbox exec lock — the held pipe handles one exec at a
        # time, so concurrent runs sharing a warm session serialise here.
        self._exec_locks: dict[UUID, asyncio.Lock] = {}
        # Strong refs for fire-and-forget tasks (J.15-补强-1 refresh_size +
        # any future background work). Without this Python may GC the
        # task mid-flight (Ruff RUF006).
        self._pending_tasks: set[asyncio.Task[None]] = set()

    async def acquire(self, request: AcquireRequest) -> AcquireResponse:
        """Reuse the caller's warm session, or launch a fresh sandbox.

        Stream J.15 — a user-scoped acquire reuses that user's warm
        sandbox session if one is live (no ``docker run``); the idle
        reaper reclaims it once unused for ``session_idle_ttl_s``.

        Raises :class:`QuotaExceededError` when the tenant is at its
        sandbox cap, :class:`WorkspaceQuotaExceededError` when the
        user's workspace volume is at its size ceiling (J.15-补强-1,
        Mini-ADR J-29 第 1 项), :class:`WorkspaceDeletedError` when
        the workspace has been soft-deleted (Mini-ADR J-36), and
        :class:`SupervisorError` when the container fails to launch.
        """
        if request.user_id is not None:
            reused = await self._reuse_session(
                request.tenant_id, request.user_id, request.image_variant
            )
            if reused is not None:
                await self._seed_workspace(reused.container_id, request.seed_files)
                return reused

        await self._enforce_quota(request.tenant_id)

        # Stream HX-6 — an ephemeral acquire (no user_id → tmpfs
        # workspace) claims a pre-launched READY container when the pool
        # holds one. A persistent-workspace acquire can never be pooled:
        # the user's named volume mounts at ``docker run`` time
        # (Mini-ADR HX-F2). Claim failure of any kind falls through to
        # the unchanged cold-start path below.
        #
        # sandbox-egress §3.3 — an egress-enabled acquire also bypasses the
        # pool: the per-sandbox proxy token is baked into the container env at
        # ``docker run``, but pooled containers are pre-warmed generically with
        # no token, so they can't carry this agent's egress. Cold-start instead.
        egress_off = request.egress in (None, "none")
        if request.user_id is None and self._pool is not None and egress_off:
            claimed = await self._claim_pooled(request)
            if claimed is not None:
                await self._seed_workspace(claimed.container_id, request.seed_files)
                return claimed

        # A user-scoped acquire mounts that user's persistent workspace
        # volume at /workspace; resolve (creating on first use) the
        # user_workspace row. No user_id → an ephemeral tmpfs workspace.
        workspace: UserWorkspace | None = None
        if request.user_id is not None:
            workspace = await self._workspaces.resolve(
                tenant_id=request.tenant_id, user_id=request.user_id
            )
            # J.15-补强-1: enforce per-workspace quota + soft-delete
            # before paying for a docker launch. Raises on reject;
            # ``QuotaEnforcer.check`` emits the audit before raising.
            if self._quota_enforcer is not None:
                await self._quota_enforcer.check(workspace=workspace)

        record = self._new_record(request, workspace=workspace)
        await self._store.insert(record)

        workspace_volume = workspace.volume_name if workspace is not None else None
        # Stream K.K10 — cold-start measurement starts here (after quota
        # + workspace resolution; before the actual docker launch).
        cold_start_started = time.monotonic()
        try:
            link = await self._docker.launch(
                self._run_argv(record, workspace_volume=workspace_volume)
            )
            await link.wait_ready(self._settings.runner_ready_timeout_s)
            if workspace_volume is not None:
                # supervisor-freeze exception #3 (same source as the other
                # two Task 6 exceptions — a necessary companion to this
                # wave's image rework, not a scope-creep addition). The
                # sandbox's own docker run always sets --workdir /workspace
                # (F.3 SandboxRuntimeProvider) to restore cwd now that the
                # image no longer declares one; empirically, when --workdir
                # names a path that's also a volume mount target, docker
                # resets that directory's ownership to root:root on *every*
                # container creation (not just the volume's first mount —
                # see CliDockerClient.chown_volume's docstring for the full
                # repro). A non-root --user sandbox can then never write its
                # own /workspace. Fixing it here (after the container is up,
                # not before the launch) is the only place the chown sticks
                # for *this* container's view — see the docstring for why.
                await self._docker.chown_volume(volume=workspace_volume, image=record.image_ref)
        except (DockerError, RunnerLinkError) as exc:
            await self._store.update(record.with_state(SandboxState.FAILED))
            msg = f"sandbox launch failed: {exc}"
            raise SupervisorError(msg) from exc
        _sandbox_cold_start_seconds.observe(time.monotonic() - cold_start_started)

        self._links[record.id] = link
        acquired_at = datetime.now(UTC)
        await self._store.update(
            record.with_state(
                SandboxState.IN_USE,
                container_id=_container_name(record.id),
                acquired_at=acquired_at,
            )
        )
        if request.user_id is not None:
            # Register the warm session for reuse by the user's next run.
            self._sessions[(request.tenant_id, request.user_id)] = record.id
        await self._emit_audit(
            tenant_id=record.tenant_id,
            action=AuditAction.SANDBOX_ACQUIRED,
            result=AuditResult.SUCCESS,
            sandbox_id=record.id,
            details={
                "image_ref": record.image_ref,
                "thread_id": record.thread_id,
                "persistent_workspace": workspace is not None,
            },
        )
        await self._seed_workspace(_container_name(record.id), request.seed_files)
        return AcquireResponse(
            sandbox_id=record.id,
            container_id=_container_name(record.id),
            cold_start=True,
            acquired_at=acquired_at,
        )

    async def _seed_workspace(self, container_name_: str, seed_files: list[SeedFile]) -> None:
        """Materialize ``seed_files`` into a running container's ``/workspace``
        (skill-runtime §5.1). Validate path + caps (trust boundary; the request
        round-trips untrusted), base64-decode, then ``docker cp``.

        Bad input (unsafe path / bad base64 / over cap) → :class:`InvalidSeedFilesError`
        (HTTP 400) — deterministic, reject the acquire. A docker-cp transport
        failure degrades gracefully (log + continue): the skill files just aren't
        on disk this call; ``skill_view`` still serves them as text.
        """
        if not seed_files:
            return
        if len(seed_files) > _MAX_SEED_FILES:
            msg = f"too many seed files: {len(seed_files)} > {_MAX_SEED_FILES}"
            raise InvalidSeedFilesError(msg)
        decoded: list[tuple[str, bytes]] = []
        total = 0
        for sf in seed_files:
            try:
                path = _validate_workspace_path(sf.path)
            except WorkspaceFileNotFoundError as exc:
                raise InvalidSeedFilesError(str(exc)) from exc
            try:
                data = base64.b64decode(sf.content_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                msg = f"seed file {sf.path!r} is not valid base64"
                raise InvalidSeedFilesError(msg) from exc
            total += len(data)
            if total > _MAX_SEED_TOTAL_BYTES:
                msg = f"seed files exceed the {_MAX_SEED_TOTAL_BYTES}-byte total cap"
                raise InvalidSeedFilesError(msg)
            decoded.append((path, data))
        try:
            await self._docker.seed_workspace(container_name_, files=decoded)
        except DockerError as exc:
            logger.warning(
                "supervisor.seed_workspace_failed container=%s files=%d reason=%s",
                container_name_,
                len(decoded),
                exc,
            )

    async def exec(
        self,
        sandbox_id: UUID,
        *,
        code: str,
        timeout_s: int | None = None,
        envs: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``code`` in an acquired sandbox via its held runner link.

        ``timeout_s`` omitted → the service default. Raises
        :class:`SandboxNotFoundError` when no live sandbox holds that id,
        and :class:`SupervisorError` when the runner link fails.

        The per-sandbox lock serialises concurrent execs sharing one
        warm session (the held pipe handles one exec at a time); each
        exec stamps ``last_used_at`` so the idle reaper measures from it.

        ``envs`` (sandbox migration wave 2, spec 决策 10) — per-call env
        overrides merged onto the runner subprocess's environment (currently
        just ``PYTHONUSERBASE`` isolation). Re-asserted on every call rather
        than baked in at acquire time: two agents can share one already-warm
        session, so the value must be able to change call-to-call.
        """
        link = self._links.get(sandbox_id)
        if link is None:
            raise SandboxNotFoundError(sandbox_id)
        resolved_timeout = timeout_s if timeout_s is not None else self._settings.default_timeout_s
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            await self._touch(sandbox_id)
            try:
                return await link.exec(code, resolved_timeout, envs=envs)
            except RunnerLinkError as exc:
                msg = f"sandbox exec failed: {exc}"
                raise SupervisorError(msg) from exc

    async def release(self, sandbox_id: UUID) -> None:
        """Routine teardown.

        A J.15 warm per-user session is **kept alive** (no-op) — it
        stays hot for the user's next run and is reclaimed by the idle
        reaper. A non-session (no ``user_id``) sandbox is destroyed.

        J.15-补强-1 — before keeping warm, fires a fire-and-forget
        :meth:`QuotaEnforcer.refresh_size` so the next acquire's quota
        check uses a fresh ``size_bytes`` measurement.
        """
        record = await self._store.get(sandbox_id)
        if (
            record is not None
            and record.user_id is not None
            and record.state is SandboxState.IN_USE
        ):
            await self._schedule_size_refresh(record.tenant_id, record.user_id)
            return
        await self.destroy(sandbox_id, reason=DESTROY_REASON_RELEASE)

    async def _schedule_size_refresh(self, tenant_id: UUID, user_id: UUID) -> None:
        """Fire-and-forget :meth:`QuotaEnforcer.refresh_size` (J.15-补强-1).

        Schedules an async task so the release path is not blocked on
        the ``du`` measure. The enforcer swallows its own errors —
        nothing here needs to handle them.
        """
        if self._quota_enforcer is None:
            return
        workspace = await self._workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        enforcer = self._quota_enforcer
        task = asyncio.create_task(enforcer.refresh_size(workspace=workspace))
        # Strong ref + auto-cleanup so Python doesn't GC the task mid-await.
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def destroy(self, sandbox_id: UUID, *, reason: str) -> None:
        """Tear a sandbox down — ``docker rm -f`` + close the link + mark DESTROYED.

        Idempotent: destroying an already-terminal sandbox is a no-op.
        A non-``release`` reason emits a ``sandbox:force_destroy`` audit.

        A forced teardown (cancel / TTL reaper) SIGKILLs the container
        *before* closing the link: ``link.close()`` waits on a stdin-EOF
        that a busy runner only sees once its current ``exec`` returns,
        which would blow the gate-#8 ≤1s budget (Mini-ADR F-8). A routine
        ``release`` closes the pipe gracefully first, with ``docker rm``
        as the backstop.
        """
        record = await self._store.get(sandbox_id)
        if record is None:
            raise SandboxNotFoundError(sandbox_id)
        if record.state in (SandboxState.DESTROYED, SandboxState.FAILED):
            return

        link = self._links.pop(sandbox_id, None)
        self._exec_locks.pop(sandbox_id, None)
        # Clear the warm-session entry — but only if it still points here
        # (a newer session for the same user may have replaced it).
        if record.user_id is not None:
            session_key = (record.tenant_id, record.user_id)
            if self._sessions.get(session_key) == sandbox_id:
                del self._sessions[session_key]
        forced = reason != DESTROY_REASON_RELEASE
        if forced:
            await self._docker.remove(_container_name(sandbox_id))
        if link is not None:
            await link.close()
        if not forced:
            await self._docker.remove(_container_name(sandbox_id))

        now = datetime.now(UTC)
        released_at = now if reason == DESTROY_REASON_RELEASE else record.released_at
        await self._store.update(
            record.with_state(
                SandboxState.DESTROYED,
                destroyed_at=now,
                destroy_reason=reason,
                released_at=released_at,
            )
        )
        if reason != DESTROY_REASON_RELEASE:
            await self._emit_audit(
                tenant_id=record.tenant_id,
                action=AuditAction.SANDBOX_FORCE_DESTROY,
                result=AuditResult.SUCCESS,
                sandbox_id=sandbox_id,
                details={"reason": reason},
            )

    async def docker_ok(self) -> bool:
        """Whether the Docker daemon is reachable — for the health probe."""
        return await self._docker.ping()

    async def mark_workspace_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """Soft-delete a user's workspace (Mini-ADR J-36 lifecycle 第 2 档).

        Idempotent. After this call:

        * Subsequent ``acquire(user_id=this_user)`` raises
          :class:`WorkspaceDeletedError` (Mini-ADR J-36).
        * Any live warm session is force-destroyed — the next acquire
          would reject anyway, but freeing the slot immediately keeps
          quota state honest.
        * The reaper picks the row up on its next sweep and triggers the
          archive job (J.15-补强-2 backup pipeline reuses the same
          mechanism). Until then the volume stays on disk but is
          inaccessible.
        * Emits ``workspace:soft_delete`` audit (Stream J.15-补强-1).
        """
        workspace = await self._workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        # Idempotent: skip the audit + destroy when already deleted.
        if workspace.deleted_at is not None:
            return
        now = datetime.now(UTC)
        await self._workspaces.soft_delete(workspace_id=workspace.id, now=now)
        session_key = (tenant_id, user_id)
        sandbox_id = self._sessions.get(session_key)
        if sandbox_id is not None:
            await self.destroy(sandbox_id, reason=DESTROY_REASON_WORKSPACE_SOFT_DELETE)
        # Phase 3a (purge_user) — the warm session (if any) is now destroyed;
        # hard-delete the user's ``sandbox_instance`` lifecycle rows so no PII
        # (the surrogate user_id) lingers. Best-effort within the idempotency
        # guard: a second mark_workspace_deleted already returned above.
        await self._store.delete_all_for_user(tenant_id=tenant_id, user_id=user_id)
        await self._emit_audit(
            tenant_id=tenant_id,
            action=AuditAction.WORKSPACE_SOFT_DELETE,
            result=AuditResult.SUCCESS,
            sandbox_id=None,
            details={
                "user_id": str(user_id),
                "workspace_id": str(workspace.id),
                "volume_name": workspace.volume_name,
            },
            resource_type="user_workspace",
            resource_id=str(workspace.id),
        )

    async def list_workspace_files(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> list[tuple[int, str]]:
        """List the files in a user's persistent workspace volume (browse).

        Read-only inventory for the playground workspace inspector. Returns
        ``(size_bytes, relpath)`` pairs. A volume that has never been created
        (the user hasn't run anything yet) yields an empty list rather than
        an error — the inspector renders "no files" the same as "no volume".
        """
        volume = workspace_volume_name(tenant_id, user_id)
        try:
            entries = await self._docker.list_volume_files(
                volume=volume,
                image=self._settings.sandbox_image,
                max_entries=_MAX_WORKSPACE_LIST_ENTRIES,
            )
        except DockerError:
            return []
        # Hide machinery (seeded skill packages) + inputs (uploads) — show only
        # what the agent itself produced. ``is_reserved_workspace_path`` is the
        # shared source of truth with the seeders that write those prefixes.
        return [(size, rel) for size, rel in entries if not is_reserved_workspace_path(rel)]

    async def read_workspace_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        """Read a file from a user's persistent workspace volume (Stream J.9).

        Backs artifact content download — the control-plane proxies to
        here because only the supervisor can read a docker volume.
        Raises :class:`WorkspaceFileNotFoundError` when the file is
        missing / unreadable, and :class:`WorkspaceFileTooLargeError`
        when it exceeds the download cap.
        """
        safe_path = _validate_workspace_path(path)
        volume = workspace_volume_name(tenant_id, user_id)
        try:
            data = await self._docker.read_volume_file(
                volume=volume,
                path=safe_path,
                image=self._settings.sandbox_image,
                max_bytes=_MAX_ARTIFACT_BYTES,
            )
        except DockerError as exc:
            raise WorkspaceFileNotFoundError(str(exc)) from exc
        if len(data) > _MAX_ARTIFACT_BYTES:
            msg = f"workspace file {path!r} exceeds the {_MAX_ARTIFACT_BYTES}-byte download cap"
            raise WorkspaceFileTooLargeError(msg)
        return data

    async def write_workspace_file(
        self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes
    ) -> None:
        """Write ``data`` to ``path`` in a user's persistent workspace volume.

        Backs the document-upload path: a user uploads a file, the control-
        plane proxies here, and the bytes land in the durable workspace so a
        later run's ``read_document`` can read them. Validates the path +
        size at this trust boundary (the request round-trips untrusted).
        Raises :class:`WorkspaceFileTooLargeError` when over the write cap,
        :class:`WorkspaceFileNotFoundError` for a bad path, and
        :class:`SupervisorError` (via :class:`DockerError`) on a write failure.
        """
        if len(data) > _MAX_WORKSPACE_WRITE_BYTES:
            msg = f"upload {path!r} exceeds the {_MAX_WORKSPACE_WRITE_BYTES}-byte write cap"
            raise WorkspaceFileTooLargeError(msg)
        safe_path = _validate_workspace_path(path)
        volume = workspace_volume_name(tenant_id, user_id)
        try:
            await self._docker.write_volume_file(
                volume=volume,
                path=safe_path,
                data=data,
                image=self._settings.sandbox_image,
            )
        except DockerError as exc:
            raise SupervisorError(str(exc)) from exc

    async def delete_workspace_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        """Delete one file from a user's persistent workspace volume.

        Backs the playground workspace cleanup. Validates the path at this
        trust boundary and refuses reserved prefixes (seeded skill packages /
        uploads machinery) — the same set ``list_workspace_files`` hides, so a
        client can only delete what the agent itself produced. Missing files are
        a no-op (``rm -f``). Raises :class:`WorkspaceFileNotFoundError` for a bad
        / reserved path and :class:`SupervisorError` on a delete failure.
        """
        safe_path = _validate_workspace_path(path)
        if is_reserved_workspace_path(safe_path):
            raise WorkspaceFileNotFoundError(f"path {path!r} is reserved and cannot be deleted")
        volume = workspace_volume_name(tenant_id, user_id)
        try:
            await self._docker.delete_volume_file(
                volume=volume,
                path=safe_path,
                image=self._settings.sandbox_image,
            )
        except DockerError as exc:
            raise SupervisorError(str(exc)) from exc

    # ------------------------------------------------------------------

    async def _reuse_session(
        self, tenant_id: UUID, user_id: UUID, image_variant: str | None = None
    ) -> AcquireResponse | None:
        """Return the user's warm session as an :class:`AcquireResponse`, or
        ``None`` when there is no live session to reuse (J.15).

        Tenant-level sandbox-count quota is skipped (the session already
        holds its slot). But J.15-补强-1 still rechecks per-workspace
        size quota + soft-delete state — the volume can grow past its
        ``size_limit_bytes`` between acquires, and a workspace can be
        soft-deleted while a session is warm; both must reject reuse.
        A stale map entry (link gone, or the row is no longer
        ``IN_USE``) is dropped so the caller falls through.
        """
        sandbox_id = self._sessions.get((tenant_id, user_id))
        if sandbox_id is None or sandbox_id not in self._links:
            return None
        record = await self._store.get(sandbox_id)
        if record is None or record.state is not SandboxState.IN_USE:
            self._sessions.pop((tenant_id, user_id), None)
            return None
        # Stream OFFICE-1a — never reuse a warm session built from a different
        # image variant (an office agent must not inherit a minimal session,
        # which lacks the office libraries). Tear the stale-variant session
        # down now (rather than waiting for the idle reaper) so it stops
        # counting toward the tenant sandbox quota, then fall through to a
        # cold start.
        if record.image_ref != self._select_image(image_variant):
            await self.destroy(sandbox_id, reason=DESTROY_REASON_VARIANT_CHANGED)
            return None
        # J.15-补强-1: workspace-level quota + soft-delete recheck on reuse.
        if self._quota_enforcer is not None:
            workspace = await self._workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
            # Raises WorkspaceQuotaExceededError / WorkspaceDeletedError
            # — let the caller propagate; the warm session entry stays
            # (the reaper / explicit destroy will clear it).
            await self._quota_enforcer.check(workspace=workspace)
        return AcquireResponse(
            sandbox_id=sandbox_id,
            container_id=_container_name(sandbox_id),
            cold_start=False,
            acquired_at=record.acquired_at or record.created_at,
        )

    async def _claim_pooled(self, request: AcquireRequest) -> AcquireResponse | None:
        """Claim a READY pool container for an ephemeral acquire (HX-6).

        Returns ``None`` on any non-hit (pool empty for the variant, CAS
        lost, limit pairing failed) — the caller cold-starts. The claim
        order is CAS first (own the row), then ``docker update`` to pair
        the request's limits; an update failure destroys the container
        (Mini-ADR HX-F3 fail-closed: limits are a security surface).
        """
        if self._pool is None:
            return None
        image_ref = self._select_image(request.image_variant)
        pooled = self._pool.take(image_ref)
        if pooled is None:
            observe_pool_event("miss")
            return None
        s = self._settings
        acquired_at = datetime.now(UTC)
        record = pooled.record.with_state(
            SandboxState.IN_USE,
            tenant_id=request.tenant_id,
            thread_id=request.thread_id,
            cpu_quota=request.cpu if request.cpu is not None else s.default_cpu,
            memory_mb=request.memory_mb if request.memory_mb is not None else s.default_memory_mb,
            pids_limit=(
                request.pids_limit if request.pids_limit is not None else s.default_pids_limit
            ),
            timeout_s=request.timeout_s if request.timeout_s is not None else s.default_timeout_s,
            acquired_at=acquired_at,
        )
        if not await self._store.claim_ready(record):
            # CAS lost — the row is no longer READY. The in-memory take
            # is exclusive so this branch is defensive only; whoever
            # re-homed the row owns the container — don't touch it.
            observe_pool_event("claim_raced")
            return None
        try:
            await self._docker.update_limits(
                container_name(record.id),
                cpus=record.cpu_quota,
                memory_mb=record.memory_mb,
                pids_limit=record.pids_limit,
            )
        except DockerError as exc:
            observe_pool_event("update_failed")
            logger.warning("pool.claim_update_failed sandbox=%s reason=%s", record.id, exc)
            await discard_pooled(
                PooledSandbox(record=record, link=pooled.link),
                docker=self._docker,
                store=self._store,
                reason=DESTROY_REASON_POOL_CLAIM_FAILED,
            )
            return None
        self._links[record.id] = pooled.link
        observe_pool_event("hit")
        await self._emit_audit(
            tenant_id=record.tenant_id,
            action=AuditAction.SANDBOX_ACQUIRED,
            result=AuditResult.SUCCESS,
            sandbox_id=record.id,
            details={
                "image_ref": record.image_ref,
                "thread_id": record.thread_id,
                "persistent_workspace": False,
                "pooled": True,
            },
        )
        return AcquireResponse(
            sandbox_id=record.id,
            container_id=container_name(record.id),
            cold_start=False,
            acquired_at=acquired_at,
        )

    async def _touch(self, sandbox_id: UUID) -> None:
        """Stamp ``last_used_at`` so the idle reaper measures from now."""
        record = await self._store.get(sandbox_id)
        if record is not None:
            await self._store.update(
                record.with_state(record.state, last_used_at=datetime.now(UTC))
            )

    async def _enforce_quota(self, tenant_id: UUID) -> None:
        limit = await self._store.sandbox_limit_for_tenant(tenant_id)
        if limit is None:
            limit = self._settings.default_max_sandboxes
        active = await self._store.count_active_for_tenant(tenant_id)
        if active >= limit:
            await self._emit_audit(
                tenant_id=tenant_id,
                action=AuditAction.SANDBOX_QUOTA_DENIED,
                result=AuditResult.DENIED,
                sandbox_id=None,
                details={"active": active, "limit": limit},
                reason=f"tenant at sandbox quota ({active}/{limit})",
            )
            raise QuotaExceededError(tenant_id, limit)

    def _select_image(self, image_variant: str | None) -> str:
        """The single sandbox image (sandbox-image-consolidation).

        The variant split (``minimal``/``office``) was collapsed into one
        image, so the now-deprecated ``image_variant`` request field is ignored
        — every acquire resolves to ``sandbox_image``. Kept as a method (not
        inlined) so the ``_reuse_session`` image-change check still routes
        through one place: if ``sandbox_image`` is reconfigured, a live session
        on the old image is recreated."""
        del image_variant  # deprecated, ignored
        return self._settings.sandbox_image

    def _new_record(
        self, request: AcquireRequest, *, workspace: UserWorkspace | None
    ) -> SandboxRecord:
        s = self._settings
        return SandboxRecord(
            id=uuid4(),
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            workspace_id=workspace.id if workspace is not None else None,
            image_ref=self._select_image(request.image_variant),
            node=s.node_name,
            container_id=None,
            state=SandboxState.CREATING,
            thread_id=request.thread_id,
            cpu_quota=request.cpu if request.cpu is not None else s.default_cpu,
            memory_mb=request.memory_mb if request.memory_mb is not None else s.default_memory_mb,
            pids_limit=(
                request.pids_limit if request.pids_limit is not None else s.default_pids_limit
            ),
            timeout_s=request.timeout_s if request.timeout_s is not None else s.default_timeout_s,
            created_at=datetime.now(UTC),
            egress_policy=request.egress,
            agent_name=request.agent_name,
            agent_version=request.agent_version,
            egress_allowlist=tuple(request.egress_allowlist),
            egress_denylist=tuple(request.egress_denylist),
        )

    def _run_argv(self, record: SandboxRecord, *, workspace_volume: str | None) -> list[str]:
        """The hardened ``docker run`` argv from the F.3 provider.

        The provider's argv already carries ``--interactive`` — option C
        keeps the container attached so the supervisor holds its stdio;
        no ``--detach`` is added. ``workspace_volume`` selects the
        ``/workspace`` backing — a J.15 persistent volume or an
        ephemeral tmpfs (``None``).
        """
        return self._runtime.docker_run_argv(
            image=record.image_ref,
            container_name=_container_name(record.id),
            limits=SandboxResourceLimits(
                cpus=record.cpu_quota,
                memory_mb=record.memory_mb,
                pids_limit=record.pids_limit,
            ),
            workspace_volume=workspace_volume,
            env=self._egress_env(record),
        )

    def _egress_env(self, record: SandboxRecord) -> tuple[tuple[str, str], ...]:
        """``HTTPS_PROXY``/``HTTP_PROXY``/``NO_PROXY`` env for the sandbox when
        its agent's egress policy is on (sandbox-egress §3.3).

        Mints a per-sandbox token bound to ``(tenant, agent, version, sandbox)``
        and embeds it as the proxy URL's username so the sandbox's HTTP clients
        authenticate to the egress proxy via standard Basic proxy auth. ``none``/
        unset → no env (the sandbox stays proxy-only / isolated)."""
        if record.egress_policy in (None, "none"):
            return ()
        s = self._settings
        token = mint_egress_token(
            s.egress_token_secret,
            tenant_id=str(record.tenant_id),
            agent_name=record.agent_name or "",
            agent_version=record.agent_version or "",
            sandbox_id=str(record.id),
            expires_at=time.time() + s.egress_token_ttl_s,
            allowlist=record.egress_allowlist,
            denylist=record.egress_denylist,
        )
        proxy_url = f"http://{token}:@{s.egress_proxy_host}:{s.egress_proxy_port}"
        # NO_PROXY keeps the credential-proxy /forward call + loopback direct.
        no_proxy = f"{s.egress_proxy_host},localhost,127.0.0.1"
        # stdlib urllib drops the proxy URL's userinfo on HTTPS CONNECT
        # (sandbox-egress §3.5), unlike requests/httpx. Hand the sitecustomize
        # shim baked into the image the exact Basic-auth bytes (base64 of
        # "<token>:") so it can add Proxy-Authorization to urllib's CONNECT.
        proxy_auth = base64.b64encode(f"{token}:".encode()).decode("ascii")
        return (
            ("HTTPS_PROXY", proxy_url),
            ("HTTP_PROXY", proxy_url),
            ("https_proxy", proxy_url),
            ("http_proxy", proxy_url),
            ("NO_PROXY", no_proxy),
            ("no_proxy", no_proxy),
            ("EXPERT_WORK_EGRESS_PROXY_AUTH", proxy_auth),
        )

    async def _emit_audit(
        self,
        *,
        tenant_id: UUID,
        action: AuditAction,
        result: AuditResult,
        sandbox_id: UUID | None,
        details: dict[str, object],
        reason: str | None = None,
        resource_type: Literal["sandbox", "user_workspace"] = "sandbox",
        resource_id: str | None = None,
    ) -> None:
        """Emit one audit entry. ``resource_id`` overrides ``sandbox_id`` when provided.

        J.15-补强-1 — ``resource_type`` defaults to ``"sandbox"`` for the
        long-standing F.1 callers; ``mark_workspace_deleted`` passes
        ``"user_workspace"`` so the workspace audit trail is separable
        from sandbox lifecycle events.
        """
        resolved_resource_id = (
            resource_id
            if resource_id is not None
            else (str(sandbox_id) if sandbox_id is not None else None)
        )
        await self._audit.write(
            AuditEntry(
                tenant_id=tenant_id,
                actor_type="system",
                actor_id=self._settings.service_name,
                action=action,
                resource_type=resource_type,
                resource_id=resolved_resource_id,
                result=result,
                reason=reason,
                details=details,
            )
        )
