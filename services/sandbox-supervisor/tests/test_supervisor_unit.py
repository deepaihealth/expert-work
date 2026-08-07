"""Unit tests for the Sandbox Supervisor — Stream F.1 + F.4a.

All Docker / DB dependencies are faked, so these run in the plain
``pytest`` job — no testcontainers. Groups:

* #40 — acquire / release lifecycle + state machine
* #41 — per-tenant quota denial + audit
* #42 — the TTL reaper
* F.4a — the held-pipe ``exec`` channel (option C)

Plus HTTP-route smoke tests over an injected supervisor.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from expert_work.common.egress_token import verify_egress_token
from expert_work.persistence import InMemoryUserWorkspaceStore, workspace_volume_name
from expert_work.protocol.audit import AuditAction, AuditResult
from expert_work.runtime.sandbox import SandboxRuntimeProvider
from sandbox_supervisor.app import create_app
from sandbox_supervisor.docker_client import DockerError
from sandbox_supervisor.domain import (
    DESTROY_REASON_IDLE_TIMEOUT,
    InvalidSeedFilesError,
    QuotaExceededError,
    SandboxNotFoundError,
    SandboxRecord,
    SandboxState,
    SupervisorError,
    WorkspaceDeletedError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceQuotaExceededError,
)
from sandbox_supervisor.pool import (
    DESTROY_REASON_POOL_CLAIM_FAILED,
    DESTROY_REASON_POOL_SHRUNK,
    POOL_TENANT_ID,
    PoolReplenisher,
    SandboxPool,
    prefetch_images,
)
from sandbox_supervisor.quota_enforcer import QuotaEnforcer
from sandbox_supervisor.reaper import SandboxReaper
from sandbox_supervisor.runner_link import ExecResult, RunnerLinkError
from sandbox_supervisor.schemas import AcquireRequest, SeedFile
from sandbox_supervisor.settings import SandboxSupervisorSettings
from sandbox_supervisor.supervisor import (
    _MAX_ARTIFACT_BYTES,
    _MAX_WORKSPACE_WRITE_BYTES,
    SandboxSupervisor,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunnerLink:
    """A :class:`RunnerLink` that never launches a real container."""

    def __init__(
        self,
        *,
        ready: bool = True,
        exec_result: ExecResult | None = None,
        exec_error: RunnerLinkError | None = None,
    ) -> None:
        self._ready = ready
        self._exec_result = exec_result or ExecResult(
            stdout="ok", stderr="", exit_code=0, timed_out=False
        )
        self._exec_error = exec_error
        self.closed = False
        self.exec_calls: list[tuple[str, int]] = []
        #: sandbox migration wave 2 (spec 决策 10) — the ``envs`` kwarg passed
        #: to each ``exec`` call, kept out of ``exec_calls`` so existing
        #: 2-tuple assertions stay unchanged.
        self.exec_envs_calls: list[dict[str, str] | None] = []

    async def wait_ready(self, timeout_s: float) -> None:
        if not self._ready:
            msg = "runner never reported ready"
            raise RunnerLinkError(msg)

    async def exec(
        self, code: str, timeout_s: int, *, envs: dict[str, str] | None = None
    ) -> ExecResult:
        self.exec_calls.append((code, timeout_s))
        self.exec_envs_calls.append(envs)
        if self._exec_error is not None:
            raise self._exec_error
        return self._exec_result

    async def close(self) -> None:
        self.closed = True


class RecordingDockerClient:
    """A :class:`DockerClient` that records calls and never touches Docker."""

    def __init__(
        self,
        *,
        launch_error: DockerError | None = None,
        link: FakeRunnerLink | None = None,
        volume_file: bytes = b"",
        volume_file_error: DockerError | None = None,
        volume_files: list[tuple[int, str]] | None = None,
        volume_list_error: DockerError | None = None,
        measured_size: int = 0,
        measure_error: DockerError | None = None,
        update_error: DockerError | None = None,
        existing_images: set[str] | None = None,
        pull_error: DockerError | None = None,
        seed_error: DockerError | None = None,
        chown_error: DockerError | None = None,
    ) -> None:
        self.launches: list[list[str]] = []
        self.removed: list[str] = []
        self.swept = 0
        self._launch_error = launch_error
        self._link = link
        self.links: list[FakeRunnerLink] = []
        self._volume_file = volume_file
        self._volume_file_error = volume_file_error
        self.volume_reads: list[tuple[str, str]] = []
        self._volume_files = volume_files if volume_files is not None else []
        self._volume_list_error = volume_list_error
        self.volume_lists: list[str] = []
        self._volume_write_error: DockerError | None = None
        self.volume_writes: list[tuple[str, str, bytes]] = []
        self._volume_delete_error: DockerError | None = None
        self.volume_deletes: list[tuple[str, str]] = []
        self._measured_size = measured_size
        self._measure_error = measure_error
        self.measure_calls: list[tuple[str, str]] = []
        self._update_error = update_error
        self.limit_updates: list[tuple[str, float, int, int]] = []
        self._existing_images = existing_images if existing_images is not None else set()
        self._pull_error = pull_error
        self.pulled: list[str] = []
        self._seed_error = seed_error
        #: (container_name, [(path, bytes), ...]) for each seed_workspace call.
        self.seeds: list[tuple[str, list[tuple[str, bytes]]]] = []
        self._chown_error = chown_error
        #: (volume, image) for each chown_volume call.
        self.chowns: list[tuple[str, str]] = []

    async def launch(self, argv: list[str]) -> FakeRunnerLink:
        self.launches.append(argv)
        if self._launch_error is not None:
            raise self._launch_error
        link = self._link if self._link is not None else FakeRunnerLink()
        self.links.append(link)
        return link

    async def remove(self, container_name: str) -> None:
        self.removed.append(container_name)

    async def ping(self) -> bool:
        return True

    async def sweep_orphans(self) -> int:
        self.swept += 1
        return 0

    async def read_volume_file(
        self, *, volume: str, path: str, image: str, max_bytes: int
    ) -> bytes:
        del image, max_bytes
        self.volume_reads.append((volume, path))
        if self._volume_file_error is not None:
            raise self._volume_file_error
        return self._volume_file

    async def list_volume_files(
        self, *, volume: str, image: str, max_entries: int
    ) -> list[tuple[int, str]]:
        del image, max_entries
        self.volume_lists.append(volume)
        if self._volume_list_error is not None:
            raise self._volume_list_error
        return self._volume_files

    async def write_volume_file(self, *, volume: str, path: str, data: bytes, image: str) -> None:
        del image
        self.volume_writes.append((volume, path, data))
        if self._volume_write_error is not None:
            raise self._volume_write_error

    async def delete_volume_file(self, *, volume: str, path: str, image: str) -> None:
        del image
        self.volume_deletes.append((volume, path))
        if self._volume_delete_error is not None:
            raise self._volume_delete_error

    async def measure_volume_size(self, *, volume: str, image: str) -> int:
        self.measure_calls.append((volume, image))
        if self._measure_error is not None:
            raise self._measure_error
        return self._measured_size

    async def update_limits(
        self, container_name: str, *, cpus: float, memory_mb: int, pids_limit: int
    ) -> None:
        self.limit_updates.append((container_name, cpus, memory_mb, pids_limit))
        if self._update_error is not None:
            raise self._update_error

    async def seed_workspace(self, container_name: str, *, files: list[tuple[str, bytes]]) -> None:
        self.seeds.append((container_name, files))
        if self._seed_error is not None:
            raise self._seed_error

    async def chown_volume(self, *, volume: str, image: str) -> None:
        self.chowns.append((volume, image))
        if self._chown_error is not None:
            raise self._chown_error

    async def image_exists(self, image: str) -> bool:
        return image in self._existing_images

    async def pull_image(self, image: str) -> None:
        if self._pull_error is not None:
            raise self._pull_error
        self.pulled.append(image)
        self._existing_images.add(image)


class InMemorySandboxStore:
    """A :class:`SandboxStore` backed by a dict — no DB."""

    def __init__(self, *, limit: int | None = None) -> None:
        self.rows: dict[UUID, SandboxRecord] = {}
        self._limit = limit

    async def insert(self, record: SandboxRecord) -> None:
        self.rows[record.id] = record

    async def update(self, record: SandboxRecord) -> None:
        self.rows[record.id] = record

    async def get(self, sandbox_id: UUID) -> SandboxRecord | None:
        return self.rows.get(sandbox_id)

    async def count_active_for_tenant(self, tenant_id: UUID) -> int:
        return sum(
            1
            for r in self.rows.values()
            if r.tenant_id == tenant_id and r.state in (SandboxState.CREATING, SandboxState.IN_USE)
        )

    async def list_idle_sessions(self, *, now: datetime, idle_ttl_s: int) -> list[SandboxRecord]:
        return [
            r
            for r in self.rows.values()
            if r.state == SandboxState.IN_USE
            and (anchor := r.last_used_at or r.acquired_at) is not None
            and anchor + timedelta(seconds=idle_ttl_s) < now
        ]

    async def sandbox_limit_for_tenant(self, tenant_id: UUID) -> int | None:
        return self._limit

    async def claim_ready(self, record: SandboxRecord) -> bool:
        # HX-6 CAS mirror: rebind only while the row is still READY.
        current = self.rows.get(record.id)
        if current is None or current.state is not SandboxState.READY:
            return False
        self.rows[record.id] = record
        return True

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            sid for sid, r in self.rows.items() if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for sid in victims:
            del self.rows[sid]
        return len(victims)

    def seed_active(self, tenant_id: UUID) -> SandboxRecord:
        """Insert an IN_USE row directly — for quota / reaper setup."""
        record = _running_record(tenant_id, acquired_at=datetime.now(UTC))
        self.rows[record.id] = record
        return record


class RecordingAuditSink:
    """An :class:`AuditSink` that collects entries in memory."""

    def __init__(self) -> None:
        self.entries: list = []

    async def write(self, entry: object) -> None:
        self.entries.append(entry)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    supervisor: SandboxSupervisor
    store: InMemorySandboxStore
    docker: RecordingDockerClient
    audit: RecordingAuditSink
    workspaces: InMemoryUserWorkspaceStore


def _harness(
    *,
    store: InMemorySandboxStore | None = None,
    docker: RecordingDockerClient | None = None,
    settings: SandboxSupervisorSettings | None = None,
    quota_enabled: bool = False,
    pool: SandboxPool | None = None,
) -> _Harness:
    """Build a fake-backed supervisor harness.

    ``quota_enabled=True`` wires a real :class:`QuotaEnforcer` against
    the same fakes — used by the J.15-补强-1 acquire / release tests.
    Default ``False`` keeps the pre-existing tests unchanged (they pre-
    date workspace quota and don't expect ``measure_volume_size`` to be
    called).
    """
    resolved_store = store if store is not None else InMemorySandboxStore()
    resolved_docker = docker if docker is not None else RecordingDockerClient()
    audit = RecordingAuditSink()
    workspaces = InMemoryUserWorkspaceStore()
    resolved_settings = settings or SandboxSupervisorSettings()
    quota_enforcer = (
        QuotaEnforcer(
            workspace_store=workspaces,
            audit=audit,
            docker=resolved_docker,
            measure_image=resolved_settings.sandbox_image,
            service_name=resolved_settings.service_name,
        )
        if quota_enabled
        else None
    )
    supervisor = SandboxSupervisor(
        store=resolved_store,
        docker=resolved_docker,
        audit=audit,
        runtime_provider=SandboxRuntimeProvider(oci_runtime="runc"),
        workspace_store=workspaces,
        settings=resolved_settings,
        quota_enforcer=quota_enforcer,
        pool=pool,
    )
    return _Harness(supervisor, resolved_store, resolved_docker, audit, workspaces)


def _running_record(tenant_id: UUID, *, acquired_at: datetime) -> SandboxRecord:
    sandbox_id = uuid4()
    return SandboxRecord(
        id=sandbox_id,
        tenant_id=tenant_id,
        image_ref="expert-work-sandbox:dev",
        node="local",
        container_id=f"expert-work-sb-{sandbox_id}",
        state=SandboxState.IN_USE,
        thread_id="t-1",
        cpu_quota=1.0,
        memory_mb=512,
        pids_limit=128,
        timeout_s=30,
        created_at=acquired_at,
        acquired_at=acquired_at,
    )


def _acquire_request(
    tenant_id: UUID | None = None,
    *,
    user_id: UUID | None = None,
    seed_files: list[SeedFile] | None = None,
) -> AcquireRequest:
    return AcquireRequest(
        tenant_id=tenant_id or uuid4(),
        thread_id="t-1",
        user_id=user_id,
        seed_files=seed_files or [],
    )


# ---------------------------------------------------------------------------
# #40 — acquire / release lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_launches_container_and_marks_in_use() -> None:
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())

    assert response.container_id == f"expert-work-sb-{response.sandbox_id}"
    assert response.cold_start is True
    assert len(h.docker.launches) == 1
    row = h.store.rows[response.sandbox_id]
    assert row.state is SandboxState.IN_USE
    assert row.container_id == f"expert-work-sb-{response.sandbox_id}"
    assert row.acquired_at is not None


@pytest.mark.asyncio
async def test_acquire_observes_cold_start_histogram() -> None:
    """Stream K.K10 — a cold acquire writes the cold-start histogram.

    Warm-session reuse (covered by ``test_acquire_reuses_warm_session_for_same_user``)
    must NOT observe; that test inspects ``cold_start=False`` on the
    response, which is the same gate the histogram uses (only the
    launch path executes).
    """
    from sandbox_supervisor.supervisor import _sandbox_cold_start_seconds

    before = _sandbox_cold_start_seconds._sum.get()  # type: ignore[attr-defined]
    h = _harness()
    await h.supervisor.acquire(_acquire_request())
    after = _sandbox_cold_start_seconds._sum.get()  # type: ignore[attr-defined]
    assert after > before


@pytest.mark.asyncio
async def test_acquire_emits_sandbox_acquired_audit() -> None:
    h = _harness()
    await h.supervisor.acquire(_acquire_request())

    assert len(h.audit.entries) == 1
    entry = h.audit.entries[0]
    assert entry.action is AuditAction.SANDBOX_ACQUIRED
    assert entry.result is AuditResult.SUCCESS


# ---------------------------------------------------------------------------
# skill-runtime §5.1 — seed_files materialized into /workspace at acquire
# ---------------------------------------------------------------------------


def _seed(path: str, raw: bytes) -> SeedFile:
    import base64

    return SeedFile(path=path, content_b64=base64.b64encode(raw).decode())


@pytest.mark.asyncio
async def test_acquire_seeds_workspace_on_cold_start() -> None:
    h = _harness()
    files = [_seed("skills/pptx/SKILL.md", b"---\nname: pptx\n---\nrun it")]
    response = await h.supervisor.acquire(_acquire_request(seed_files=files))

    assert len(h.docker.seeds) == 1
    container, seeded = h.docker.seeds[0]
    assert container == response.container_id
    assert seeded == [("skills/pptx/SKILL.md", b"---\nname: pptx\n---\nrun it")]


@pytest.mark.asyncio
async def test_acquire_no_seed_when_empty() -> None:
    h = _harness()
    await h.supervisor.acquire(_acquire_request())
    assert h.docker.seeds == []  # back-compat: no seed_files → no cp


@pytest.mark.asyncio
async def test_acquire_seeds_reused_warm_session() -> None:
    h = _harness()
    tenant_id, user_id = uuid4(), uuid4()
    # First acquire establishes a warm session for the user.
    first = await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))
    # Second acquire (same user) reuses it AND must still seed.
    files = [_seed("skills/a/SKILL.md", b"x")]
    second = await h.supervisor.acquire(
        _acquire_request(tenant_id, user_id=user_id, seed_files=files)
    )
    assert second.sandbox_id == first.sandbox_id
    assert second.cold_start is False
    assert h.docker.seeds[-1] == (second.container_id, [("skills/a/SKILL.md", b"x")])


@pytest.mark.asyncio
async def test_acquire_rejects_seed_path_traversal() -> None:
    h = _harness()
    files = [_seed("../escape.py", b"x")]
    with pytest.raises(InvalidSeedFilesError):
        await h.supervisor.acquire(_acquire_request(seed_files=files))


@pytest.mark.asyncio
async def test_acquire_rejects_seed_bad_base64() -> None:
    h = _harness()
    bad = SeedFile(path="skills/a/x.py", content_b64="not!valid!base64!")
    with pytest.raises(InvalidSeedFilesError):
        await h.supervisor.acquire(_acquire_request(seed_files=[bad]))


@pytest.mark.asyncio
async def test_acquire_rejects_seed_over_total_cap() -> None:
    from sandbox_supervisor.supervisor import _MAX_SEED_TOTAL_BYTES

    h = _harness()
    files = [_seed("skills/a/big.bin", b"\x00" * (_MAX_SEED_TOTAL_BYTES + 1))]
    with pytest.raises(InvalidSeedFilesError):
        await h.supervisor.acquire(_acquire_request(seed_files=files))


@pytest.mark.asyncio
async def test_acquire_degrades_when_seed_cp_fails() -> None:
    # A docker-cp transport failure must NOT fail the acquire (skill_view is the
    # fallback) — the sandbox still comes up.
    h = _harness(docker=RecordingDockerClient(seed_error=DockerError("cp boom")))
    files = [_seed("skills/a/SKILL.md", b"x")]
    response = await h.supervisor.acquire(_acquire_request(seed_files=files))
    assert response.sandbox_id is not None
    assert h.store.rows[response.sandbox_id].state is SandboxState.IN_USE


@pytest.mark.asyncio
async def test_release_removes_container_and_marks_destroyed() -> None:
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())
    await h.supervisor.release(response.sandbox_id)

    assert h.docker.removed == [f"expert-work-sb-{response.sandbox_id}"]
    assert h.docker.links[0].closed is True
    row = h.store.rows[response.sandbox_id]
    assert row.state is SandboxState.DESTROYED
    assert row.destroy_reason == "release"
    assert row.released_at is not None


@pytest.mark.asyncio
async def test_release_does_not_emit_force_destroy_audit() -> None:
    # A routine release is not a force-destroy — only the acquire audit fires.
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())
    await h.supervisor.release(response.sandbox_id)

    actions = [e.action for e in h.audit.entries]
    assert actions == [AuditAction.SANDBOX_ACQUIRED]


@pytest.mark.asyncio
async def test_acquire_launch_failure_marks_failed_and_raises() -> None:
    h = _harness(docker=RecordingDockerClient(launch_error=DockerError("daemon down")))
    with pytest.raises(SupervisorError, match="sandbox launch failed"):
        await h.supervisor.acquire(_acquire_request())

    states = [r.state for r in h.store.rows.values()]
    assert states == [SandboxState.FAILED]
    # docker.remove() is called unconditionally in the failure path (cheap
    # and idempotent even when launch() itself never got a container up);
    # asserted here too for completeness alongside the two tests below.
    sandbox_id = next(iter(h.store.rows))
    assert h.docker.removed == [f"expert-work-sb-{sandbox_id}"]


@pytest.mark.asyncio
async def test_acquire_runner_not_ready_marks_failed_and_raises() -> None:
    # The container launches but the runner never reports ready.
    h = _harness(docker=RecordingDockerClient(link=FakeRunnerLink(ready=False)))
    with pytest.raises(SupervisorError, match="sandbox launch failed"):
        await h.supervisor.acquire(_acquire_request())

    states = [r.state for r in h.store.rows.values()]
    assert states == [SandboxState.FAILED]
    # code-reviewer Important-2: a container that launched but never
    # reported ready must not be orphaned (nothing else points to it —
    # self._links / the store's container_id are only set after this
    # whole try block succeeds) — it has to be torn down right here, not
    # left for the next supervisor-restart sweep_orphans() pass.
    sandbox_id = next(iter(h.store.rows))
    assert h.docker.removed == [f"expert-work-sb-{sandbox_id}"]


@pytest.mark.asyncio
async def test_destroy_is_idempotent() -> None:
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())
    await h.supervisor.destroy(response.sandbox_id, reason="cancelled")
    # A second destroy is a no-op — no extra docker.remove call.
    await h.supervisor.destroy(response.sandbox_id, reason="cancelled")

    assert h.docker.removed == [f"expert-work-sb-{response.sandbox_id}"]


@pytest.mark.asyncio
async def test_destroy_unknown_sandbox_raises_not_found() -> None:
    h = _harness()
    with pytest.raises(SandboxNotFoundError):
        await h.supervisor.destroy(uuid4(), reason="cancelled")


@pytest.mark.asyncio
async def test_forced_destroy_emits_force_destroy_audit() -> None:
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())
    await h.supervisor.destroy(response.sandbox_id, reason="cancelled")

    actions = [e.action for e in h.audit.entries]
    assert AuditAction.SANDBOX_FORCE_DESTROY in actions


# ---------------------------------------------------------------------------
# J.15 — the per-user persistent workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_without_user_uses_ephemeral_tmpfs() -> None:
    # No user_id → the pre-J.15 ephemeral-tmpfs path.
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())

    argv = h.docker.launches[0]
    assert "--tmpfs" in argv
    assert "--volume" not in argv
    row = h.store.rows[response.sandbox_id]
    assert row.user_id is None
    assert row.workspace_id is None


@pytest.mark.asyncio
async def test_acquire_with_user_mounts_persistent_volume() -> None:
    h = _harness()
    tenant, user = uuid4(), uuid4()
    response = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    argv = h.docker.launches[0]
    assert f"{workspace_volume_name(tenant, user)}:/workspace" in argv
    # /workspace is a named volume (not a tmpfs); the scratch /tmp tmpfs plus
    # the W2 Task 6 skills/agents/home tmpfs (unconditional) stay.
    tmpfs_targets = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    assert tmpfs_targets == [
        "/tmp:rw,size=256m,mode=1777",  # noqa: S108 — mount spec literal
        "/opt/skills:rw,size=64m,uid=10000,gid=10000",
        "/opt/agents:rw,size=256m,uid=10000,gid=10000",
        "/home/agent:rw,size=64m,uid=10000,gid=10000",
    ]

    row = h.store.rows[response.sandbox_id]
    assert row.user_id == user
    assert row.workspace_id is not None


# ---------------------------------------------------------------------------
# W2 Task 6 follow-up — chown the persistent volume after every cold start
# (docker resets --workdir /workspace's ownership to root:root on *every*
# container creation, not just the volume's first mount — see
# CliDockerClient.chown_volume's docstring for the full repro)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_chowns_persistent_volume_after_cold_start() -> None:
    h = _harness()
    tenant, user = uuid4(), uuid4()
    response = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    volume = workspace_volume_name(tenant, user)
    assert h.docker.chowns == [(volume, SandboxSupervisorSettings().sandbox_image)]
    row = h.store.rows[response.sandbox_id]
    assert row.state is SandboxState.IN_USE


@pytest.mark.asyncio
async def test_acquire_ephemeral_does_not_chown() -> None:
    # No user_id → the ephemeral tmpfs path; nothing to chown (mode=1777
    # already lets the non-root agent write regardless of ownership).
    h = _harness()
    await h.supervisor.acquire(_acquire_request())
    assert h.docker.chowns == []


@pytest.mark.asyncio
async def test_acquire_warm_session_reuse_does_not_rechown() -> None:
    # A second acquire for the same (tenant, user) that reuses the warm
    # session never calls docker.launch() at all, so there's no fresh
    # --workdir reset to fix — chown must not run again (idempotent AND
    # cheap: no extra docker round-trip on the hot warm-reuse path).
    h = _harness()
    tenant, user = uuid4(), uuid4()
    await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    assert len(h.docker.launches) == 1
    assert len(h.docker.chowns) == 1


@pytest.mark.asyncio
async def test_acquire_chown_failure_marks_failed_and_raises() -> None:
    # chown failing must fail the whole acquire closed (DockerError →
    # SupervisorError, same contract as a launch/wait_ready failure) —
    # never hand back a sandbox that silently can't write its own
    # workspace.
    h = _harness(docker=RecordingDockerClient(chown_error=DockerError("chown boom")))
    with pytest.raises(SupervisorError, match="sandbox launch failed"):
        await h.supervisor.acquire(_acquire_request(user_id=uuid4()))

    states = [r.state for r in h.store.rows.values()]
    assert states == [SandboxState.FAILED]
    # code-reviewer Important-2: chown_volume fails *after* launch() has
    # already created a live, READY container — nothing (self._links, the
    # store's container_id) points to it yet at that point, so it must be
    # torn down here or it leaks until the next supervisor-restart
    # sweep_orphans() pass.
    sandbox_id = next(iter(h.store.rows))
    assert h.docker.removed == [f"expert-work-sb-{sandbox_id}"]


@pytest.mark.asyncio
async def test_acquire_reuses_one_workspace_per_user() -> None:
    # Two acquires for the same (tenant, user) resolve the same workspace.
    h = _harness()
    tenant, user = uuid4(), uuid4()
    r1 = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    r2 = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    workspace_id = h.store.rows[r1.sandbox_id].workspace_id
    assert workspace_id is not None
    assert h.store.rows[r2.sandbox_id].workspace_id == workspace_id


@pytest.mark.asyncio
async def test_acquire_audit_flags_persistent_workspace() -> None:
    h = _harness()
    await h.supervisor.acquire(_acquire_request(user_id=uuid4()))
    assert h.audit.entries[0].details["persistent_workspace"] is True

    await h.supervisor.acquire(_acquire_request())
    assert h.audit.entries[1].details["persistent_workspace"] is False


# ---------------------------------------------------------------------------
# J.15 — warm per-user sandbox sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_reuses_warm_session_for_same_user() -> None:
    h = _harness()
    tenant, user = uuid4(), uuid4()

    first = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    second = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    # Same warm session — no second docker run, and the reuse is not cold.
    assert second.sandbox_id == first.sandbox_id
    assert second.cold_start is False
    assert len(h.docker.launches) == 1


@pytest.mark.asyncio
async def test_acquire_without_user_never_reuses() -> None:
    h = _harness()
    first = await h.supervisor.acquire(_acquire_request())
    second = await h.supervisor.acquire(_acquire_request())

    assert second.sandbox_id != first.sandbox_id
    assert len(h.docker.launches) == 2


@pytest.mark.asyncio
async def test_release_keeps_warm_session_alive() -> None:
    h = _harness()
    tenant, user = uuid4(), uuid4()
    acquired = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    await h.supervisor.release(acquired.sandbox_id)

    # A warm session is not torn down on release — it stays IN_USE...
    assert h.docker.removed == []
    assert h.store.rows[acquired.sandbox_id].state is SandboxState.IN_USE
    # ...and the user's next acquire reuses it.
    again = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    assert again.sandbox_id == acquired.sandbox_id
    assert len(h.docker.launches) == 1


@pytest.mark.asyncio
async def test_exec_stamps_last_used_at() -> None:
    h = _harness()
    acquired = await h.supervisor.acquire(_acquire_request(user_id=uuid4()))
    assert h.store.rows[acquired.sandbox_id].last_used_at is None

    await h.supervisor.exec(acquired.sandbox_id, code="print(1)")
    assert h.store.rows[acquired.sandbox_id].last_used_at is not None


@pytest.mark.asyncio
async def test_destroy_clears_warm_session() -> None:
    h = _harness()
    tenant, user = uuid4(), uuid4()
    first = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    await h.supervisor.destroy(first.sandbox_id, reason="cancelled")

    # The session entry is gone — the next acquire is a fresh cold start.
    second = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    assert second.sandbox_id != first.sandbox_id
    assert second.cold_start is True
    assert len(h.docker.launches) == 2


@pytest.mark.asyncio
async def test_reaper_reaps_idle_warm_session() -> None:
    h = _harness()
    acquired = await h.supervisor.acquire(_acquire_request(user_id=uuid4()))
    # Backdate last_used_at past the idle TTL.
    record = h.store.rows[acquired.sandbox_id]
    h.store.rows[acquired.sandbox_id] = replace(
        record, last_used_at=datetime.now(UTC) - timedelta(hours=1)
    )

    reaper = SandboxReaper(supervisor=h.supervisor, store=h.store, interval_s=10.0, idle_ttl_s=900)
    reaped = await reaper.run_once()

    assert reaped == 1
    assert h.store.rows[acquired.sandbox_id].state is SandboxState.DESTROYED
    assert h.store.rows[acquired.sandbox_id].destroy_reason == DESTROY_REASON_IDLE_TIMEOUT


async def test_reaper_force_reaps_non_idle_session() -> None:
    # Stream P (Mini-ADR P-14) — idle_ttl_s=0 treats every active session as
    # idle, so a freshly-acquired (non-idle) session is reaped.
    h = _harness()
    acquired = await h.supervisor.acquire(_acquire_request(user_id=uuid4()))
    reaper = SandboxReaper(supervisor=h.supervisor, store=h.store, interval_s=10.0, idle_ttl_s=900)

    assert await reaper.run_once(idle_ttl_s=0) == 1
    assert h.store.rows[acquired.sandbox_id].state is SandboxState.DESTROYED


def test_reap_route_without_reaper_returns_zero() -> None:
    h = _harness()
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.post("/v1/sandboxes:reap", json={"force": True})
    assert resp.status_code == 200
    assert resp.json()["reaped_count"] == 0


# ---------------------------------------------------------------------------
# J.15-补强-1 — volume quota + lifecycle (Mini-ADR J-29 第 1 项 + J-36)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_rejects_when_workspace_size_at_limit() -> None:
    """Acquire raises WorkspaceQuotaExceededError when size_bytes >= size_limit_bytes."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    workspace = await h.workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    # Lower the limit + push size above it to force the reject.
    await h.workspaces.update_size(workspace_id=workspace.id, size_bytes=2048)
    # InMemory model_copy idiom — modify the row's size_limit_bytes.
    h.workspaces._rows[(tenant_id, user_id)] = h.workspaces._rows[(tenant_id, user_id)].model_copy(
        update={"size_limit_bytes": 1024}
    )

    with pytest.raises(WorkspaceQuotaExceededError):
        await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    # Audit captured the deny.
    actions = [e.action for e in h.audit.entries]
    assert AuditAction.WORKSPACE_QUOTA_DENIED in actions
    # And no docker launch happened.
    assert h.docker.launches == []


