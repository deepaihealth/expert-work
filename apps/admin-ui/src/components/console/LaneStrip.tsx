/**
 * LaneStrip v2 —— 调试台右栏的三条 8px 细泳道(用户 / 模型 / 工具)。每条轨迹
 * 行一个块,块的横向位置来自 `lane_strip_model.ts` 的双投影
 * (`sequence` 等宽按顺序 / `duration` 按真实起止毫秒)。
 *
 * 交互(spec §八.7):hover 出「类型 · #序号 · 摘要 / 起止 · 耗时」提示并回抛
 * `onHoverRow` 与行表联动;点击选中行;在轨道上横向拖选一段 → `onRangeChange`
 * 给出行序号闭区间(段外块变淡),双击复位;下方一条刻度;运行中的尾块呼吸。
 *
 * 见 .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-5-brief.md。
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties, type JSX, type PointerEvent } from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { SseEvent } from "../../api/sessions";
import type { TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { laneProjection, rangeToRowSpan, type Lane, type LaneBlock, type LaneMode } from "./lane_strip_model";
import "./lane_strip.css";

export interface LaneStripProps {
  rows: readonly TrajectoryRow[];
  events: readonly SseEvent[];
  running: boolean;
  mode: LaneMode;
  selectedRowId: string | null;
  hoveredRowId: string | null;
  onHoverRow: (rowId: string | null) => void;
  onSelectRow: (rowId: string) => void;
  /** 行序号闭区间筛选;null = 无筛选。 */
  range: { from: number; to: number } | null;
  onRangeChange: (range: { from: number; to: number } | null) => void;
  /** 每行的摘要(行表同一函数,提示气泡里用)。 */
  summaryOf: (row: TrajectoryRow) => string;
}

const LANES: readonly Lane[] = ["user", "model", "tools"];
const LANE_LABEL_KEY: Record<Lane, string> = {
  user: "console.lane_user",
  model: "console.lane_model",
  tools: "console.lane_tools",
};
/** 块的最小可见宽度(域占比 %)—— 点块与 1s 工具都得看得见、点得着。 */
const MIN_BLOCK_PCT = 0.6;
/** 小于这个位移的按下-抬起算点击,不产生筛选区间。 */
const DRAG_THRESHOLD_PX = 4;

