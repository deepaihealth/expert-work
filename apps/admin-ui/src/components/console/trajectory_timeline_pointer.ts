/**
 * trajectory_timeline_pointer —— `TrajectoryTimeline` 指针状态机与域投影的纯
 * 部件:手势记录、轨道量尺、命中反查、视口 → 域坐标的投影、贴边自动平移的目标
 * 视口、抬手时的选区定案,以及悬停提示的文案行。全是纯函数(除了读 DOM 几何),
 * 组件只负责把它们串起来 + setState。拆出来是为了让组件文件守住 400 行上限。
 *
 * 交互参照 deepseek-harness ui-trajectory(MIT)重写。
 */
import type { CSSProperties } from "react";

import { kindLabel } from "./kind_label";
import { centeredRange, clampFraction, formatClock, minimumSelection, orderedRange, panViewport, type TimelineModel, type TimelineSpan, type TimeRange } from "./ledger_timeline";
import type { LedgerRecord } from "./ledger_types";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";

/** 小于这个位移的按下-抬起算点击,不产生选区。 */
export const DRAG_THRESHOLD_PX = 3;
/** 悬停多久才弹提示(秒)—— spec §九「悬停:块 → 500ms 后提示」。 */
export const TOOLTIP_DELAY_S = 0.5;
/** 缩放态下拖到轨道两端这个比例内就自动平移。 */
const EDGE_PAN_ZONE = 0.08;
const EDGE_PAN_STEP = 0.025;
const EDGE_PAN_MAX_PX = 32;

export interface Gesture {
  pointerId: number;
  clientX0: number;
  /** 左键拖:按下时的域时刻(右键平移用不到)。 */
  anchorTime: number;
  /** 右键平移:按下时的视口;全景态为 null(不可平移)。 */
  viewport0: TimeRange | null;
  /** 按下时命中的记录;空白处为 null。 */
  index: number | null;
  captured: boolean;
  moved: boolean;
}

/** 轨道量尺。jsdom 下 `getBoundingClientRect` 恒零、未挂载时可能拿不到 —— 宽度兜到 1 以免除零。 */
export function rectOf(el: HTMLElement | null): { left: number; width: number } {
  const rect = el?.getBoundingClientRect?.();
  return { left: rect?.left ?? 0, width: Math.max(1, rect?.width ?? 1) };
}

/** 事件目标 → 块的 `data-index`(块是 `aria-hidden` 的 span,点击靠轨道反查)。 */
export function blockIndexAt(target: EventTarget | null): number | null {
  const el = target instanceof HTMLElement ? target.closest<HTMLElement>("[data-testid='console-lane-block']") : null;
  const raw = el?.dataset.index;
  if (raw === undefined) return null;
  const index = Number(raw);
  return Number.isFinite(index) ? index : null;
}

export interface DomainProjection {
  fullDuration: number;
  domainDuration: number;
  domainStart: number;
  domainEnd: number;
  /** 缩放态下把块 / 轮边界整体拉宽左移的两个 CSS 变量(全景 = 0% / 100%)。 */
  style: CSSProperties;
}

/** 当前可见域:全景(`viewport === null`)= 整个模型,否则 = 夹进模型的视口。 */
export function domainProjection(model: TimelineModel | null, viewport: TimeRange | null): DomainProjection {
  const fullDuration = model === null ? 1 : Math.max(1, model.end - model.start);
  const domainDuration = viewport === null
    ? fullDuration
    : Math.min(fullDuration, Math.max(1, viewport.end - viewport.start));
  const domainStart = model === null || viewport === null
    ? model?.start ?? 0
    : Math.min(Math.max(viewport.start, model.start), model.end - domainDuration);
  return {
    fullDuration,
    domainDuration,
    domainStart,
    domainEnd: domainStart + domainDuration,
    style: {
      "--traj-domain-left": `${model === null ? 0 : -(domainStart - model.start) / domainDuration * 100}%`,
      "--traj-domain-width": `${fullDuration / domainDuration * 100}%`,
    } as CSSProperties,
  };
}

/** 选区 → 视口内的比例(先夹到域边界;超出视口的部分靠轨道 overflow 裁掉)。 */
export function selectionFractions(
  model: TimelineModel | null,
  range: TimeRange | null,
  domainStart: number,
  domainDuration: number,
): { start: number; end: number } | null {
  if (model === null || range === null) return null;
  const at = (v: number): number => (Math.min(Math.max(v, model.start), model.end) - domainStart) / domainDuration;
  return { start: at(range.start), end: at(range.end) };
}

/** 拖到轨道两端 8% 内时的目标视口;不在边缘 / 全景态 / 已到头 → null。 */
export function edgePanTarget(
  model: TimelineModel,
  viewport: TimeRange | null,
  domainDuration: number,
  localX: number,
  width: number,
): TimeRange | null {
  if (viewport === null) return null;
  const zone = Math.min(EDGE_PAN_MAX_PX, Math.max(1, width * EDGE_PAN_ZONE));
  const direction = localX < zone ? -1 : localX > width - zone ? 1 : 0;
  if (direction === 0) return null;
  const distance = direction < 0 ? zone - localX : localX - (width - zone);
  const strength = Math.max(0.2, clampFraction(distance / zone));
  const next = panViewport(model, viewport, direction * domainDuration * EDGE_PAN_STEP * strength);
  return next.start === viewport.start ? null : next;
}

/** 抬手定案:窄于「一条记录宽」的选区按中心补足(点击时以落点为中心)。 */
export function commitSelection(
  model: TimelineModel,
  anchorTime: number,
  pointTime: number,
  domainDuration: number,
  click: boolean,
): TimeRange {
  const selected = orderedRange(anchorTime, pointTime);
  const minimum = minimumSelection(model, domainDuration);
  if (selected.end - selected.start >= minimum) return selected;
  const center = click ? selected.start : (selected.start + selected.end) / 2;
  return centeredRange(center, minimum, model.start, model.end);
}

/**
 * 提示最多四行:类型标签 / 起止钟点(只有时长模式拿到真实时序才报 —— 顺序模式
 * 里块的横向位置跟真实时刻无关,报钟点会误导)/ 总计 / 首 token(assistant 且
 * 不早于起点时)。
 */
export function tooltipLines(
  span: TimelineSpan,
  record: LedgerRecord | undefined,
  model: TimelineModel,
  t: (key: string, vars?: Record<string, string>) => string,
): string[] {
  const startedAt = record?.startedAt ?? null;
  const endedAt = record?.endedAt ?? null;
  // 标签与账本行、详情头部同一份(`kind_label.ts`)。
  const lines = [kindLabel(span.kind, t)];
  if (model.mode === "duration" && !model.degraded && startedAt !== null) {
    lines.push(endedAt === null
      ? t("console.timeline_tip_started", { t: formatClock(startedAt) })
      : `${formatClock(startedAt)} → ${formatClock(endedAt)}`);
  }
  if (startedAt !== null && endedAt !== null && endedAt >= startedAt) {
    lines.push(t("console.timeline_tip_total", { d: fmtDuration(Math.round(endedAt - startedAt)) }));
  }
  const firstTokenAt = record?.firstTokenAt ?? null;
  if (startedAt !== null && firstTokenAt !== null && firstTokenAt >= startedAt) {
    lines.push(t("console.timeline_tip_ttft", { d: fmtDuration(Math.round(firstTokenAt - startedAt)) }));
  }
  return lines;
}