@pytest.mark.asyncio
async def test_acquire_rejects_when_workspace_is_soft_deleted() -> None:
    """Acquire raises WorkspaceDeletedError when deleted_at is set."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    workspace = await h.workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    await h.workspaces.soft_delete(workspace_id=workspace.id, now=datetime.now(UTC))

    with pytest.raises(WorkspaceDeletedError):
        await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))


@pytest.mark.asyncio
async def test_acquire_succeeds_under_quota_with_enforcer() -> None:
    """A healthy under-quota acquire still works when the enforcer is wired."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    response = await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    assert response.cold_start is True
    # No quota-denied audit; only the SANDBOX_ACQUIRED success.
    quota_denies = [e for e in h.audit.entries if e.action is AuditAction.WORKSPACE_QUOTA_DENIED]
    assert quota_denies == []


@pytest.mark.asyncio
async def test_reuse_session_rechecks_quota() -> None:
    """A warm-session reuse re-runs the workspace quota check (size can grow between
    acquires; soft-delete can land while a session is warm)."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    # Volume "grew" past quota between runs — push size + tighten limit.
    workspace = await h.workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    await h.workspaces.update_size(workspace_id=workspace.id, size_bytes=4096)
    h.workspaces._rows[(tenant_id, user_id)] = h.workspaces._rows[(tenant_id, user_id)].model_copy(
        update={"size_limit_bytes": 1024}
    )

    with pytest.raises(WorkspaceQuotaExceededError):
        await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))


@pytest.mark.asyncio
async def test_release_fires_size_refresh_and_keeps_session_warm() -> None:
    """Releasing a user-scoped acquire keeps the warm session AND schedules a du.

    The QuotaEnforcer.refresh_size call is fire-and-forget — we let the
    event loop run pending tasks via ``asyncio.sleep(0)`` so the fake
    docker's ``measure_calls`` and the store's ``size_bytes`` are
    populated before we assert.
    """
    import asyncio as _asyncio  # local import keeps the test scope explicit

    docker = RecordingDockerClient(measured_size=12345)
    h = _harness(docker=docker, quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    response = await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    await h.supervisor.release(response.sandbox_id)

    # Yield once so the create_task'd refresh runs to completion.
    for _ in range(5):
        await _asyncio.sleep(0)

    # Warm session stayed alive.
    row = h.store.rows[response.sandbox_id]
    assert row.state is SandboxState.IN_USE
    # And du was scheduled + written back.
    assert len(docker.measure_calls) == 1
    refreshed = await h.workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    assert refreshed.size_bytes == 12345


@pytest.mark.asyncio
async def test_mark_workspace_deleted_soft_deletes_and_destroys_warm_session() -> None:
    """mark_workspace_deleted soft-deletes the row + force-destroys any warm session."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    await h.supervisor.mark_workspace_deleted(tenant_id=tenant_id, user_id=user_id)

    workspace = await h.workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    assert workspace.deleted_at is not None

    # Warm session was force-destroyed; subsequent acquire rejects.
    with pytest.raises(WorkspaceDeletedError):
        await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    # Audit captured the soft-delete + the implicit force-destroy.
    actions = [e.action for e in h.audit.entries]
    assert AuditAction.WORKSPACE_SOFT_DELETE in actions
    assert AuditAction.SANDBOX_FORCE_DESTROY in actions

    # The workspace audit entry uses resource_type="user_workspace".
    workspace_entries = [
        e for e in h.audit.entries if e.action is AuditAction.WORKSPACE_SOFT_DELETE
    ]
    assert len(workspace_entries) == 1
    assert workspace_entries[0].resource_type == "user_workspace"
    assert workspace_entries[0].resource_id == str(workspace.id)


