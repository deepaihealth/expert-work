"""0196 — 价目表补 ``doubao / doubao-seed-2-1-turbo-260628`` 与 ``doubao-seed-2-1-lite-260915``。

新模型随模型目录一起上架。用量按配置里的模型名记账,新名字没有价目行就记成「未定价」、成本 0
(B-104 的教训);价目表的写接口又只收模型目录里有的名字,新目录随镜像上线之前加不进去。所以
跟着这次发布一起补,测试和生产一样,不留手工步骤(同 0187 / 0192)。

价格来源:2026-11-20 火山方舟控制台模型页(用户截图),与厂商计费文档
(volcengine.com/docs/82379/1544106,同日读)一致;在线推理、不分长度档。单位:元 / 百万 token。

* ``doubao-seed-2-1-turbo-260628``:输入 3、输出 15、缓存命中 0.6。
* ``doubao-seed-2-1-lite-260915``:输入 0.8、输出 2.7、缓存命中 0.16。

缓存创建价是 0(同 0187):平台对豆包不发显式缓存请求,用量里没有缓存创建 token;页面上的缓存
存储费(0.017 元 / 百万 token / 小时,只有显式缓存才有)没有对应的列。批量 / Fast / Flex 档的
价也不在这里:平台只走在线推理。

lite 的音频输入另有单价(输入 12、缓存命中 2.4),价目表只有一列输入价、装不下;平台也没接音频
输入,用量里不会有音频 token。真接音频时要先给价目表加列。

已经有同名行就不动(人工定的价优先)。别的豆包行不插、不改。

downgrade 只删四个价与这里种的一字不差的行 —— 人工改过价的行留着。人工补了一模一样价的
行与本迁移插的分不出来,会一并删掉;再升级时原样补回。

Revision ID: 0196_rate_card_doubao_seed21
Revises: 0195_rate_card_qwen38_cache
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0196_rate_card_doubao_seed21"
down_revision: str | Sequence[str] | None = "0195_rate_card_qwen38_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: ``(provider, model, input, output, cache creation, cache read)`` — micro-元 per
#: million tokens; source in the module docstring.
_PRICES = (
    ("doubao", "doubao-seed-2-1-turbo-260628", 3_000_000, 15_000_000, 0, 600_000),
    ("doubao", "doubao-seed-2-1-lite-260915", 800_000, 2_700_000, 0, 160_000),
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
