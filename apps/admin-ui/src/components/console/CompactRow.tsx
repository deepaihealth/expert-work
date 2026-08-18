/**
 * CompactRow — one row of the console's middle-column step timeline: a
 * one-line summary (i18n-composed in this file, not stored on the row) plus
 * an optional expanded detail view. Consumes ``api/trajectory_rows.ts``'s
 * ``CompactRow`` union (Task 4) — this file's own component is named the
 * same as that type by design (the brief's ``Produces`` block), so the type
 * is imported under an alias everywhere it's referenced.
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-10-brief.md.
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { CompactRow as CompactRowT } from "../../api/trajectory_rows";
import type { PlanStepStatus } from "../../api/plan";
import type { ToolCallEntry } from "../../api/tool_timeline";
import type { FireNowResult } from "../../api/triggers";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { ToolCallCard } from "../ToolTimeline";

type TFn = ReturnType<typeof useTranslation>["t"];

/** think / tool / plan / memory / reflect / subagent carry a detail view;
 *  marker rows (compaction / retry / error / approval / guard / gap) are a
 *  single line, nothing to expand. */
export function rowIsExpandable(row: CompactRowT): boolean {
  switch (row.kind) {
    case "think":
    case "tool":
    case "plan":
    case "memory":
    case "reflect":
    case "subagent":
      return true;
    default:
      return false;
  }
}

const STATUS_COLOR: Record<CompactRowT["status"], string> = {
  running: "var(--ew-color-brand-500, #4c8dff)",
  ok: "var(--ew-color-success-500, #52c41a)",
  error: "var(--ew-color-error-500, #f5222d)",
  warn: "var(--ew-color-warning-500, #d4a017)",
  pause: "var(--ew-color-warning-500, #d4a017)",
};

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** The first sentence of a (possibly multi-sentence) note — cuts before the
 *  first sentence-ending punctuation (Chinese or Western), else the whole
 *  (trimmed) string. */
function firstSentence(text: string): string {
  const match = /^[^。.!?！？\n]*/.exec(text);
  return (match ? match[0] : text).trim();
}

function toolResultText(entry: ToolCallEntry, t: TFn): string {
  if (entry.resultPreview === null) return t("console.row_tool_pending");
  const firstLine = entry.resultPreview.split("\n")[0];
  return entry.status === "error"
    ? `${t("console.row_tool_error")}: ${firstLine}`
    : firstLine;
}

/** The row's one-line summary. ``liveText`` only applies to (and is only
 *  meaningful for) a ``think`` row — the currently-streaming step's not-yet-
 *  settled reasoning text. */
function rowLabel(row: CompactRowT, liveText: string | undefined, t: TFn): string {
  switch (row.kind) {
    case "think": {
      if (liveText !== undefined) {
        const lines = liveText.split("\n");
        return `${t("console.row_think_live")} · ${lines[lines.length - 1]}`;
      }
      const firstLine = row.text.split("\n")[0];
      return `${t("console.row_think")} · ${firstLine}`;
    }
    case "tool": {
      const argsStr = truncate(JSON.stringify(row.entry.args), 80);
      return `${row.entry.toolName} · ${argsStr} → ${toolResultText(row.entry, t)}`;
    }
    case "plan": {
      if (row.source === "update_plan") {
        const base = t("console.row_plan_update", { n: row.stepsTotal });
        return row.reason ? `${base} · ${firstSentence(row.reason)}` : base;
      }
      return t("console.row_plan_create", { n: row.stepsTotal });
    }
    case "memory":
      return row.direction === "recall"
        ? t("console.row_memory_recall", { n: row.count })
        : t("console.row_memory_writeback", { n: row.count });
    case "reflect":
      return row.verdict === "pass"
        ? t("console.row_reflect_pass")
        : t("console.row_reflect_revise");
    case "subagent":
      return t("console.row_subagent", { name: row.worker.label });
    default:
      return row.text;
  }
}

const PLAN_STEP_GLYPH: Record<PlanStepStatus, string> = {
  pending: "○",
  in_progress: "◐",
  completed: "✓",
};