@pytest.mark.asyncio
async def test_mark_workspace_deleted_is_idempotent() -> None:
    """A second mark_workspace_deleted is a no-op (no extra audit, no extra destroy)."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id = uuid4(), uuid4()
    await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))

    await h.supervisor.mark_workspace_deleted(tenant_id=tenant_id, user_id=user_id)
    audits_after_first = len(h.audit.entries)

    await h.supervisor.mark_workspace_deleted(tenant_id=tenant_id, user_id=user_id)
    # Second call adds no new audit entries.
    assert len(h.audit.entries) == audits_after_first


@pytest.mark.asyncio
async def test_mark_workspace_deleted_hard_deletes_only_that_users_sandbox_rows() -> None:
    """Phase 3a — mark_workspace_deleted drops the user's sandbox_instance rows.

    The warm session is force-destroyed first, THEN the user's rows are
    hard-deleted (no PII left). Another user's rows in the same tenant survive."""
    h = _harness(quota_enabled=True)
    tenant_id, user_id, other_user = uuid4(), uuid4(), uuid4()
    await h.supervisor.acquire(_acquire_request(tenant_id, user_id=user_id))
    # A second user's row in the same tenant (seeded directly) must survive.
    other = _running_record(tenant_id, acquired_at=datetime.now(UTC))
    other = replace(other, user_id=other_user)
    await h.store.insert(other)

    await h.supervisor.mark_workspace_deleted(tenant_id=tenant_id, user_id=user_id)

    assert all(r.user_id != user_id for r in h.store.rows.values())  # purged user's rows gone
    assert any(r.user_id == other_user for r in h.store.rows.values())  # other user untouched


