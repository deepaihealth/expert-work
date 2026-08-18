/**
 * Transcript — the debug console's middle-column turn timeline: the scrolling
 * container that renders every ``ConsoleTurn`` as a ``TurnBlock``, the
 * degraded flat-history fallback (count mismatch or a failed replay —
 * ``PlaygroundTab.tsx:1379-1428``'s ``playground-history`` block, ported
 * verbatim), the history/live divider, the empty state, and the trailing
 * fire-now task-result cards. Auto-scrolls to the bottom as turns or live
 * frames arrive, unless the reader has scrolled up to read earlier history
 * (``TrajectoryRows.tsx``'s same 80px-slack rule).
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-11-brief.md.
 */
import { useEffect, useRef, type JSX } from "react";
import { Empty } from "antd";
import { useTranslation } from "react-i18next";

import type { ApprovalItem } from "../../api/approvals";
import type { RateCardRecord } from "../../api/rate_card";
import type { HistoryMessage } from "../../api/sessions";
import type { FireNowResult } from "../../api/triggers";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { MarkdownView } from "../MarkdownView";
import { CommentarySegmentLine } from "../turn/TurnCard";
import { HistoryDivider } from "../turn/HistoryDivider";
import { TaskResultCard } from "../turn/TaskResultCard";
import type { Turn } from "../turn/types";
import { TurnBlock } from "./TurnBlock";
import type { ConsoleTurn } from "./types";

export interface TranscriptProps {
  turns: readonly ConsoleTurn[];
  /** 历史降级块(计数对不上时的扁平文本);非空且 turns 里没有历史轮时渲染,
   *  沿用 ``PlaygroundTab.tsx:1379-1428`` 的样式与 ``playground-history``
   *  testid。 */
  flatHistory: readonly HistoryMessage[];
  taskResults: readonly FireNowResult[];
  threadId: string | null;
  /** null = 跟随最新。 */
  selectedKey: string | null;
  onSelectTurn: (key: string) => void;
  /** 透传 TurnBlock。 */
  onInspectRow: (turnKey: string, rowId: string) => void;
  streamTurnKey: string | null;
  liveByStep: ReadonlyMap<number, LiveStep>;
  registerHistoryRow: (
    runId: string,
    threadId: string,
  ) => (el: HTMLElement | null) => void;
  // 透传给 TurnBlock 的一组回调 / 标志(同上)。
  rate: RateCardRecord | null;
  isSystemAdmin: boolean;
  readOnly: boolean;
  isTenantSwitched: boolean;
  onDecide: (
    turnId: string,
    approval: ApprovalItem,
    decision: "approve" | "reject",
  ) => void;
  deciding: boolean;
  onExport: (turn: Turn) => void;
  exportingKey: string | null;
  onRetryLive?: (turn: Turn) => void;
  onRetryHistory?: (turn: Turn) => void;
  onDownloadArtifact: (name: string) => Promise<void>;
  onFireResult?: (r: FireNowResult) => void;
}

/** How close to the bottom (px) still counts as "hasn't scrolled up" —
 *  same rule as ``TrajectoryRows.tsx``'s ``AUTO_SCROLL_SLACK_PX``. */
const AUTO_SCROLL_SLACK_PX = 80;

