/**
 * TrajectoryLedger —— 轨迹视图的账本表格(spec §九「账本」):整个会话一张两列
 * 表(事件槽 122px | 内容 1fr),行序即事件序,固定 27px 行高 + 视口虚拟化。
 * 事件槽里挂轮标签 / 当前轮竖轨 / 选中竖轨 / 每次 LLM 请求一枚圆点 / 类型标签;
 * 内容列一行省略;折叠的轮与折叠的调用各自缩成一行摘要。纯展示组件 —— 折叠集、
 * 选中、选区都由父级(Task 10 的 `TrajectoryView`)持有,这里只回调。
 *
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。见
 * .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-8-brief.md。
 */
import type { JSX, KeyboardEvent } from "react";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";

import type { DisplayRow } from "./ledger_collapse";
import type { LedgerRecord, LedgerRequest } from "./ledger_types";
import { LedgerRow } from "./TrajectoryLedgerRow";
import { useVirtualRows } from "./use_virtual_rows";
import "./kind_tag.css";
import "./trajectory_ledger.css";

/** 固定行高(CSS 里的 `--ew-ledger-row` 同值)—— 虚拟化按它算窗口与 spacer。 */
const ROW_HEIGHT_PX = 27;
const OVERSCAN_ROWS = 12;
/** 离底多少像素之内仍算「没上滚」(与 `Transcript.tsx` 同一条规则)。 */
const AUTO_SCROLL_SLACK_PX = 80;

export interface TrajectoryLedgerProps {
  rows: readonly DisplayRow[];
  requestsByRecordId: ReadonlyMap<string, LedgerRequest>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  selectedRequestNo: number | null;
  onSelectRequest: (no: number) => void;
  hoveredId: string | null;
  onHover: (id: string | null) => void;
  /** 时间轴选区求交的记录 index 集;null = 无选区。**引用必须稳定**(调用方
   *  `useMemo`):选区一变账本就把段内首行拽进视口,每渲染一次换一个新 Set
   *  等于每帧都把读者的视口抢走。 */
  focusIndexes: ReadonlySet<number> | null;
  /** 当前轮(选中记录所在轮,或最新轮)。 */
  activeTurnKey: string | null;
  onToggleTurn: (turnKey: string) => void;
  onToggleOwner: (ownerId: string) => void;
  running: boolean;
  hasEarlier: boolean;
  earlierCount: number;
  loadingEarlier: boolean;
  onLoadEarlier: () => void;
  /** 一次性滚动请求(nonce 变才动)。 */
  scrollTo: { id: string; nonce: number } | null;
  loading: boolean;
}

/** 双击落点要知道:哪几轮此刻是折叠的、哪几条 assistant 名下有子记录、每轮有
 *  几条非 USER 记录(只有 ≥ 2 条的轮才值得折)。一趟扫完 `rows` 全拿到。 */
function foldContextOf(rows: readonly DisplayRow[]): {
  collapsedTurns: ReadonlySet<string>;
  ownersWithChildren: ReadonlySet<string>;
  nonUserByTurn: ReadonlyMap<string, number>;
} {
  const collapsedTurns = new Set<string>();
  const ownersWithChildren = new Set<string>();
  const nonUserByTurn = new Map<string, number>();
  for (const row of rows) {
    if (row.kind === "turn-summary") {
      collapsedTurns.add(row.turnKey);
      continue;
    }
    if (row.kind === "calls-summary") {
      // 折叠状态下子记录已经不在 `rows` 里,主人只能从摘要行认出来。
      ownersWithChildren.add(row.ownerId);
      continue;
    }
    const { record } = row;
    // 与 `ledger_collapse.childrenOf` 同一口径:只有 tool / plan 算主人的子调用;
    // reflect / memory 写回也带 parentId(详情层级链接),但不是调用,不能让
    // 「唯一子记录是 reflect」的 assistant 被判成有调用可折 —— 那会把双击吃掉。
    if (record.parentId !== null && (record.kind === "tool" || record.kind === "plan")) {
      ownersWithChildren.add(record.parentId);
    }
    if (record.kind !== "user") {
      nonUserByTurn.set(record.turnKey, (nonUserByTurn.get(record.turnKey) ?? 0) + 1);
    }
  }
  return { collapsedTurns, ownersWithChildren, nonUserByTurn };
}

/** 请求号 → 开启它的那条 assistant 记录 id(`requestsByRecordId` 反查)。 */
function requestRecordId(
  requestsByRecordId: ReadonlyMap<string, LedgerRequest>,
  no: number | null,
): string | null {
  if (no === null) return null;
  for (const [recordId, request] of requestsByRecordId) {
    if (request.no === no) return recordId;
  }
  return null;
}

