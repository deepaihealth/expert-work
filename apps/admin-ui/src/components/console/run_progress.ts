/**
 * B-269 / B-270 —— 排队 / 运行中气泡的进度文案。数据来自 run 详情
 * (``GET /v1/sessions/{thread}/runs/{run}`` 的 ``progress`` / ``queued_behind``)
 * 与排队帧(``queued`` 的 ``ahead_started_at``);这里只做格式化,不推算。
 */
import type { TFunction } from "i18next";

import type { RunProgress } from "../../api/runs";
import type { SseEvent } from "../../api/sessions";

/** 气泡只说到分钟:``{prefix}_minutes``(``n``)或 ``{prefix}_under_minute``。 */
function minutesText(prefix: string, seconds: number, t: TFunction): string {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  return minutes < 1 ? t(`${prefix}_under_minute`) : t(`${prefix}_minutes`, { n: minutes });
}

/** 「已进行 N 分钟 · 第 S 步 · 调用工具 T 次 · 子任务 W 个在跑」;
 *  零或未知的部分不出现,全都没有时 ``null``。 */
export function progressLine(progress: RunProgress, t: TFunction): string | null {
  const parts: string[] = [];
  if (progress.elapsed_s !== null) {
    parts.push(minutesText("playground.progress_elapsed", progress.elapsed_s, t));
  }
  if (progress.step > 0) parts.push(t("playground.progress_step", { n: progress.step }));
  if (progress.tool_calls > 0) {
    parts.push(t("playground.progress_tool_calls", { n: progress.tool_calls }));
  }
  if (progress.active_workers > 0) {
    parts.push(t("playground.progress_workers", { n: progress.active_workers }));
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** 排队气泡:知道上一轮何时开始执行就说它跑了多久,否则原文案。 */
export function queuedText(aheadStartedAt: string | null, nowMs: number, t: TFunction): string {
  const started = aheadStartedAt === null ? Number.NaN : Date.parse(aheadStartedAt);
  if (Number.isNaN(started)) return t("playground.turn_queued");
  return minutesText("playground.turn_queued_behind", (nowMs - started) / 1000, t);
}

/** live 轮:排队帧里领头那一轮的开始时刻(还没开始 / 没有排队帧 = ``null``)。 */
export function aheadStartedAtOf(events: readonly SseEvent[]): string | null {
  const queued = events.find((e) => e.event === "queued");
  if (queued === undefined || queued.data === null || typeof queued.data !== "object") {
    return null;
  }
  const value = (queued.data as Record<string, unknown>).ahead_started_at;
  return typeof value === "string" ? value : null;
}
