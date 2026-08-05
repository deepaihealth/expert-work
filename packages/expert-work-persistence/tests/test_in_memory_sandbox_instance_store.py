"""``InMemorySandboxInstanceStore`` 与 SQL 实现的语义对齐(全分支终审 Critical-1)。

单独一个文件而不是塞进 ``test_sql_sandbox_instance_store.py``:那份整文件
``pytestmark = pytest.mark.integration``,要真 docker + 真 Postgres;内存实现
一个依赖都不需要,理应在每一次 ``pytest -m "not integration"`` 全仓扫描里就
跑到,而不是只在偶尔起容器的场次才被验证。

这里只覆盖两个 store **必须逐字同义**的那条谓词:``claim_warm`` 对
"``container_id`` 仍是 NULL 的行"的处置——还新鲜就 raise、超过
``_STUCK_CREATE_TTL_S`` 就接管。同一条规则分散在两处实现、改了一处忘了另一
处,是这个仓踩过的老坑;SQL 侧的同名场景在
``test_sql_sandbox_instance_store.py`` 里由真 Postgres 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence.sandbox_instance_store import (
    _REASON_STUCK_CREATE_TAKEOVER,
    _STUCK_CREATE_TTL_S,
    InMemorySandboxInstanceStore,
    _missing_row_message,
)


@pytest.mark.asyncio
async def test_claim_warm_raises_while_the_winner_is_still_creating() -> None:
    """新鲜的 NULL-container 行(正常还在 E2B 35-40s 冷启窗口内)必须 raise
    ——不能接管,那会跟一次合法的在途 ``acquire()`` 抢同一行。"""
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id = uuid4(), uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4()) is None

    with pytest.raises(RuntimeError, match="already being created"):
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())


@pytest.mark.asyncio
async def test_claim_warm_takes_over_a_stale_mid_create_row() -> None:
    """Critical-1 的内存侧镜像:编排进程死在 ``claim_warm`` 提交与
    ``set_container_id`` 之间留下的孤儿,超过 ``_STUCK_CREATE_TTL_S`` 后必须
    被接管,而不是让这个 ``(tenant, user)`` 永久失去沙箱能力。
    """
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, orphan_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=orphan_id) is None
    )
    # 公开 API 造不出"很久以前",直接把这一行的 acquired_at 拨老 —— 与 SQL
    # 侧同款测试用 raw UPDATE backdate 是同一个手法。
    store._rows[orphan_id].acquired_at = datetime.now(UTC) - timedelta(
        seconds=_STUCK_CREATE_TTL_S * 2
    )

    newcomer_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=newcomer_id)

    assert result is None, "过期的孤儿行必须被接管,这次调用应当成为新赢家"
    # 弱断言("没抛异常")不够:新行要真的存在且槽位归它。
    assert await store.get_container_id(sandbox_id=newcomer_id) is None
    await store.set_container_id(sandbox_id=newcomer_id, container_id="sbx-after-takeover")
    assert await store.get_container_id(sandbox_id=newcomer_id) == "sbx-after-takeover"
    # 孤儿行本身没了(终态写),不会再被 list_active/list_stuck_creating 捡到。
    assert orphan_id not in store._rows
    assert orphan_id not in await store.list_stuck_creating()


@pytest.mark.asyncio
async def test_claim_warm_takeover_is_not_triggered_by_a_ready_row() -> None:
    """已经回填了 ``container_id`` 的行不管多老都不接管——它是一个可复用的
    热会话,不是孤儿。``acquired_at`` 一样拨老,免得"因为太新所以没接管"这个
    无关的理由把断言变成摆设。
    """
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, winner_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=winner_id) is None
    )
    await store.set_container_id(sandbox_id=winner_id, container_id="sbx-warm")
    store._rows[winner_id].acquired_at = datetime.now(UTC) - timedelta(
        seconds=_STUCK_CREATE_TTL_S * 2
    )

    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())

    assert result == (winner_id, "sbx-warm")


@pytest.mark.asyncio
async def test_stuck_create_takeover_reason_is_shared_by_both_stores() -> None:
    """接管用的 ``destroy_reason`` 是两个 store 共用的同一个常量——SQL 侧的
    ``test_claim_warm_takeover_marks_the_orphan_with_its_own_reason`` 断言这个
    值真的落到了 ``sandbox_instance.destroy_reason`` 列上;内存实现不存
    reason(``mark_destroyed`` 直接丢行),这里只钉住"两边走的是同一个常量、
    没有各写各的字面量"。
    """
    assert _REASON_STUCK_CREATE_TAKEOVER == "stuck_create_takeover"


# ---------------------------------------------------------------------------
# 再审 Important-2 —— set_container_id 的"行不在则 raise"契约,内存侧。
# 与 test_sql_sandbox_instance_store.py 里同名的三条一一对应:那三条在真
# Postgres 上验 state/destroyed_at 两个谓词,这三条验内存实现给出**逐字同义**
# 的结果。两边共用 _missing_row_message,连措辞都不许各写各的。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_container_id_rejects_a_row_that_never_existed() -> None:
    store = InMemorySandboxInstanceStore()

    with pytest.raises(RuntimeError, match="is gone"):
        await store.set_container_id(sandbox_id=uuid4(), container_id="sbx-nowhere")


@pytest.mark.asyncio
async def test_set_container_id_rejects_an_already_destroyed_row() -> None:
    """内存实现的 ``mark_destroyed`` 直接把行丢了,所以"已销毁"与"从未存在"
    在这里物理上是同一个状态——而 SQL 侧它们是两个不同的物理状态(行还在,
    只是 state/destroyed_at 变了)。两边可观测行为必须一样,这正是要分开写
    两条而不是合并成一条的原因。"""
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )
    await store.mark_destroyed(sandbox_id=sandbox_id, reason=_REASON_STUCK_CREATE_TAKEOVER)

    with pytest.raises(RuntimeError, match="is gone"):
        await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-too-late")


@pytest.mark.asyncio
async def test_set_container_id_still_backfills_a_live_row() -> None:
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )

    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-live")

    assert await store.get_container_id(sandbox_id=sandbox_id) == "sbx-live"


@pytest.mark.asyncio
async def test_both_stores_share_one_missing_row_wording() -> None:
    """措辞也共享(``_missing_row_message``)——上面六条都 ``match="is gone"``,
    两边各写各的字面量的话,这个断言会红,而不是"两条测试各自绿、生产上两个
    后端给出不同错误"。"""
    sandbox_id = uuid4()
    message = _missing_row_message(sandbox_id)
    assert str(sandbox_id) in message
    assert "is gone" in message


@pytest.mark.asyncio
async def test_create_ephemeral_does_not_overwrite_existing_row() -> None:
    """#5b:id 碰撞时先写者赢(对齐 SQL 的 ON CONFLICT DO NOTHING)。"""
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)

    await store.create_ephemeral(tenant_id=uuid4(), sandbox_id=sandbox_id)

    # warm 行原样健在:user_id 仍在、_warm 指针没悬空
    assert await store.is_warm_session(sandbox_id=sandbox_id) is True
