/**
 * B-209 PR-2 §5.6 — a document's picture counts next to its status:
 * 「已描述 N 张图 · 描述失败 M 张 · 待描述 K 张」, with every count (filtered,
 * over the limit, unreadable) in the tooltip. Nothing when the document has
 * no pictures, or was ingested before pictures were described.
 */
import type { ReactElement } from "react";
import { Space, Tag, Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { FigureStats } from "../../api/knowledge";

/** Pictures that failed or wait for a description model — what 「补识别」
 *  retries (the ones already described come from the cache). */
export function figuresNeedDescription(stats: FigureStats | null | undefined): boolean {
  return stats != null && stats.failed + stats.pending > 0;
}

export interface FigureStatsTagsProps {
  stats: FigureStats | null | undefined;
  documentId: string;
}

export function FigureStatsTags({ stats, documentId }: FigureStatsTagsProps): ReactElement | null {
  const { t } = useTranslation();
  if (stats == null || stats.extracted === 0) return null;
  const counts: { key: "described" | "failed" | "pending"; label: string; color: string }[] = [
    { key: "described", label: "knowledge_page.figures_described", color: "green" },
    { key: "failed", label: "knowledge_page.figures_failed", color: "red" },
    { key: "pending", label: "knowledge_page.figures_pending", color: "orange" },
  ];
  const shown = counts.filter(({ key }) => stats[key] > 0);
  if (shown.length === 0) return null;
  return (
    <Tooltip title={t("knowledge_page.figures_hint", { ...stats })}>
      <Space size={4} wrap data-testid={`doc-figures-${documentId}`}>
        {shown.map(({ key, label, color }) => (
          <Tag key={key} color={color} bordered={false}>
            {t(label, { count: stats[key] })}
          </Tag>
        ))}
      </Space>
    </Tooltip>
  );
}
