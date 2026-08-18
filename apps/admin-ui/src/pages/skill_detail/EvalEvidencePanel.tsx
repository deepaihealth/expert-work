/**
 * Eval evidence panel — Stream SE (SE-8-5).
 *
 * Replay-verification evidence for a skill: per ``skill_eval_result`` a
 * hand-rolled SVG paired bar (baseline vs with-skill score) + the delta and
 * verdict. Hand SVG (not a chart lib) keeps the bundle lean and on-brand per
 * the Stream H baseline — each result is just two values, a chart lib is
 * overkill. Newest first (the backend sorts).
 */
import { useCallback, useEffect, useState } from "react";
import { Card, Empty, Skeleton, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  listEvalResults,
  type EvalVerdict,
  type SkillEvalResult,
} from "../../api/skill-evolution";

const { Text } = Typography;

const BAR_W = 200;
const BAR_H = 12;

const VERDICT_COLOR: Record<EvalVerdict, string> = {
  pass: "success",
  fail: "error",
  inconclusive: "gold",
};

function clamp01(v: number): number {
  if (Number.isNaN(v)) return 0;
  return Math.max(0, Math.min(1, v));
}

interface EvalEvidencePanelProps {
  skillId: string;
  /** Cross-tenant W4(D2)— 权威读口径:URL ``?tenant_id=`` 原样透传优先;
   *  无 URL 参数时取 ambient scope("*" 折叠成 undefined),由 SkillDetail
   *  统一下传。 */
  readScope: string | undefined;
}

export function EvalEvidencePanel({ skillId, readScope }: EvalEvidencePanelProps) {
  const { t } = useTranslation();
  const [results, setResults] = useState<SkillEvalResult[] | null>(null);

  const verdictLabel = (v: EvalVerdict): string =>
    v === "pass"
      ? t("skill_evolution.verdict_pass")
      : v === "fail"
        ? t("skill_evolution.verdict_fail")
        : t("skill_evolution.verdict_inconclusive");

  const load = useCallback(async () => {
    try {
      setResults(await listEvalResults(skillId, readScope));
    } catch {
      setResults([]);
    }
  }, [skillId, readScope]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Card
      size="small"
      title={t("skill_evolution.eval_title")}
      style={{ marginBottom: 16 }}
      data-testid="skill-eval-panel"
    >
      {results === null ? (
        <Skeleton active paragraph={{ rows: 2 }} />
      ) : results.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("skill_evolution.eval_empty")}
          data-testid="skill-eval-empty"
        />
      ) : (
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          {results.map((r) => {
            const base = clamp01(r.baseline_score);
            const skill = clamp01(r.skill_score);
            const deltaPositive = r.delta >= 0;
            return (
              <div key={r.id} data-testid={`skill-eval-row-${r.id}`}>
                <Space size={8} style={{ marginBottom: 4 }} wrap>
                  <Text strong style={{ fontSize: 12 }}>
                    v{r.skill_version}
                  </Text>
                  <Tag color={VERDICT_COLOR[r.verdict]}>{verdictLabel(r.verdict)}</Tag>
                  <Text
                    style={{
                      fontSize: 12,
                      color: deltaPositive
                        ? "var(--ew-color-accent-500)"
                        : "var(--ew-status-danger-fg)",
                    }}
                  >
                    {deltaPositive ? "+" : ""}
                    {r.delta.toFixed(2)} Δ
                  </Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {t("skill_evolution.eval_n_cases", { n: r.n_cases })} · {r.replay_source}
                  </Text>
                </Space>
                {/* Paired bars: baseline (neutral) over skill (brand cyan). */}
                <svg
                  width={BAR_W}
                  height={BAR_H * 2 + 10}
                  role="img"
                  aria-label={t("skill_evolution.eval_aria", {
                    baseline: base.toFixed(2),
                    skill: skill.toFixed(2),
                  })}
                >
                  <rect x={0} y={0} width={BAR_W} height={BAR_H} rx={2} fill="var(--ew-fill-subtle, #2a2a2a)" />
                  <rect
                    x={0}
                    y={0}
                    width={Math.round(base * BAR_W)}
                    height={BAR_H}
                    rx={2}
                    fill="var(--ew-text-tertiary, #888)"
                  />
                  <rect
                    x={0}
                    y={BAR_H + 6}
                    width={BAR_W}
                    height={BAR_H}
                    rx={2}
                    fill="var(--ew-fill-subtle, #2a2a2a)"
                  />
                  <rect
                    x={0}
                    y={BAR_H + 6}
                    width={Math.round(skill * BAR_W)}
                    height={BAR_H}
                    rx={2}
                    fill="var(--ew-color-brand-500, #2C56C4)"
                  />
                </svg>
                <div style={{ display: "flex", gap: 12 }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {t("skill_evolution.eval_baseline")}: {base.toFixed(2)}
                  </Text>
                  <Text style={{ fontSize: 11, color: "var(--ew-color-brand-500)" }}>
                    {t("skill_evolution.eval_with_skill")}: {skill.toFixed(2)}
                  </Text>
                </div>
              </div>
            );
          })}
        </Space>
      )}
    </Card>
  );
}
