/**
 * How a knowledge search ran — B-175 P2-PR4 (B-206 §6 降级标记). Reads the
 * search outcome's flags: the retrieval test's response fields, or a
 * retrieval record's ``flags``. Only the flags passed in are shown, so the
 * retrieval test (which already says how the rerank ended) passes just the
 * degradation ones.
 */
import { Alert, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { rerankFallbackOf, rerankNoticeTone } from "../../api/rerank_outcome";

const { Text } = Typography;

function count(value: unknown): number {
  return typeof value === "number" && value > 0 ? value : 0;
}

export function SearchNotices({
  flags,
  prefix,
}: {
  flags: Record<string, unknown>;
  /** Testid prefix: ``${prefix}-embedding-degraded`` … */
  prefix: string;
}) {
  const { t } = useTranslation();
  const fallback = rerankFallbackOf(flags.rerank_outcome);
  const unsearched = Array.isArray(flags.unsearched_bases)
    ? flags.unsearched_bases.filter((b): b is string => typeof b === "string")
    : [];
  const error = typeof flags.error === "string" && flags.error ? flags.error : null;
  const duplicates = count(flags.duplicates_dropped);
  const unscored = count(flags.rerank_dropped);

  const alerts: { key: string; type: "info" | "warning" | "error"; message: string }[] = [];
  if (error !== null) {
    alerts.push({ key: "error", type: "error", message: t("knowledge_retrieval.notice_error", { error }) });
  }
  if (fallback !== null) {
    alerts.push({
      key: "rerank-outcome",
      type: rerankNoticeTone(fallback),
      message: t(`knowledge_page.rerank_outcome_${fallback}`),
    });
  }
  if (flags.rerank_threshold_paused === true) {
    alerts.push({
      key: "threshold-paused",
      type: "warning",
      message: t("knowledge_retrieval.notice_threshold_paused"),
    });
  }
  if (flags.embedding_degraded === true) {
    alerts.push({
      key: "embedding-degraded",
      type: "warning",
      message: t("knowledge_retrieval.notice_embedding_degraded"),
    });
  }
  if (flags.keyword_degraded === true) {
    alerts.push({
      key: "keyword-degraded",
      type: "warning",
      message: t("knowledge_retrieval.notice_keyword_degraded"),
    });
  }
  if (unsearched.length > 0) {
    alerts.push({
      key: "unsearched",
      type: "warning",
      message: t("knowledge_retrieval.notice_unsearched", { bases: unsearched.join(", ") }),
    });
  }
  if (alerts.length === 0 && duplicates === 0 && unscored === 0) return null;

  return (
    <Space direction="vertical" size={8} style={{ width: "100%", marginBottom: 12 }}>
      {alerts.map((alert) => (
        <Alert
          key={alert.key}
          type={alert.type}
          showIcon
          message={alert.message}
          data-testid={`${prefix}-${alert.key}`}
        />
      ))}
      {duplicates > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid={`${prefix}-duplicates-dropped`}>
          {t("knowledge_retrieval.notice_duplicates_dropped", { count: duplicates })}
        </Text>
      )}
      {unscored > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid={`${prefix}-rerank-dropped`}>
          {t("knowledge_retrieval.notice_rerank_dropped", { count: unscored })}
        </Text>
      )}
    </Space>
  );
}
