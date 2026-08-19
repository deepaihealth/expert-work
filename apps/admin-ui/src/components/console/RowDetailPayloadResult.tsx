/**
 * RowDetailPayloadResult — the Payload and Result tabs' per-`TrajectoryRow`-kind
 * rendering, split out of RowDetail.tsx (Task 17 of the debug-console PR-A
 * plan) to keep that file under its 400-line budget. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-17-brief.md.
 *
 * `asMemories` / `planGoal` / `planSteps` / `reflectCritique` below are
 * copied verbatim from `StepTimeline.tsx:555-602` (not imported — PR-B
 * retires that file, so this is the only surviving copy). `planGoal` /
 * `planSteps` aren't consumed by this file today (a `PlanRow` already
 * carries typed `goal` / `plan.steps`, richer than the generic `detail`
 * shape those two read from) — they stay exported so a later dedupe pass
 * can still reach them instead of losing them a second time.
 */
import type { ReactNode } from "react";
import { Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { SseEvent } from "../../api/sessions";
import type { RunTraceIo } from "../../api/trace_facade";
import type { SpanMatch } from "../../api/trace_match";
import type { AssistantRow, TrajectoryRow, UserRow } from "../../api/trajectory_rows";
import type { FireNowResult } from "../../api/triggers";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { CopyButton } from "../CopyButton";
import { MarkdownView } from "../MarkdownView";
import { ToolCallCard } from "../ToolTimeline";
import { FullTextTrigger, type FullTextState } from "../turn/FullTextModal";

const { Text } = Typography;

/** Result tab's think row switches the raw `<pre>` reasoning for a
 *  「查看全文」 trigger past this length (matches StepTimeline/TurnCard's
 *  existing full-text-modal threshold convention). */
const FULL_TEXT_CHARS = 2000;

// ---- StepTimeline.tsx:555-602 aux detail helpers (copied, not imported). ----

interface MemoryDetailItem {
  id: string;
  kind: string;
  content: string;
  importance?: number;
  confidence?: number;
}

function record(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

export function asMemories(detail: Record<string, unknown>): MemoryDetailItem[] {
  const raw = detail.memories;
  if (!Array.isArray(raw)) return [];
  return raw.map((m, i) => {
    const o = record(m);
    return {
      id: typeof o.id === "string" ? o.id : String(i),
      kind: typeof o.kind === "string" ? o.kind : "",
      content: typeof o.content === "string" ? o.content : "",
      importance: typeof o.importance === "number" ? o.importance : undefined,
      confidence: typeof o.confidence === "number" ? o.confidence : undefined,
    };
  });
}

export function planGoal(detail: Record<string, unknown>): string | null {
  const p = record(detail.plan);
  const goal = p.goal ?? p.objective;
  return typeof goal === "string" && goal.trim() !== "" ? goal : null;
}

export function planSteps(detail: Record<string, unknown>): string[] {
  const p = record(detail.plan);
  const steps = Array.isArray(p.steps) ? p.steps : [];
  return steps.map((s) => {
    if (typeof s === "string") return s;
    const so = record(s);
    const text = so.description ?? so.text ?? so.title;
    return typeof text === "string" ? text : JSON.stringify(s);
  });
}

export function reflectCritique(detail: Record<string, unknown>): string {
  return typeof detail.critique === "string" ? detail.critique : "";
}

// ---- shared small renderers ----

function Pre({ children }: { children: string }) {
  return (
    <pre
      style={{
        margin: 0,
        fontSize: 11.5,
        fontFamily: "var(--ew-font-mono)",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        color: "var(--ew-text-secondary)",
      }}
    >
      {children}
    </pre>
  );
}

function JsonBlock({ value, copyTestId }: { value: unknown; copyTestId?: string }) {
  const json = JSON.stringify(value, null, 2);
  return (
    <div style={{ position: "relative" }}>
      {copyTestId !== undefined && (
        <span style={{ position: "absolute", right: 0, top: -4 }}>
          <CopyButton text={json} testId={copyTestId} />
        </span>
      )}
      <Pre>{json}</Pre>
    </div>
  );
}

export function RenderedIo({ io }: { io: RunTraceIo }) {
  if (io.kind === "text") return <Pre>{io.text}</Pre>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {io.messages.map((m, i) => (
        <div key={i}>
          <Tag bordered={false} style={{ margin: "0 0 4px" }}>
            {m.role}
          </Tag>
          <Pre>{m.content}</Pre>
        </div>
      ))}
    </div>
  );
}

// ---- Assistant / User 正文两视图(PR-A.2 Task 9,spec §九「详情」)----

/** 账本的 ASSISTANT 记录一条顶一整步:正文 + 思考。USER 记录复用同一组件
 *  (它没有 `reasoning`,折叠段自然不出)。 */
export type TextRow = UserRow | AssistantRow;

/** 「预览」= Markdown 正文,上方一段可折叠的思考。 */
export function AssistantPreview({ row }: { row: TextRow }) {
  const { t } = useTranslation();
  const reasoning = row.kind === "assistant" ? row.reasoning : "";
  const n = (row.kind === "assistant" ? row.reasoningTokens : undefined) ?? reasoning.length;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {reasoning !== "" && (
        <details data-testid="console-detail-thinking">
          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--ew-text-tertiary)" }}>
            {n === 0 ? t("console.detail_thinking_none") : t("console.detail_thinking", { n })}
          </summary>
          <Pre>{reasoning}</Pre>
        </details>
      )}
      <MarkdownView>{row.text}</MarkdownView>
    </div>
  );
}

