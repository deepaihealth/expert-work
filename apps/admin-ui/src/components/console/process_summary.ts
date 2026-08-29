/**
 * process_summary — the pure counters + one-line headline behind the
 * console's 过程条 (spec §八.3): a settled turn collapses its compact rows
 * into 「思考 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败」. No
 * rendering, no state — ``ProcessStrip.tsx`` splits the failed tail off for
 * its own red span by summarizing again with ``failed: 0``.
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-3-brief.md.
 */
import { skillNameOf, toolSummaryLabel, type TurnSkill } from "../../api/tool_timeline";
import type { CompactRow } from "../../api/trajectory_rows";

type TFn = (key: string, opts?: Record<string, unknown>) => string;

export interface ProcessSummary {
  think: number;
  tools: number;
  other: number;
  /** 出错的**非 think** 行数(think 行的 error 是从同步的工具继承来的,不另记一次)。 */
  failed: number;
  /** "web_search ×4 · http ×1",按次数降序(同次数按名字);空串表示无工具。
   *  `skill_view` 调用以 `skill:<技能名>` 入账,而不是裸工具名。 */
  toolBreakdown: string;
  /** 本轮成功读取过的技能(`skill_view`),首读顺序;没有 → []。 */
  skills: TurnSkill[];
  /** 行 durationMs 之和(null 一律按 0);无行 → null。
   *  **不含 `subagent` 行** —— 见 {@link summarizeProcess}。 */
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
  const skillsByName = new Map<string, TurnSkill>();
  for (const r of rows) {
    if (r.kind === "think") think += 1;
    else if (r.kind === "tool") {
      tools += 1;
      const skill = skillNameOf(r.entry);
      const key = toolSummaryLabel(r.entry);
      byTool.set(key, (byTool.get(key) ?? 0) + 1);
      if (skill !== null && r.entry.status === "success") {
        const existing = skillsByName.get(skill);
        if (existing) existing.reads += 1;
        else skillsByName.set(skill, { name: skill, reads: 1 });
      }
    } else other += 1;
    // A think row's `error` status is inherited from its step's failing tool
    // (`trajectory_rows.ts:139` ← `timeline.ts`'s `hasError = tools.some(…)`),
    // so counting it too would report 「2 次失败」 for one failing call.
    if (r.status === "error" && r.kind !== "think") failed += 1;
    // `subagent` 行的时间已经由它的父 `spawn_worker` 工具行记过一次:该工具
    // 同步等 worker 跑完,工具结果的 `duration_ms` 就是 worker 的墙钟
    // (线上 f562fa69:工具 938_112ms ↔ worker end 帧 933_000ms,差值是框架
    // 开销)。而 `trajectory_rows.ts` 为这一次调用同时投影出工具行与
    // subagent 行,全加就等于把同一段时间计两遍——那次的过程条因此显示
    // 44m23s,而整轮墙钟只有 23m45s,「思考」耗时反超总耗时。
    // 并行派多个 worker 时更明显:工具行是这一批的真实墙钟,而 n 个
    // subagent 行相加会数倍于它。
    if (r.durationMs !== null && r.kind !== "subagent") {
      dur += r.durationMs;
      any = true;
    }
  }
  const toolBreakdown = [...byTool.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, count]) => `${name} ×${count}`)
    .join(" · ");
  return {
    think,
    tools,
    other,
    failed,
    toolBreakdown,
    skills: [...skillsByName.values()],
    durationMs: any ? dur : null,
  };
}

/** 「思考 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败」——无工具省略
 *  括号,无失败省略尾段,三项皆 0 → `console.process_empty`。 */
export function processHeadline(s: ProcessSummary, t: TFn): string {
  const parts: string[] = [];
  if (s.think > 0) parts.push(t("console.process_think", { n: s.think }));
  if (s.tools > 0) {
    parts.push(
      s.toolBreakdown
        ? t("console.process_tools_detailed", { n: s.tools, breakdown: s.toolBreakdown })
        : t("console.process_tools", { n: s.tools }),
    );
  }
  if (s.other > 0) parts.push(t("console.process_other", { n: s.other }));
  if (parts.length === 0) return t("console.process_empty");
  if (s.failed > 0) parts.push(t("console.process_failed", { n: s.failed }));
  return parts.join(" · ");
}
