"""``SandboxPool`` + ``PoolReplenisher`` — the HX-6 warm sandbox pool.

STREAM-HX-DESIGN § 7 (Mini-ADR HX-F1/F2/F3). The pool holds pre-launched
``READY`` containers per image variant; an ephemeral acquire (no
``user_id``) claims one instead of paying a cold ``docker run``. A
persistent-workspace acquire can never be pooled — the user's named
volume must be mounted at ``docker run`` time (Mini-ADR HX-F2) — so the
supervisor only consults the pool for the tmpfs path.

The replenisher is the reaper-shaped background task: every tick it
tops each variant up to its configured target (``pool_size_*`` settings,
0 = off) and shrinks past it when the target was lowered. Every failure
is fail-open: a launch error is logged + counted and retried next tick;
the acquire path never degrades below the cold-start baseline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.common.observability import expert_work_counter, expert_work_gauge
from expert_work.runtime.sandbox import SandboxResourceLimits, SandboxRuntimeProvider
from sandbox_supervisor.docker_client import DockerClient, DockerError
from sandbox_supervisor.domain import SandboxRecord, SandboxState, container_name
from sandbox_supervisor.runner_link import RunnerLink, RunnerLinkError
from sandbox_supervisor.settings import SandboxSupervisorSettings
from sandbox_supervisor.store import SandboxStore

logger = logging.getLogger(__name__)

#: Sentinel tenant for a pool container's ``sandbox_instance`` row — the
#: container is platform-neutral until claim binds a real tenant
#: (STREAM-HX-DESIGN § 7.2-①: sentinel over a nullable migration).
#: ``READY`` is not an active state, so the sentinel never collides with
#: any tenant's quota count.
POOL_TENANT_ID = UUID(int=0)

#: ``thread_id`` recorded on a not-yet-claimed pool row.
POOL_THREAD_ID = "pool"

#: A pool container torn down because the configured target shrank.
DESTROY_REASON_POOL_SHRUNK = "pool_shrunk"
#: A claimed pool container whose ``docker update`` limit pairing failed
#: (Mini-ADR HX-F3 fail-closed: limits are a security surface).
DESTROY_REASON_POOL_CLAIM_FAILED = "pool_claim_failed"

# Stream HX-6 — pool flow events. ``hit`` / ``miss`` are the acquire
# path (miss = pool enabled but empty for the variant); ``replenish`` /
# ``replenish_failed`` are the background top-up; ``update_failed`` is a
# claim whose limit pairing failed (fail-closed → cold start);
# ``claim_raced`` is the defensive CAS-lost branch; ``prepull`` /
# ``prepull_failed`` are the startup image prefetch (PR2).
_pool_events = expert_work_counter(
    "expert_work_sandbox_pool_total",
    "Warm sandbox pool flow events (Stream HX-6).",
    ("event",),
)


def observe_pool_event(event: str) -> None:
    """Count one pool flow event — shared by the pool and the supervisor."""
    _pool_events.labels(event=event).inc()


# Stream HX-6 PR2 — current READY inventory per image variant. Two fixed
# label values (minimal / office), re-set by the replenisher every tick
# (the ApprovalGaugeWorker periodic-set precedent); SLO #4's M1
# acceptance reads hit ratio + this gauge together.
_pool_ready = expert_work_gauge(
    "expert_work_sandbox_pool_ready",
    "READY warm-pool containers currently held, per image variant.",
    ("variant",),
)


@dataclass(frozen=True)
class PooledSandbox:
    """One READY pool container: its row + the held runner link."""

    record: SandboxRecord
    link: RunnerLink


class SandboxPool:
    """In-process inventory of READY containers, keyed by image ref.

    The supervisor owns the held-pipe transport, so pool inventory is
    process-local by construction (a link cannot cross processes). The
    DB rows mirror the state for observability; the authoritative CAS
    on claim is :meth:`SandboxStore.claim_ready`.
    """

    def __init__(self) -> None:
        self._ready: dict[str, list[PooledSandbox]] = {}

    def take(self, image_ref: str) -> PooledSandbox | None:
        """Pop one READY container for ``image_ref``, or ``None`` when empty.

        Synchronous (no await) so an asyncio caller's check-and-pop is
        atomic — two concurrent acquires can never receive the same
        container.
        """
        items = self._ready.get(image_ref)
        if not items:
            return None
        return items.pop()

    def put(self, item: PooledSandbox) -> None:
        """Add a READY container to its variant's inventory."""
        self._ready.setdefault(item.record.image_ref, []).append(item)

    def size(self, image_ref: str) -> int:
        """How many READY containers the pool holds for ``image_ref``."""
        return len(self._ready.get(image_ref, ()))


async def discard_pooled(
    pooled: PooledSandbox,
    *,
    docker: DockerClient,
    store: SandboxStore,
    reason: str,
) -> None:
    """Tear one pool container down — link, container, row.

    Shared by the replenisher (shrink) and the supervisor (claim whose
    ``docker update`` failed). Never raises: each step is best-effort so
    a half-dead container cannot wedge the caller.
    """
    with contextlib.suppress(Exception):
        await pooled.link.close()
    with contextlib.suppress(DockerError, OSError):
        await docker.remove(container_name(pooled.record.id))
    await store.update(
        pooled.record.with_state(
            SandboxState.DESTROYED,
            destroyed_at=datetime.now(UTC),
            destroy_reason=reason,
        )
    )


