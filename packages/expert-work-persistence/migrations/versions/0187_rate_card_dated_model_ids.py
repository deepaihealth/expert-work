"""0187 — 价目表补 ``qwen / qwen3.8-max-0902`` 与 ``doubao / doubao-seed-2-1-pro-260915``。

两个带日期的新模型名随模型目录一起上架。用量按配置里的模型名记账,新名字没有价目行就记成
「未定价」、成本 0(B-104 的教训);价目表的写接口又只收模型目录里有的名字,新目录随镜像上线
之前加不进去。所以跟着这次发布一起补,测试和生产一样,不留手工步骤(同 0166 / 0169)。

价格来源:2026-11-11 产品负责人在厂商控制台确认;通义页面是 qwen3.8-max 的标价、豆包页面是
Seed 2.1 pro 档的标价,均未单列带日期快照。单位:元 / 百万 token。

* ``qwen3.8-max-0902``:输入 12、输出 36、输入缓存命中 1.5。
* ``doubao-seed-2-1-pro-260915``:输入 6、输出 30、缓存命中 1.2。

缓存创建价两行都是 0:平台对这两家不发显式缓存请求,用量里没有缓存创建 token。通义页面上
的显式缓存创建 15、显式缓存命中 1 没有对应的列(一个模型只有一个缓存命中价),不入表。

已经有同名行就不动(人工定的价优先)。旧名(``qwen3.8-max``、
``doubao-seed-2-1-pro-260628``)不插、不改。

downgrade 只删四个价与这里种的一字不差的行 —— 人工改过价的行留着。人工补了一模一样价的
行与本迁移插的分不出来,会一并删掉;再升级时原样补回。

Revision ID: 0187_rate_card_dated_ids
Revises: 0186_b282_conv_dir_expiry
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0187_rate_card_dated_ids"
down_revision: str | Sequence[str] | None = "0186_b282_conv_dir_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: ``(provider, model, input, output, cache creation, cache read)`` — micro-元 per
#: million tokens; sources in the module docstring.
_PRICES = (
    ("qwen", "qwen3.8-max-0902", 12_000_000, 36_000_000, 0, 1_500_000),
    ("doubao", "doubao-seed-2-1-pro-260915", 6_000_000, 30_000_000, 0, 1_200_000),
)


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
