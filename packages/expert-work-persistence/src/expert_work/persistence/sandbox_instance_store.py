"""``sandbox_instance`` store for the Agent Sandbox warm-session CAS (波 1 Task 7).

``sandbox_instance`` is the SAME table the docker-supervisor's own
``DbSandboxStore`` (``services/sandbox-supervisor/src/sandbox_supervisor/store.py``)
manages — the migration-0141 brief and
``docs/superpowers/specs/2026-08-03-sandbox-migration-design.md`` § 6.2 are
explicit that ``AgentSandboxClient`` reuses the existing ``container_id``
column rather than adding one. This module only implements the narrow
operations :class:`orchestrator.tools.agent_sandbox.SandboxInstanceStore`
(a :class:`typing.Protocol` — structural typing, no inheritance needed)
declares; it does not import from ``orchestrator`` (persistence must not
depend upward on a consuming service) and does not import
``sandbox_supervisor.domain`` either (that package's ``SandboxState``/
``SandboxRecord`` are that OTHER service's local domain types, tied to
docker-specific fields like per-row CPU/memory/pids limits that have no
Agent-Sandbox equivalent — importing them here would be a same-shaped
wrong-direction dependency).

``sandbox_instance`` carries no RLS policy (allowlisted in
``test_rls_policy_coverage.py`` as "platform/worker-managed"), so unlike
most stores in this package, no ``bypass_rls_session`` wrapping is needed
here — the shared RLS-wrapped sessionmaker is harmless for a table with no
attached policy (the same precedent as ``SqlSandboxEgressAuditStore``).

Several ``sandbox_instance`` columns are NOT NULL but describe
docker-supervisor-only concepts that do not apply to an Agent-Sandbox-backed
warm session (``image_ref`` / ``node`` / ``thread_id`` / ``cpu_quota`` /
``memory_mb`` / ``pids_limit`` / ``timeout_s``). Rather than add a migration
loosening their nullability (out of this task's scope — the table is shared,
changing it could ripple into the supervisor's own store/queries), warm-CAS
rows fill them with the inert :data:`_UNUSED_TEXT` marker / zero. This is a
wart worth revisiting at wave 4's "dead-field disposition" pass (design spec
§ 波4), alongside ``sandbox_instance.node``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import SandboxInstanceRow

#: ``sandbox_instance.state`` values (STREAM-F-DESIGN § 2.2 / migration
#: 0012). The Agent Sandbox warm-CAS path only ever writes ``IN_USE``
#: (straight from ``claim_warm`` — there is no separate ``CREATING`` phase
#: recorded in this column; "still provisioning" is instead expressed as
#: ``state='IN_USE' AND container_id IS NULL``, since migration 0141's
#: partial unique index only guards ``state = 'IN_USE'`` rows — inserting as
#: ``CREATING`` first would let a second concurrent claim slip past the
#: index and defeat the whole point of this CAS).
_STATE_IN_USE = "IN_USE"
_STATE_DESTROYED = "DESTROYED"

#: Inert marker for the docker-supervisor-only NOT NULL text columns this
#: backend has no real value for (see module docstring).
_UNUSED_TEXT = "agent-sandbox"

#: Bounded retry for the rare case where a conflicting row vanishes between
#: our failed INSERT and the follow-up SELECT (its owner released the slot
#: concurrently) — not a wait-for-readiness poll (see :meth:`claim_warm`).
_CLAIM_WARM_MAX_ATTEMPTS = 3

#: Idle TTL for a warm Agent-Sandbox session before a non-force
#: :meth:`SqlSandboxInstanceStore.list_active`/``AgentSandboxClient.reap``
#: sweep reclaims it (波 1 Task 9). Same value AND "``last_used_at``,
#: falling back to ``acquired_at``" semantics as the docker-supervisor's
#: own idle reaper default (``session_idle_ttl_s`` in
#: ``services/sandbox-supervisor/src/sandbox_supervisor/settings.py``,
#: consumed by ``_session_idle`` in that service's own ``store.py``) — the
#: value is mirrored here rather than imported because this package must
#: not depend upward on that service's settings (same wrong-direction-
#: dependency rule as the module docstring's "does not import from
#: ``orchestrator``" / "does not import ``sandbox_supervisor.domain``").
_IDLE_TTL_S = 15 * 60


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class SqlSandboxInstanceStore:
    """SQL-backed store for :class:`AgentSandboxClient`'s warm-session CAS.

    Structurally implements ``orchestrator.tools.agent_sandbox.SandboxInstanceStore``
    (a ``Protocol`` — no formal inheritance; see module docstring for why no
    local ABC is declared here).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        """spec § 6.2 CAS: ``INSERT ... ON CONFLICT DO NOTHING RETURNING``.

        The bare (no explicit conflict target) ``ON CONFLICT DO NOTHING``
        is deliberate: migration 0141 creates a plain ``CREATE UNIQUE
        INDEX ... WHERE ...`` (a partial index), not a named table
        CONSTRAINT — Postgres's ``ON CONFLICT ON CONSTRAINT <name>`` form
        requires an actual constraint object, which this is not. The bare
        form matches ANY unique/exclusion violation on the table, which is
        exactly what we want (the row's own ``id`` PK collision is
        astronomically unlikely — ``sandbox_id`` is a fresh ``uuid4()`` —
        so in practice only the 0141 partial index can conflict here).

        Design note (task-7-report.md — NOT what task-7-brief.md's sketch
        or the design spec's § 6.2 prose literally describe): when the
        INSERT loses the race, the conflicting row's ``container_id`` may
        still be NULL — its owner is still mid-``create()`` (E2B cold start
        measured 35-40s in the 2026-08-04 probe; this is a wide, common
        window, not a rare edge). Returning that NULL as if it were a
        connectable id would either (silently) hand the caller an unusable
        empty value, or — worse — let the caller fall through into
        creating a SECOND sandbox for this ``(tenant, user)`` while this
        row still holds the CAS slot, defeating the entire point of the
        index. Failing loudly here is the wave-1 choice: the caller
        (``AgentSandboxClient._claim_warm``) wraps this into
        ``SandboxSupervisorError`` uniformly, which the ``tools`` node
        already turns into a retryable ``ToolMessage(status="error")`` —
        acceptable UX for what should be an uncommon collision (two
        concurrent runs racing to open the SAME user's SAME warm session).
        A future wave could instead poll/wait here; deliberately not built
        now (no test demands it, and it would add a timeout knob + retry
        policy this task's scope does not ask for).

        Review fix — returns ``(winner sandbox_id, container_id)`` on a
        losing claim, not just the container_id: the caller's own freshly
        minted ``sandbox_id`` was never inserted anywhere on this path, so
        it needs the WINNER's real row id to hand back to ITS caller
        (otherwise a later ``destroy()`` on that never-persisted id would
        silently no-op — see ``AgentSandboxClient.acquire``). Both values
        come from the same ``SELECT`` already, no extra round trip.
        """
        for _ in range(_CLAIM_WARM_MAX_ATTEMPTS):
            now = _utc_now()
            async with self._sf() as session:
                won = (
                    await session.execute(
                        pg_insert(SandboxInstanceRow)
                        .values(
                            id=sandbox_id,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=None,
                            image_ref=_UNUSED_TEXT,
                            node=_UNUSED_TEXT,
                            container_id=None,
                            state=_STATE_IN_USE,
                            thread_id=_UNUSED_TEXT,
                            cpu_quota=0,
                            memory_mb=0,
                            pids_limit=0,
                            timeout_s=0,
                            acquired_at=now,
                        )
                        .on_conflict_do_nothing()
                        .returning(SandboxInstanceRow.id)
                    )
                ).scalar_one_or_none()
                if won is not None:
                    await session.commit()
                    return None
                # Select id + container_id together (not just container_id
                # alone) — otherwise "no row found" (id absent) and "row
                # found but container_id is still NULL" would both collapse
                # to the same `None`, and we need to tell those two apart.
                found = (
                    await session.execute(
                        select(SandboxInstanceRow.id, SandboxInstanceRow.container_id).where(
                            SandboxInstanceRow.tenant_id == tenant_id,
                            SandboxInstanceRow.user_id == user_id,
                            SandboxInstanceRow.state == _STATE_IN_USE,
                            SandboxInstanceRow.destroyed_at.is_(None),
                        )
                    )
                ).one_or_none()
                await session.commit()
            if found is None:
                # The row that beat us vanished between our failed insert
                # and this read (its owner dropped/destroyed it) — the slot
                # may be free now, retry the claim.
                continue
            winner_id, existing_container_id = found
            if existing_container_id:
                return (winner_id, str(existing_container_id))
            msg = (
                f"a sandbox is already being created for tenant={tenant_id} "
                f"user={user_id} — retry shortly"
            )
            raise RuntimeError(msg)
        msg = (
            f"could not claim a warm sandbox slot for tenant={tenant_id} "
            f"user={user_id} after {_CLAIM_WARM_MAX_ATTEMPTS} attempts"
        )
        raise RuntimeError(msg)

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        async with self._sf() as session:
            await session.execute(
                sa_update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_id)
                .values(container_id=container_id)
            )
            await session.commit()

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        async with self._sf() as session:
            await session.execute(
                sa_update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_id)
                .values(state=_STATE_DESTROYED, destroyed_at=_utc_now(), destroy_reason=reason)
            )
            await session.commit()

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        # Same terminal write as mark_destroyed (frees the 0141 partial
        # index slot) but keyed by (tenant, user) instead of sandbox_id —
        # acquire() doesn't have the dead row's internal id at this point,
        # only the warm-session coordinate it failed to connect to.
        async with self._sf() as session:
            await session.execute(
                sa_update(SandboxInstanceRow)
                .where(
                    SandboxInstanceRow.tenant_id == tenant_id,
                    SandboxInstanceRow.user_id == user_id,
                    SandboxInstanceRow.state == _STATE_IN_USE,
                    SandboxInstanceRow.destroyed_at.is_(None),
                )
                .values(
                    state=_STATE_DESTROYED,
                    destroyed_at=_utc_now(),
                    destroy_reason="warm_reconnect_failed",
                )
            )
            await session.commit()

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        async with self._sf() as session:
            container_id = (
                await session.execute(
                    select(SandboxInstanceRow.container_id).where(
                        SandboxInstanceRow.id == sandbox_id
                    )
                )
            ).scalar_one_or_none()
        return str(container_id) if container_id is not None else None

    async def list_active(self, *, only_idle: bool) -> list[tuple[UUID, str]]:
        """``AgentSandboxClient.reap`` (波 1 Task 9) 用 —— 见
        ``orchestrator.tools.agent_sandbox.SandboxInstanceStore.list_active``
        的完整契约 docstring(``only_idle`` 语义、idle 判定口径、为什么
        ``container_id`` 仍是 NULL 的行不返回)。

        M0 沙箱规模小(同 ``DbSandboxStore.list_idle_sessions`` 的理由,见
        ``sandbox_supervisor/store.py``):取回 ``IN_USE``/未销毁/
        ``container_id`` 已回填的行,``only_idle`` 的 idle 判定在 Python
        侧过滤,不写 per-row SQL 区间表达式。
        """
        async with self._sf() as session:
            result = await session.execute(
                select(
                    SandboxInstanceRow.id,
                    SandboxInstanceRow.container_id,
                    SandboxInstanceRow.last_used_at,
                    SandboxInstanceRow.acquired_at,
                ).where(
                    SandboxInstanceRow.state == _STATE_IN_USE,
                    SandboxInstanceRow.destroyed_at.is_(None),
                    SandboxInstanceRow.container_id.is_not(None),
                )
            )
            rows = result.all()
        now = _utc_now()
        active: list[tuple[UUID, str]] = []
        for row_id, container_id, last_used_at, acquired_at in rows:
            if container_id is None:
                continue  # belt-and-braces — the WHERE clause already excludes this
            if only_idle:
                anchor = last_used_at or acquired_at
                if anchor is None or anchor + timedelta(seconds=_IDLE_TTL_S) >= now:
                    continue
            active.append((row_id, str(container_id)))
        return active


