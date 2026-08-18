/**
 * StatsBar — the debug console's one-line session-level status bar
 * (调试台重设计 PR-A Task 12, spec §二.1 状态栏表). Pure presentational:
 * lays out an already-computed ``SessionStats`` (Task 5,
 * ``src/api/session_stats.ts``) as a ``|``-joined line with a full-text
 * tooltip; the caller (Task 19) recomputes ``SessionStats`` from live +
 * history turns and passes it down.
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { SessionStats } from "../../api/session_stats";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { formatCompact } from "../../utils/runFormat";

export interface StatsBarProps {
  stats: SessionStats;
  isSystemAdmin: boolean;
}

export function StatsBar({ stats, isSystemAdmin }: StatsBarProps): JSX.Element | null {
  const { t } = useTranslation();

  if (stats.turns === 0) return null;

  const items: { name: string; text: string }[] = [
    { name: "turns", text: t("console.stats_turns", { turns: stats.turns, steps: stats.steps }) },
    {
      name: "durations",
      text: t("console.stats_llm_tools", {
        llm: fmtDuration(stats.llmMs),
        tools: fmtDuration(stats.toolMs),
      }),
    },
  ];
  if (stats.ttftAvgMs !== null) {
    items.push({
      name: "ttft",
      text: t("console.stats_ttft", { v: fmtDuration(stats.ttftAvgMs) }),
    });
  }
  if (stats.tokPerSec !== null) {
    items.push({ name: "tps", text: t("console.stats_tps", { v: stats.tokPerSec }) });
  }
  if (stats.cacheHitPct !== null) {
    items.push({ name: "cache", text: t("console.stats_cache", { v: stats.cacheHitPct }) });
  }
  items.push({
    name: "tokens",
    text: t("console.stats_tokens", {
      in: formatCompact(stats.inputTokens),
      out: formatCompact(stats.outputTokens),
    }),
  });
  if (isSystemAdmin && stats.costCny !== null) {
    items.push({
      name: "cost",
      text: t("console.stats_cost", { v: stats.costCny.toFixed(2) }),
    });
  }

  const fullTextParts = items.map((item) => item.text);
  if (stats.partial) fullTextParts.push(t("console.stats_partial"));
  const fullText = fullTextParts.join(" | ");

  return (
    <div
      data-testid="console-stats-bar"
      title={fullText}
      style={{
        display: "flex",
        gap: 8,
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {items.map((item, idx) => (
        <span key={item.name} style={{ display: "contents" }}>
          {idx > 0 && <span aria-hidden="true">|</span>}
          <span data-testid={`console-stat-${item.name}`}>{item.text}</span>
        </span>
      ))}
      {stats.partial && (
        <>
          <span aria-hidden="true">|</span>
          <span data-testid="console-stat-partial">{t("console.stats_partial")}</span>
        </>
      )}
    </div>
  );
}
