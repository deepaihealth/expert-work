/**
 * RowDetailTiming tests — Task 17 of the debug-console PR-A plan. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-17-brief.md
 * for the four required `it`s (verbatim titles below).
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "../../../i18n";

import type { RunTrace, TraceSpan } from "../../../api/trace_facade";
import type { SpanMatch } from "../../../api/trace_match";
import type { ThinkRow } from "../../../api/trajectory_rows";
import { RowDetailTiming } from "../RowDetailTiming";

function thinkRow(over: Partial<ThinkRow> = {}): ThinkRow {
  return {
    id: "think:1",
    kind: "think",
    seq: 1,
    step: 1,
    status: "ok",
    durationMs: 1500,
    eventIndexes: [0],
    serverMs: 1_700_000_000_123,
    text: "reasoning",
    content: null,
    model: "gpt-x",
    inputTokens: 100,
    outputTokens: 50,
    finishReason: null,
    ...over,
  };
}

function span(over: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: "s1",
    parentId: null,
    kind: "llm",
    label: "main",
    detail: null,
    startMs: 250,
    latencyMs: 1800,
    model: "gpt-x-exact",
    inputTokens: 110,
    outputTokens: 55,
    costUsd: 0.0123,
    input: null,
    output: null,
    level: "default",
    statusMessage: null,
    purpose: "",
    group: null,
    ...over,
  };
}

function renderTiming(over: {
  match: SpanMatch;
  trace?: RunTrace | null;
  traceLoading?: boolean;
  onRefreshTrace?: () => void;
}) {
  return render(
    <RowDetailTiming
      row={thinkRow()}
      match={over.match}
      trace={over.trace ?? null}
      traceLoading={over.traceLoading ?? false}
      onRefreshTrace={over.onRefreshTrace ?? (() => {})}
    />,
  );
}

describe("RowDetailTiming", () => {
  it("matched span: two columns with SSE duration and Langfuse latency/tokens/cost", () => {
    // costUsd 0.0123 → $0.0123
    renderTiming({ match: { span: span(), reason: "matched" } });
    const table = screen.getByTestId("console-detail-timing");
    expect(table.textContent).toContain("1.5s"); // SSE durationMs
    expect(table.textContent).toContain("1.8s"); // Langfuse latencyMs
    expect(table.textContent).toContain("110 / 55"); // Langfuse tokens
    expect(table.textContent).toContain("$0.0123"); // Langfuse cost
  });

  it("no trace: loading → 加载中; not_ready → 入库中 + refresh calls onRefreshTrace; unavailable → 不可用", () => {
    // ← 1661 / 1585(UI 侧)/ 1493 迁入
    const { rerender } = render(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={null}
        traceLoading={true}
        onRefreshTrace={() => {}}
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain("Loading…");

    const onRefreshTrace = vi.fn();
    rerender(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={{ status: "not_ready" }}
        traceLoading={false}
        onRefreshTrace={onRefreshTrace}
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain(
      "Langfuse still ingesting — retrying shortly",
    );
    fireEvent.click(screen.getByTestId("console-timing-refresh"));
    expect(onRefreshTrace).toHaveBeenCalledTimes(1);

    rerender(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={{ status: "unavailable" }}
        traceLoading={false}
        onRefreshTrace={() => {}}
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain("Trace unavailable");
    expect(screen.queryByTestId("console-timing-refresh")).not.toBeInTheDocument();
  });

  it("matched span with level=error renders the Langfuse column in danger colour with the status message", () => {
    // ← 1493
    renderTiming({
      match: { span: span({ level: "error", statusMessage: "LLM call failed: timeout" }), reason: "matched" },
    });
    const message = screen.getByText("LLM call failed: timeout");
    expect(message.closest("td")?.getAttribute("style")).toContain("ew-text-danger");
    const modelCell = screen.getByText("gpt-x-exact");
    expect(modelCell.closest("td")?.getAttribute("style")).toContain("ew-text-danger");
  });

  it("count_mismatch / unsupported show their explanatory text", () => {
    const { rerender } = render(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "count_mismatch" }}
        trace={null}
        traceLoading={false}
        onRefreshTrace={() => {}}
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain(
      "Could not align with a span",
    );

    rerender(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "unsupported" }}
        trace={null}
        traceLoading={false}
        onRefreshTrace={() => {}}
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain("No span for this row");
  });
});