class PoolReplenisher:
    """Background task keeping each variant's READY count at its target."""

    def __init__(
        self,
        *,
        pool: SandboxPool,
        store: SandboxStore,
        docker: DockerClient,
        runtime_provider: SandboxRuntimeProvider,
        settings: SandboxSupervisorSettings,
    ) -> None:
        self._pool = pool
        self._store = store
        self._docker = docker
        self._runtime = runtime_provider
        self._settings = settings
        #: Warm-pool target — (label, image ref, READY count). One entry since
        #: the variant split was collapsed into a single image
        #: (sandbox-image-consolidation); kept as a tuple list so the reconcile
        #: loop + gauge labelling stay unchanged.
        self._targets: tuple[tuple[str, str, int], ...] = (
            ("default", settings.sandbox_image, settings.pool_size),
        )

    async def run_once(self) -> None:
        """Reconcile the pool target, then re-set the READY gauge (PR2)."""
        for variant, image_ref, target in self._targets:
            await self._reconcile(image_ref, target)
            _pool_ready.labels(variant=variant).set(self._pool.size(image_ref))

    async def _reconcile(self, image_ref: str, target: int) -> None:
        while self._pool.size(image_ref) > target:
            pooled = self._pool.take(image_ref)
            if pooled is None:  # pragma: no cover — size() just said non-empty
                break
            await discard_pooled(
                pooled,
                docker=self._docker,
                store=self._store,
                reason=DESTROY_REASON_POOL_SHRUNK,
            )
        while self._pool.size(image_ref) < target:
            try:
                await self._launch_one(image_ref)
            except (DockerError, RunnerLinkError, OSError) as exc:
                # Fail-open: the pool stays short, acquire falls back to
                # cold start, and the next tick retries the top-up.
                observe_pool_event("replenish_failed")
                logger.warning("pool.replenish_failed image=%s reason=%s", image_ref, exc)
                break
            observe_pool_event("replenish")

    async def _launch_one(self, image_ref: str) -> None:
        """Launch one READY container with the settings' default limits + tmpfs.

        The ``limits=`` below is the same translation
        :meth:`Supervisor._docker_run_argv` does for a claimed sandbox —
        settings' ``default_*`` in, :class:`SandboxResourceLimits` out — and
        it has to be spelled out here too (wave 2 final re-review, New 3).
        Omitting it fell back to ``DEFAULT_RESOURCE_LIMITS``, whose
        ``memory_mb`` is 512 while the tmpfs mounts that same dataclass
        declares sum to 704 MB: a self-contradictory container that only
        stayed harmless because a pooled container runs nothing until
        ``claim`` raises its memory to the settings' default. Reading the
        cap from the same place the ``SandboxRecord`` above reads it keeps
        the two from drifting, and keeps the
        "every tmpfs, summed, stays under ``memory_mb``" invariant true of
        the argv we actually launch, not just of the argv ``claim`` builds.
        """
        s = self._settings
        record = SandboxRecord(
            id=uuid4(),
            tenant_id=POOL_TENANT_ID,
            image_ref=image_ref,
            node=s.node_name,
            container_id=None,
            state=SandboxState.CREATING,
            thread_id=POOL_THREAD_ID,
            cpu_quota=s.default_cpu,
            memory_mb=s.default_memory_mb,
            pids_limit=s.default_pids_limit,
            timeout_s=s.default_timeout_s,
            created_at=datetime.now(UTC),
        )
        await self._store.insert(record)
        argv = self._runtime.docker_run_argv(
            image=image_ref,
            container_name=container_name(record.id),
            limits=SandboxResourceLimits(
                cpus=record.cpu_quota,
                memory_mb=record.memory_mb,
                pids_limit=record.pids_limit,
            ),
            workspace_volume=None,
        )
        try:
            link = await self._docker.launch(argv)
            await link.wait_ready(s.runner_ready_timeout_s)
        except (DockerError, RunnerLinkError, OSError):
            await self._store.update(record.with_state(SandboxState.FAILED))
            raise
        ready = record.with_state(SandboxState.READY, container_id=container_name(record.id))
        await self._store.update(ready)
        self._pool.put(PooledSandbox(record=ready, link=link))

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Reconcile every ``reaper_interval_s`` until ``stop`` is set."""
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("pool.replenish_sweep_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.reaper_interval_s)


async def prefetch_images(docker: DockerClient, settings: SandboxSupervisorSettings) -> None:
    """Pull both variant images when missing locally (Mini-ADR HX-F4).

    The one warm-path piece that helps *every* acquire shape — including
    the persistent-workspace first touch the pool cannot serve — by
    cutting the multi-second registry pull off a rebuilt node's first
    cold start. Best-effort + fail-open: a failed probe or pull is
    logged and counted; ``docker run`` pulls on demand anyway. Runs as a
    background lifespan task so service readiness never waits on a
    registry.
    """
    for image in (settings.sandbox_image,):
        try:
            present = await docker.image_exists(image)
        except OSError:
            present = False  # probe failed — attempt the pull anyway
        if present:
            continue
        try:
            await docker.pull_image(image)
        except (DockerError, OSError) as exc:
            observe_pool_event("prepull_failed")
            logger.warning("pool.prepull_failed image=%s reason=%s", image, exc)
            continue
        observe_pool_event("prepull")
        logger.info("pool.prepulled image=%s", image)
