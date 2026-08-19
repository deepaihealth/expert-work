/**
 * TrajectoryTimeline —— 轨迹视图顶部 50px 的概览时间轴(spec §九「概览时间轴」)。
 * 三条泳道(输入 / 模型 / 工具)每条记录一个块,左键拖出选区、点块选中记录、
 * 点空白开最小选区并把最近记录滚进账本视口,滚轮以鼠标为锚缩放、右键按住平移,
 * 悬停出提示并与账本双向高亮。
 *
 * 几何与状态迁移全部来自 `ledger_timeline.ts` 的纯函数;组件只管指针状态机与
 * 渲染。**视口(缩放 / 平移)是组件内部状态**,选区 `range` 由父层持有 ——
 * 账本、工具条要一起读它。投影模型 / 交互参照 deepseek-harness ui-trajectory
 * (MIT)重写。
 *
 * See .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-7-brief.md.
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties, type JSX, type KeyboardEvent, type PointerEvent } from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import {
  clampFraction, focusIndexes, minimumSelection, nearestSpan, orderedRange, panViewport,
  revealInViewport, zoomViewport, type TimelineModel, type TimeRange,
} from "./ledger_timeline";
import type { LedgerRecord } from "./ledger_types";
import { TrajectoryTimelineBlocks } from "./TrajectoryTimelineBlocks";
import {
  blockIndexAt, commitSelection, domainProjection, edgePanTarget, rectOf, selectionFractions,
  DRAG_THRESHOLD_PX, TOOLTIP_DELAY_S, type Gesture,
} from "./trajectory_timeline_pointer";
import "./trajectory_timeline.css";

export interface TrajectoryTimelineProps {
  model: TimelineModel | null;
  /** 提示文案用(类型 / 起止 / 时长):按 index 取。 */
  records: readonly LedgerRecord[];
  range: TimeRange | null;
  onRangeChange: (r: TimeRange | null) => void;
  selectedIndex: number | null;
  hoveredIndex: number | null;
  onHoverIndex: (i: number | null) => void;
  /** 点块。 */
  onSelectRecord: (index: number) => void;
  /** 点空白:最近记录滚进账本视口(不打开详情)。 */
  onFocusRecord: (index: number) => void;
  searchMatches: ReadonlySet<number> | null;
  hasEarlier: boolean;
  loadingEarlier: boolean;
  onLoadEarlier: () => void;
}

const LANE_LABEL_KEYS = ["console.lane_input", "console.lane_model", "console.lane_tools"] as const;