export function TrajectoryLedger(props: TrajectoryLedgerProps): JSX.Element {
  const {
    rows, requestsByRecordId, selectedId, onSelect, selectedRequestNo, onSelectRequest,
    hoveredId, onHover, focusIndexes, activeTurnKey, onToggleTurn, onToggleOwner,
    running, hasEarlier, earlierCount, loadingEarlier, onLoadEarlier, scrollTo, loading,
  } = props;
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevRowCountRef = useRef(rows.length);
  const initialTailDoneRef = useRef(false);

  const { start, end, topPad, bottomPad } = useVirtualRows({
    scrollRef, count: rows.length, rowHeight: ROW_HEIGHT_PX, overscan: OVERSCAN_ROWS,
  });

  const recordIds = useMemo(
    () => rows.flatMap((row) => (row.kind === "record" ? [row.record.id] : [])),
    [rows],
  );
  /** 记录 id → 它在 `rows` 里的下标(不是 `record.index`:折叠会让两者分家)。
   *  虚拟窗口外的行没有 DOM 节点,只能靠这个下标算滚动位置。 */
  const rowIndexById = useMemo(() => {
    const map = new Map<string, number>();
    rows.forEach((row, at) => {
      if (row.kind === "record") map.set(row.record.id, at);
    });
    return map;
  }, [rows]);
  const foldContext = useMemo(() => foldContextOf(rows), [rows]);

  /** 按 `data-record-id` 找已渲染的那一行 —— 记录 id 里有 `/` 和 `:`,直接塞进
   *  属性选择器要转义,遍历反而是最不容易写错的写法。虚拟化窗口外返回 null。 */
  const rowElement = useCallback((id: string): HTMLElement | null => {
    const root = scrollRef.current;
    if (root === null) return null;
    const nodes = Array.from(root.querySelectorAll<HTMLElement>('[data-testid="console-traj-row"]'));
    return nodes.find((node) => node.dataset.recordId === id) ?? null;
  }, []);

  /** 把某一行带到眼前。行渲染出来了就用 `scrollIntoView`(jsdom 没有它,一律
   *  可选调用);**没渲染出来**就自己算 —— 虚拟化只挂视口 + overscan,窗口外
   *  的目标在 DOM 里根本不存在,只扫已渲染的行会静默什么都不做、且永远不会
   *  收敛(滚动没发生 → 窗口不变 → 行还是不在)。设 `scrollTop` 之后
   *  `useVirtualRows` 会因 scroll 事件重算窗口,目标行随即挂上。 */
  const scrollToRow = useCallback(
    (id: string, block: "nearest" | "center"): void => {
      const el = rowElement(id);
      if (el !== null) {
        el.scrollIntoView?.({ block });
        return;
      }
      const node = scrollRef.current;
      const at = rowIndexById.get(id);
      if (node === null || at === undefined) return;
      // 「加载更早」那一行也占一行高,算偏移时不能漏。
      const top = (at + (hasEarlier ? 1 : 0)) * ROW_HEIGHT_PX;
      node.scrollTop = Math.max(block === "center" ? top - node.clientHeight / 2 : top, 0);
    },
    [rowElement, rowIndexById, hasEarlier],
  );

  // 选中行滚进视口。依赖只留 `selectedId`:effect 的回调每次渲染都会被 React
  // 换成最新的一份,所以真跑起来时闭包里的 `scrollToRow` 就是当前这次渲染的,
  // 把它写进依赖只会让「`rows` 变了」也去抢视口。
  useEffect(() => {
    if (selectedId === null) return;
    scrollToRow(selectedId, "nearest");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // 时间轴刚定格一个选区 → 把段内第一行带到眼前。
  useEffect(() => {
    if (focusIndexes === null || focusIndexes.size === 0) return;
    const first = rows.find((row) => row.kind === "record" && focusIndexes.has(row.record.index));
    if (first === undefined || first.kind !== "record") return;
    scrollToRow(first.record.id, "nearest");
    // `rows` 每次实时刷新都换新数组,跟着它跑会把「选区变了」变成「每帧滚一次」;
    // 这个效果只该由选区本身触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusIndexes]);

  // 一次性滚动请求:只认 nonce,同一个 nonce 重渲染不重复滚。
  useEffect(() => {
    if (scrollTo === null) return;
    scrollToRow(scrollTo.id, "center");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollTo?.nonce]);

  // §九「尾随:**初始**与运行中跟到最新」—— 首批行落地时对齐到最新一条,只做
  // 一次。有人明确指了要看哪一行(`scrollTo` / 选区 / 已选中)时让位:那几条
  // 请求带着读者的意图,尾随只是默认落点。
  useEffect(() => {
    if (initialTailDoneRef.current || rows.length === 0) return;
    initialTailDoneRef.current = true;
    if (scrollTo !== null || focusIndexes !== null || selectedId !== null) return;
    const node = scrollRef.current;
    if (node !== null) node.scrollTop = node.scrollHeight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows.length]);

  // 运行中长出新行时跟到底,除非读者已经上滚去看历史了。
  useEffect(() => {
    const grew = rows.length > prevRowCountRef.current;
    prevRowCountRef.current = rows.length;
    if (!running || !grew) return;
    const node = scrollRef.current;
    if (node === null) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight <= AUTO_SCROLL_SLACK_PX;
    if (nearBottom) node.scrollTop = node.scrollHeight;
  }, [rows.length, running]);

  const handleDoubleClick = useCallback(
    (record: LedgerRecord): void => {
      const { collapsedTurns, ownersWithChildren, nonUserByTurn } = foldContext;
      if (collapsedTurns.has(record.turnKey)) {
        onToggleTurn(record.turnKey);
        return;
      }
      if (record.kind === "assistant" && ownersWithChildren.has(record.id)) {
        onToggleOwner(record.id);
        return;
      }
      // 不限轮首行 —— 双击账本里这一轮的**任意**一行都该折得动它;只有非 USER
      // 记录不到 2 条的轮不值得折(折了也省不出一行)。
      if ((nonUserByTurn.get(record.turnKey) ?? 0) >= 2) onToggleTurn(record.turnKey);
    },
    [foldContext, onToggleTurn, onToggleOwner],
  );

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    // 只在**看得见的**记录行之间走:折叠掉的记录不在 `recordIds` 里,跳过去
    // 等于把选中扔进一个没有行的坐标。只点了请求圆点时 `selectedId` 是 null,
    // 但读者眼里的落点不是首行 —— 是那次请求的 assistant 记录。
    const anchorId = selectedId ?? requestRecordId(requestsByRecordId, selectedRequestNo);
    const at = anchorId === null ? -1 : recordIds.indexOf(anchorId);
    const delta = event.key === "ArrowDown" ? 1 : -1;
    const next = at === -1 ? 0 : Math.min(Math.max(at + delta, 0), recordIds.length - 1);
    const id = recordIds[next];
    if (id !== undefined) onSelect(id);
  };

  const headerOffset = hasEarlier ? 1 : 0;

  return (
    // 滚动容器无条件渲染:虚拟化挂在它的实时几何上,loading 态换掉它会让
    // `useVirtualRows` 从头量一遍(粘性覆盖层因此浮在表格上,而不是替掉它)。
    // `tabIndex` 兼顾 axe 的 scrollable-region-focusable 与容器级 ↑ ↓。
    <div ref={scrollRef} className="ew-ledger" data-testid="console-traj-ledger" tabIndex={0} onKeyDown={handleKeyDown}>
      {loading && (
        <div className="ew-ledger__loading" data-testid="console-traj-loading" role="status" aria-live="polite">
          <span className="ew-ledger__loading-bar">
            <span className="ew-ledger__spinner" aria-hidden="true" />
            {t("console.ledger_loading")}
          </span>
        </div>
      )}
      <table
        className="ew-ledger__table"
        role="grid"
        aria-label={t("console.ledger_aria")}
        aria-rowcount={rows.length + headerOffset}
      >
        <colgroup>
          <col className="ew-ledger__col-event" />
          <col />
        </colgroup>
        <tbody>
          {hasEarlier && (
            <tr className="ew-ledger__earlier" role="row" aria-rowindex={1}>
              <td colSpan={2}>
                <button
                  type="button"
                  className="ew-ledger__earlier-btn"
                  data-testid="console-traj-load-earlier"
                  disabled={loadingEarlier}
                  onClick={onLoadEarlier}
                >
                  {loadingEarlier && <span className="ew-ledger__spinner" aria-hidden="true" />}
                  {loadingEarlier
                    ? t("console.ledger_loading_earlier")
                    : t("console.ledger_load_earlier", { n: earlierCount })}
                </button>
              </td>
            </tr>
          )}
          {topPad > 0 && (
            <tr aria-hidden="true" data-virtual-spacer="top" className="ew-ledger__spacer">
              <td colSpan={2} style={{ height: `${topPad}px` }} />
            </tr>
          )}
          {rows.slice(start, end).map((row, i) => {
            const key = row.kind === "record"
              ? row.record.id
              : row.kind === "turn-summary"
                ? `turn-summary:${row.turnKey}`
                : `calls-summary:${row.ownerId}`;
            const request = row.kind === "record" ? requestsByRecordId.get(row.record.id) : undefined;
            const turnKey = row.kind === "record" ? row.record.turnKey : row.turnKey;
            return (
              <LedgerRow
                key={key}
                row={row}
                ariaRowIndex={start + i + 1 + headerOffset}
                request={request}
                selected={row.kind === "record" && row.record.id === selectedId}
                hovered={row.kind === "record" && row.record.id === hoveredId}
                focus={row.kind !== "record" || focusIndexes === null
                  ? undefined
                  : focusIndexes.has(row.record.index) ? "inside" : "outside"}
                activeTurn={turnKey === activeTurnKey}
                requestActive={request !== undefined && request.no === selectedRequestNo}
                onSelect={onSelect}
                onSelectRequest={onSelectRequest}
                onHover={onHover}
                onDoubleClickRecord={handleDoubleClick}
                onToggleTurn={onToggleTurn}
                onToggleOwner={onToggleOwner}
                t={t}
              />
            );
          })}
          {bottomPad > 0 && (
            <tr aria-hidden="true" data-virtual-spacer="bottom" className="ew-ledger__spacer">
              <td colSpan={2} style={{ height: `${bottomPad}px` }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
