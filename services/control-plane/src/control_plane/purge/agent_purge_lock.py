"""B-278 —— 同一个智能体名字同一时间只跑一个彻底删除(跨副本)。

两个管理员同时点「彻底删除」、或者一次超时后又点了一次:两个删除并发跑会互相把对方正在
删的东西删掉(NAS 子项 ENOENT、同一行删两遍),汇总也各报一半。所以整个
``purge_agent`` 期间持有一把按 ``(tenant, name)`` 的锁;拿不到就直接 409 ``AGENT_BUSY``,
不排队。

锁是 ``pg_try_advisory_xact_lock``(与 ``_tenant_resource_lock`` 同一种):一个专用会话开着
事务把它拿住,删完 ``rollback`` 释放;进程死了连接一断锁就没了。删除可能跑很久,而这个会话
一直空着 —— 所以这个事务关掉 ``idle_in_transaction_session_timeout``(``= 0``):要是让库按
空闲超时掐掉会话,锁就悄悄丢了,第二个删除能同时进来。释放(``rollback``)失败只记日志、
绝不往外抛:删除的结果(汇总、审计、缓存失效)比释放一把已经没了的锁重要。没有数据库
(单进程内存栈)时退化成进程内的一个集合 —— 只有一个进程,不存在跨副本的竞争。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import AGENT_PURGE_LOCK_CLASSID

logger = logging.getLogger("expert_work.control_plane.purge.agent")


#: 单进程栈(没有数据库)里正在删除的名字。
_LOCAL: set[tuple[UUID, str]] = set()


@asynccontextmanager
async def agent_purge_lock(
    session_factory: async_sessionmaker[AsyncSession] | None, tenant_id: UUID, name: str
) -> AsyncIterator[bool]:
    """``async with agent_purge_lock(...) as got:`` —— ``got`` 为 ``False`` 表示同名的另一个
    彻底删除正在跑,调用方回 409,什么都别做。"""
    if session_factory is None:
        key = (tenant_id, name)
        if key in _LOCAL:
            yield False
            return
        _LOCAL.add(key)
        try:
            yield True
        finally:
            _LOCAL.discard(key)
        return

    session = session_factory()
    try:
        # 0 = 不按空闲掐:删除在别的会话上跑,这个会话一直空着;进程死了连接自己会断。
        await session.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        got = bool(
            (
                await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
                    {"cid": AGENT_PURGE_LOCK_CLASSID, "k": f"{tenant_id}:{name}"},
                )
            ).scalar_one()
        )
    except BaseException:
        await _release(session)
        raise
    try:
        if not got:
            logger.info("agent_purge_lock.contended tenant_id=%s", tenant_id)
        yield got
    finally:
        await _release(session)


async def _release(session: AsyncSession) -> None:
    """``rollback`` ends the transaction → releases the xact advisory lock; then close.
    A dead connection (killed session, network) has nothing left to release — never
    let that replace the purge's own outcome (summary, audit, cache invalidation)."""
    try:
        await session.rollback()
    except Exception as exc:
        logger.warning("agent_purge_lock.release_failed error=%s", type(exc).__name__)
    try:
        await session.close()
    except Exception as exc:
        logger.warning("agent_purge_lock.close_failed error=%s", type(exc).__name__)


__all__ = ["agent_purge_lock"]