# ---------------------------------------------------------------------------
# J.9 — workspace file read (artifact content download)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_workspace_file_returns_content() -> None:
    h = _harness(docker=RecordingDockerClient(volume_file=b"report body"))
    data = await h.supervisor.read_workspace_file(
        tenant_id=uuid4(), user_id=uuid4(), path="report.md"
    )
    assert data == b"report body"
    # The volume name is the deterministic per-(tenant, user) identifier.
    assert h.docker.volume_reads[0][1] == "report.md"


@pytest.mark.asyncio
async def test_read_workspace_file_rejects_unsafe_path() -> None:
    h = _harness()
    for bad in ("/etc/passwd", "../escape"):
        with pytest.raises(WorkspaceFileNotFoundError):
            await h.supervisor.read_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path=bad)
    # A rejected path never reaches docker.
    assert h.docker.volume_reads == []


@pytest.mark.asyncio
async def test_read_workspace_file_missing_maps_to_not_found() -> None:
    h = _harness(docker=RecordingDockerClient(volume_file_error=DockerError("no such file")))
    with pytest.raises(WorkspaceFileNotFoundError):
        await h.supervisor.read_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path="gone.md")


@pytest.mark.asyncio
async def test_read_workspace_file_too_large_raises() -> None:
    oversize = b"\0" * (_MAX_ARTIFACT_BYTES + 1)
    h = _harness(docker=RecordingDockerClient(volume_file=oversize))
    with pytest.raises(WorkspaceFileTooLargeError):
        await h.supervisor.read_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path="big.bin")