const PRE_STYLE = {
  margin: 0,
  fontSize: 11,
  fontFamily: "var(--ew-font-mono)",
  whiteSpace: "pre-wrap" as const,
  wordBreak: "break-word" as const,
  maxHeight: 240,
  overflow: "auto" as const,
};

function RowDetail({
  row,
  onFireResult,
}: {
  row: CompactRowT;
  onFireResult?: (r: FireNowResult) => void;
}): ReactNode {
  switch (row.kind) {
    case "think":
      return <pre style={PRE_STYLE}>{row.text}</pre>;
    case "tool":
      return <ToolCallCard entry={row.entry} onFireResult={onFireResult} />;
    case "plan":
      if (row.plan !== null) {
        return (
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
            {row.plan.steps.map((s) => (
              <li key={s.id}>
                {PLAN_STEP_GLYPH[s.status]} {s.description}
              </li>
            ))}
          </ol>
        );
      }
      return (
        <div style={{ fontSize: 12 }}>
          {row.goal && <div>{row.goal}</div>}
          {row.reason && <div>{row.reason}</div>}
        </div>
      );
    case "memory":
    case "reflect":
      return <pre style={PRE_STYLE}>{JSON.stringify(row.detail, null, 2)}</pre>;
    case "subagent":
      return (
        <div style={{ fontSize: 12 }}>
          <div>{row.worker.taskExcerpt}</div>
          <div>{row.worker.steps.length}</div>
        </div>
      );
    default:
      return null;
  }
}

export interface CompactRowProps {
  row: CompactRowT;
  expanded: boolean;
  onToggle: () => void;
  /** 流式:think 行的实时文本(仅 live 轮当前步)。 */
  liveText?: string;
  /** 行尾「检查」→ 右栏选中本轮并定位到同 id 的轨迹行(TurnBlock 包成
   *  `() => onInspectRow(turn.key, row.id)`)。Omitted → the button doesn't
   *  render. */
  onInspect?: () => void;
  /** 透传给 ToolCallCard(见 ToolTimeline.tsx 的「立即触发」按钮)。 */
  onFireResult?: (r: FireNowResult) => void;
}

export function CompactRow({
  row,
  expanded,
  onToggle,
  liveText,
  onInspect,
  onFireResult,
}: CompactRowProps) {
  const { t } = useTranslation();
  const expandable = rowIsExpandable(row);
  const label = rowLabel(row, row.kind === "think" ? liveText : undefined, t);

  const headContent = (
    <>
      <span
        aria-hidden
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          flexShrink: 0,
          background: STATUS_COLOR[row.status],
        }}
      />
      <span
        style={{
          flex: 1,
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontSize: 12,
        }}
      >
        {label}
      </span>
      {row.durationMs !== null && (
        <span style={{ fontSize: 11, opacity: 0.6, flexShrink: 0 }}>
          {fmtDuration(row.durationMs)}
        </span>
      )}
      {onInspect && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onInspect();
          }}
          data-testid="console-row-inspect"
          style={{
            border: 0,
            background: "transparent",
            color: "var(--ew-text-info, #4c8dff)",
            cursor: "pointer",
            fontSize: 11,
            flexShrink: 0,
          }}
        >
          {t("console.row_inspect")}
        </button>
      )}
    </>
  );

  const rowStyle = {
    display: "flex",
    alignItems: "center",
    gap: 6,
    padding: "3px 6px",
    width: "100%",
    textAlign: "left" as const,
  };

  return (
    <div>
      {expandable ? (
        <div
          role="button"
          tabIndex={0}
          aria-expanded={expanded}
          onClick={onToggle}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onToggle();
            }
          }}
          data-testid={`console-row-${row.kind}`}
          data-status={row.status}
          style={{ ...rowStyle, cursor: "pointer" }}
        >
          {headContent}
        </div>
      ) : (
        <div
          data-testid={`console-row-${row.kind}`}
          data-status={row.status}
          style={rowStyle}
        >
          {headContent}
        </div>
      )}
      {expandable && expanded && (
        <div data-testid="console-row-detail" style={{ padding: "4px 6px 6px 18px" }}>
          <RowDetail row={row} onFireResult={onFireResult} />
        </div>
      )}
    </div>
  );
}