export function LaneStrip(props: LaneStripProps): JSX.Element | null {
  const {
    rows, events, running, mode, selectedRowId, hoveredRowId,
    onHoverRow, onSelectRow, range, onRangeChange, summaryOf,
  } = props;
  const { t } = useTranslation();

  // Running-growth tick — mirrors TurnCard.tsx:404-410: only ticks while the
  // turn is running, cleaned up on unmount/settle so no stray interval keeps
  // firing once the caller stops passing `running`.
  const [nowTick, setNowTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNowTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  const projection = useMemo(
    () => laneProjection(rows, events, { mode, running, nowMs: Date.now() }),
    [rows, events, mode, running, nowTick],
  );

  // 轨道量尺:与三条泳道的轨道区严格同宽同左,拖选的域坐标由它换算。
  const gaugeRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x0: number; from: number; captured: boolean } | null>(null);
  const [draft, setDraft] = useState<{ from: number; to: number } | null>(null);

  if (rows.length === 0) return null;

  const total = projection.total;
  const pct = (v: number): number => (total > 0 ? (v / total) * 100 : 0);

  const domainAt = (clientX: number): number => {
    const el = gaugeRef.current;
    if (el === null) return 0;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return 0;
    return Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1) * total;
  };

  // 指针捕获**只在真的开始拖(位移过阈值)之后**才拿。按下就捕获的话,后续
  // pointerup / click 会被重定向到捕获容器上,块按钮的 onClick 在真实浏览器里
  // 永远不触发(spec §八.7「点击块 = 选中行 + 打开详情」直接失效);jsdom 没有
  // setPointerCapture,这个坑测试是照不出来的,所以这里靠 spy 断言把它钉住。
  const handlePointerDown = (e: PointerEvent<HTMLDivElement>): void => {
    dragRef.current = { x0: e.clientX, from: domainAt(e.clientX), captured: false };
    setDraft(null);
  };
  const handlePointerMove = (e: PointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current;
    if (drag === null) return;
    if (Math.abs(e.clientX - drag.x0) < DRAG_THRESHOLD_PX) {
      // 还在阈值内 = 仍可能是点击:不捕获、不画草稿(回退到阈值内也要擦掉)。
      setDraft(null);
      return;
    }
    if (!drag.captured) {
      dragRef.current = { ...drag, captured: true };
      e.currentTarget.setPointerCapture?.(e.pointerId);
    }
    setDraft(rangeToRowSpan(projection, drag.from, domainAt(e.clientX)));
  };
  /** 收尾:清拖选状态与草稿遮罩,捕获过才释放。返回收尾前的拖选状态。 */
  const endDrag = (e: PointerEvent<HTMLDivElement>): { x0: number; from: number; captured: boolean } | null => {
    const drag = dragRef.current;
    dragRef.current = null;
    setDraft(null);
    if (drag !== null && drag.captured) e.currentTarget.releasePointerCapture?.(e.pointerId);
    return drag;
  };
  const handlePointerUp = (e: PointerEvent<HTMLDivElement>): void => {
    const drag = endDrag(e);
    // 位移不足阈值 = 点击(块自己的 onClick 已经处理选中),不当拖选。
    if (drag === null || Math.abs(e.clientX - drag.x0) < DRAG_THRESHOLD_PX) return;
    onRangeChange(rangeToRowSpan(projection, drag.from, domainAt(e.clientX)));
  };
  const handlePointerCancel = (e: PointerEvent<HTMLDivElement>): void => {
    endDrag(e);
  };
  const handlePointerLeave = (e: PointerEvent<HTMLDivElement>): void => {
    // 捕获生效时指针离开元素照样送事件到这里,不该中断;没捕获(还在阈值内)
    // 就等于跟丢了,清掉。
    if (dragRef.current?.captured !== true) endDrag(e);
  };

  /** 行序号闭区间 → 遮罩几何(取区间内所有块的域并集)。 */
  const maskStyle = (span: { from: number; to: number }): CSSProperties | null => {
    const inSpan = projection.blocks.filter((b) => b.rowIndex >= span.from && b.rowIndex <= span.to);
    if (inSpan.length === 0) return null;
    const start = Math.min(...inSpan.map((b) => b.start));
    const end = Math.max(...inSpan.map((b) => b.end));
    return { left: `${pct(start)}%`, width: `${Math.max(pct(end - start), MIN_BLOCK_PCT)}%` };
  };
  const rangeStyle = range === null ? null : maskStyle(range);
  const draftStyle = draft === null ? null : maskStyle(draft);

  const headOf = (block: LaneBlock, row: TrajectoryRow): string =>
    `${t(`console.traj_kind_${block.kind}`)} · #${block.rowIndex} · ${summaryOf(row)}`;
  const timeOf = (block: LaneBlock, row: TrajectoryRow): string => {
    if (mode === "duration" && !projection.degraded) {
      return t("console.lane_tip_range", {
        start: fmtDuration(Math.round(block.start)),
        end: fmtDuration(Math.round(block.end)),
        d: fmtDuration(Math.round(block.end - block.start)),
      });
    }
    return row.durationMs === null ? "" : fmtDuration(row.durationMs);
  };

  const byLane: Record<Lane, LaneBlock[]> = { user: [], model: [], tools: [] };
  for (const block of projection.blocks) byLane[block.lane].push(block);

  return (
    <div
      className="ew-lanes"
      data-testid="console-lane-strip"
      data-mode={mode}
      data-degraded={projection.degraded ? "true" : undefined}
    >
      <div
        className="ew-lanes__body"
        data-testid="console-lane-track"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerCancel}
        onPointerLeave={handlePointerLeave}
        onDoubleClick={() => onRangeChange(null)}
      >
        {LANES.map((lane) => (
          <div key={lane} className="ew-lane">
            <span className="ew-lane__label">{t(LANE_LABEL_KEY[lane])}</span>
            <div className="ew-lane__track">
              {byLane[lane].map((block) => {
                const row = rows[block.rowIndex - 1];
                const head = headOf(block, row);
                const time = timeOf(block, row);
                const dimmed = range !== null && (block.rowIndex < range.from || block.rowIndex > range.to);
                return (
                  <Tooltip
                    key={block.key}
                    title={
                      <>
                        {head}
                        <br />
                        {time !== "" && (
                          <>
                            {time}
                            <br />
                          </>
                        )}
                        {t("console.lane_tip_hint")}
                      </>
                    }
                  >
                    <button
                      type="button"
                      className="ew-lane__block"
                      data-testid="console-lane-block"
                      data-row-id={block.rowId}
                      data-lane={block.lane}
                      data-kind={block.kind}
                      data-index={block.rowIndex}
                      data-error={block.hasError ? "true" : undefined}
                      data-live={block.live ? "true" : undefined}
                      data-selected={block.rowId === selectedRowId ? "true" : undefined}
                      data-hovered={block.rowId === hoveredRowId ? "true" : undefined}
                      data-dimmed={dimmed ? "true" : undefined}
                      aria-label={head}
                      aria-pressed={block.rowId === selectedRowId}
                      style={{
                        left: `${pct(block.start)}%`,
                        width: `${Math.max(pct(block.end - block.start), MIN_BLOCK_PCT)}%`,
                      }}
                      onMouseEnter={() => onHoverRow(block.rowId)}
                      onMouseLeave={() => onHoverRow(null)}
                      onClick={() => onSelectRow(block.rowId)}
                    />
                  </Tooltip>
                );
              })}
            </div>
          </div>
        ))}
        <div className="ew-lanes__gauge" ref={gaugeRef} aria-hidden="true">
          {rangeStyle !== null && (
            <div className="ew-lanes__range" data-testid="console-lane-range" style={rangeStyle} />
          )}
          {draftStyle !== null && (
            <div className="ew-lanes__draft" data-testid="console-lane-draft" style={draftStyle} />
          )}
        </div>
      </div>
      <div className="ew-lanes__ticks">
        {projection.ticks.map((tick) => (
          <span
            key={`${tick.at}-${tick.label}`}
            className="ew-lanes__tick"
            data-testid="console-lane-tick"
            style={{ left: `${pct(tick.at)}%` }}
          >
            {tick.label}
          </span>
        ))}
      </div>
    </div>
  );
}
