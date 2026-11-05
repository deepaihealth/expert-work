/**
 * process_summary — the pure counters + one-line headline behind the
 * console's 过程条 (spec §八.3): a settled turn collapses its compact rows
 * into 「模型调用 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败」. No
 * rendering, no state — ``ProcessStrip.tsx`` splits the failed tail off for
 * its own red span by summarizing again with ``failed: 0``.
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-3-brief.md.
 */
import { skillNameOf, toolSummaryLabel, type TurnSkill } from "../../api/tool_timeline";
import type { CompactRow } from "../../api/trajectory_rows";
import type { ModelCall } from "./live_rows";

type TFn = (key: string, opts?: Record<string, unknown>) => string;

export interface ProcessSummary {
  /** 模型调用次数 —— 每一步都算,不论有没有写思考(B-202:原来数 think 行,
   *  直接调工具的步骤不出 think 行,就没算进去)。 */
  modelCalls: number;
  tools: number;
  other: number;
  /** 出错的**非 think** 行数(think 行的 error 是从同步的工具继承来的,不另记一次)。 */
  failed: number;
  /** "web_search ×4 · http ×1",按次数降序(同次数按名字);空串表示无工具。
   *  `skill_view` 调用以 `skill:<技能名>` 入账,而不是裸工具名。 */
  toolBreakdown: string;
  /** 本轮成功读取过的技能(`skill_view`),首读顺序;没有 → []。 */
  skills: TurnSkill[];
  /** 每次模型调用的时长 + 工具时长(`toolMs`)+ 其余非 think 行的 durationMs 之和
   *  (null 一律按 0);一个都没有 → null。**不含 `subagent` 行** —— 见 {@link summarizeProcess}。 */
  durationMs: number | null;
  /** B-254 —— 合计拆开:模型调用的时长、工具行的时长、其中出错工具行的时长;
   *  与 `durationMs` 同口径(null 按 0,一个都没有 → null;不含 `subagent` 行)。
   *  B-255 —— 工具时长按「一批同时跑的调用」取墙钟,不是逐条相加,见 {@link toolTimeOf}。 */
  modelMs: number | null;
  toolMs: number | null;
  failedMs: number | null;
  /** 合计里既不是模型调用、也不是工具行的那部分(计划 / 规划 / 反思 / 记忆等行),
   *  让「模型 + 工具 + 其他 = 合计」对得上;没有就是 0。拆分不显示时无意义。 */
  otherMs: number;
}

/** `rows` 是过程条要显示的紧凑行;`modelCalls` 是这一轮的全部模型调用
 *  (`modelCallsOf`),与哪些行被显示无关 —— 次数和模型那部分时长都只认它。
 *  `toolNodeMs` 是本轮每个 `tools` 节点帧的墙钟(`toolNodeMsOf`),工具时长按它算。 */
export function summarizeProcess(
  rows: readonly CompactRow[],
  modelCalls: readonly ModelCall[],
  toolNodeMs: ReadonlyMap<number, number>,
): ProcessSummary {
  let tools = 0;
  let other = 0;
  let failed = 0;
  let dur = 0;
  let any = false;
  let modelMs: number | null = null;
  const byTool = new Map<string, number>();
  const skillsByName = new Map<string, TurnSkill>();
  for (const call of modelCalls) {
    if (call.durationMs === null) continue;
    dur += call.durationMs;
    any = true;
    modelMs = (modelMs ?? 0) + call.durationMs;
  }
  for (const r of rows) {
    if (r.kind === "tool") {
      tools += 1;
      const skill = skillNameOf(r.entry);
      const key = toolSummaryLabel(r.entry);
      byTool.set(key, (byTool.get(key) ?? 0) + 1);
      if (skill !== null && r.entry.status === "success") {
        const existing = skillsByName.get(skill);
        if (existing) existing.reads += 1;
        else skillsByName.set(skill, { name: skill, reads: 1 });
      }
    } else if (r.kind !== "think") other += 1;
    // A think row's `error` status is inherited from its step's failing tool
    // (`trajectory_rows.ts:139` ← `timeline.ts`'s `hasError = tools.some(…)`),
    // so counting it too would report 「2 次失败」 for one failing call.
    if (r.status === "error" && r.kind !== "think") failed += 1;
    // think 行的时长就是那一步模型调用的时长,已经在上面按 `modelCalls` 记过。
    // `subagent` 行的时间已经由它的父 `spawn_worker` 工具行记过一次:该工具
    // 同步等 worker 跑完,工具结果的 `duration_ms` 就是 worker 的墙钟
    // (线上 f562fa69:工具 938_112ms ↔ worker end 帧 933_000ms,差值是框架
    // 开销)。而 `trajectory_rows.ts` 为这一次调用同时投影出工具行与
    // subagent 行,全加就等于把同一段时间计两遍——那次的过程条因此显示
    // 44m23s,而整轮墙钟只有 23m45s,「思考」耗时反超总耗时。
    // 并行派多个 worker 时更明显:工具行是这一批的真实墙钟,而 n 个
    // subagent 行相加会数倍于它。工具行本身也不逐条加 —— 见下面 `toolTimeOf`(B-255)。
    if (
      r.durationMs !== null &&
      r.kind !== "think" &&
      r.kind !== "subagent" &&
      r.kind !== "tool"
    ) {
      dur += r.durationMs;
      any = true;
    }
  }
  const { toolMs, failedMs } = toolTimeOf(rows, toolNodeMs);
  if (toolMs !== null) {
    dur += toolMs;
    any = true;
  }
  const toolBreakdown = [...byTool.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, count]) => `${name} ×${count}`)
    .join(" · ");
  return {
    modelCalls: modelCalls.length,
    tools,
    other,
    failed,
    toolBreakdown,
    skills: [...skillsByName.values()],
    durationMs: any ? dur : null,
    modelMs,
    toolMs,
    failedMs,
    otherMs: any ? dur - (modelMs ?? 0) - (toolMs ?? 0) : 0,
  };
}

