"""0166 — 价目表补 ``deepseek / deepseek-flash``,价格照抄平台现有的 ``deepseek-v4-flash`` 那一行。

DeepSeek 2026-10 把 Flash 换成 V4.1,新模型名 ``deepseek-flash``;旧名 ``deepseek-v4-flash``
仍可调用,但由 V4.1-Flash 应答、按 Flash 价计费 —— 两个名字是同一个模型、同一个价。用量按
配置里的模型名记账,新名字没有价目行就记成「未定价」;价目表的写接口又只收模型目录里有的
名字,新目录随镜像上线之前加不进去。所以跟着这次发布一起补,测试和生产一样,不留手工步骤。

只照抄,不编价格:平台没有 ``deepseek-v4-flash`` 的价目行就什么都不写(没有可抄的价,
由运维按官网标价补);已经有 ``deepseek-flash`` 的价目行就不动(人工定的价优先)。

downgrade 不删:这一行和人工补的一行分不出来,删错了会让用量变成未定价;留着无害。

Revision ID: 0166_rate_card_deepseek_flash
Revises: 0165_knowledge_model_v2
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0166_rate_card_deepseek_flash"
down_revision: str | Sequence[str] | None = "0165_knowledge_model_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                     output_per_mtok_micros, cache_creation_per_mtok_micros,
                                     cache_read_per_mtok_micros)
        SELECT NULL, 'deepseek', 'deepseek-flash', input_per_mtok_micros, output_per_mtok_micros,
               cache_creation_per_mtok_micros, cache_read_per_mtok_micros
          FROM model_rate_card
         WHERE tenant_id IS NULL AND provider = 'deepseek' AND model = 'deepseek-v4-flash'
           AND NOT EXISTS (
               SELECT 1 FROM model_rate_card
                WHERE tenant_id IS NULL AND provider = 'deepseek' AND model = 'deepseek-flash'
           )
        """
    )


def downgrade() -> None:
    pass
