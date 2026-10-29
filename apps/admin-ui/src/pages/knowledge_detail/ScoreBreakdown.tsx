/**
 * A hit's four scores — B-175 P2-PR4 (B-206 §6). Vector / keyword / fused /
 * rerank, each the best hit chunk's (a passage scores as its best chunk);
 * "—" for a path that did not find it, or a rerank that gave no score.
 */
import { Space, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

const { Text } = Typography;

export interface HitScores {
  vector_score?: number | null;
  keyword_score?: number | null;
  fused_score?: number | null;
  rerank_score?: number | null;
}

const SCORES = [
  ["vector_score", "score_vector"],
  ["keyword_score", "score_keyword"],
  ["fused_score", "score_fused"],
  ["rerank_score", "score_rerank"],
] as const;

export function formatScore(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(4) : "—";
}

export function ScoreBreakdown({ scores, testId }: { scores: HitScores; testId: string }) {
  const { t } = useTranslation();
  return (
    <Space size={12} wrap data-testid={testId}>
      {SCORES.map(([key, label]) => (
        <Tooltip key={key} title={t(`knowledge_retrieval.${label}_hint`)}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t(`knowledge_retrieval.${label}`)}{" "}
            <Text style={{ fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
              {formatScore(scores[key])}
            </Text>
          </Text>
        </Tooltip>
      ))}
    </Space>
  );
}
