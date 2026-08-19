/**
 * TrajectoryToolbar — the trajectory panel's sticky toolbar: timeline mode
 * (sequence/duration), fold-all for turns and tool calls, and a live ledger
 * search box with a match counter. Pure display component — all state lives
 * in the parent controller (Task 10).
 *
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。
 *
 * See .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-5-brief.md.
 */
import type { JSX } from "react";
import { Clock, Search } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { TimelineMode } from "./ledger_timeline";
import "./trajectory_toolbar.css";

export interface TrajectoryToolbarProps {
  mode: TimelineMode;
  onModeChange: (mode: TimelineMode) => void;
  /** 时长投影退化(ledger.timed === false):按钮仍可切,旁边标「时长不可用」。 */
  degraded: boolean;
  allTurnsCollapsed: boolean;
  onToggleAllTurns: () => void;
  turnsCollapsible: boolean;
  allCallsCollapsed: boolean;
  onToggleAllCalls: () => void;
  callsCollapsible: boolean;
  query: string;
  onQueryChange: (query: string) => void;
  /** null = 无查询;否则匹配数。 */
  matchCount: number | null;
}

export function TrajectoryToolbar({
  mode,
  onModeChange,
  degraded,
  allTurnsCollapsed,
  onToggleAllTurns,
  turnsCollapsible,
  allCallsCollapsed,
  onToggleAllCalls,
  callsCollapsible,
  query,
  onQueryChange,
  matchCount,
}: TrajectoryToolbarProps): JSX.Element {
  const { t } = useTranslation();
  const isDuration = mode === "duration";

  return (
    <div role="toolbar" aria-label={t("console.toolbar_aria")} data-testid="console-traj-toolbar" className="ew-tbar">
      <div className="ew-tbar__actions">
        <button
          type="button"
          className="ew-tbar__toggle"
          data-testid="console-lane-mode"
          aria-pressed={isDuration}
          title={isDuration ? t("console.toolbar_duration_title_on") : t("console.toolbar_duration_title_off")}
          onClick={() => onModeChange(isDuration ? "sequence" : "duration")}
        >
          <Clock size={12} className="ew-tbar__toggle-icon" aria-hidden="true" />
          {t("console.toolbar_duration")}
        </button>
        {degraded && (
          <span className="ew-tbar__degraded" data-testid="console-traj-degraded">
            {t("console.toolbar_degraded")}
          </span>
        )}
        <button
          type="button"
          className="ew-tbar__action"
          data-testid="console-traj-collapse-turns"
          aria-pressed={allTurnsCollapsed}
          aria-label={allTurnsCollapsed ? t("console.toolbar_expand_turns") : t("console.toolbar_collapse_turns")}
          title={allTurnsCollapsed ? t("console.toolbar_expand_turns") : t("console.toolbar_collapse_turns")}
          disabled={!turnsCollapsible}
          onClick={onToggleAllTurns}
        >
          <span className="ew-tbar__icon" aria-hidden="true">
            {allTurnsCollapsed ? "⊞" : "⊟"}
          </span>
          {t("console.toolbar_turns")}
        </button>
        <button
          type="button"
          className="ew-tbar__action"
          data-testid="console-traj-collapse-calls"
          aria-pressed={allCallsCollapsed}
          aria-label={allCallsCollapsed ? t("console.toolbar_expand_calls") : t("console.toolbar_collapse_calls")}
          title={allCallsCollapsed ? t("console.toolbar_expand_calls") : t("console.toolbar_collapse_calls")}
          disabled={!callsCollapsible}
          onClick={onToggleAllCalls}
        >
          <span className="ew-tbar__icon" aria-hidden="true">
            {allCallsCollapsed ? "⊞" : "⊟"}
          </span>
          {t("console.toolbar_calls")}
        </button>
      </div>
      <div className="ew-tbar__search">
        <Search size={11} className="ew-tbar__search-icon" aria-hidden="true" />
        <input
          type="search"
          className="ew-tbar__search-input"
          data-testid="console-traj-search"
          aria-label={t("console.toolbar_search")}
          placeholder={t("console.toolbar_search")}
          value={query}
          onChange={(event) => onQueryChange(event.currentTarget.value)}
        />
        {matchCount !== null && (
          <span className="ew-tbar__search-count" data-testid="console-traj-search-count">
            {t("console.toolbar_search_count", { n: matchCount })}
          </span>
        )}
      </div>
    </div>
  );
}
