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
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.skill import InMemorySkillStore
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.protocol import (
    AuditAction,
    AuditQuery,
    SkillStatus,
    TenantConfigPatch,
)
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
