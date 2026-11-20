"""产物分享链接表 —— B-326。

``artifact_share_links``:一行 = 一条公开、限时的下载链接,钉死在某一个产物版本
(``artifact_version.id``)上。智能体用 ``share_file`` 把某个文件标记为「交给用户」,平台铸
一行,链接出现在这一轮 run 的产物清单里(``share_url``),不进模型的文字。

* ``id`` 是链接里那段 16 位随机串(``secrets.token_urlsafe``),也是唯一的凭据。
* ``file_name`` 是铸链接时的产物名,只用来给地址加最后一段文件名(``/v1/public/files/<id>/
  <名字>``,下游按地址最后一段给文件起名),不参与查找与鉴权。
* ``thread_id`` 是铸它的对话:一个 (对话, 版本) 同一时刻最多一条还能用的链接;读产物清单时
  链接不能再用了(过期 / 快过期 / 次数用完),就为同一版本、同一对话再铸一条。``run_id``
  留作审计。
* ``download_count`` 记已下载次数(``>= 0``),每条链接有上限;``expires_at > created_at``。
* ``thread_id`` 非空:链接一定属于某段对话(清除对话按它删)。
* ``artifact_version_id`` 是裸 UUID 列,不建外键:指向 ``FORCE`` RLS 表的外键是已知的坑
  (Mini-ADR J-1a,``artifact_version.artifact_id`` 同样不建)。版本行被删(留存作业 / 清除用户 /
  删智能体)之后,链接行还在,但下载端点按 id 回查版本行查不到 → 404。访问从不删行;过期满宽限期
  后由留存作业删(``retention_cleanup_worker`` 有本表的 SELECT / DELETE)。
* RLS 与 ``artifact`` / ``artifact_version``(0019)同一个租户 + 用户的组合谓词;公开下载端点
  只有 id,按 id 查在 RLS 旁路作用域里做(``bypass_rls_session``)。
* 索引:``expires_at``(留存作业按它删过期行)、``(tenant_id, thread_id,
  artifact_version_id, created_at)``(按 (对话, 版本) 取最新一条;删对话时删链接用它的前缀)。

纯新增一张表,不动存量数据;旧镜像不读它,回滚只回镜像、不降级。

Revision ID: 0194_b326_artifact_share_links
Revises: 0193_rate_card_claude5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0194_b326_artifact_share_links"
down_revision: str | Sequence[str] | None = "0193_rate_card_claude5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "artifact_share_links"
#: 留存作业按 ``expires_at`` 删过期满宽限期的行(BYPASSRLS,只补表级授权,同 0162)。
_RETENTION_ROLE = "retention_cleanup_worker"
_POLICY = "artifact_share_links_isolation"
#: 与 0019 ``artifact`` / ``artifact_version`` 相同:两个 GUC 都没设 → NULLIF→NULL → 拒。
_ISOLATION = (
    "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid "
    "AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_key", sa.Text(), nullable=False),
        sa.Column("artifact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("file_name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.CheckConstraint("download_count >= 0", name="ck_artifact_share_links_download_count"),
        sa.CheckConstraint("expires_at > created_at", name="ck_artifact_share_links_lifetime"),
    )
    op.create_index("ix_artifact_share_links_expires_at", _TABLE, ["expires_at"])
    op.create_index(
        "ix_artifact_share_links_lookup",
        _TABLE,
        ["tenant_id", "thread_id", "artifact_version_id", "created_at"],
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {_TABLE} USING ({_ISOLATION}) WITH CHECK ({_ISOLATION});"
    )
    op.execute(f"GRANT SELECT, DELETE ON TABLE {_TABLE} TO {_RETENTION_ROLE};")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE {_TABLE} FROM {_RETENTION_ROLE};")
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};")
    op.drop_index("ix_artifact_share_links_lookup", table_name=_TABLE)
    op.drop_index("ix_artifact_share_links_expires_at", table_name=_TABLE)
    op.drop_table(_TABLE)
