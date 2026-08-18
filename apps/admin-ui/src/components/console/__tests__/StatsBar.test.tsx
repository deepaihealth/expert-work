/**
 * StatsBar — the debug console's session-level status bar (调试台重设计
 * PR-A Task 12). Pure presentational: renders a ``SessionStats`` (Task 5,
 * ``src/api/session_stats.ts``) as one ``|``-joined line, using the same
 * ``fmtDuration`` / ``formatCompact`` formatters the rest of the console
 * already standardizes on.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import i18n from "../../../i18n";

import { StatsBar } from "../StatsBar";
import type { SessionStats } from "../../../api/session_stats";
import { fmtDuration } from "../../../pages/agent_detail/playground/duration_format";
import { formatCompact } from "../../../utils/runFormat";

function stats(overrides: Partial<SessionStats> = {}): SessionStats {
  return {
    turns: 2,
    steps: 17,
    llmMs: 85000,
    toolMs: 4600,
    ttftAvgMs: 823,
    tokPerSec: 144,
    cacheHitPct: 94,
    inputTokens: 641000,
    outputTokens: 3000,
    costCny: 0.12,
    partial: false,
    ...overrides,
  };
}

describe("StatsBar", () => {
  // Locale-sensitive assertions below — pin zh-CN explicitly and restore
  // afterward so it doesn't leak into other test files (the i18n
  // singleton persists its resolved language across `it` blocks / files
  // in the same worker).
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders every item (admin) with the spec order and a tooltip title", () => {
    const s = stats();
    render(<StatsBar stats={s} isSystemAdmin />);

    const bar = screen.getByTestId("console-stats-bar");
    expect(screen.getByTestId("console-stat-turns")).toHaveTextContent(
      `${s.turns} 轮 · ${s.steps} 步`,
    );
    expect(screen.getByTestId("console-stat-durations")).toHaveTextContent(
      `LLM ${fmtDuration(s.llmMs)} · 工具 ${fmtDuration(s.toolMs)}`,
    );
    expect(screen.getByTestId("console-stat-ttft")).toHaveTextContent(
      `首 token ${fmtDuration(s.ttftAvgMs as number)}`,
    );
    expect(screen.getByTestId("console-stat-tps")).toHaveTextContent(
      `≈ ${s.tokPerSec} tok/s`,
    );
    expect(screen.getByTestId("console-stat-cache")).toHaveTextContent(
      `缓存 ${s.cacheHitPct}%`,
    );
    expect(screen.getByTestId("console-stat-tokens")).toHaveTextContent(
      `入 ${formatCompact(s.inputTokens)} · 出 ${formatCompact(s.outputTokens)}`,
    );
    expect(screen.getByTestId("console-stat-cost")).toHaveTextContent(
      `≈ ¥${(s.costCny as number).toFixed(2)}`,
    );

    // spec order: turns / durations / ttft / tps / cache / tokens / cost
    const names = Array.from(
      bar.querySelectorAll<HTMLElement>("[data-testid^='console-stat-']"),
    ).map((el) => el.dataset.testid);
    expect(names).toEqual([
      "console-stat-turns",
      "console-stat-durations",
      "console-stat-ttft",
      "console-stat-tps",
      "console-stat-cache",
      "console-stat-tokens",
      "console-stat-cost",
    ]);

    // title = full text tooltip, includes every rendered item
    expect(bar).toHaveAttribute("title");
    const title = bar.getAttribute("title") ?? "";
    expect(title).toContain(`${s.turns} 轮 · ${s.steps} 步`);
    expect(title).toContain(`≈ ¥${(s.costCny as number).toFixed(2)}`);
  });

  it("omits ttft / tps / cache when their inputs are null", () => {
    render(
      <StatsBar
        stats={stats({ ttftAvgMs: null, tokPerSec: null, cacheHitPct: null })}
        isSystemAdmin
      />,
    );
    expect(screen.getByTestId("console-stats-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("console-stat-ttft")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-stat-tps")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-stat-cache")).not.toBeInTheDocument();
    // tokens item (never null in SessionStats) still renders
    expect(screen.getByTestId("console-stat-tokens")).toBeInTheDocument();
  });

  it("never renders cost for a non-admin, even when costCny is set", () => {
    render(<StatsBar stats={stats({ costCny: 0.5 })} isSystemAdmin={false} />);
    expect(screen.queryByTestId("console-stat-cost")).not.toBeInTheDocument();
  });

  it("omits cost for an admin when costCny is null (no rate card)", () => {
    render(<StatsBar stats={stats({ costCny: null })} isSystemAdmin />);
    expect(screen.queryByTestId("console-stat-cost")).not.toBeInTheDocument();
  });

  it("renders null when turns === 0", () => {
    const { container } = render(
      <StatsBar stats={stats({ turns: 0 })} isSystemAdmin />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("appends the partial marker when stats.partial is true", () => {
    render(<StatsBar stats={stats({ partial: true })} isSystemAdmin />);
    expect(screen.getByTestId("console-stats-bar")).toHaveTextContent(
      "(仅已加载轮)",
    );
  });

  it("does not append the partial marker when stats.partial is false", () => {
    render(<StatsBar stats={stats({ partial: false })} isSystemAdmin />);
    expect(screen.getByTestId("console-stats-bar")).not.toHaveTextContent(
      "(仅已加载轮)",
    );
  });
});
