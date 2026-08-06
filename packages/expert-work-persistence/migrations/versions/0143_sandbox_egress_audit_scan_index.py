"""0143 — 出网审计表的扫描索引 + retention 角色授权.

**索引**。``SandboxEgressMetricsWorker`` 每 60 秒跑一次
``occurred_at >= :since AND occurred_at < :until AND verdict <> 'allowed'``
的窗口聚合(``SqlSandboxEgressAuditStore.count_by_verdict_since``)。0087 建的
两个索引都以 ``tenant_id`` 打头,这条不带 tenant 谓词的查询用不上它们,只能
顺序扫整张 append-only 表——随行数线性变贵,而 ``allowed`` 又恰好是量最大的
那一类。索引谓词与查询的 ``WHERE`` 逐字相同(``verdict <> 'allowed'``),
Postgres 才认得出这是可用的 partial index;两列 ``(occurred_at, verdict)``
让 ``GROUP BY verdict`` 也走 index-only scan,不用回表。

**授权**。这张表建表至今没有任何表级 GRANT(0087/0088 都没写),而波 1 PR-E
给它加了 retention 清扫 pass —— ``retention_cleanup_worker``(0010 建)拿不到
SELECT/DELETE 就是 permission denied。0131 记过这笔历史欠账的教训:清扫 pass
上线要跟着补授权。这张表没有 RLS/policy,所以只补表级 GRANT 就够。

非 CONCURRENTLY(同 0141/0142 的理由:alembic 迁移跑在事务里,
``CREATE INDEX CONCURRENTLY`` 不允许)。这张表写多读少,建索引会短暂持写锁
——runbook 记一笔,生产上挑低峰执行。

Revision id ``0143_egress_audit_scan_idx`` = 26 chars(文件名保留完整描述,
revision 字段收窄到 32 字符的 alembic ``version_num`` 上限内,同
``0048_tenant_mcp_creds`` 先例,见 [memory:alembic-revision-id-32-chars])。

Revision ID: 0143_egress_audit_scan_idx
Revises: 0142_sandbox_warm_backend_scope
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0143_egress_audit_scan_idx"
down_revision: str | Sequence[str] | None = "0142_sandbox_warm_backend_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_INDEX = "sandbox_egress_audit_scan_idx"
_TABLE = "sandbox_egress_audit"
_RETENTION_ROLE = "retention_cleanup_worker"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX {_INDEX}
          ON {_TABLE} (occurred_at, verdict)
          WHERE verdict <> 'allowed'
        """
    )
    op.execute(f"GRANT SELECT, DELETE ON TABLE {_TABLE} TO {_RETENTION_ROLE};")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE {_TABLE} FROM {_RETENTION_ROLE};")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
