/**
 * Lineage panel — Stream SE (SE-8-5).
 *
 * Fork + distill lineage for a skill: an SVG fork edge (source → this skill)
 * when forked, plus a version timeline tagging each version's evolution origin
 * (human / in_session / distilled). The lineage for a single skill is small
 * (one fork source + its versions), so a hand-rolled SVG beats pulling in a
 * graph-layout lib — on-brand + lean per the Stream H baseline.
 */
import { useCallback, useEffect, useState } from "react";
import { Card, Skeleton, Space, Tag, Typography } from "antd";
import { GitFork } from "lucide-react";
import { useTranslation } from "react-i18next";

import { getLineage, type SkillLineage } from "../../api/skill-evolution";
import type { EvolutionOrigin } from "../../api/skills";

const { Text } = Typography;

const ORIGIN_COLOR: Record<string, string> = {
  in_session: "blue",
  distilled: "purple",
  human: "default",
};

interface LineagePanelProps {
  skillId: string;
  /** Cross-tenant W4(D2)— 权威读口径:URL ``?tenant_id=`` 原样透传优先;
   *  无 URL 参数时取 ambient scope("*" 折叠成 undefined),由 SkillDetail
   *  统一下传。 */
  readScope: string | undefined;
}

export function LineagePanel({ skillId, readScope }: LineagePanelProps) {
  const { t } = useTranslation();
  const [lineage, setLineage] = useState<SkillLineage | null>(null);
  const [failed, setFailed] = useState(false);

  const originLabel = (origin: EvolutionOrigin | null | undefined): string =>
    origin === "in_session"
      ? t("skill_evolution.origin_in_session")
      : origin === "distilled"
        ? t("skill_evolution.origin_distilled")
        : t("skill_evolution.origin_human");

  const load = useCallback(async () => {
    try {
      setLineage(await getLineage(skillId, readScope));
    } catch {
      setFailed(true);
    }
  }, [skillId, readScope]);

  useEffect(() => {
    void load();
  }, [load]);

  if (failed) return null;

  // Defensive: tolerate a malformed / partial payload (e.g. an empty stub)
  // so a bad response degrades to "no lineage" instead of crashing the page.
  const source = lineage?.forked_from_source ?? null;
  const versions = lineage?.versions ?? [];
  const skillName = lineage?.skill?.name ?? "";

  return (
    <Card
      size="small"
      title={t("skill_evolution.lineage_title")}
      style={{ marginBottom: 16 }}
      data-testid="skill-lineage-panel"
    >
      {lineage === null ? (
        <Skeleton active paragraph={{ rows: 1 }} />
      ) : (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {source !== null && (
            <div data-testid="skill-lineage-fork">
              <svg width={320} height={48} role="img" aria-label={t("skill_evolution.lineage_fork_aria")}>
                <rect x={1} y={10} width={130} height={28} rx={4} fill="none" stroke="var(--ew-border, #444)" />
                <text x={66} y={28} textAnchor="middle" fontSize={11} fill="var(--ew-text-secondary, #aaa)">
                  {source.name.slice(0, 16)}
                </text>
                <line x1={131} y1={24} x2={188} y2={24} stroke="var(--ew-color-brand-500, #06b6d4)" strokeWidth={1.5} markerEnd="url(#arrow)" />
                <defs>
                  <marker id="arrow" markerWidth={8} markerHeight={8} refX={6} refY={3} orient="auto">
                    <path d="M0,0 L6,3 L0,6 Z" fill="var(--ew-color-brand-500, #06b6d4)" />
                  </marker>
                </defs>
                <rect x={189} y={10} width={130} height={28} rx={4} fill="none" stroke="var(--ew-color-brand-500, #06b6d4)" />
                <text x={254} y={28} textAnchor="middle" fontSize={11} fill="var(--ew-text-primary, #eee)">
                  {skillName.slice(0, 16)}
                </text>
              </svg>
              <Text type="secondary" style={{ fontSize: 11 }}>
                <GitFork size={11} strokeWidth={1.75} style={{ verticalAlign: "-1px", marginRight: 4 }} />
                {t("skill_evolution.forked_from")}: {source.name}
              </Text>
            </div>
          )}

          <div data-testid="skill-lineage-versions">
            <Text type="secondary" style={{ fontSize: 11 }}>
              {t("skill_evolution.lineage_versions")}
            </Text>
            <Space direction="vertical" size={4} style={{ width: "100%", marginTop: 6 }}>
              {versions.map((v) => {
                const origin = (v.evolution_origin ?? "human") as string;
                return (
                  <Space key={v.id} size={8}>
                    <Text code style={{ fontSize: 12 }}>
                      v{v.version}
                    </Text>
                    <Tag color={ORIGIN_COLOR[origin]} bordered={false}>
                      {originLabel(v.evolution_origin)}
                    </Tag>
                    {v.distilled_from_trajectory_key != null && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        ← {v.distilled_from_trajectory_key.slice(0, 28)}
                      </Text>
                    )}
                  </Space>
                );
              })}
            </Space>
          </div>
        </Space>
      )}
    </Card>
  );
}
