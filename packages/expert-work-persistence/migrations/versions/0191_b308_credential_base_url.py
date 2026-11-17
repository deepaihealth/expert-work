"""平台凭证加「请求地址」—— B-308(设计 §4.1)。

``platform_provider_secret`` 与 ``tenant_provider_secret`` 各加两列:

* ``base_url`` —— 这把密钥的请求地址(API 根地址,含版本段)。空 = 官方地址。
* ``base_url_unverified`` —— 强制保存后的「未通过测试」标记;非空,默认 false。

``base_url`` 带一条兜底的检查约束(只许 ``https://`` 开头或空);真正的校验在接口层。

不回填:老行 ``base_url`` 为空,行为与今天相同。旧镜像不读不写这两列(``base_url_unverified``
有库级默认值,旧镜像的插入照常成功;它的 upsert 不动这两列,解析密钥时无视地址);降级删列。
两张表已有的访问方式(平台行不带租户、调用方绕过 RLS)不涉及列,不动。

发布与回滚注意:

* 滚动升级完成前不要给任何密钥配地址 —— 还没换镜像的副本会把那把密钥发到官方地址。
* 回滚镜像前,先把配了地址的行清空地址(只打印行数的核对 SQL 见设计 §4.10)。否则
  旧镜像带着「为转发贴的密钥」去打官方地址(调用失败,不外泄);旧镜像上切换启用也不会清掉
  库里的地址,再升级回来时地址还在。

Revision ID: 0191_b308_credential_base_url
Revises: 0190_b300_memory_evidence
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0191_b308_credential_base_url"
down_revision: str | Sequence[str] | None = "0190_b300_memory_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLES = ("platform_provider_secret", "tenant_provider_secret")
_HTTPS_ONLY = "base_url IS NULL OR base_url LIKE 'https://%'"


def _check_name(table: str) -> str:
    return f"ck_{table}_base_url_https"


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("base_url", sa.Text(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "base_url_unverified", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
        )
        op.create_check_constraint(_check_name(table), table, _HTTPS_ONLY)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(_check_name(table), table, type_="check")
        op.drop_column(table, "base_url_unverified")
        op.drop_column(table, "base_url")
