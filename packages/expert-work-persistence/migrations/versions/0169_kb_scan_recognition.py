"""0169 — 知识库扫描页识别(B-209 PR-1):逐页记录、识别缓存、平台「文档识别」配置、价目表。

* ``knowledge_document.page_records`` / ``page_stats``(JSONB,可空):PDF 每页怎么拿到的
  文字(文字层 / 识别 / 空白)、识别模型与成败,以及「已识别 N 页 · 失败 M 页 · 待识别 K 页」
  计数。设计 §5.6 允许「新表或文档上的 JSON 列,按数据量定」:单份上限 1,000 页、每页一条
  百来字节,放文档行上;读列表只取 ``page_stats``,逐页明细单独读。
* ``knowledge_parse_cache``:识别结果缓存,**按文档**(复核 M1:识别文字可能是健康资料,
  文档删了它必须跟着删)。``document_id`` 外键指向 ``knowledge_document``、``ON DELETE
  CASCADE``(删文档、删库都带走;``delete_document`` / ``delete_base`` 另有显式 DELETE,
  外键是兜底,同 0136)。键 = 送模型的图片字节 sha256 + 模型 + 请求参数 + 渲染参数;同一份
  文档重传 / 补识别 / 重新切分都先查它,不重复付费;同一个文件传成另一份文档会再付一次。
  存厂商原始回答(读出时按当前规则清洗)。带 ``tenant_id`` + 与 0165 同形的 ``app.tenant_id``
  策略(ENABLE + FORCE)。
* ``platform_recognition_config``:平台级单行配置(``id = 'singleton'``,无租户、无 RLS,
  同 ``platform_judge_config``):扫描页识别模型、兜底模型、图片描述模型、单份文档识别页数
  上限。没有行 = 用默认(glm-ocr / qwen3.7-flash / qwen3.8-flash / 1,000)。
* ``model_rate_card``:补 ``qwen / qwen3.7-flash``(¥0.2 / ¥0.8 每百万 token)与
  ``qwen / qwen3.8-flash``(¥0.8 / ¥2.7),价格取自 B-209 设计 §4.1(2026-10-21 阿里价目表);
  ``glm / glm-ocr``(输入输出同价 ¥0.2 每百万 token,智谱 GLM-OCR 官方文档,2026-10-22 读)与
  ``qwen / text-embedding-v4``(¥0.0005 每千 token = ¥0.5 每百万,输出不计费,阿里云百炼
  「文本向量」文档价目表,2026-10-22 读)。没价目行的用量记成「未定价」(B-104 的教训)。
  已经有同名行就不动(人工定的价优先)。

expand-only:新列可空、新表旧镜像不读;回滚不需要 downgrade。downgrade 不删价目行(与人工
补的分不出来,删错了用量变成未定价)。

Revision ID: 0169_kb_scan_recognition
Revises: 0168_kb_rerank_score_threshold
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0169_kb_scan_recognition"
down_revision: str | Sequence[str] | None = "0168_kb_rerank_score_threshold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_CACHE = "knowledge_parse_cache"
_CONFIG = "platform_recognition_config"

#: ``(provider, model, input micros/百万 token, output micros/百万 token)`` — sources in
#: the module docstring.
_PRICES = (
    ("qwen", "qwen3.7-flash", 200_000, 800_000),
    ("qwen", "qwen3.8-flash", 800_000, 2_700_000),
    ("glm", "glm-ocr", 200_000, 200_000),
    ("qwen", "text-embedding-v4", 500_000, 0),
)


def upgrade() -> None:
    op.add_column("knowledge_document", sa.Column("page_records", JSONB(), nullable=True))
    op.add_column("knowledge_document", sa.Column("page_stats", JSONB(), nullable=True))

    op.create_table(
        _CACHE,
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "knowledge_document.id",
                ondelete="CASCADE",
                name="fk_knowledge_parse_cache_document",
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "kind", "cache_key", name="knowledge_parse_cache_pkey"
        ),
        sa.CheckConstraint(
            "kind IN ('page_ocr', 'figure_desc', 'figure_ocr')",
            name="knowledge_parse_cache_kind_enum",
        ),
    )
    op.execute(f"ALTER TABLE {_CACHE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_CACHE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_CACHE}_tenant_isolation ON {_CACHE}
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        """
    )

    op.create_table(
        _CONFIG,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("ocr_provider", sa.Text(), nullable=True),
        sa.Column("ocr_model", sa.Text(), nullable=True),
        sa.Column("fallback_provider", sa.Text(), nullable=True),
        sa.Column("fallback_model", sa.Text(), nullable=True),
        sa.Column("figure_provider", sa.Text(), nullable=True),
        sa.Column("figure_model", sa.Text(), nullable=True),
        sa.Column("max_pages", sa.Integer(), nullable=False, server_default=sa.text("1000")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.CheckConstraint("max_pages >= 1", name="platform_recognition_config_max_pages_check"),
    )

    # Module constants only — nothing user-supplied reaches this SQL.
    for provider, model, input_price, output_price in _PRICES:
        op.execute(
            f"""
            INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                         output_per_mtok_micros, cache_creation_per_mtok_micros,
                                         cache_read_per_mtok_micros)
            SELECT NULL, '{provider}', '{model}', {input_price}, {output_price}, 0, 0
             WHERE NOT EXISTS (
                 SELECT 1 FROM model_rate_card
                  WHERE tenant_id IS NULL AND provider = '{provider}' AND model = '{model}'
             )
            """  # noqa: S608
        )


def downgrade() -> None:
    op.drop_table(_CONFIG)
    op.execute(f"DROP POLICY IF EXISTS {_CACHE}_tenant_isolation ON {_CACHE};")
    op.drop_table(_CACHE)
    op.drop_column("knowledge_document", "page_stats")
    op.drop_column("knowledge_document", "page_records")
