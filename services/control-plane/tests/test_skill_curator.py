"""Sprint #4 PR B — Curator worker + throttled activity recorder.

Covers the four state-machine paths from Mini-ADRs U-26 / U-27 / U-29:

1. ``active`` → ``stale`` after ``stale_days`` of no activity
2. ``stale`` → ``archived`` after additional ``archive_days``
3. ``pinned`` rows are skipped at both stages
4. ``stale`` auto-revives to ``active`` on activity (via the throttled
   recorder's ``bump_last_used_at`` SQL)

Plus throttle behaviour: same skill bumped twice within the TTL window
produces exactly one SQL UPDATE; bumps for different skills both fire.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from control_plane.skill_activity import ThrottledActivityRecorder
from control_plane.skill_curator import (
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_STALE_DAYS,
    SkillCurator,
)
from control_plane.tenancy import TenantConfigService
from expert_work.common.skill_activity import SkillViewEvent
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.skill import InMemorySkillStore
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.protocol import (
    AuditAction,
    AuditQuery,
    SkillRunUsage,
    SkillStatus,
    TenantConfigPatch,
)
from expert_work.protocol.skill import SKILL_USAGE_VIEWED
from expert_work.runtime.audit.fallback import InMemoryAuditFallbackQueue
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.audit.redactor import DefaultSecretRedactor

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _build_logger() -> tuple[AuditLogger, InMemoryAuditLogStore]:
    store = InMemoryAuditLogStore()
    logger = AuditLogger(
        store=store,
        redactor=DefaultSecretRedactor(),
        fallback=InMemoryAuditFallbackQueue(),
    )
    return logger, store


async def _seed_skill(
    *,
    store: InMemorySkillStore,
    tenant_id: UUID,
    name: str,
    status: SkillStatus,
    last_used_at: datetime | None,
    pinned: bool = False,
) -> UUID:
    skill_id = uuid4()
    await store.create_skill(
        skill_id=skill_id,
        tenant_id=tenant_id,
        name=name,
        description=name,
        category="ops",
    )
    # Add at least one version so latest_version > 0; tests reaching
    # for resolve_by_name need it.
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill_id,
        tenant_id=tenant_id,
        prompt_fragment="body",
        tool_names=("log_viewer",),
    )
    # Force the underlying state via direct DTO mutation (InMemory
    # store exposes its dict). Curator tests need precise control of
    # last_used_at to verify the threshold math, which the public
    # API can't give without time-traveling.
    current = store._skills[skill_id]
    store._skills[skill_id] = current.model_copy(
        update={
            "status": status,
            "last_used_at": last_used_at,
            "pinned": pinned,
            "state_changed_at": last_used_at or current.state_changed_at,
        }
    )
    return skill_id


def _curator(
    store: InMemorySkillStore,
    config_service: TenantConfigService,
    audit_logger: AuditLogger,
) -> SkillCurator:
    # interval_s is irrelevant for run_once(); tests bypass the loop.
    return SkillCurator(
        skill_store=store,
        tenant_config_service=config_service,
        audit_logger=audit_logger,
        interval_s=60.0,
    )


# ─── State machine ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_to_stale_after_threshold() -> None:
    store = InMemorySkillStore()
    audit_logger, audit_store = _build_logger()
    config_service = TenantConfigService(
        store=InMemoryTenantConfigStore(),
        audit_logger=audit_logger,
    )
    curator = _curator(store, config_service, audit_logger)

    long_ago = datetime.now(UTC) - timedelta(days=DEFAULT_STALE_DAYS + 1)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="cold",
        status=SkillStatus.ACTIVE,
        last_used_at=long_ago,
    )

    summary = await curator.run_once()

    assert summary.active_to_stale == 1
    assert summary.stale_to_archived == 0
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.status == SkillStatus.STALE
    # Audit summary row recorded
    page = await audit_store.query(AuditQuery(tenant_id=UUID(int=0), limit=10))
    assert AuditAction.SKILL_CURATOR_RUN in [r.action for r in page.entries]


@pytest.mark.asyncio
async def test_stale_to_archived_after_threshold() -> None:
    store = InMemorySkillStore()
    audit_logger, _ = _build_logger()
    curator = _curator(
        store,
        TenantConfigService(store=InMemoryTenantConfigStore(), audit_logger=audit_logger),
        audit_logger,
    )

    long_ago = datetime.now(UTC) - timedelta(days=DEFAULT_ARCHIVE_DAYS + 1)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="frozen",
        status=SkillStatus.STALE,
        last_used_at=long_ago,
    )

    summary = await curator.run_once()

    assert summary.stale_to_archived == 1
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.status == SkillStatus.ARCHIVED


@pytest.mark.asyncio
async def test_pinned_skill_never_transitions() -> None:
    store = InMemorySkillStore()
    audit_logger, _ = _build_logger()
    curator = _curator(
        store,
        TenantConfigService(store=InMemoryTenantConfigStore(), audit_logger=audit_logger),
        audit_logger,
    )

    long_ago = datetime.now(UTC) - timedelta(days=DEFAULT_ARCHIVE_DAYS * 2)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="vip",
        status=SkillStatus.ACTIVE,
        last_used_at=long_ago,
        pinned=True,
    )

    summary = await curator.run_once()
    assert summary.active_to_stale == 0
    assert summary.stale_to_archived == 0
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.status == SkillStatus.ACTIVE


@pytest.mark.asyncio
async def test_per_tenant_thresholds_override_defaults() -> None:
    """Tenant config thresholds beat the platform defaults."""
    store = InMemorySkillStore()
    audit_logger, _ = _build_logger()
    config_store = InMemoryTenantConfigStore()
    config_service = TenantConfigService(store=config_store, audit_logger=audit_logger)
    # Tighter thresholds for tenant A.
    await config_store.upsert(
        tenant_id=_TENANT_A,
        patch=TenantConfigPatch(
            display_name="Acme",
            skill_stale_days=7,
            skill_archive_days=14,
        ),
        actor_id="admin",
    )
    curator = _curator(store, config_service, audit_logger)

    # Last used 10 days ago: stale under tenant A (7 day threshold), still
    # active under the platform default (30).
    ten_days_ago = datetime.now(UTC) - timedelta(days=10)
    a_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="medium-cold",
        status=SkillStatus.ACTIVE,
        last_used_at=ten_days_ago,
    )
    b_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_B,
        name="medium-cold",
        status=SkillStatus.ACTIVE,
        last_used_at=ten_days_ago,
    )

    summary = await curator.run_once()
    assert summary.active_to_stale == 1
    a_row = await store.get_skill(skill_id=a_id, tenant_id=_TENANT_A)
    b_row = await store.get_skill(skill_id=b_id, tenant_id=_TENANT_B)
    assert a_row is not None and a_row.status == SkillStatus.STALE
    assert b_row is not None and b_row.status == SkillStatus.ACTIVE


# ─── Activity recorder + auto-revive ─────────────────────────────────


@pytest.mark.asyncio
async def test_activity_recorder_throttles_within_ttl() -> None:
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="hot",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC) - timedelta(days=1),
    )

    first = await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A)
    second = await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A)
    assert first is True
    assert second is False, "second bump within TTL must be a no-op"


@pytest.mark.asyncio
async def test_activity_recorder_auto_revives_stale() -> None:
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="snoozing",
        status=SkillStatus.STALE,
        last_used_at=datetime.now(UTC) - timedelta(days=40),
    )

    fired = await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A)
    assert fired is True
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.status == SkillStatus.ACTIVE
    # Auto-revive bumps state_changed_at
    assert row.state_changed_at is not None
    assert (datetime.now(UTC) - row.state_changed_at).total_seconds() < 5.0


@pytest.mark.asyncio
async def test_activity_recorder_skips_archived() -> None:
    """Archived skills must not auto-revive — admin must unarchive."""
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="cold-storage",
        status=SkillStatus.ARCHIVED,
        last_used_at=datetime.now(UTC) - timedelta(days=200),
    )

    fired = await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A)
    assert fired is False
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.status == SkillStatus.ARCHIVED


# ─── B-84: 绑定 vs 打开是两件事, 证据落 skill_run_usage ────────────────


def _view(*, version: int = 1, thread_id: UUID | None = None, agent: str = "ai-health-plan"):
    return SkillViewEvent(
        skill_version=version,
        thread_id=thread_id or uuid4(),
        agent_name=agent,
    )


async def _viewed_rows(store: InMemorySkillStore) -> list[SkillRunUsage]:
    return [u for u in store._run_usage if u.outcome == SKILL_USAGE_VIEWED]


@pytest.mark.asyncio
async def test_bind_writes_no_viewed_row() -> None:
    """绑定不是阅读 —— agent_factory 那条路只能 bump last_used_at。"""
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="bound-not-read",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC) - timedelta(days=1),
    )

    fired = await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A)
    assert fired is True
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.last_used_at is not None, "bind 仍然要 bump last_used_at"
    assert await _viewed_rows(store) == [], "bind 写了 viewed 行就等于没分开这两件事"


@pytest.mark.asyncio
async def test_view_writes_a_viewed_row_and_still_bumps_last_used_at() -> None:
    """``skill_view`` 是唯一写 viewed 行的入口; 它同时也算一次 use。"""
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="actually-read",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC) - timedelta(days=1),
    )
    thread_id = uuid4()

    fired = await recorder.maybe_record(
        skill_id=skill_id,
        tenant_id=_TENANT_A,
        kind="view",
        view=_view(version=3, thread_id=thread_id),
    )
    assert fired is True
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None and row.last_used_at is not None

    rows = await _viewed_rows(store)
    assert len(rows) == 1
    assert rows[0].tenant_id == _TENANT_A
    assert rows[0].skill_id == skill_id
    assert rows[0].skill_version == 3
    assert rows[0].thread_id == thread_id
    assert rows[0].agent_name == "ai-health-plan"


@pytest.mark.asyncio
async def test_platform_skill_view_is_recorded_under_the_consuming_tenant() -> None:
    """**本次返工的全部意义**: 平台技能 (``skill.tenant_id IS NULL``) 必须量得到。

    对接方 ai-health-plan 绑的 19 个技能 19/19 都是平台技能。证据行写在
    ``skill_run_usage`` 而不是 ``skill`` 行上, 所以 ``tenant_id`` 填的是**消费方
    租户** —— 既不需要 bypass RLS 去写 NULL-tenant 行, 也让同一个平台技能对每个
    租户各自可量。
    """
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    platform_id = uuid4()
    await store.create_platform_skill(skill_id=platform_id, name="ui-ux-pro-max")

    fired = await recorder.maybe_record(
        skill_id=platform_id,
        tenant_id=_TENANT_A,
        kind="view",
        view=_view(),
    )
    # 平台行的 last_used_at bump 本来就匹配不上 (按 tenant_id 过滤), 与本改动无关。
    assert fired is False

    rows = await _viewed_rows(store)
    assert len(rows) == 1, "平台技能被打开过, 必须留下证据行"
    assert rows[0].skill_id == platform_id
    assert rows[0].tenant_id == _TENANT_A, "证据行归消费方租户, 不是 NULL"


@pytest.mark.asyncio
async def test_view_row_not_swallowed_by_a_prior_bind() -> None:
    """两道门各算各的 —— 否则 bind 每次构建都占着窗口, 证据行永远写不进去。

    这是本改动的核心失效模式: 共用一个节流窗口时, 每轮都发生的 bind 会把偶发
    的 view 吃掉, 一个**正在被读**的技能照样留不下证据行。
    """
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="bound-then-read",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC) - timedelta(days=1),
    )

    assert await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A) is True
    # 同一个 TTL 窗口内紧跟一次真实阅读 —— last_used_at 的 bump 会被节流掉,
    # 证据行不该跟着被吞。
    await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A, kind="view", view=_view())
    assert len(await _viewed_rows(store)) == 1, "view 的去重门是独立的, 不该被 bind 关上"


@pytest.mark.asyncio
async def test_view_row_deduped_within_one_thread() -> None:
    """一个 thread 内同一技能只记一次 —— 问的是「有没有」不是「几次」。"""
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="read-twice",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC) - timedelta(days=1),
    )
    thread_id = uuid4()

    for _ in range(3):
        await recorder.maybe_record(
            skill_id=skill_id,
            tenant_id=_TENANT_A,
            kind="view",
            view=_view(thread_id=thread_id),
        )
    assert len(await _viewed_rows(store)) == 1

    # 换一个 thread 就是新的一次「打开过」, 必须再记一行。
    await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A, kind="view", view=_view())
    assert len(await _viewed_rows(store)) == 2


@pytest.mark.asyncio
async def test_view_without_payload_degrades_to_a_plain_bump() -> None:
    """``view=None``(没有 thread 绑定)不写证据行, 也不能炸掉热路径。"""
    store = InMemorySkillStore()
    recorder = ThrottledActivityRecorder(store, ttl_seconds=3600)
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="no-thread",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC) - timedelta(days=1),
    )

    fired = await recorder.maybe_record(skill_id=skill_id, tenant_id=_TENANT_A, kind="view")
    assert fired is True
    assert await _viewed_rows(store) == []


@pytest.mark.asyncio
async def test_curator_ignores_viewed_rows() -> None:
    """硬约束: 「没被打开过」不是归档理由 —— hermes 的 absence-of-evidence 判据。

    一个刚被绑定 (``last_used_at`` 是现在) 但一条 viewed 行都没有的技能, 扫一遍
    之后必须还是 ACTIVE。
    """
    store = InMemorySkillStore()
    audit_logger, _ = _build_logger()
    curator = _curator(
        store,
        TenantConfigService(store=InMemoryTenantConfigStore(), audit_logger=audit_logger),
        audit_logger,
    )
    skill_id = await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="never-read-but-fresh",
        status=SkillStatus.ACTIVE,
        last_used_at=datetime.now(UTC),
    )

    summary = await curator.run_once()
    assert summary.active_to_stale == 0
    assert summary.stale_to_archived == 0
    row = await store.get_skill(skill_id=skill_id, tenant_id=_TENANT_A)
    assert row is not None
    assert await _viewed_rows(store) == []
    assert row.status == SkillStatus.ACTIVE


@pytest.mark.asyncio
async def test_curator_sweep_idempotent() -> None:
    """Re-running a sweep with no new activity produces zero transitions."""
    store = InMemorySkillStore()
    audit_logger, _ = _build_logger()
    curator = _curator(
        store,
        TenantConfigService(store=InMemoryTenantConfigStore(), audit_logger=audit_logger),
        audit_logger,
    )
    long_ago = datetime.now(UTC) - timedelta(days=DEFAULT_STALE_DAYS + 1)
    await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="going-cold",
        status=SkillStatus.ACTIVE,
        last_used_at=long_ago,
    )

    first = await curator.run_once()
    second = await curator.run_once()
    assert first.active_to_stale == 1
    assert second.active_to_stale == 0
    assert second.stale_to_archived == 0


# ─── RLS 上下文(班车 2,09-17 读代码判定) ─────────────────────────────


def _rls_ctx() -> tuple[UUID | None, UUID | None, bool]:
    from expert_work.persistence.rls import (
        bypass_rls_var,
        current_tenant_id_var,
        current_user_id_var,
    )

    return current_tenant_id_var.get(), current_user_id_var.get(), bypass_rls_var.get()


class _CtxRecorder:
    """包一层 store:每个 async 方法调用连同当时的 RLS 上下文、位置参数记进 ``log``。"""

    def __init__(
        self, inner: object, log: list[tuple[str, object, tuple[object, ...]]], label: str
    ):
        self._inner = inner
        self._log = log
        self._label = label

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def call(*args: object, **kwargs: object) -> object:
            self._log.append((f"{self._label}.{name}", _rls_ctx(), args))
            return await attr(*args, **kwargs)

        return call


@pytest.mark.asyncio
async def test_sweep_runs_every_store_call_under_an_explicit_rls_context() -> None:
    """``skill`` 表的策略是 ``tenant_id = current_setting('app.tenant_id')::uuid``(没有
    ``NULLIF``):enforce 下不带租户的会话直接报错,不只是读不到。所以:单飞锁、跨租户
    枚举、跨租户 ``count_pinned`` → 显式 bypass;读配置与两次状态推进 → 该租户;
    扫完一轮的审计 → 平台租户。"""
    from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory

    log: list[tuple[str, object, tuple[object, ...]]] = []
    store = InMemorySkillStore()
    long_ago = datetime.now(UTC) - timedelta(days=DEFAULT_STALE_DAYS + 1)
    await _seed_skill(
        store=store,
        tenant_id=_TENANT_A,
        name="cold",
        status=SkillStatus.ACTIVE,
        last_used_at=long_ago,
    )
    audit_store = InMemoryAuditLogStore()
    audit_logger = AuditLogger(
        store=_CtxRecorder(audit_store, log, "audit"),  # type: ignore[arg-type]
        redactor=DefaultSecretRedactor(),
        fallback=InMemoryAuditFallbackQueue(),
    )
    config_service = TenantConfigService(
        store=_CtxRecorder(InMemoryTenantConfigStore(), log, "tenant_config"),  # type: ignore[arg-type]
        audit_logger=audit_logger,
    )
    lock_ctx: list[object] = []
    factory = FakeAdvisoryLockSessionFactory()

    def recording_factory() -> object:
        session = factory()
        real = session.execute

        async def execute(stmt: object, params: dict[str, object] | None = None) -> object:
            lock_ctx.append(_rls_ctx())
            return await real(stmt, params)

        session.execute = execute  # type: ignore[method-assign]
        return session

    curator = SkillCurator(
        skill_store=_CtxRecorder(store, log, "skill"),  # type: ignore[arg-type]
        tenant_config_service=config_service,
        audit_logger=audit_logger,
        interval_s=60.0,
        session_factory=recording_factory,  # type: ignore[arg-type]
    )
    summary = await curator.run_once()
    assert summary.active_to_stale == 1

    assert lock_ctx and set(lock_ctx) == {(None, None, True)}
    bypass_calls = {"skill.curator_distinct_tenant_ids", "skill.count_pinned"}
    tenant_calls = {
        "tenant_config.get",
        "skill.curator_promote_active_to_stale",
        "skill.curator_promote_stale_to_archived",
    }
    seen = {name for name, _, _ in log}
    assert bypass_calls | tenant_calls | {"audit.append"} <= seen
    for name, ctx, args in log:
        if name in bypass_calls:
            assert ctx == (None, None, True), name
        elif name in tenant_calls:
            assert ctx == (_TENANT_A, None, False), name
        elif name == "audit.append":
            assert ctx == (args[0].tenant_id, None, False), name  # type: ignore[attr-defined]
            assert args[0].tenant_id == UUID(int=0)  # type: ignore[attr-defined]
    assert _rls_ctx() == (None, None, False), "上下文必须复原,不能漏给调用方"