@pytest.mark.asyncio
async def test_list_workspace_files_returns_entries() -> None:
    h = _harness(
        docker=RecordingDockerClient(volume_files=[(2048, "report.pdf"), (12, "notes.txt")])
    )
    entries = await h.supervisor.list_workspace_files(tenant_id=uuid4(), user_id=uuid4())
    assert entries == [(2048, "report.pdf"), (12, "notes.txt")]
    assert len(h.docker.volume_lists) == 1


@pytest.mark.asyncio
async def test_list_workspace_files_hides_reserved_prefixes() -> None:
    # ``skills/`` (seeded machinery) + ``uploads/`` (user inputs) are reserved
    # namespaces — the browse listing shows only the agent's own output.
    h = _harness(
        docker=RecordingDockerClient(
            volume_files=[
                (8900, "skills/pptx/SKILL.md"),
                (4900, "skills/pptx/scripts/office/pack.py"),
                (50, "uploads/ticket.pdf"),
                (2048, "report.pdf"),
                (12, "out/notes.txt"),
            ]
        )
    )
    entries = await h.supervisor.list_workspace_files(tenant_id=uuid4(), user_id=uuid4())
    assert entries == [(2048, "report.pdf"), (12, "out/notes.txt")]


@pytest.mark.asyncio
async def test_list_workspace_files_missing_volume_is_empty() -> None:
    # A volume that has never been created surfaces as an empty inventory,
    # not an error — the inspector renders "no files" the same as "no volume".
    h = _harness(docker=RecordingDockerClient(volume_list_error=DockerError("no such volume")))
    entries = await h.supervisor.list_workspace_files(tenant_id=uuid4(), user_id=uuid4())
    assert entries == []


@pytest.mark.asyncio
async def test_delete_workspace_file_delegates_to_docker() -> None:
    h = _harness()
    await h.supervisor.delete_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path="out/old.md")
    assert len(h.docker.volume_deletes) == 1
    assert h.docker.volume_deletes[0][1] == "out/old.md"


@pytest.mark.asyncio
async def test_delete_workspace_file_rejects_unsafe_path() -> None:
    h = _harness()
    for bad in ("/etc/passwd", "../escape"):
        with pytest.raises(WorkspaceFileNotFoundError):
            await h.supervisor.delete_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path=bad)
    assert h.docker.volume_deletes == []


@pytest.mark.asyncio
async def test_delete_workspace_file_refuses_reserved_prefix() -> None:
    # Seeded machinery / uploads are reserved — a client can only delete what
    # the agent itself produced, never the skill packages or its own inputs.
    h = _harness()
    for reserved in ("skills/pptx/SKILL.md", "uploads/ticket.pdf"):
        with pytest.raises(WorkspaceFileNotFoundError):
            await h.supervisor.delete_workspace_file(
                tenant_id=uuid4(), user_id=uuid4(), path=reserved
            )
    assert h.docker.volume_deletes == []


def test_list_workspace_files_route_returns_files() -> None:
    h = _harness(docker=RecordingDockerClient(volume_files=[(2048, "report.pdf")]))
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.get(f"/v1/workspaces/{uuid4()}/{uuid4()}/files")
    assert resp.status_code == 200
    assert resp.json() == {"files": [{"path": "report.pdf", "size": 2048}]}


def test_read_workspace_file_route_returns_content() -> None:
    h = _harness(docker=RecordingDockerClient(volume_file=b"hi"))
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.get(f"/v1/workspaces/{uuid4()}/{uuid4()}/file", params={"path": "x.txt"})
    assert resp.status_code == 200
    assert resp.content == b"hi"


