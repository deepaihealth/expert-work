"""B-63 / B-283 —— 「按最后使用到期」的那一个时钟。

附件(``user_upload``)与图片(``image_upload``)共用。
两张表各写一份的话,改一边忘一边就是同一个文件的登记行与字节在不同的日子到期。这里只写一次:

* 到期 = ``greatest(created_at, last_used_at, 会话最后一次 run)`` + 租户的保留期(``days_column``,
  没设用 ``default_days``)。``greatest`` 跳过 NULL(没用过、会话没有 run)。
* 会话最后一次 run 用 CTE **一次**算出:先取候选行涉及的 ``(tenant_id, thread_id)``,再对这些会话的
  ``agent_run`` 按会话聚合一遍(走 ``ix_agent_run_thread_id``),然后 join 回来。只认**同租户**的
  run —— 租户条件在聚合与 join 回候选行时各有一份,两份是纵深防御(任何一份单独都足以挡住别的
  租户的 run)。
* ``give_up_from`` = ``least(greatest(到期, tenant_config.updated_at), 到期 + GIVE_UP_DEFER_CAP)``:
  租户刚把天数改小时,「过了到期日 7 天还在失败就放弃」从这次改动算起,但最多推迟
  ``GIVE_UP_DEFER_CAP``(配置的任何改动都会刷新 ``updated_at``)。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, Subquery, and_, func, select

from expert_work.persistence.models import AgentRunRow, TenantConfigRow


def last_use_expiry(
    model: Any,
    *,
    days_column: Any,
    default_days: int,
    give_up_cap: timedelta,
    name: str,
    candidates: tuple[ColumnElement[bool], ...] = (),
) -> Subquery:
    """``model`` 里没删(``deleted_at IS NULL``)、满足 ``candidates`` 的行各自的
    ``(id, expires_at, give_up_from)``。``model`` 要有 ``id`` / ``tenant_id`` / ``thread_id`` /
    ``created_at`` / ``last_used_at`` / ``deleted_at`` 这几列;``name`` 给 CTE 起名用。"""
    cand = (
        select(
            model.id,
            model.tenant_id,
            model.thread_id,
            model.created_at,
            model.last_used_at,
        )
        .where(model.deleted_at.is_(None), *candidates)
        .cte(f"{name}_candidates")
    )
    threads = select(cand.c.tenant_id, cand.c.thread_id).distinct().cte(f"{name}_threads")
    last_run = (
        select(
            AgentRunRow.tenant_id,
            AgentRunRow.thread_id,
            func.max(AgentRunRow.created_at).label("at"),
        )
        .join(
            threads,
            and_(
                AgentRunRow.thread_id == threads.c.thread_id,
                AgentRunRow.tenant_id == threads.c.tenant_id,
            ),
        )
        .group_by(AgentRunRow.tenant_id, AgentRunRow.thread_id)
        .cte(f"{name}_thread_last_run")
    )
    days = func.coalesce(days_column, default_days)
    expires_at = func.greatest(
        cand.c.created_at, cand.c.last_used_at, last_run.c.at
    ) + func.make_interval(0, 0, 0, days)
    return (
        select(
            cand.c.id,
            expires_at.label("expires_at"),
            func.least(
                func.greatest(expires_at, TenantConfigRow.updated_at),
                expires_at + give_up_cap,
            ).label("give_up_from"),
        )
        .select_from(cand)
        .outerjoin(
            last_run,
            and_(
                last_run.c.thread_id == cand.c.thread_id,
                last_run.c.tenant_id == cand.c.tenant_id,
            ),
        )
        .outerjoin(TenantConfigRow, TenantConfigRow.tenant_id == cand.c.tenant_id)
        .subquery(f"{name}_expiry")
    )


__all__ = ["last_use_expiry"]
