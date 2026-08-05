"""``sandbox_instance`` store for the Agent Sandbox warm-session CAS (波 1 Task 7).

``sandbox_instance`` is the SAME table the docker-supervisor's own
``DbSandboxStore`` (``services/sandbox-supervisor/src/sandbox_supervisor/store.py``)
manages — the migration-0141 brief and
``docs/superpowers/specs/2026-08-03-sandbox-migration-design.md`` § 6.2 are
explicit that ``AgentSandboxClient`` reuses the existing ``container_id``
column rather than adding one. This module only implements the narrow
operations :class:`orchestrator.tools.sandbox_instance_store.SandboxInstanceStore`
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

#: 本后端(AgentSandboxClient)写进 ``image_ref`` 的标记值 —— 同时是
#: ``sandbox_instance`` 表**按后端限定行**的判据:docker supervisor 写的是
#: 真实镜像引用,永远不会等于这个值。三处消费:迁移 0142 的部分唯一索引
#: WHERE(字面量,alembic 不 import 应用代码)、本模块的集合查询谓词、
#: docker supervisor 侧 ``DbSandboxStore`` 的排除谓词(import 本常量)。
AGENT_SANDBOX_IMAGE_REF = "agent-sandbox"

#: Inert marker for the docker-supervisor-only NOT NULL text columns this
#: backend has no real value for (see module docstring). Alias of
#: :data:`AGENT_SANDBOX_IMAGE_REF` — the marker doubles as the backend
#: discriminator, one value on purpose.
_UNUSED_TEXT = AGENT_SANDBOX_IMAGE_REF

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

#: Independent-review Important-1 (task-9-report.md § addendum) — how old
#: an ``acquired_at`` must be before a ``container_id IS NULL`` row is
#: treated as an orphan of a process that died between ``claim_warm``'s
#: commit and ``set_container_id`` (pod OOM-kill / eviction / rolling
#: update — expected events under this system's multi-replica production
#: design, not theoretical edges), rather than a row still inside its
#: legitimate E2B cold-start window (measured 35-40s, 2026-08-04 probe
#: report). 5 minutes is ~8x that measured window — comfortably wide
#: enough to never race a genuine in-flight ``acquire()``, while not
#: leaving a real orphan wedged for long. Consulted by
#: ``list_stuck_creating`` and by ``claim_warm``'s stale-row takeover
#: (whole-branch review Critical-1) on BOTH stores — always through
#: :func:`_stuck_create_cutoff`, never inlined. The regular idle sweep
#: (``list_active(only_idle=True)``) must never touch a "looks like it's
#: still creating" row.
_STUCK_CREATE_TTL_S = 5 * 60

#: ``destroy_reason`` written when ``claim_warm`` takes over a stale
#: mid-create row (whole-branch review Critical-1). Deliberately distinct
#: from ``AgentSandboxClient``'s ``"warm_reconnect_failed"`` and from
#: ``reap``'s ``"reap_orphaned_create"``: all three clear the same wedged
#: shape, but an operator reading ``destroy_reason`` should be able to tell
#: WHICH path did it — this one means "a later acquire found the orphan and
#: took the slot", which is the only one of the three that happens with no
#: operator action and no reaper cycle.
_REASON_STUCK_CREATE_TAKEOVER = "stuck_create_takeover"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _missing_row_message(sandbox_id: UUID) -> str:
    """The single wording both stores raise when ``set_container_id`` finds no
    live row (re-review Important-2).

    Shared for the same reason :func:`_stuck_create_cutoff` is: the two
    implementations must be identical in *meaning*, and a caller that
    string-matches one store's error (tests do) must match the other's.
    """
    return (
        f"sandbox row {sandbox_id} is gone (destroyed, or never inserted) — "
        "cannot record its container id"
    )


def _stuck_create_cutoff(now: datetime) -> datetime:
    """``acquired_at`` older than this ⇒ an orphaned mid-create row.

    A single source for the predicate so the SQL store, the in-memory
    store, ``claim_warm``'s takeover and ``list_stuck_creating`` cannot
    drift: all four express "is this row stuck" as
    ``acquired_at < _stuck_create_cutoff(now)`` verbatim. This codebase has
    been bitten before by the same rule being spelled out independently in
    two stores and then only one of them being updated — the two stores'
    predicates must stay identical in meaning, so they share the arithmetic
    rather than each re-deriving it from :data:`_STUCK_CREATE_TTL_S`.
    """
    return now - timedelta(seconds=_STUCK_CREATE_TTL_S)


class SqlSandboxInstanceStore:
    """SQL-backed store for :class:`AgentSandboxClient`'s warm-session CAS.

    Structurally implements ``orchestrator.tools.sandbox_instance_store.SandboxInstanceStore``
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

        Whole-branch review Critical-1 — the "still creating, raise" branch
        above needs a time bound, or a single badly-timed process death
        disables a user's sandbox **permanently**. The row this method
        commits (``IN_USE`` + ``container_id=NULL``) is only backfilled by a
        later ``set_container_id``; if the orchestrator pod is killed in
        between (OOM-kill / eviction / rolling update — routine on a
        multi-replica deployment, and it already happened once during wave-1
        acceptance), no handler in that process ever runs. Every subsequent
        ``claim_warm`` for that ``(tenant, user)`` then hits the raise
        forever, and the review traced every path that could clear it:
        ``acquire``'s warm-reconnect cleanup is unreachable (this method
        raises instead of returning the tuple that branch needs),
        ``_create_and_track``'s unwind is same-process only, and
        :meth:`list_stuck_creating` is only reachable from
        ``reap(force=True)`` which nothing calls on a timer.

        So: ``acquired_at`` comes back in the SAME ``SELECT`` (same row, no
        extra round trip), and a NULL-``container_id`` row older than
        :data:`_STUCK_CREATE_TTL_S` is taken over — cleared, then the
        existing retry loop re-runs the INSERT and this caller normally
        wins the freed slot. There is no race against a legitimate in-flight
        ``acquire``: the 5-minute threshold is ~8x the measured 35-40s cold
        start. A row with a NULL ``acquired_at`` is never taken over (its
        age is unknowable) — same defensive stance as
        :meth:`list_stuck_creating`'s ``is_not(None)``.
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
                # ``acquired_at`` rides along for the Critical-1 stale
                # takeover below — same row, same round trip.
                #
                # ``image_ref`` 谓词与迁移 0142 的索引 WHERE 同义 —— 表是两
                # 后端共用的,docker supervisor 的 warm 行不是本 CAS 的参与
                # 者;0142 之前的 DB 上这条过滤让"撞上 docker 行"表现为大声
                # 的 could-not-claim raise,而不是把 docker container id 当
                # E2B sandbox id 交出去。
                found = (
                    await session.execute(
                        select(
                            SandboxInstanceRow.id,
                            SandboxInstanceRow.container_id,
                            SandboxInstanceRow.acquired_at,
                        ).where(
                            SandboxInstanceRow.tenant_id == tenant_id,
                            SandboxInstanceRow.user_id == user_id,
                            SandboxInstanceRow.state == _STATE_IN_USE,
                            SandboxInstanceRow.destroyed_at.is_(None),
                            SandboxInstanceRow.image_ref == AGENT_SANDBOX_IMAGE_REF,
                        )
                    )
                ).one_or_none()
                await session.commit()
            if found is None:
                # The row that beat us vanished between our failed insert
                # and this read (its owner dropped/destroyed it) — the slot
                # may be free now, retry the claim.
                continue
            winner_id, existing_container_id, winner_acquired_at = found
            if existing_container_id:
                return (winner_id, str(existing_container_id))
            if winner_acquired_at is not None and winner_acquired_at < _stuck_create_cutoff(now):
                # Critical-1: an orphan of a process death between this
                # method's commit and set_container_id. Clear it and let the
                # loop re-INSERT — see this method's docstring.
                await self.mark_destroyed(
                    sandbox_id=winner_id, reason=_REASON_STUCK_CREATE_TAKEOVER
                )
                continue
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

    async def create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """Task 10 契约测试实测发现的缺口——完整理由见
        ``orchestrator.tools.sandbox_instance_store.SandboxInstanceStore.create_ephemeral``
        的 docstring。``user_id=None`` 显式排除在迁移 0141 的部分唯一索引
        (``WHERE ... AND user_id IS NOT NULL``)之外,所以这里不需要
        :meth:`claim_warm` 那套"conflict → 重新 SELECT 判断"的 CAS 逻辑,
        单纯 ``INSERT ... ON CONFLICT DO NOTHING``(``ON CONFLICT`` 只是防
        ``sandbox_id`` PK 碰撞这种概率可忽略的边界,同 :meth:`claim_warm`
        自己的理由)。
        """
        async with self._sf() as session:
            await session.execute(
                pg_insert(SandboxInstanceRow)
                .values(
                    id=sandbox_id,
                    tenant_id=tenant_id,
                    user_id=None,
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
                    acquired_at=_utc_now(),
                )
                .on_conflict_do_nothing()
            )
            await session.commit()

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        """Backfill the E2B sandbox id — raises if the row is gone.

        Re-review Important-2. This used to be an unguarded ``UPDATE ...
        WHERE id``, i.e. a silent no-op when the row was absent or already
        DESTROYED, while ``InMemorySandboxInstanceStore`` raised ``KeyError``
        on the same input. Two stores whose predicates are not identical in
        meaning is a failure mode this codebase has been bitten by before,
        and C-1's takeover plus ``acquire``'s rebuild path both made "row
        absent or destroyed" *reachable*, so the two backends started taking
        opposite branches on identical production input.

        Both now reject. Rationale for reject over tolerate: this write is
        not idempotent bookkeeping, it is the second half of a two-phase
        claim — the row IS the CAS credential. If it silently no-ops, the
        caller's ``acquire`` returns a ``sandbox_id`` that exists nowhere
        while its microVM is already running on the platform, invisible to
        ``destroy`` (``get_container_id`` → ``None``), to ``reap``
        (``list_active`` keys off the row) and to ``list_stuck_creating``
        — leaked until the 20-minute platform timeout, with the run's next
        ``exec`` failing on a baffling "has no recorded container id".
        Raising instead lets ``AgentSandboxClient``'s post-create guard do
        what it was built for: kill the sandbox nobody remembers, unwind the
        slot, and surface one retryable error. Reject is also what the rest
        of the system already assumed — the in-memory store, the
        orchestrator's ``FakeInstanceStore``, and ``acquire``'s own comment
        on the rebuild path all say "this raises"; the SQL store was the
        only dissenter.

        The ``state``/``destroyed_at`` predicates are what make "already
        DESTROYED" reject too: without them the UPDATE would happily write
        ``container_id`` onto a terminal row and resurrect it for
        :meth:`get_container_id`.
        """
        async with self._sf() as session:
            result = await session.execute(
                sa_update(SandboxInstanceRow)
                .where(
                    SandboxInstanceRow.id == sandbox_id,
                    SandboxInstanceRow.state == _STATE_IN_USE,
                    SandboxInstanceRow.destroyed_at.is_(None),
                )
                .values(container_id=container_id)
            )
            await session.commit()
        # ``CursorResult`` exposes ``rowcount``; the generic ``Result`` type
        # does not — same ``getattr`` spelling as the runtime package's stores
        # (``runs/store.py``, ``event_log/db.py``). Default 0 is the safe
        # direction here: an implementation that stopped reporting rowcount
        # would make this raise, not silently go back to no-op.
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise RuntimeError(_missing_row_message(sandbox_id))

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        """First writer wins — a terminal row is never re-stamped.

        The ``state``/``destroyed_at`` predicates make a repeat call a
        no-op, matching :class:`InMemorySandboxInstanceStore` (whose
        ``_rows.pop`` has always made the second call inert). Without them
        SQL overwrote ``destroy_reason`` and pushed ``destroyed_at``
        forward on an already-terminal row.

        Re-review of the Important-1 fix flagged this: keying every unwind
        by ``sandbox_id`` put repeat calls squarely on the takeover path.
        Caller B takes over A's stale slot and marks A's row with
        ``_REASON_STUCK_CREATE_TAKEOVER``; A then fails and its own cleanup
        stamps the SAME row with ``post_create_failed``, erasing exactly the
        forensic signal that constant's docstring exists to preserve — an
        operator could no longer tell which path destroyed the row. Nothing
        keys off ``destroy_reason`` behaviourally, so this is a
        diagnosability fix, not a correctness one.

        The in-memory store needs no change; its second call was already a
        no-op. This is SQL catching up, the same direction as
        :meth:`set_container_id` and
        :meth:`touch_and_get_container_id`.
        """
        async with self._sf() as session:
            await session.execute(
                sa_update(SandboxInstanceRow)
                .where(
                    SandboxInstanceRow.id == sandbox_id,
                    SandboxInstanceRow.state == _STATE_IN_USE,
                    SandboxInstanceRow.destroyed_at.is_(None),
                )
                .values(state=_STATE_DESTROYED, destroyed_at=_utc_now(), destroy_reason=reason)
            )
            await session.commit()

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """只读活行 —— 与 in-memory store(``mark_destroyed`` 即 pop)同义。

        原先无谓词:已销毁行照样返回 container_id,同族第四条 SQL↔memory
        分歧(前三条:``set_container_id`` / ``touch_and_get_container_id`` /
        ``mark_destroyed``,每一条都是 SQL 向 memory 对齐收场)。曾以
        「destroy 二次调用可重杀 kill 失败的沙箱」为由保留 —— 复盘推翻:
        代码里没有任何路径会对同一 sandbox_id 二次 destroy,而行终结后
        没人再 connect 续期,平台 ``_SANDBOX_TIMEOUT_S``(20 分钟)超时是
        确定性兜底。刻意不加 ``image_ref`` 谓词:按 id 定位,id 恒来自
        本后端 acquire 的返回值(Global Constraints)。
        """
        async with self._sf() as session:
            container_id = (
                await session.execute(
                    select(SandboxInstanceRow.container_id).where(
                        SandboxInstanceRow.id == sandbox_id,
                        SandboxInstanceRow.state == _STATE_IN_USE,
                        SandboxInstanceRow.destroyed_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return str(container_id) if container_id is not None else None

    async def is_warm_session(self, *, sandbox_id: UUID) -> bool:
        """Whole-branch review Important-1 — see
        ``orchestrator.tools.sandbox_instance_store.SandboxInstanceStore.is_warm_session``
        for the contract. The three predicates below are the SQL spelling of
        the docker supervisor's own ``release`` condition (``record is not
        None and record.user_id is not None and record.state is IN_USE``).
        """
        async with self._sf() as session:
            found = (
                await session.execute(
                    select(SandboxInstanceRow.id).where(
                        SandboxInstanceRow.id == sandbox_id,
                        SandboxInstanceRow.user_id.is_not(None),
                        SandboxInstanceRow.state == _STATE_IN_USE,
                        SandboxInstanceRow.destroyed_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return found is not None

    async def touch_and_get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """Independent-review Important-2 —
        ``orchestrator.tools.sandbox_instance_store.SandboxInstanceStore.touch_and_get_container_id``'s
        full contract docstring covers the "why" (``exec`` must advance
        ``last_used_at`` or the idle sweep degrades to "time since
        acquire"). One ``UPDATE ... RETURNING`` round trip, not a separate
        read followed by a separate write — same row, same statement.

        The ``state``/``destroyed_at`` predicates mirror
        :meth:`set_container_id`'s, for the same reason and one this round
        made reachable. ``InMemorySandboxInstanceStore``'s ``_rows`` only
        holds live rows, so it has always returned ``None`` for a destroyed
        row; the unguarded SQL ``UPDATE`` instead returned the container id
        AND stamped ``last_used_at`` onto a terminal row. The two stores
        disagreeing here is the same failure class as Important-2.

        It stopped being theoretical when the periodic reaper landed
        (``control_plane.sandbox_reap_worker``): a tool call whose session
        goes idle past ``_IDLE_TTL_S`` now really does get its row swept to
        DESTROYED mid-run, and without these predicates that run's next
        ``exec`` would keep executing code inside a sandbox the system has
        already torn down — silently, and while writing "last used" onto a
        terminal row. Returning ``None`` routes it into ``_attach``'s
        ``SandboxSupervisorError`` instead, which is a legible failure the
        tools node already handles.
        """
        async with self._sf() as session:
            container_id = (
                await session.execute(
                    sa_update(SandboxInstanceRow)
                    .where(
                        SandboxInstanceRow.id == sandbox_id,
                        SandboxInstanceRow.state == _STATE_IN_USE,
                        SandboxInstanceRow.destroyed_at.is_(None),
                    )
                    .values(last_used_at=_utc_now())
                    .returning(SandboxInstanceRow.container_id)
                )
            ).scalar_one_or_none()
            await session.commit()
        return str(container_id) if container_id is not None else None

    async def list_stuck_creating(self) -> list[UUID]:
        """Independent-review Important-1 — orphans of a process that died
        between ``claim_warm``'s commit and ``set_container_id``'s
        backfill (pod OOM-kill / eviction / rolling update). See
        ``orchestrator.tools.sandbox_instance_store.SandboxInstanceStore.list_stuck_creating``
        for the full contract (only a ``force=True`` reap may call this;
        the regular idle sweep must not).

        ``acquired_at`` is always set by :meth:`claim_warm` at INSERT time,
        so ``is_not(None)`` is belt-and-braces, not a real-world branch —
        kept for the same defensive reason as :meth:`list_active`'s
        ``container_id is None`` re-check.

        Shares :func:`_stuck_create_cutoff` with :meth:`claim_warm`'s
        takeover and with the in-memory store — "how old counts as stuck"
        must be one rule, not four copies of the same arithmetic.

        ``image_ref`` 谓词是 belt-and-braces —— docker 的 mid-create 行
        ``state='CREATING'`` 本就不命中上面的 ``state == _STATE_IN_USE``,
        谓词纯粹为三个集合查询口径一致而加。
        """
        cutoff = _stuck_create_cutoff(_utc_now())
        async with self._sf() as session:
            result = await session.execute(
                select(SandboxInstanceRow.id).where(
                    SandboxInstanceRow.state == _STATE_IN_USE,
                    SandboxInstanceRow.destroyed_at.is_(None),
                    SandboxInstanceRow.container_id.is_(None),
                    SandboxInstanceRow.acquired_at.is_not(None),
                    SandboxInstanceRow.acquired_at < cutoff,
                    SandboxInstanceRow.image_ref == AGENT_SANDBOX_IMAGE_REF,
                )
            )
            return [row_id for (row_id,) in result.all()]

    async def list_active(self, *, only_idle: bool) -> list[tuple[UUID, str]]:
        """``AgentSandboxClient.reap`` (波 1 Task 9) 用 —— 见
        ``orchestrator.tools.sandbox_instance_store.SandboxInstanceStore.list_active``
        的完整契约 docstring(``only_idle`` 语义、idle 判定口径、为什么
        ``container_id`` 仍是 NULL 的行不返回)。

        M0 沙箱规模小(同 ``DbSandboxStore.list_idle_sessions`` 的理由,见
        ``sandbox_supervisor/store.py``):取回 ``IN_USE``/未销毁/
        ``container_id`` 已回填的行,``only_idle`` 的 idle 判定在 Python
        侧过滤,不写 per-row SQL 区间表达式。

        ``image_ref`` 谓词保证 reap 只清本后端的行 —— 否则周期 reap 会把
        docker 后端名下每一行标销毁。
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
                    SandboxInstanceRow.image_ref == AGENT_SANDBOX_IMAGE_REF,
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
    #: ``None`` for an ephemeral (no-``user_id``) sandbox row — see
    #: :meth:`InMemorySandboxInstanceStore.create_ephemeral`. Never ``None``
    #: for a row created via :meth:`InMemorySandboxInstanceStore.claim_warm`.
    user_id: UUID | None
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

    PR-A #5 —— image_ref 维度(SQL 侧三个集合查询上的
    ``AGENT_SANDBOX_IMAGE_REF`` 谓词,见 :meth:`SqlSandboxInstanceStore.list_active` /
    :meth:`SqlSandboxInstanceStore.list_stuck_creating` /
    :meth:`SqlSandboxInstanceStore.claim_warm`)对本实现**空洞地成立**:本
    store 只被 agent 后端进程写入,不存在 docker-supervisor 行共存于同一
    ``_rows``——``_rows`` 里的每一行定义上都是本后端的行。不为此加一个永远
    是同一个值的假字段。
    """

    def __init__(self) -> None:
        self._rows: dict[UUID, _MemRow] = {}
        #: (tenant_id, user_id) -> sandbox_id of the live IN_USE row, if any.
        self._warm: dict[tuple[UUID, UUID], UUID] = {}

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        key = (tenant_id, user_id)
        for _ in range(_CLAIM_WARM_MAX_ATTEMPTS):
            existing_id = self._warm.get(key)
            if existing_id is None:
                self._warm[key] = sandbox_id
                self._rows[sandbox_id] = _MemRow(tenant_id=tenant_id, user_id=user_id)
                return None
            existing = self._rows[existing_id]
            if existing.container_id:
                # Return the WINNER's real row id, not the caller's own
                # sandbox_id (never inserted on this path) — see
                # SqlSandboxInstanceStore.claim_warm's docstring for why.
                return (existing_id, existing.container_id)
            if existing.acquired_at < _stuck_create_cutoff(_utc_now()):
                # Whole-branch review Critical-1 — mirror of the SQL store's
                # stale-mid-create takeover, same predicate (shared
                # _stuck_create_cutoff) and same terminal write. The two
                # stores' claim_warm predicates must stay identical in
                # meaning; see SqlSandboxInstanceStore.claim_warm's
                # docstring for the full reasoning.
                await self.mark_destroyed(
                    sandbox_id=existing_id, reason=_REASON_STUCK_CREATE_TAKEOVER
                )
                continue
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

    async def create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """Mirrors :meth:`SqlSandboxInstanceStore.create_ephemeral` — a plain
        insert, no ``_warm`` bookkeeping (ephemeral rows never participate in
        the warm-session CAS). First writer wins, same as the SQL side's
        ``ON CONFLICT DO NOTHING``: 无条件 ``self._rows[...] = ...`` 会在
        id 碰撞时把一行活着的 warm 行整个换成 ``user_id=None`` 的临时行,
        ``_warm`` 指针悬空(指向一行不再是 warm 的行)。"""
        if sandbox_id in self._rows:
            return
        self._rows[sandbox_id] = _MemRow(tenant_id=tenant_id, user_id=None)

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        """Mirrors :meth:`SqlSandboxInstanceStore.set_container_id` — raises
        when the row is gone (re-review Important-2; that method's docstring
        carries the full reasoning for reject-over-tolerate).

        This already raised, but as a bare ``KeyError`` from ``self._rows[...]``
        — a different exception type, a different message, and (crucially) an
        *incidental* one: a reader could not tell it was contractual rather
        than a missed ``.get()``. Now it is explicit and shares
        :func:`_missing_row_message` with the SQL store, so both stores are
        identical in meaning AND in wording. ``_rows`` only ever holds live
        rows (``mark_destroyed`` pops them), so "absent" here already covers
        the SQL store's ``state``/``destroyed_at`` predicates.
        """
        row = self._rows.get(sandbox_id)
        if row is None:
            raise RuntimeError(_missing_row_message(sandbox_id))
        row.container_id = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        del reason
        row = self._rows.pop(sandbox_id, None)
        # An ephemeral row (user_id is None, see create_ephemeral) never has
        # a _warm entry to begin with — skip the lookup entirely rather than
        # building a key _warm's tuple[UUID, UUID] type could never hold.
        if row is not None and row.user_id is not None:
            key = (row.tenant_id, row.user_id)
            if self._warm.get(key) == sandbox_id:
                del self._warm[key]

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        row = self._rows.get(sandbox_id)
        return row.container_id if row is not None else None

    async def is_warm_session(self, *, sandbox_id: UUID) -> bool:
        """Mirrors :meth:`SqlSandboxInstanceStore.is_warm_session`.

        ``_rows`` only ever holds live rows (``mark_destroyed`` pops them),
        so "row present" already carries the SQL store's ``state='IN_USE'
        AND destroyed_at IS NULL`` half — the only thing left to check is
        ``user_id``.
        """
        row = self._rows.get(sandbox_id)
        return row is not None and row.user_id is not None

    async def touch_and_get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """Mirrors :meth:`SqlSandboxInstanceStore.touch_and_get_container_id`.

        ``_rows`` only ever holds live rows (``mark_destroyed`` pops them),
        so the ``.get()`` here already covers that method's
        ``state``/``destroyed_at`` predicates — this side needed no change;
        SQL was the one that had to catch up.
        """
        row = self._rows.get(sandbox_id)
        if row is None:
            return None
        row.last_used_at = _utc_now()
        return row.container_id

    async def list_stuck_creating(self) -> list[UUID]:
        """Mirrors :meth:`SqlSandboxInstanceStore.list_stuck_creating` —
        same shared :func:`_stuck_create_cutoff` predicate."""
        cutoff = _stuck_create_cutoff(_utc_now())
        return [
            sandbox_id
            for sandbox_id, row in self._rows.items()
            if row.container_id is None and row.acquired_at < cutoff
        ]

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