@dataclass
class _MemRow:
    tenant_id: UUID
    user_id: UUID
    container_id: str | None = None
    #: Mirrors ``SandboxInstanceRow.acquired_at`` — set at ``claim_warm``
    #: time so :meth:`InMemorySandboxInstanceStore.list_active` has the
    #: same idleness anchor as :meth:`SqlSandboxInstanceStore.list_active`.
    acquired_at: datetime = field(default_factory=_utc_now)
    #: Mirrors ``SandboxInstanceRow.last_used_at``. Nothing currently calls
    #: a "touch" method on either store (see the ``SandboxInstanceStore``
    #: Protocol docstring's "known gap" note), so this stays ``None`` in
    #: practice — same as production today. Kept as a real field (not
    #: hardcoded away) so both stores would fall out of sync the instant
    #: one of them starts actually writing it and the other doesn't.
    last_used_at: datetime | None = None


class InMemorySandboxInstanceStore:
    """Dev / no-DB fallback — mirrors the SQL store's contract (incl. the
    "claimed but not ready" raise; see :meth:`SqlSandboxInstanceStore.claim_warm`)
    so ``sandbox_backend="agent_sandbox"`` behaves the same regardless of
    ``persistence_backend``. Not used by the orchestrator's own unit tests
    (those hand-roll a local ``FakeInstanceStore`` per repo convention) —
    this backs ``control_plane.runtime.build_sandbox_runtime`` when no SQL
    session factory is wired (``persistence_backend="memory"``).
    """

    def __init__(self) -> None:
        self._rows: dict[UUID, _MemRow] = {}
        #: (tenant_id, user_id) -> sandbox_id of the live IN_USE row, if any.
        self._warm: dict[tuple[UUID, UUID], UUID] = {}

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        key = (tenant_id, user_id)
        existing_id = self._warm.get(key)
        if existing_id is None:
            self._warm[key] = sandbox_id
            self._rows[sandbox_id] = _MemRow(tenant_id=tenant_id, user_id=user_id)
            return None
        container_id = self._rows[existing_id].container_id
        if container_id:
            # Return the WINNER's real row id, not the caller's own
            # sandbox_id (never inserted on this path) — see
            # SqlSandboxInstanceStore.claim_warm's docstring for why.
            return (existing_id, container_id)
        msg = (
            f"a sandbox is already being created for tenant={tenant_id} "
            f"user={user_id} — retry shortly"
        )
        raise RuntimeError(msg)

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        self._rows[sandbox_id].container_id = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        del reason
        row = self._rows.pop(sandbox_id, None)
        if row is not None:
            key = (row.tenant_id, row.user_id)
            if self._warm.get(key) == sandbox_id:
                del self._warm[key]

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        key = (tenant_id, user_id)
        sandbox_id = self._warm.pop(key, None)
        if sandbox_id is not None:
            self._rows.pop(sandbox_id, None)

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        row = self._rows.get(sandbox_id)
        return row.container_id if row is not None else None

    async def list_active(self, *, only_idle: bool) -> list[tuple[UUID, str]]:
        """Mirrors :meth:`SqlSandboxInstanceStore.list_active` — same
        "``container_id`` populated" filter and (``only_idle=True``) the
        same "``last_used_at``, falling back to ``acquired_at``" idleness
        rule, using ``_MemRow``'s mirrored timestamp fields.
        """
        now = _utc_now()
        active: list[tuple[UUID, str]] = []
        for sandbox_id, row in self._rows.items():
            if row.container_id is None:
                continue
            if only_idle:
                anchor = row.last_used_at or row.acquired_at
                if anchor is None or anchor + timedelta(seconds=_IDLE_TTL_S) >= now:
                    continue
            active.append((sandbox_id, row.container_id))
        return active
