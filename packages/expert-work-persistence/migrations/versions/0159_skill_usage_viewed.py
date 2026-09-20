"""``skill_run_usage.outcome`` 放行 ``viewed`` —— 技能「绑了」vs「被打开过」(B-84)。

``skill.last_used_at`` 由**两条**路径 bump: ``agent_factory._load_skills`` 的
build-time 绑定 (Mini-ADR U-27) 与 ``skill_view`` 的运行期读取。绑定在每次 agent
build 都发生, 于是那一列回答的是「这个技能还绑在某个 agent 上吗」, 而不是「它被
打开过吗」。

实测缺口 (测试环境近 60 天, 2026-09-20):

* ``ai-health-plan`` 绑定 19 个技能, 其中 **15 个 (79%) 一次都没被 skill_view
  过**; 没被读过的里面有 ``ui-ux-pro-max`` / ``banner-design`` / ``humanizer-zh``,
  跟健康方案无关却每轮都占着系统提示词。
* ``sop2-designer`` 绑定 13 个技能, 其中 **8 个 (62%) 一次都没被 skill_view 过**。

**为什么记在这张表而不是给 ``skill`` 加列**: 那 19 个技能 **19/19 都是平台技能**
(``skill.tenant_id IS NULL``)。``skill`` 行是全平台共享的一行, 写在上面既答不了
「**哪个 agent** 没打开过」(只能答「有没有人打开过」), 又要求租户会话去写
NULL-tenant 行 —— RLS enforce 上线后会静默变成 0 行, 信号无声死掉。
``skill_run_usage`` 的行是**消费方租户自有**的 (``tenant_id`` = 读它的那个租户,
``skill_id`` 指向平台技能行), 天然 per-(租户, agent, thread), RLS 干净。

**本迁移只放宽一条 CHECK**, 不加表不加列不动数据 —— ``viewed`` 之前不是合法值,
所以存量零行受影响 (这张表在测试环境本来就是 0 行: 它此前只为 distilled 技能写)。

**⚠️ ``viewed`` 行绝不能进回滚判定的样本。**
``control_plane.skill_rollback.decide_rollback`` 算 ``successes / len(非 cancelled)``;
一条 ``viewed`` 就是一个「非 success」样本, 会凭空拉低成功率、把健康版本自动
archive 掉。守在两处: ``SkillStore.skill_run_usage_window`` (回滚判定唯一的读取口)
过滤掉 ``viewed``, ``SkillRollbackGate`` 再过滤一次。两道, 因为这条路的失败后果是
静默删技能。

Revision ID: 0159_skill_usage_viewed
Revises: 0158_run_completion
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0159_skill_usage_viewed"
down_revision: str | Sequence[str] | None = "0158_run_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_CHECK = "skill_run_usage_outcome_check"
_OLD = "outcome IN ('success', 'failed', 'max_steps', 'cancelled')"
_NEW = "outcome IN ('success', 'failed', 'max_steps', 'cancelled', 'viewed')"


def upgrade() -> None:
    op.drop_constraint(_CHECK, "skill_run_usage", type_="check")
    op.create_check_constraint(_CHECK, "skill_run_usage", _NEW)


def downgrade() -> None:
    # 收窄回去之前必须先清掉新取值的行, 否则 ADD CONSTRAINT 会被存量行顶回来。
    # 删 ``viewed`` 行是安全的: 它们只喂 B-84 的只读盘点, 没有别的读者。
    op.execute("DELETE FROM skill_run_usage WHERE outcome = 'viewed'")
    op.drop_constraint(_CHECK, "skill_run_usage", type_="check")
    op.create_check_constraint(_CHECK, "skill_run_usage", _OLD)
