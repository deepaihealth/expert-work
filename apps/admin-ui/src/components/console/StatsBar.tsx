/**
 * StatsBar — the debug console's session-level status bar
 * (调试台重设计 PR-A Task 12, revised 调试台 PR-A.1 反馈修订 Task 2 /
 * spec §八.2). Pure presentational: lays out an already-computed
 * ``SessionStats`` (Task 5, ``src/api/session_stats.ts``) as a wrapping
 * row of small label/value chips that never truncates (no
 * ``overflow`` / ``ellipsis`` / ``title``); the caller (Task 19)
 * recomputes ``SessionStats`` from live + history turns and passes it
 * down.
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { SessionStats } from "../../api/session_stats";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { formatCompact } from "../../utils/runFormat";
import "./console.css";

export interface StatsBarProps {
  stats: SessionStats;
  isSystemAdmin: boolean;
}

export function StatsBar({ stats, isSystemAdmin }: StatsBarProps): JSX.Element | null {
  const { t } = useTranslation();

  if (stats.turns === 0) return null;

  const chips: { name: string; label: string; value: string }[] = [
    { name: "turns", label: t("console.stats_chip_turns"), value: String(stats.turns) },
    { name: "steps", label: t("console.stats_chip_steps"), value: String(stats.steps) },
    { name: "llm", label: t("console.stats_chip_llm"), value: fmtDuration(stats.llmMs) },
    { name: "tools", label: t("console.stats_chip_tools"), value: fmtDuration(stats.toolMs) },
  ];
  if (stats.ttftAvgMs !== null) {
    chips.push({
      name: "ttft",
      label: t("console.stats_chip_ttft"),
      value: fmtDuration(stats.ttftAvgMs),
    });
  }
  if (stats.tokPerSec !== null) {
    chips.push({
      name: "tps",
      label: t("console.stats_chip_tps"),
      value: `≈ ${stats.tokPerSec} tok/s`,
    });
  }
  if (stats.cacheHitPct !== null) {
    chips.push({
      name: "cache",
      label: t("console.stats_chip_cache"),
      value: `${stats.cacheHitPct}%`,
    });
  }
  chips.push({
    name: "in",
    label: t("console.stats_chip_in"),
    value: formatCompact(stats.inputTokens),
  });
  chips.push({
    name: "out",
    label: t("console.stats_chip_out"),
    value: formatCompact(stats.outputTokens),
  });
  if (isSystemAdmin && stats.costCny !== null) {
    chips.push({
      name: "cost",
      label: t("console.stats_chip_cost"),
      value: `¥${stats.costCny.toFixed(2)}`,
    });
  }

  return (
    <div
      data-testid="console-stats-bar"
      style={{ display: "flex", flexWrap: "wrap", gap: "4px 6px" }}
    >
      {chips.map((c) => (
        <span key={c.name} className="ew-stat-chip" data-testid={`console-stat-${c.name}`}>
          <span className="ew-stat-chip__k">{c.label}</span>
          <span className="ew-stat-chip__v">{c.value}</span>
        </span>
      ))}
      {stats.partial && (
        <span className="ew-stat-chip" data-testid="console-stat-partial">
          {t("console.stats_partial")}
        </span>
      )}
    </div>
  );
}