def test_read_workspace_file_route_404_on_missing() -> None:
    h = _harness(docker=RecordingDockerClient(volume_file_error=DockerError("no such file")))
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.get(f"/v1/workspaces/{uuid4()}/{uuid4()}/file", params={"path": "gone"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_write_workspace_file_writes_to_volume() -> None:
    h = _harness(docker=RecordingDockerClient())
    await h.supervisor.write_workspace_file(
        tenant_id=uuid4(), user_id=uuid4(), path="uploads/report.pdf", data=b"PDF BYTES"
    )
    assert len(h.docker.volume_writes) == 1
    _volume, path, data = h.docker.volume_writes[0]
    assert path == "uploads/report.pdf"
    assert data == b"PDF BYTES"


@pytest.mark.asyncio
async def test_write_workspace_file_rejects_unsafe_path() -> None:
    h = _harness(docker=RecordingDockerClient())
    for bad in ("/etc/passwd", "../escape.txt"):
        with pytest.raises(WorkspaceFileNotFoundError):
            await h.supervisor.write_workspace_file(
                tenant_id=uuid4(), user_id=uuid4(), path=bad, data=b"x"
            )
    assert h.docker.volume_writes == []


@pytest.mark.asyncio
async def test_write_workspace_file_too_large_raises() -> None:
    h = _harness(docker=RecordingDockerClient())
    oversize = b"\0" * (_MAX_WORKSPACE_WRITE_BYTES + 1)
    with pytest.raises(WorkspaceFileTooLargeError):
        await h.supervisor.write_workspace_file(
            tenant_id=uuid4(), user_id=uuid4(), path="uploads/big.pdf", data=oversize
        )
    # The size gate runs before docker — nothing is written.
    assert h.docker.volume_writes == []


@pytest.mark.asyncio
async def test_write_workspace_file_docker_error_maps_to_supervisor_error() -> None:
    docker = RecordingDockerClient()
    docker._volume_write_error = DockerError("disk full")
    h = _harness(docker=docker)
    with pytest.raises(SupervisorError):
        await h.supervisor.write_workspace_file(
            tenant_id=uuid4(), user_id=uuid4(), path="uploads/x.txt", data=b"x"
        )


def test_write_workspace_file_route_writes_and_204() -> None:
    h = _harness(docker=RecordingDockerClient())
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.put(
            f"/v1/workspaces/{uuid4()}/{uuid4()}/file",
            params={"path": "uploads/memo.docx"},
            content=b"DOCX BYTES",
        )
    assert resp.status_code == 204
    assert h.docker.volume_writes[0][1] == "uploads/memo.docx"
    assert h.docker.volume_writes[0][2] == b"DOCX BYTES"


def test_metrics_route_exposes_prometheus_text() -> None:
    # Stream P (Mini-ADR P-15) — the supervisor is a standalone scrape target;
    # /metrics must expose its in-process registry (cold-start histogram et al.)
    # so Prometheus can collect the Phase 6 sandbox SLO.
    h = _harness(docker=RecordingDockerClient(volume_file=b"hi"))
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    # The cold-start histogram is module-level in supervisor.py, registered at
    # import, so it appears in the exposition even before any observation.
    assert "expert_work_sandbox_cold_start_seconds" in resp.text


# ---------------------------------------------------------------------------
# F.4a — the held-pipe exec channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_runs_code_via_the_runner_link() -> None:
    link = FakeRunnerLink(
        exec_result=ExecResult(stdout="42\n", stderr="", exit_code=0, timed_out=False)
    )
    h = _harness(docker=RecordingDockerClient(link=link))
    response = await h.supervisor.acquire(_acquire_request())

    result = await h.supervisor.exec(response.sandbox_id, code="print(42)", timeout_s=10)
    assert result.stdout == "42\n"
    assert result.exit_code == 0
    assert link.exec_calls == [("print(42)", 10)]


@pytest.mark.asyncio
async def test_exec_defaults_timeout_to_service_default() -> None:
    link = FakeRunnerLink()
    h = _harness(
        docker=RecordingDockerClient(link=link),
        settings=SandboxSupervisorSettings(default_timeout_s=25),
    )
    response = await h.supervisor.acquire(_acquire_request())

    await h.supervisor.exec(response.sandbox_id, code="print(1)")
    assert link.exec_calls == [("print(1)", 25)]


@pytest.mark.asyncio
async def test_exec_forwards_envs_to_the_runner_link() -> None:
    """sandbox migration wave 2 (spec 决策 10) — ``envs`` reaches the runner
    link on every call (not baked in at acquire time — two agents can share
    one already-warm session, see ``SandboxSupervisor.exec`` docstring)."""
    link = FakeRunnerLink()
    h = _harness(docker=RecordingDockerClient(link=link))
    response = await h.supervisor.acquire(_acquire_request())

    await h.supervisor.exec(
        response.sandbox_id, code="print(1)", envs={"PYTHONUSERBASE": "/opt/agents/a1"}
    )
    assert link.exec_envs_calls == [{"PYTHONUSERBASE": "/opt/agents/a1"}]


@pytest.mark.asyncio
async def test_exec_omits_envs_when_not_given() -> None:
    link = FakeRunnerLink()
    h = _harness(docker=RecordingDockerClient(link=link))
    response = await h.supervisor.acquire(_acquire_request())

    await h.supervisor.exec(response.sandbox_id, code="print(1)")
    assert link.exec_envs_calls == [None]


@pytest.mark.asyncio
async def test_exec_unknown_sandbox_raises_not_found() -> None:
    h = _harness()
    with pytest.raises(SandboxNotFoundError):
        await h.supervisor.exec(uuid4(), code="print(1)")


@pytest.mark.asyncio
async def test_exec_link_failure_raises_supervisor_error() -> None:
    link = FakeRunnerLink(exec_error=RunnerLinkError("runner closed the connection"))
    h = _harness(docker=RecordingDockerClient(link=link))
    response = await h.supervisor.acquire(_acquire_request())

    with pytest.raises(SupervisorError, match="sandbox exec failed"):
        await h.supervisor.exec(response.sandbox_id, code="print(1)")


@pytest.mark.asyncio
async def test_exec_unavailable_after_release() -> None:
    h = _harness()
    response = await h.supervisor.acquire(_acquire_request())
    await h.supervisor.release(response.sandbox_id)
    # The link was dropped on release — exec can no longer reach it.
    with pytest.raises(SandboxNotFoundError):
        await h.supervisor.exec(response.sandbox_id, code="print(1)")


# ---------------------------------------------------------------------------
# #41 — quota denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_denied_when_tenant_at_quota() -> None:
    tenant = uuid4()
    store = InMemorySandboxStore(limit=2)
    store.seed_active(tenant)
    store.seed_active(tenant)
    h = _harness(store=store)

    with pytest.raises(QuotaExceededError) as excinfo:
        await h.supervisor.acquire(_acquire_request(tenant))
    assert excinfo.value.limit == 2
    # The container was never launched.
    assert h.docker.launches == []


@pytest.mark.asyncio
async def test_quota_denial_emits_audit() -> None:
    tenant = uuid4()
    store = InMemorySandboxStore(limit=1)
    store.seed_active(tenant)
    h = _harness(store=store)

    with pytest.raises(QuotaExceededError):
        await h.supervisor.acquire(_acquire_request(tenant))

    assert len(h.audit.entries) == 1
    entry = h.audit.entries[0]
    assert entry.action is AuditAction.SANDBOX_QUOTA_DENIED
    assert entry.result is AuditResult.DENIED
    assert entry.reason is not None


@pytest.mark.asyncio
async def test_acquire_falls_back_to_default_quota_without_a_row() -> None:
    # No tenant_quota row (limit=None) → the settings default applies.
    tenant = uuid4()
    store = InMemorySandboxStore(limit=None)
    store.seed_active(tenant)
    h = _harness(store=store, settings=SandboxSupervisorSettings(default_max_sandboxes=1))

    with pytest.raises(QuotaExceededError) as excinfo:
        await h.supervisor.acquire(_acquire_request(tenant))
    assert excinfo.value.limit == 1


@pytest.mark.asyncio
async def test_acquire_allowed_below_quota() -> None:
    tenant = uuid4()
    store = InMemorySandboxStore(limit=5)
    store.seed_active(tenant)
    h = _harness(store=store)

    response = await h.supervisor.acquire(_acquire_request(tenant))
    assert response.sandbox_id in store.rows


# ---------------------------------------------------------------------------
# #42 — TTL reaper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_destroys_orphaned_sandbox() -> None:
    h = _harness()
    orphan = h.store.seed_active(uuid4())
    # Backdate acquired_at well past the idle TTL (900s).
    stale = replace(orphan, acquired_at=datetime.now(UTC) - timedelta(hours=1))
    h.store.rows[orphan.id] = stale

    reaper = SandboxReaper(supervisor=h.supervisor, store=h.store, interval_s=10.0, idle_ttl_s=900)
    reaped = await reaper.run_once()

    assert reaped == 1
    assert h.store.rows[orphan.id].state is SandboxState.DESTROYED
    assert h.store.rows[orphan.id].destroy_reason == DESTROY_REASON_IDLE_TIMEOUT
    assert f"expert-work-sb-{orphan.id}" in h.docker.removed


@pytest.mark.asyncio
async def test_reaper_leaves_fresh_sandbox_alone() -> None:
    h = _harness()
    fresh = h.store.seed_active(uuid4())  # acquired_at = now

    reaper = SandboxReaper(supervisor=h.supervisor, store=h.store, interval_s=10.0, idle_ttl_s=900)
    reaped = await reaper.run_once()

    assert reaped == 0
    assert h.store.rows[fresh.id].state is SandboxState.IN_USE


# ---------------------------------------------------------------------------
# HTTP route smoke tests
# ---------------------------------------------------------------------------


def test_acquire_route_returns_response() -> None:
    h = _harness()
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/sandboxes:acquire",
            json={"tenant_id": str(uuid4()), "thread_id": "t-1"},
        )
    assert resp.status_code == 200
    assert resp.json()["container_id"].startswith("expert-work-sb-")


def test_exec_route_returns_runner_output() -> None:
    link = FakeRunnerLink(
        exec_result=ExecResult(stdout="hi\n", stderr="", exit_code=0, timed_out=False)
    )
    h = _harness(docker=RecordingDockerClient(link=link))
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        acquired = client.post(
            "/v1/sandboxes:acquire",
            json={"tenant_id": str(uuid4()), "thread_id": "t-1"},
        ).json()
        resp = client.post(
            f"/v1/sandboxes/{acquired['sandbox_id']}:exec",
            json={"code": "print('hi')"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stdout"] == "hi\n"
    assert body["exit_code"] == 0


def test_release_route_returns_204() -> None:
    h = _harness()
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        acquired = client.post(
            "/v1/sandboxes:acquire",
            json={"tenant_id": str(uuid4()), "thread_id": "t-1"},
        ).json()
        resp = client.post(f"/v1/sandboxes/{acquired['sandbox_id']}:release")
    assert resp.status_code == 204


def test_destroy_route_returns_204() -> None:
    # The forced-teardown route the F.7 cancellation path calls.
    h = _harness()
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        acquired = client.post(
            "/v1/sandboxes:acquire",
            json={"tenant_id": str(uuid4()), "thread_id": "t-1"},
        ).json()
        resp = client.post(
            f"/v1/sandboxes/{acquired['sandbox_id']}:destroy",
            json={"reason": "cancelled"},
        )
    assert resp.status_code == 204


def test_acquire_route_returns_429_when_at_quota() -> None:
    tenant = uuid4()
    store = InMemorySandboxStore(limit=1)
    store.seed_active(tenant)
    h = _harness(store=store)
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/sandboxes:acquire",
            json={"tenant_id": str(tenant), "thread_id": "t-1"},
        )
    assert resp.status_code == 429


def test_health_route_reports_docker_status() -> None:
    h = _harness()
    app = create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["docker_ok"] is True
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Image selection — single image (variant collapsed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_always_selects_single_image() -> None:
    # The image variant split was collapsed into one image — acquire always
    # uses settings.sandbox_image regardless of the (deprecated, ignored)
    # request field.
    settings = SandboxSupervisorSettings(sandbox_image="expert-work-sandbox:test")
    h = _harness(settings=settings)
    resp = await h.supervisor.acquire(_acquire_request())
    assert h.store.rows[resp.sandbox_id].image_ref == "expert-work-sandbox:test"


# ---------------------------------------------------------------------------
# Stream HX-6 — warm READY pool + claim-time limit pairing
# ---------------------------------------------------------------------------


def _replenisher(
    h: _Harness,
    pool: SandboxPool,
    *,
    pool_size: int = 1,
) -> PoolReplenisher:
    settings = SandboxSupervisorSettings(pool_size=pool_size)
    return PoolReplenisher(
        pool=pool,
        store=h.store,
        docker=h.docker,
        runtime_provider=SandboxRuntimeProvider(oci_runtime="runc"),
        settings=settings,
    )


@pytest.mark.asyncio
async def test_replenisher_tops_up_to_target() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=2).run_once()

    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 2
    assert len(h.docker.launches) == 2
    ready = [r for r in h.store.rows.values() if r.state is SandboxState.READY]
    assert len(ready) == 2
    # Pool rows are platform-neutral until claim binds a real tenant.
    assert all(r.tenant_id == POOL_TENANT_ID for r in ready)
    assert all(r.user_id is None for r in ready)


@pytest.mark.asyncio
async def test_replenisher_zero_target_launches_nothing() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=0).run_once()

    assert h.docker.launches == []
    assert h.store.rows == {}


@pytest.mark.asyncio
async def test_replenisher_shrinks_past_target() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=2).run_once()

    # The operator lowered the target — the next sweep destroys extras.
    await _replenisher(h, pool, pool_size=1).run_once()

    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 1
    assert len(h.docker.removed) == 1
    destroyed = [r for r in h.store.rows.values() if r.state is SandboxState.DESTROYED]
    assert len(destroyed) == 1
    assert destroyed[0].destroy_reason == DESTROY_REASON_POOL_SHRUNK