export function Transcript(props: TranscriptProps): JSX.Element {
  const {
    turns,
    flatHistory,
    taskResults,
    threadId,
    selectedKey,
    onSelectTurn,
    onInspectRow,
    streamTurnKey,
    liveByStep,
    registerHistoryRow,
    rate,
    isSystemAdmin,
    readOnly,
    isTenantSwitched,
    onDecide,
    deciding,
    onExport,
    exportingKey,
    onRetryLive,
    onRetryHistory,
    onDownloadArtifact,
    onFireResult,
  } = props;
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the bottom as turns settle or live frames arrive —
  // unless the reader has scrolled away from the bottom to read earlier
  // history (``liveByStep`` is a fresh ``Map`` each token flush, so its
  // reference changing is exactly "a live frame landed").
  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const nearBottom =
      node.scrollHeight - node.scrollTop - node.clientHeight <=
      AUTO_SCROLL_SLACK_PX;
    if (nearBottom) node.scrollTop = node.scrollHeight;
  }, [turns, liveByStep]);

  const historyPart = turns.filter((turn) => turn.source === "history");
  const livePart = turns.filter((turn) => turn.source === "live");
  const showFlatFallback = flatHistory.length > 0 && historyPart.length === 0;
  const isEmpty = turns.length === 0 && flatHistory.length === 0;
  const selected = selectedKey ?? turns.at(-1)?.key ?? null;

  return (
    <div
      ref={containerRef}
      data-testid="playground-transcript"
      // 可滚动区域必须能被键盘聚焦(axe scrollable-region-focusable):三栏壳把
      // 高度钉死后这一列在空态也会溢出,而空态里没有任何可聚焦子元素。
      role="region"
      aria-label={t("playground.transcript_label")}
      tabIndex={0}
      style={{
        flex: 1,
        minHeight: 0,
        padding: 12,
        overflow: "auto",
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      {isEmpty && (
        <Empty
          description={t("console.no_turns")}
          style={{ marginTop: 64 }}
          data-testid="playground-empty-log"
        />
      )}

      {showFlatFallback && (
        <div
          data-testid="playground-history"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            flexShrink: 0,
          }}
        >
          {flatHistory.map((m, idx) => (
            <div
              key={idx}
              style={{
                alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                maxWidth: "85%",
                padding: "6px 10px",
                borderRadius: 8,
                fontSize: 13,
                whiteSpace: "pre-wrap",
                background:
                  m.role === "user" ? "var(--ew-surface-raised)" : "transparent",
                border:
                  m.role === "user" ? "1px solid var(--ew-border-subtle)" : "none",
                opacity: 0.75,
              }}
            >
              {m.role === "user" ? (
                m.content
              ) : m.channel === "commentary" ? (
                <CommentarySegmentLine
                  text={m.content}
                  label={t("playground.segment_commentary")}
                />
              ) : (
                <MarkdownView>{m.content}</MarkdownView>
              )}
            </div>
          ))}
          <HistoryDivider />
        </div>
      )}

      {historyPart.map((turn) => (
        <TurnBlock
          key={turn.key}
          turn={turn}
          threadId={threadId}
          selected={turn.key === selected}
          onSelect={onSelectTurn}
          onInspectRow={onInspectRow}
          rate={rate}
          isSystemAdmin={isSystemAdmin}
          readOnly={readOnly}
          isTenantSwitched={isTenantSwitched}
          onDecide={onDecide}
          deciding={deciding}
          onExport={onExport}
          exporting={exportingKey === turn.key}
          onRetry={onRetryHistory}
          onDownloadArtifact={onDownloadArtifact}
          onFireResult={onFireResult}
          rowRef={
            turn.runId !== null
              ? registerHistoryRow(turn.runId, threadId ?? "")
              : undefined
          }
        />
      ))}
      {historyPart.length > 0 && <HistoryDivider />}

      {livePart.map((turn) => (
        <TurnBlock
          key={turn.key}
          turn={turn}
          threadId={threadId}
          selected={turn.key === selected}
          onSelect={onSelectTurn}
          onInspectRow={onInspectRow}
          liveByStep={turn.key === streamTurnKey ? liveByStep : undefined}
          rate={rate}
          isSystemAdmin={isSystemAdmin}
          readOnly={readOnly}
          isTenantSwitched={isTenantSwitched}
          onDecide={onDecide}
          deciding={deciding}
          onExport={onExport}
          exporting={exportingKey === turn.key}
          onRetry={onRetryLive}
          onDownloadArtifact={onDownloadArtifact}
          onFireResult={onFireResult}
        />
      ))}

      {taskResults.map((result) => (
        <TaskResultCard key={result.run_id} result={result} />
      ))}
    </div>
  );
}
