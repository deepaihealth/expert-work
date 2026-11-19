"""0193 —— 价目表补 Claude 5 四个型号(B-320)。

新模型随模型目录一起上架。用量按配置里的模型名记账,新名字没有价目行就记成「未定价」、成本 0
(B-104 的教训);价目表的写接口又只收模型目录里有的名字,新目录随镜像上线之前加不进去。所以
跟着这次发布一起补,测试和生产一样,不留手工步骤(同 0187 / 0192)。

价格来源:Anthropic 官方接口文档的价目(2026-11-03 版,美元 / 百万 token;没有直接打开官网
价目页核对)。价目表的单位是元(B-202 用户拍板:成本按人民币算),所以按 ``_CNY_PER_USD`` 换算:
2026-11-17 人民币汇率中间价 6.7132(用户 2026-11-19 拍板,写死;仓库里此前没有美元换人民币的
约定,旧的三个 Claude 型号、OpenAI 的价目行都是运维在控制台手填的)。之后价格变动在控制台
价目表里按型号改。

============== ====== ====== ================ ========
型号             输入    输出    缓存写入(5 分钟) 缓存命中
============== ====== ====== ================ ========
claude-fable-5-2  8      40      10               0.20
claude-opus-5-5   4      20      5                0.20
claude-sonnet-5-5 2      10      2.50             0.10
claude-haiku-5-5  0.10   0.50    0.125            0.01
============== ====== ====== ================ ========

缓存写入取 5 分钟档:平台给 Anthropic 发的是 ``cache_control: ephemeral``(默认 5 分钟),
1 小时档(输入价 2 倍)平台不用。

**Haiku 5.5 分档价装不下**:提示 ≤ 100K token 是上表的价,超过 100K 是 0.50 / 2.50(缓存按
同样倍数)。价目表一个模型只有一行、不分长度档,这里种 ≤ 100K 档 —— 长提示的调用会被**少算
5 倍**。要算准得给价目表加长度分档(B-321,未排期)。

已经有同名行就不动(人工定的价优先)。别的行不插、不改。

downgrade 只删四个价与这里种的一字不差的行 —— 人工改过价的行留着。人工补了一模一样价的
行与本迁移插的分不出来,会一并删掉;再升级时原样补回。

Revision ID: 0193_rate_card_claude5
Revises: 0192_rate_card_qwen4_max
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from alembic import op

revision: str = "0193_rate_card_claude5"
down_revision: str | Sequence[str] | None = "0192_rate_card_qwen4_max"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: 元 / 美元。中国外汇交易中心受权公布的人民币汇率中间价,2026-11-17:1 美元对人民币
#: 6.7132 元(用户 2026-11-19 拍板,固定写死)。之后价格变动在控制台价目表里按型号改,
#: 不改这里。
_CNY_PER_USD = Decimal("6.7132")

#: ``(model, input, output, cache creation, cache read)`` —— 美元 / 百万 token;来源见模块说明。
_USD_PRICES = (
    ("claude-fable-5-2", "8", "40", "10", "0.20"),
    ("claude-opus-5-5", "4", "20", "5", "0.20"),
    ("claude-sonnet-5-5", "2", "10", "2.50", "0.10"),
    ("claude-haiku-5-5", "0.10", "0.50", "0.125", "0.01"),
)


def _micro_cny(usd: str) -> int:
    value = Decimal(usd) * _CNY_PER_USD * 1_000_000
    if value != value.to_integral_value():
        msg = f"{usd} USD x {_CNY_PER_USD} is not a whole number of micro-元"
        raise ValueError(msg)
    return int(value)


#: ``(provider, model, input, output, cache creation, cache read)`` — micro-元 per
#: million tokens.
_PRICES = tuple(
    ("anthropic", model, *(_micro_cny(usd) for usd in prices)) for model, *prices in _USD_PRICES
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
