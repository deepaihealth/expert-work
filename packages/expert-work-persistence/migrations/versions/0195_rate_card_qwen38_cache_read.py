"""0195 — 价目表 ``qwen / qwen3.8-max`` 的输入缓存命中价 2.4 改成厂商标价 1.5。

这一行不是迁移种的,是早先在控制台手工填的,缓存命中价写成了 2.4(元 / 百万 token)。
2026-11-20 用户截了通义的模型页:qwen3.8-max 输入 12、输出 36、输入缓存命中 1.5
(显式缓存创建 15、显式缓存命中 1 —— 平台对通义不发显式缓存请求,同 0187 / 0192 不入表)。
0187 种的带日期名 ``qwen3.8-max-0902`` 已经是 1.5;两行同一个模型,现在对齐。

命中缓存的 token 一直按 2.4 计,每百万多算 0.9 元。测试和生产都随这次发布改,不留手工步骤。

只改「缓存命中价还是 2.4、输入价还是 12」的那一行:别的值说明有人在控制台按别的口径
改过,不覆盖;没有这一行就什么都不做(不插)。

downgrade 把「输入 12、缓存命中 1.5」的这一行改回 2.4 —— 恢复升级前的状态(即使 2.4 是错
价)。之后人工改成别的值的行不动。升级前就已经被人工改成 12 / 1.5 的行与本迁移改的分不出来,
downgrade 也会把它改回 2.4。

Revision ID: 0195_rate_card_qwen38_cache
Revises: 0194_b326_artifact_share_links
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0195_rate_card_qwen38_cache"
down_revision: str | Sequence[str] | None = "0194_b326_artifact_share_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: micro-元 per million tokens; source in the module docstring.
_INPUT = 12_000_000
_WRONG_CACHE_READ = 2_400_000
_VENDOR_CACHE_READ = 1_500_000


def _set_cache_read(old: int, new: int) -> None:
    # The values are module constants, not user input.
    op.execute(
        f"""
        UPDATE model_rate_card
           SET cache_read_per_mtok_micros = {new}, updated_at = now()
         WHERE tenant_id IS NULL AND provider = 'qwen' AND model = 'qwen3.8-max'
           AND input_per_mtok_micros = {_INPUT}
           AND cache_read_per_mtok_micros = {old}
        """  # noqa: S608
    )


def upgrade() -> None:
    _set_cache_read(_WRONG_CACHE_READ, _VENDOR_CACHE_READ)


def downgrade() -> None:
    _set_cache_read(_VENDOR_CACHE_READ, _WRONG_CACHE_READ)
