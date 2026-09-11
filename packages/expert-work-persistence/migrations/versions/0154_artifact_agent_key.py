"""artifact 加 agent_key —— 同一用户下不同 agent 的同名产物不再合并成一行。

B-50 工作区分层 spec §6.1。原唯一键 ``(tenant_id, user_id, name)`` 让两个 agent
存同名产物时走 ``ON CONFLICT DO UPDATE``,合并成一行、版本号累加、旧字节被覆盖:
用户以为自己有两份报告,实际只剩一份。新键 ``(tenant_id, user_id, agent_key, name)``
把它们分成两条独立行,各自版本序列。

**回填链比字段名长一跳。** ``artifact_version.created_in_thread`` 存的是
**run_id** 不是 thread_id(``orchestrator/tools/artifact.py`` 写的是
``str(ctx.run_id)``,无 run_id 时回落到一个非 UUID 的常量)。所以是
``created_in_thread`` → ``agent_run.id`` → ``agent_run.thread_id``
→ ``thread_meta.agent_name``。少一跳不会报错,只是所有行都留空串。

推不出来的留空串,**不猜**:``created_in_thread`` 是回落常量的、``agent_run``
行已被清理的。空串 = 「归属不明的历史产物」,与 ``ToolContext.agent_key`` 的
「空串=没绑 agent」同一套语义。

一条 artifact 若被两个 agent 先后写过,归**最早**那个版本的作者 —— 判给最后
写的会让第一个 agent 的产物凭空改姓,而后来者在新键下本来就会分出自己的行。

``agent_key`` 含 sha256,SQL 算不出来。照 ``0111`` 的先例 import 生产代码的
实现(``expert_work.protocol.agent_key``,为此从 orchestrator 下沉到 protocol)——
**绝不在这里复制第二份算法**:它与沙箱的 ``/opt/skills/<agent_key>`` 必须永远一致。

Revision ID: 0154_artifact_agent_key
Revises: 0153_agent_run_supersede
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from expert_work.protocol.agent_key import sanitize_agent_key

revision: str = "0154_artifact_agent_key"
down_revision: str | Sequence[str] | None = "0153_agent_run_supersede"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: 每条 artifact 取它**最早**那个版本的归属。``created_in_thread`` 存的是 run_id,
#: 而它是 TEXT 列、可能装着非 UUID 的回落常量 —— 所以 join 时把 ``agent_run.id``
#: 转成 text 来比,而不是把这一列转成 uuid(后者遇到回落常量直接抛
#: ``invalid input syntax for type uuid``,整条迁移失败)。
_RESOLVE_OWNERS = sa.text(
    """
    SELECT DISTINCT ON (a.id) a.id AS artifact_id, tm.agent_name AS agent_name
      FROM artifact AS a
      JOIN artifact_version AS av ON av.artifact_id = a.id
      LEFT JOIN agent_run AS r ON r.id::text = av.created_in_thread
      LEFT JOIN thread_meta AS tm ON tm.thread_id = r.thread_id
     ORDER BY a.id, av.version ASC
    """
)


def upgrade() -> None:
    op.add_column(
        "artifact",
        sa.Column("agent_key", sa.Text(), nullable=False, server_default=""),
    )

    bind = op.get_bind()
    # 按 agent_name 分组后一句 UPDATE 一组:不同 agent 名的数量是个位数,
    # 逐行 UPDATE 在大表上是没必要的往返。
    by_agent: dict[str, list[str]] = {}
    for row in bind.execute(_RESOLVE_OWNERS):
        if not row.agent_name:
            continue  # 链断了 → 保持空串,不猜
        by_agent.setdefault(row.agent_name, []).append(str(row.artifact_id))
    for agent_name, artifact_ids in by_agent.items():
        bind.execute(
            sa.text("UPDATE artifact SET agent_key = :key WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"key": sanitize_agent_key(agent_name), "ids": artifact_ids},
        )

    op.drop_constraint("artifact_identity_uniq", "artifact", type_="unique")
    op.create_unique_constraint(
        "artifact_identity_uniq", "artifact", ["tenant_id", "user_id", "agent_key", "name"]
    )


def downgrade() -> None:
    # 回滚在「已经有两个 agent 存了同名产物」之后会失败:重建三元组唯一约束时
    # 那两行撞键。这是不可逆迁移的正常形态,回滚窗口在同名产物出现之前。
    # **不要为了让 downgrade 一定成功而去删数据。**
    op.drop_constraint("artifact_identity_uniq", "artifact", type_="unique")
    op.create_unique_constraint(
        "artifact_identity_uniq", "artifact", ["tenant_id", "user_id", "name"]
    )
    op.drop_column("artifact", "agent_key")