export function TrajectoryTimeline(props: TrajectoryTimelineProps): JSX.Element {
  const {
    model, records, range, onRangeChange, selectedIndex, hoveredIndex, onHoverIndex,
    onSelectRecord, onFocusRecord, searchMatches, hasEarlier, loadingEarlier, onLoadEarlier,
  } = props;
  const { t } = useTranslation();

  const rootRef = useRef<HTMLElement>(null);
  const dragRef = useRef<Gesture | null>(null);
  const panRef = useRef<Gesture | null>(null);
  /** 上一次回抛给父层的悬停记录 —— 只在真的变了才回调。 */
  const hoverSentRef = useRef<number | null>(null);
  /** 给「挪视口」effect 读最新模型,又不让它随模型引用变化重跑。 */
  const modelRef = useRef<TimelineModel | null>(model);
  const prevSelectedRef = useRef<number | null>(selectedIndex);
  const [viewport, setViewport] = useState<TimeRange | null>(null);
  const [animate, setAnimate] = useState(false);
  const [draft, setDraft] = useState<TimeRange | null>(null);
  /** 空白处悬停竖线的位置;指针停在块上时为 null(那时靠块自己的描边表达)。 */
  const [hoverFraction, setHoverFraction] = useState<number | null>(null);
  const [panning, setPanning] = useState(false);

  // 账本换了一批记录(加载更早 / 切会话)后旧视口可能整段落在域外,回全景。
  useEffect(() => {
    modelRef.current = model;
    if (model === null) return;
    setAnimate(false);
    setViewport((cur) => (cur !== null && (cur.end < model.start || cur.start > model.end) ? null : cur));
  }, [model]);

  // 选中账本里视口外的记录 → 180ms 平滑挪过去(全景态不用挪)。**只认
  // `selectedIndex` 真的变化**:运行中每来一帧 `model` 就换引用,跟着跑会把用户
  // 刚缩放 / 平移到的位置一次次拽回选中块;挂载时也不该先亮起动画。
  useEffect(() => {
    if (prevSelectedRef.current === selectedIndex) return;
    prevSelectedRef.current = selectedIndex;
    const current = modelRef.current;
    if (current === null || selectedIndex === null) return;
    setAnimate(true);
    setViewport((cur) => revealInViewport(current, cur, selectedIndex));
  }, [selectedIndex]);

  const projection = useMemo(() => domainProjection(model, viewport), [model, viewport]);
  const { fullDuration, domainDuration, domainStart, domainEnd, style: domainStyle } = projection;

  // 滚轮缩放要 `preventDefault`(否则整页跟着滚),React 的 onWheel 是 passive
  // 的,只能自己挂原生监听。
  useEffect(() => {
    const root = rootRef.current;
    if (root === null) return;
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault();
      if (model === null) return;
      const { left, width } = rectOf(root.querySelector<HTMLElement>("[data-testid='console-lane-track']"));
      const anchor = clampFraction((event.clientX - left) / width);
      setAnimate(false);
      setViewport((cur) => zoomViewport(model, cur, anchor, event.deltaY));
    };
    root.addEventListener("wheel", onWheel, { passive: false });
    return () => root.removeEventListener("wheel", onWheel);
  }, [model]);

  const reportHover = (index: number | null): void => {
    if (hoverSentRef.current === index) return;
    hoverSentRef.current = index;
    onHoverIndex(index);
  };

  const timeAt = (clientX: number, el: HTMLElement): { fraction: number; time: number } => {
    const { left, width } = rectOf(el);
    const fraction = clampFraction((clientX - left) / width);
    return { fraction, time: domainStart + fraction * domainDuration };
  };

  const handlePointerDown = (e: PointerEvent<HTMLDivElement>): void => {
    if (model === null) return;
    const base = { pointerId: e.pointerId, clientX0: e.clientX, captured: false, moved: false };
    if (e.button === 2) {
      // 全景态下右键按下不进平移态、也不拿捕获(没得可平移);手势照样记着,
      // 好让抬手时的「右键单击 = 清选区」照旧生效。
      panRef.current = { ...base, anchorTime: domainStart, viewport0: viewport, index: null };
      if (viewport === null) return;
      setAnimate(false);
      setPanning(true);
      return;
    }
    if (e.button !== 0) return;
    const { fraction, time } = timeAt(e.clientX, e.currentTarget);
    const index = blockIndexAt(e.target);
    dragRef.current = { ...base, anchorTime: time, viewport0: viewport, index };
    setHoverFraction(index === null ? fraction : null);
    reportHover(index);
    setDraft({ start: time, end: time });
  };

  /** 拖到轨道两端自动平移;返回平移后的域起点(没动就是原来的)。 */
  const edgePan = (clientX: number, el: HTMLElement): number => {
    if (model === null) return domainStart;
    const { left, width } = rectOf(el);
    const next = edgePanTarget(model, viewport, domainDuration, clientX - left, width);
    if (next === null) return domainStart;
    setAnimate(false);
    setViewport(next);
    return next.start;
  };

  const handlePointerMove = (e: PointerEvent<HTMLDivElement>): void => {
    if (model === null) return;
    const pan = panRef.current;
    if (pan !== null && pan.pointerId === e.pointerId) {
      if (Math.abs(e.clientX - pan.clientX0) >= DRAG_THRESHOLD_PX && !pan.moved) {
        panRef.current = { ...pan, captured: pan.viewport0 !== null, moved: true };
        if (pan.viewport0 !== null) e.currentTarget.setPointerCapture?.(e.pointerId);
      }
      if (pan.viewport0 === null) return;
      const { width } = rectOf(e.currentTarget);
      const delta = -((e.clientX - pan.clientX0) / width) * domainDuration;
      setViewport(panViewport(model, pan.viewport0, delta));
      return;
    }
    const { fraction } = timeAt(e.clientX, e.currentTarget);
    const index = blockIndexAt(e.target);
    setHoverFraction(index === null ? fraction : null);
    reportHover(index);
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== e.pointerId) return;
    // 指针捕获只在位移过阈值之后才拿 —— 按下就捕获会把随后的 pointerup 重定向
    // 到轨道,「点块 = 选中记录」在真实浏览器里直接失效(PR-A.1 Task 5 的坑)。
    if (Math.abs(e.clientX - drag.clientX0) >= DRAG_THRESHOLD_PX && !drag.captured) {
      dragRef.current = { ...drag, captured: true, moved: true };
      e.currentTarget.setPointerCapture?.(e.pointerId);
    }
    const nextStart = edgePan(e.clientX, e.currentTarget);
    setDraft(orderedRange(drag.anchorTime, nextStart + fraction * domainDuration));
  };

  const handlePointerUp = (e: PointerEvent<HTMLDivElement>): void => {
    const pan = panRef.current;
    if (pan !== null && pan.pointerId === e.pointerId) {
      if (pan.captured) e.currentTarget.releasePointerCapture?.(e.pointerId);
      panRef.current = null;
      setPanning(false);
      // 右键按下抬起没挪动 = 右键单击 = 清选区。
      if (!pan.moved && Math.abs(e.clientX - pan.clientX0) < DRAG_THRESHOLD_PX) onRangeChange(null);
      return;
    }
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== e.pointerId || model === null) return;
    if (drag.captured) e.currentTarget.releasePointerCapture?.(e.pointerId);
    dragRef.current = null;
    setDraft(null);
    const { time } = timeAt(e.clientX, e.currentTarget);
    const selected = orderedRange(drag.anchorTime, time);
    const click = Math.abs(e.clientX - drag.clientX0) < DRAG_THRESHOLD_PX;
    if (click && drag.index !== null) {
      onRangeChange(null);
      onSelectRecord(drag.index);
      return;
    }
    // 域退化(整轮记录都落在同一时刻,`model.start === model.end`)时「一条记录
    // 宽」是 0 —— 点空白定案出来的会是一条零宽选区:每一行都判 outside、整片块
    // 压暗,读者什么也没选却像选了个空。只把最近的记录带进视口。
    if (click && minimumSelection(model, domainDuration) === 0) {
      const nearestAt = nearestSpan(model, selected.start);
      if (nearestAt !== null) onFocusRecord(nearestAt.index);
      return;
    }
    onRangeChange(commitSelection(model, drag.anchorTime, time, domainDuration, click));
    if (!click) return;
    const nearest = nearestSpan(model, selected.start);
    if (nearest !== null) onFocusRecord(nearest.index);
  };

  const handlePointerCancel = (e: PointerEvent<HTMLDivElement>): void => {
    if (dragRef.current?.captured === true || panRef.current?.captured === true) {
      e.currentTarget.releasePointerCapture?.(e.pointerId);
    }
    dragRef.current = null;
    panRef.current = null;
    setDraft(null);
    setHoverFraction(null);
    setPanning(false);
    reportHover(null);
  };

  const handlePointerLeave = (): void => {
    // 捕获中的手势即使指针离开轨道也照样收事件,不能中断。**还没过阈值(没
    // 捕获)的按下**一旦离开,`pointerup` 就再也回不到轨道了 —— 当场收尾,否则
    // 零宽草稿会卡住把所有块压成 `data-selected="false"`,点击也丢了。
    if (dragRef.current?.captured === true || panRef.current?.captured === true) return;
    if (dragRef.current !== null || panRef.current !== null) {
      dragRef.current = null;
      panRef.current = null;
      setDraft(null);
      setPanning(false);
    }
    setHoverFraction(null);
    reportHover(null);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== "Escape" || range === null) return;
    e.preventDefault();
    onRangeChange(null);
  };

  const earlier = hasEarlier && (model === null || domainStart === model.start)
    ? (
      <Tooltip
        title={loadingEarlier ? t("console.timeline_loading_earlier") : t("console.timeline_load_earlier")}
        mouseEnterDelay={TOOLTIP_DELAY_S}
      >
        <button
          type="button"
          className="ew-traj-tl__earlier"
          data-testid="console-lane-earlier"
          aria-label={loadingEarlier ? t("console.timeline_loading_earlier") : t("console.timeline_load_earlier")}
          aria-disabled={loadingEarlier}
          onClick={() => {
            if (!loadingEarlier) onLoadEarlier();
          }}
          onPointerDown={(e) => e.stopPropagation()}
          onPointerMove={(e) => e.stopPropagation()}
        >
          …
        </button>
      </Tooltip>
    )
    : null;

  const activeRange = draft ?? range;
  const focused = useMemo(
    () => (model === null || activeRange === null ? null : focusIndexes(model, activeRange)),
    [model, activeRange],
  );
  const visible = selectionFractions(model, activeRange, domainStart, domainDuration);
  const selectionStyle = visible === null
    ? undefined
    : { left: `${visible.start * 100}%`, width: `${(visible.end - visible.start) * 100}%` };

  return (
    <section
      ref={rootRef}
      className="ew-traj-tl"
      data-testid="console-lane-strip"
      data-mode={model?.mode}
      data-degraded={model?.degraded === true ? "true" : undefined}
      aria-label={t("console.timeline_aria")}
    >
      <div className="ew-traj-tl__plot">
        <div className="ew-traj-tl__labels" aria-hidden="true">
          {LANE_LABEL_KEYS.map((key) => <span key={key}>{t(key)}</span>)}
        </div>
        <div
          className="ew-traj-tl__track"
          data-testid="console-lane-track"
          data-panning={panning ? "true" : undefined}
          // 裸 `div` 上不许挂 `aria-label`(axe `aria-prohibited-attr`,
          // PR-A.2 Task 11 的 e2e 扫描逮到)—— 给它一个容器角色,标签才合法。
          role="group"
          tabIndex={0}
          aria-label={t("console.timeline_track_aria")}
          onKeyDown={handleKeyDown}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerCancel}
          onPointerLeave={handlePointerLeave}
          onDoubleClick={(e) => {
            e.preventDefault();
            onRangeChange(null);
          }}
          onContextMenu={(e) => e.preventDefault()}
        >
          {earlier}
          {model === null && (
            <span className="ew-traj-tl__empty" data-testid="console-lane-empty">{t("console.timeline_empty")}</span>
          )}
          {hoverFraction !== null && draft === null && (
            <div
              className="ew-traj-tl__hover-line"
              data-testid="console-lane-hover-line"
              aria-hidden="true"
              style={{ "--traj-hover-left": `${hoverFraction * 100}%` } as CSSProperties}
            />
          )}
          {visible !== null && selectionStyle !== undefined && (
            <>
              <div
                className="ew-traj-tl__dim"
                data-testid="console-lane-dim"
                aria-hidden="true"
                style={{ left: 0, width: `${clampFraction(visible.start) * 100}%` }}
              />
              <div
                className="ew-traj-tl__dim"
                data-testid="console-lane-dim"
                aria-hidden="true"
                style={{ left: `${clampFraction(visible.end) * 100}%`, width: `${(1 - clampFraction(visible.end)) * 100}%` }}
              />
              <div
                className="ew-traj-tl__range"
                data-testid="console-lane-range"
                data-dragging={draft === null ? undefined : "true"}
                aria-hidden="true"
                style={selectionStyle}
              />
              <div
                className="ew-traj-tl__edges"
                data-testid="console-lane-range-edges"
                data-dragging={draft === null ? undefined : "true"}
                aria-hidden="true"
                style={selectionStyle}
              />
            </>
          )}
          {model !== null && (
            <>
              <div
                className="ew-traj-tl__boundaries"
                data-animate-viewport={animate ? "true" : undefined}
                aria-hidden="true"
                style={domainStyle}
              >
                {model.turnBoundaries
                  .filter((b) => b.time > model.start && b.time >= domainStart && b.time <= domainEnd)
                  .map((b) => (
                    <span
                      key={b.turnSeq}
                      className="ew-traj-tl__boundary"
                      data-testid="console-lane-turn-boundary"
                      data-turn={b.turnSeq}
                      style={{ "--traj-turn-left": `${(b.time - model.start) / fullDuration * 100}%` } as CSSProperties}
                    />
                  ))}
              </div>
              <TrajectoryTimelineBlocks
                model={model}
                records={records}
                projection={projection}
                selectedIndex={selectedIndex}
                hoveredIndex={hoveredIndex}
                searchMatches={searchMatches}
                focused={focused}
                animate={animate}
              />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
