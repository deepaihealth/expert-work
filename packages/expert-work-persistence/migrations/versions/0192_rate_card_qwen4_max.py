"""0192 — 价目表补 ``qwen / qwen4-max``。

新模型随模型目录一起上架。用量按配置里的模型名记账,新名字没有价目行就记成「未定价」、成本 0
(B-104 的教训);价目表的写接口又只收模型目录里有的名字,新目录随镜像上线之前加不进去。所以
跟着这次发布一起补,测试和生产一样,不留手工步骤(同 0166 / 0169 / 0187)。

价格来源:2026-11-17 厂商模型页(用户截图),没有分长度档。单位:元 / 百万 token。

* ``qwen4-max``:输入 10、输出 30、输入缓存命中 0.5。

缓存创建价是 0(同 0187):平台对通义不发显式缓存请求,用量里没有缓存创建 token。页面上的
显式缓存创建(5 分钟 12.5、1 小时 20)没有对应的列;显式缓存命中 0.5 与隐式命中同价,一列装得下。

已经有同名行就不动(人工定的价优先)。别的通义行不插、不改。

downgrade 只删四个价与这里种的一字不差的行 —— 人工改过价的行留着。人工补了一模一样价的
行与本迁移插的分不出来,会一并删掉;再升级时原样补回。

Revision ID: 0192_rate_card_qwen4_max
Revises: 0191_b308_credential_base_url
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0192_rate_card_qwen4_max"
down_revision: str | Sequence[str] | None = "0191_b308_credential_base_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: ``(provider, model, input, output, cache creation, cache read)`` — micro-元 per
#: million tokens; source in the module docstring.
_PRICES = (("qwen", "qwen4-max", 10_000_000, 30_000_000, 0, 500_000),)


def upgrade() -> None:
    for provider, model, input_price, output_price, creation_price, read_price in _PRICES:
        # The values are module constants, not user input.
        op.execute(
            f"""
            INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                         output_per_mtok_micros, cache_creation_per_mtok_micros,
                                         cache_read_per_mtok_micros)
            SELECT NULL, '{provider}', '{model}', {input_price}, {output_price},
                   {creation_price}, {read_price}
             WHERE NOT EXISTS (
                 SELECT 1 FROM model_rate_card
                  WHERE tenant_id IS NULL AND provider = '{provider}' AND model = '{model}'
             )
            """  # noqa: S608
        )


def downgrade() -> None:
    for provider, model, input_price, output_price, creation_price, read_price in _PRICES:
        op.execute(
            f"""
            DELETE FROM model_rate_card
             WHERE tenant_id IS NULL AND provider = '{provider}' AND model = '{model}'
               AND input_per_mtok_micros = {input_price}
               AND output_per_mtok_micros = {output_price}
               AND cache_creation_per_mtok_micros = {creation_price}
               AND cache_read_per_mtok_micros = {read_price}
            """  # noqa: S608
        )