@pytest.mark.asyncio
async def test_replenisher_launch_failure_is_fail_open() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool, docker=RecordingDockerClient(launch_error=DockerError("daemon down")))

    # No raise: the pool just stays short; the next tick retries.
    await _replenisher(h, pool, pool_size=2).run_once()

    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 0
    states = [r.state for r in h.store.rows.values()]
    assert states == [SandboxState.FAILED]


@pytest.mark.asyncio
async def test_acquire_claims_pooled_container() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()
    tenant = uuid4()

    request = AcquireRequest(
        tenant_id=tenant, thread_id="t-1", cpu=2.0, memory_mb=1024, pids_limit=256
    )
    response = await h.supervisor.acquire(request)

    # Claimed, not cold-started — the pool launch is the only docker run.
    assert response.cold_start is False
    assert len(h.docker.launches) == 1
    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 0
    # The request's limits were paired onto the claimed container.
    assert h.docker.limit_updates == [(f"expert-work-sb-{response.sandbox_id}", 2.0, 1024, 256)]
    # The row was rebound: tenant + IN_USE + per-acquire limits.
    row = h.store.rows[response.sandbox_id]
    assert row.state is SandboxState.IN_USE
    assert row.tenant_id == tenant
    assert row.cpu_quota == 2.0
    assert row.memory_mb == 1024
    # Audit marks the claim as pooled.
    entry = h.audit.entries[-1]
    assert entry.action is AuditAction.SANDBOX_ACQUIRED
    assert entry.details["pooled"] is True
    # exec works over the pool container's held link.
    result = await h.supervisor.exec(response.sandbox_id, code="print(1)")
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_acquire_pool_empty_falls_back_to_cold_start() -> None:
    pool = SandboxPool()  # built but never replenished
    h = _harness(pool=pool)

    response = await h.supervisor.acquire(_acquire_request())

    assert response.cold_start is True
    assert len(h.docker.launches) == 1
    assert h.docker.limit_updates == []


@pytest.mark.asyncio
async def test_claim_update_failure_destroys_and_cold_starts() -> None:
    pool = SandboxPool()
    docker = RecordingDockerClient(update_error=DockerError("update refused"))
    h = _harness(pool=pool, docker=docker)
    await _replenisher(h, pool, pool_size=1).run_once()

    response = await h.supervisor.acquire(_acquire_request())

    # Fail-closed: the mispaired container is destroyed; the acquire
    # still succeeds via a fresh cold start (1 pool + 1 cold launch).
    assert response.cold_start is True
    assert len(h.docker.launches) == 2
    assert len(h.docker.removed) == 1
    discarded = [r for r in h.store.rows.values() if r.state is SandboxState.DESTROYED]
    assert len(discarded) == 1
    assert discarded[0].destroy_reason == DESTROY_REASON_POOL_CLAIM_FAILED


@pytest.mark.asyncio
async def test_concurrent_claims_one_hit_one_cold() -> None:
    import asyncio as _asyncio

    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()

    first, second = await _asyncio.gather(
        h.supervisor.acquire(_acquire_request()),
        h.supervisor.acquire(_acquire_request()),
    )

    assert sorted([first.cold_start, second.cold_start]) == [False, True]
    assert first.sandbox_id != second.sandbox_id
    # 1 pool launch + exactly 1 cold launch — never a double claim.
    assert len(h.docker.launches) == 2


@pytest.mark.asyncio
async def test_claim_cas_lost_falls_back_without_touching_container() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()
    # Simulate the defensive CAS-lost branch: the row is no longer READY.
    (ready_id,) = [r.id for r in h.store.rows.values() if r.state is SandboxState.READY]
    h.store.rows[ready_id] = h.store.rows[ready_id].with_state(SandboxState.IN_USE)

    response = await h.supervisor.acquire(_acquire_request())

    assert response.cold_start is True
    # The lost container was not destroyed and its limits were not touched.
    assert h.docker.removed == []
    assert h.docker.limit_updates == []


@pytest.mark.asyncio
async def test_user_scoped_acquire_bypasses_pool() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()

    response = await h.supervisor.acquire(_acquire_request(user_id=uuid4()))

    # A persistent-workspace acquire can never claim from the pool —
    # the named volume must mount at docker run time (Mini-ADR HX-F2).
    assert response.cold_start is True
    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 1
    assert h.docker.limit_updates == []


@pytest.mark.asyncio
async def test_warm_session_takes_priority_over_pool() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()
    tenant, user = uuid4(), uuid4()

    first = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))
    second = await h.supervisor.acquire(_acquire_request(tenant, user_id=user))

    # J.15 session reuse wins; the pool inventory is untouched.
    assert second.sandbox_id == first.sandbox_id
    assert second.cold_start is False
    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 1


@pytest.mark.asyncio
async def test_ready_pool_rows_do_not_count_toward_tenant_quota() -> None:
    pool = SandboxPool()
    store = InMemorySandboxStore(limit=1)
    h = _harness(pool=pool, store=store)
    await _replenisher(h, pool, pool_size=2).run_once()

    # READY rows count against nobody — the sentinel has no active rows...
    assert await store.count_active_for_tenant(POOL_TENANT_ID) == 0
    # ...and a tenant at limit=1 can still claim one (quota holds after).
    tenant = uuid4()
    first = await h.supervisor.acquire(_acquire_request(tenant))
    assert first.cold_start is False
    with pytest.raises(QuotaExceededError):
        await h.supervisor.acquire(_acquire_request(tenant))