/** 一批同时跑的工具调用:同一个 `tools` 节点帧(墙钟已知),或帧没带时长时的同一步。 */
interface ToolBatch {
  /** 该 `tools` 节点帧的 `_duration_ms`;不知道 → null。 */
  frameMs: number | null;
  /** 批内各行时长的最大值(帧时长缺失时的兜底);都没有 → null。 */
  maxMs: number | null;
  /** 批内出错行的时长之和;没有带时长的出错行 → null。 */
  failedSumMs: number | null;
}

/** B-255 —— 工具时长 = 每一批的墙钟之和,不是每条工具行时长之和。同一步里的
 *  调用是并发跑的(tools 节点按 stage `asyncio.gather`),逐条相加会把重叠的
 *  时间算好几遍:测试对话 8478cedb 那一轮墙钟 34 分钟,几步里各有 2~3 个
 *  ~120s 的 `ask_image` 并发,过程条报「工具 33m14s」,而状态栏按 tools 节点
 *  帧算的是 18m11s。所以一批的墙钟取该 `tools` 节点帧的 `_duration_ms`(与
 *  `session_stats.ts` `nodeDurations` 同一个数);帧没带时长(还在跑 / 老帧)
 *  退回批内最长一条 —— 并发下这是下限,但不会重复计。`step` 为 null 的工具行
 *  自成一批(原样用自己的时长)。
 *  出错时长 = min(批墙钟, 批内出错行时长之和):出错调用串行时(stage 之间、
 *  bash 这类强制独占的调用)和是准的,并发时由墙钟封顶 —— 失败那段永远不超过
 *  它所在那批的工具时长。 */
function toolTimeOf(
  rows: readonly CompactRow[],
  toolNodeMs: ReadonlyMap<number, number>,
): { toolMs: number | null; failedMs: number | null } {
  const batches = new Map<string, ToolBatch>();
  for (const r of rows) {
    if (r.kind !== "tool") continue;
    // 工具行的 eventIndexes = [所属 agent 帧, 结果所在 tools 帧];只有后者在表里。
    const frame = r.eventIndexes.find((i) => toolNodeMs.has(i));
    const key =
      frame !== undefined ? `frame:${frame}` : r.step !== null ? `step:${r.step}` : `row:${r.id}`;
    const batch = batches.get(key) ?? {
      frameMs: frame === undefined ? null : (toolNodeMs.get(frame) ?? null),
      maxMs: null,
      failedSumMs: null,
    };
    if (r.durationMs !== null) {
      batch.maxMs = Math.max(batch.maxMs ?? 0, r.durationMs);
      if (r.status === "error") batch.failedSumMs = (batch.failedSumMs ?? 0) + r.durationMs;
    }
    batches.set(key, batch);
  }
  let toolMs: number | null = null;
  let failedMs: number | null = null;
  for (const b of batches.values()) {
    const wall = b.frameMs ?? b.maxMs;
    if (wall === null) continue;
    toolMs = (toolMs ?? 0) + wall;
    if (b.failedSumMs !== null) failedMs = (failedMs ?? 0) + Math.min(wall, b.failedSumMs);
  }
  return { toolMs, failedMs };
}

/** 「模型调用 3 次 · 工具 5 次(web_search ×4 · http ×1) · 1 次失败」——无工具
 *  省略括号,无失败省略尾段,三项皆 0 → `console.process_empty`。 */
export function processHeadline(s: ProcessSummary, t: TFn): string {
  const parts: string[] = [];
  if (s.modelCalls > 0) parts.push(t("console.process_model_calls", { n: s.modelCalls }));
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