/** 「原文」= 思考与正文各一段 `<pre>`,超长给「查看全文」。 */
export function AssistantRawText({
  row,
  onOpenFullText,
}: {
  row: TextRow;
  onOpenFullText: (state: FullTextState) => void;
}) {
  const { t } = useTranslation();
  const reasoning = row.kind === "assistant" ? row.reasoning : "";
  const block = (text: string, title: string): ReactNode => (
    <div>
      <Pre>{text}</Pre>
      {text.length > FULL_TEXT_CHARS && (
        <FullTextTrigger onClick={() => onOpenFullText({ title, text })} />
      )}
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {reasoning !== "" && block(reasoning, t("console.detail_thinking_none"))}
      {block(row.text, t("console.detail_tab_rawtext"))}
    </div>
  );
}

// ---- Payload tab ----

export interface RowDetailPayloadProps {
  row: TrajectoryRow;
  match: SpanMatch;
  /** Marker rows' Payload tab shows the first raw frame's ``data`` —
   *  ``row.eventIndexes[0]`` resolved against the turn's full frame list. */
  events: readonly SseEvent[];
}

export function RowDetailPayload({ row, match, events }: RowDetailPayloadProps) {
  const { t } = useTranslation();

  let body: ReactNode;
  switch (row.kind) {
    case "think": {
      const io = match.span?.input ?? null;
      body =
        io === null ? (
          <Text type="secondary">{t("console.detail_need_langfuse")}</Text>
        ) : (
          <RenderedIo io={io} />
        );
      break;
    }
    case "tool":
      body = <JsonBlock value={row.entry.args} copyTestId="console-detail-payload-copy" />;
      break;
    case "plan":
      body =
        row.source === "update_plan" ? (
          <JsonBlock
            value={{ goal: row.goal, steps: row.stepsTotal, reason: row.reason }}
            copyTestId="console-detail-payload-copy"
          />
        ) : (
          <JsonBlock value={row.plan} />
        );
      break;
    case "memory":
      body = <JsonBlock value={row.detail.memories ?? []} />;
      break;
    case "reflect":
    case "assistant":
      body = <Text type="secondary">{t("console.detail_none")}</Text>;
      break;
    case "user":
      body = (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Pre>{row.text}</Pre>
          {row.attachmentNames.length > 0 && (
            <div>
              <Text type="secondary" style={{ fontSize: 11.5 }}>
                {t("console.detail_attachments")}
              </Text>
              <ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>
                {row.attachmentNames.map((name, i) => (
                  <li key={i} style={{ fontSize: 12 }}>
                    {name}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {Object.keys(row.inputs).length > 0 && (
            <div>
              <Text type="secondary" style={{ fontSize: 11.5 }}>
                {t("console.detail_variables")}
              </Text>
              <ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>
                {Object.entries(row.inputs).map(([k, v]) => (
                  <li key={k} style={{ fontSize: 12 }}>
                    {k}: {v}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      );
      break;
    default: {
      // Marker rows (compaction / retry / error / approval / guard / gap) —
      // the first raw frame's ``data``.
      const idx = row.eventIndexes[0];
      const evt = idx !== undefined ? events[idx] : undefined;
      body =
        evt === undefined ? (
          <Text type="secondary">{t("console.detail_none")}</Text>
        ) : (
          <Pre>{typeof evt.data === "string" ? evt.data : JSON.stringify(evt.data, null, 2)}</Pre>
        );
      break;
    }
  }

  return <div data-testid="console-detail-payload">{body}</div>;
}

// ---- Result tab ----

export interface RowDetailResultProps {
  row: TrajectoryRow;
  onFireResult?: (result: FireNowResult) => void;
  onOpenFullText: (state: FullTextState) => void;
}

export function RowDetailResult({ row, onFireResult, onOpenFullText }: RowDetailResultProps) {
  const { t } = useTranslation();

  let body: ReactNode;
  switch (row.kind) {
    case "tool":
      body = <ToolCallCard entry={row.entry} onFireResult={onFireResult} />;
      break;
    case "think":
      body = (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div>
            <Pre>{row.text}</Pre>
            {row.text.length > FULL_TEXT_CHARS && (
              <FullTextTrigger
                onClick={() => onOpenFullText({ title: t("console.row_think"), text: row.text })}
              />
            )}
          </div>
          {row.content !== null && <MarkdownView>{row.content}</MarkdownView>}
        </div>
      );
      break;
    case "plan":
      body =
        row.plan === null ? (
          <Text type="secondary">{row.reason ?? t("console.detail_none")}</Text>
        ) : (
          <ol style={{ margin: 0, padding: 0, listStyle: "none" }}>
            {row.plan.steps.map((s) => (
              <li key={s.id} style={{ display: "flex", gap: 8, padding: "3px 0", fontSize: 12.5 }}>
                <span aria-hidden>
                  {s.status === "completed" ? "✓" : s.status === "in_progress" ? "◐" : "○"}
                </span>
                <span>{s.description}</span>
              </li>
            ))}
          </ol>
        );
      break;
    case "memory": {
      const memories = asMemories(row.detail);
      body = (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {memories.map((m) => (
            <div key={m.id} style={{ fontSize: 12.5 }}>
              <Tag bordered={false} style={{ margin: "0 6px 0 0" }}>
                {m.kind}
              </Tag>
              {m.content}
              {m.importance !== undefined && (
                <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                  {t("playground.tl_importance", { v: m.importance.toFixed(2) })}
                </Text>
              )}
            </div>
          ))}
        </div>
      );
      break;
    }
    case "reflect":
      body = (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <Text strong style={{ fontSize: 12.5 }}>
            {t(row.verdict === "pass" ? "console.row_reflect_pass" : "console.row_reflect_revise")}
          </Text>
          <Text style={{ fontSize: 12.5 }}>{reflectCritique(row.detail)}</Text>
        </div>
      );
      break;
    case "subagent":
      body = (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12.5 }}>
          <div>
            {t("playground.tl_worker_task")}: {row.worker.taskExcerpt}
          </div>
          {row.worker.summary !== null && (
            <div>
              {t("console.detail_worker_summary", {
                calls: row.worker.summary.llmCallCount,
                duration: fmtDuration(row.worker.summary.wallClockMs),
              })}
            </div>
          )}
          <div>
            {t("console.detail_worker_steps")}: {row.worker.steps.length}
          </div>
        </div>
      );
      break;
    case "assistant":
      // PR-A.2 Task 9(spec §九):正文 Markdown + 前置「思考」折叠段。
      body = <AssistantPreview row={row} />;
      break;
    case "user":
      body = <Text type="secondary">{t("console.detail_none")}</Text>;
      break;
    default:
      // Marker rows (compaction / retry / error / approval / guard / gap).
      body = <Text style={{ fontSize: 12.5 }}>{row.text}</Text>;
      break;
  }

  return <div data-testid="console-detail-result">{body}</div>;
}