def test_pool_size_settings_clamped_to_bounds() -> None:
    # Defensive parse (fail-open): out-of-range targets clamp, not crash.
    assert SandboxSupervisorSettings(pool_size=99).pool_size == 16
    assert SandboxSupervisorSettings(pool_size=-3).pool_size == 0


@pytest.mark.asyncio
async def test_released_pooled_claim_is_destroyed_like_any_ephemeral() -> None:
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()

    response = await h.supervisor.acquire(_acquire_request())
    assert response.cold_start is False
    await h.supervisor.release(response.sandbox_id)

    # No user_id → release destroys (the claim does not return to the pool).
    row = h.store.rows[response.sandbox_id]
    assert row.state is SandboxState.DESTROYED
    assert row.destroy_reason == "release"


# ---------------------------------------------------------------------------
# Stream HX-6 PR2 — image prefetch + READY gauge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_pulls_missing_images() -> None:
    docker = RecordingDockerClient()
    settings = SandboxSupervisorSettings(sandbox_image="expert-work-sandbox:dev")

    await prefetch_images(docker, settings)

    assert docker.pulled == ["expert-work-sandbox:dev"]


@pytest.mark.asyncio
async def test_prefetch_skips_present_images() -> None:
    docker = RecordingDockerClient(existing_images={SandboxSupervisorSettings().sandbox_image})

    await prefetch_images(docker, SandboxSupervisorSettings())

    assert docker.pulled == []


@pytest.mark.asyncio
async def test_prefetch_pull_failure_is_fail_open() -> None:
    # A registry failure must not raise out of the prefetch task — the
    # other image is still attempted and docker run pulls on demand.
    docker = RecordingDockerClient(pull_error=DockerError("registry unreachable"))

    await prefetch_images(docker, SandboxSupervisorSettings())

    assert docker.pulled == []


@pytest.mark.asyncio
async def test_replenisher_sets_ready_gauge() -> None:
    from sandbox_supervisor.pool import _pool_ready

    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=2).run_once()

    assert float(_pool_ready.labels(variant="default")._value.get()) == 2.0  # type: ignore[attr-defined]

    # A claim drains one; the next sweep re-records the lower level
    # (target raced down to 1 keeps the claimed one out of the pool).
    await h.supervisor.acquire(_acquire_request())
    await _replenisher(h, pool, pool_size=1).run_once()
    assert float(_pool_ready.labels(variant="default")._value.get()) == 1.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# sandbox-egress §3.3 — per-sandbox egress proxy env + token injection
# ---------------------------------------------------------------------------


def _egress_envs(argv: list[str]) -> dict[str, str]:
    """Pull the ``--env KEY=VALUE`` flags out of a docker run argv."""
    out: dict[str, str] = {}
    for i, flag in enumerate(argv):
        if flag == "--env":
            key, _, value = argv[i + 1].partition("=")
            out[key] = value
    return out


@pytest.mark.asyncio
async def test_acquire_with_egress_injects_proxy_env_and_signed_token() -> None:
    secret = "unit-egress-secret"
    h = _harness(
        settings=SandboxSupervisorSettings(
            egress_token_secret=secret,
            egress_proxy_host="credential-proxy.internal",
            egress_proxy_port=8081,
        )
    )
    tenant = uuid4()
    response = await h.supervisor.acquire(
        AcquireRequest(
            tenant_id=tenant,
            thread_id="t-1",
            egress="proxy",
            agent_name="pptx-agent",
            agent_version="1.2.0",
        )
    )
    envs = _egress_envs(h.docker.launches[0])
    assert envs["HTTPS_PROXY"] == envs["HTTP_PROXY"]
    assert envs["HTTPS_PROXY"].startswith("http://")
    assert envs["NO_PROXY"].startswith("credential-proxy.internal")

    # http://<token>:@credential-proxy.internal:8081 — pull the token back out
    # and verify it is a real, signed, agent-bound egress token.
    token = envs["HTTPS_PROXY"].split("//", 1)[1].split(":@", 1)[0]
    assert "credential-proxy.internal:8081" in envs["HTTPS_PROXY"]
    identity = verify_egress_token(secret, token, now=time.time())
    assert identity is not None
    assert identity.tenant_id == str(tenant)
    assert identity.agent_name == "pptx-agent"
    assert identity.agent_version == "1.2.0"
    assert identity.sandbox_id == str(response.sandbox_id)

    # §3.5 — the urllib CONNECT shim auth: base64("<token>:"), the exact bytes a
    # Basic proxy-auth header carries. The sitecustomize shim baked into the image
    # adds this to urllib's CONNECT (which otherwise drops the proxy userinfo).
    assert envs["EXPERT_WORK_EGRESS_PROXY_AUTH"] == base64.b64encode(f"{token}:".encode()).decode(
        "ascii"
    )


@pytest.mark.asyncio
async def test_acquire_egress_allowlist_embedded_in_token() -> None:
    secret = "unit-egress-secret"
    h = _harness(settings=SandboxSupervisorSettings(egress_token_secret=secret))
    await h.supervisor.acquire(
        AcquireRequest(
            tenant_id=uuid4(),
            thread_id="t-1",
            egress="proxy",
            agent_name="a",
            agent_version="1.0.0",
            egress_allowlist=["api.openai.com", "files.example.com"],
        )
    )
    envs = _egress_envs(h.docker.launches[0])
    token = envs["HTTPS_PROXY"].split("//", 1)[1].split(":@", 1)[0]
    identity = verify_egress_token(secret, token, now=time.time())
    assert identity is not None
    assert identity.allowlist == ("api.openai.com", "files.example.com")


@pytest.mark.asyncio
async def test_acquire_without_egress_has_no_proxy_env() -> None:
    h = _harness()
    await h.supervisor.acquire(_acquire_request())  # egress defaults to None
    assert _egress_envs(h.docker.launches[0]) == {}


@pytest.mark.asyncio
async def test_acquire_egress_none_has_no_proxy_env() -> None:
    h = _harness()
    await h.supervisor.acquire(AcquireRequest(tenant_id=uuid4(), thread_id="t-1", egress="none"))
    assert _egress_envs(h.docker.launches[0]) == {}


@pytest.mark.asyncio
async def test_egress_acquire_bypasses_pool() -> None:
    # A ready pooled container exists, but an egress acquire must cold-start
    # (the pooled container has no per-sandbox token baked in).
    pool = SandboxPool()
    h = _harness(pool=pool)
    await _replenisher(h, pool, pool_size=1).run_once()
    launches_after_fill = len(h.docker.launches)
    settings = SandboxSupervisorSettings()
    assert pool.size(settings.sandbox_image) == 1

    await h.supervisor.acquire(
        AcquireRequest(
            tenant_id=uuid4(),
            thread_id="t-1",
            egress="proxy",
            agent_name="a",
            agent_version="1.0.0",
        )
    )
    # A NEW cold launch happened and the pooled container was NOT claimed.
    assert len(h.docker.launches) == launches_after_fill + 1
    assert pool.size(settings.sandbox_image) == 1


def test_tmpfs_total_stays_under_the_memory_cgroup() -> None:
    """W2 终审 Minor-4 —— tmpfs 页计入 memory cgroup。所有 tmpfs 尺寸之和超过
    ``--memory`` 的话,大的那块永远填不满:容器先被 OOM-kill(整个沙箱带信号
    猝死),而不是在挂载点上给一个可读的 ``ENOSPC``。此前 ``/opt/agents`` 是
    512m,五块加起来 960m,顶着这里的默认 1024m,留给真正跑代码的内存不到
    64m;一次 ``pip install --user`` 就能把沙箱打死。

    这条闸住在 supervisor 而不是 ``expert-work-runtime`` 的测试里:``memory_mb``
    的生产取值由 :class:`SandboxSupervisorSettings` 决定,而
    ``SandboxResourceLimits.memory_mb`` 的 dataclass 默认值在生产路径上恒被
    ``_docker_run_argv`` 用 ``record.memory_mb`` 覆盖 —— 两个值住在两个包里,
    只有这个服务同时依赖二者。留 25% 余量给进程本身。
    """
    from expert_work.runtime.sandbox import SandboxResourceLimits

    limits = SandboxResourceLimits()
    total = (
        limits.tmp_size_mb
        + limits.workspace_size_mb
        + limits.skills_size_mb
        + limits.agents_size_mb
        + limits.home_size_mb
    )
    budget = SandboxSupervisorSettings().default_memory_mb
    assert total <= budget * 0.75, (
        f"tmpfs 总量 {total}MB 超过 memory cgroup {budget}MB 的 75% —— "
        f"写满任一块都会 OOM-kill 而不是 ENOSPC"
    )
