/**
 * process_summary — the pure counters + one-line headline behind the
 * console's 过程条 (spec §八.3): a settled turn collapses its compact rows
 * into 「思考 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败」. No
 * rendering, no state — ``ProcessStrip.tsx`` splits the failed tail off for
 * its own red span by summarizing again with ``failed: 0``.
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-3-brief.md.
 */
import type { CompactRow } from "../../api/trajectory_rows";

type TFn = (key: string, opts?: Record<string, unknown>) => string;

export interface ProcessSummary {
  think: number;
  tools: number;
  other: number;
  /** 出错的**非 think** 行数(think 行的 error 是从同步的工具继承来的,不另记一次)。 */
  failed: number;
  /** "web_search ×4 · http ×1",按次数降序(同次数按名字);空串表示无工具。 */
  toolBreakdown: string;
  /** 全部行的 durationMs 之和(null 一律按 0);无行 → null。 */
  durationMs: number | null;
}

export function summarizeProcess(rows: readonly CompactRow[]): ProcessSummary {
  let think = 0;
  let tools = 0;
  let other = 0;
  let failed = 0;
  let dur = 0;
  let any = false;
  const byTool = new Map<string, number>();
  for (const r of rows) {
    if (r.kind === "think") think += 1;
    else if (r.kind === "tool") {
      tools += 1;
      byTool.set(r.entry.toolName, (byTool.get(r.entry.toolName) ?? 0) + 1);
    } else other += 1;
    // A think row's `error` status is inherited from its step's failing tool
    // (`trajectory_rows.ts:139` ← `timeline.ts`'s `hasError = tools.some(…)`),
    // so counting it too would report 「2 次失败」 for one failing call.
    if (r.status === "error" && r.kind !== "think") failed += 1;
    if (r.durationMs !== null) {
      dur += r.durationMs;
      any = true;
    }
  }
  const toolBreakdown = [...byTool.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, count]) => `${name} ×${count}`)
    .join(" · ");
  return { think, tools, other, failed, toolBreakdown, durationMs: any ? dur : null };
}

/** 「思考 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败」——无工具省略
 *  括号,无失败省略尾段,三项皆 0 → `console.process_empty`。 */
export function processHeadline(s: ProcessSummary, t: TFn): string {
  const parts: string[] = [];
  if (s.think > 0) parts.push(t("console.process_think", { n: s.think }));
  if (s.tools > 0) {
    parts.push(
      t("console.process_tools", { n: s.tools }) +
        (s.toolBreakdown ? `(${s.toolBreakdown})` : ""),
    );
  }
  if (s.other > 0) parts.push(t("console.process_other", { n: s.other }));
  if (parts.length === 0) return t("console.process_empty");
  if (s.failed > 0) parts.push(t("console.process_failed", { n: s.failed }));
  return parts.join(" · ");
}
