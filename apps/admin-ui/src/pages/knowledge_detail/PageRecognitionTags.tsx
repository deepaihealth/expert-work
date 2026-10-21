/**
 * B-209 §5.6 — a PDF's scanned-page counts next to its status:
 * 「已识别 N 页 · 识别失败 M 页 · 待识别 K 页」(+ over the page limit). Nothing
 * for a document without scanned pages — and never a silent gap for one with
 * pages that did not make it into the knowledge base.
 */
import type { ReactElement } from "react";
import { Space, Tag, Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { PageStats } from "../../api/knowledge";

/** Scanned pages that failed or wait — what 「补识别」 retries. */
export function needsRecognition(stats: PageStats | null | undefined): boolean {
  return stats != null && stats.pages_failed + stats.pages_pending > 0;
}

export interface PageRecognitionTagsProps {
  stats: PageStats | null | undefined;
  documentId: string;
}

export function PageRecognitionTags({
  stats,
  documentId,
}: PageRecognitionTagsProps): ReactElement | null {
  const { t } = useTranslation();
  if (stats == null) return null;
  const counts: { key: keyof PageStats; label: string; color: string }[] = [
    { key: "pages_ocr", label: "knowledge_page.pages_recognized", color: "blue" },
    { key: "pages_failed", label: "knowledge_page.pages_failed", color: "red" },
    { key: "pages_pending", label: "knowledge_page.pages_pending", color: "orange" },
    { key: "pages_over_limit", label: "knowledge_page.pages_over_limit", color: "default" },
  ];
  const shown = counts.filter(({ key }) => stats[key] > 0);
  if (shown.length === 0) return null;
  const tags = (
    <Space size={4} wrap data-testid={`doc-pages-${documentId}`}>
      {shown.map(({ key, label, color }) => (
        <Tag key={key} color={color} bordered={false}>
          {t(label, { count: stats[key] })}
        </Tag>
      ))}
    </Space>
  );
  return needsRecognition(stats) ? (
    <Tooltip title={t("knowledge_page.pages_hint")}>{tags}</Tooltip>
  ) : (
    tags
  );
}
