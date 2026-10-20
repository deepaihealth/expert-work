/**
 * RowDetailTiming tests — Task 17 of the debug-console PR-A plan. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-17-brief.md
 * for the four required `it`s (verbatim titles below).
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import i18n from "../../../i18n";

import { rateKey, type RateBook } from "../../../api/cost";
import type { RunTrace, TraceSpan } from "../../../api/trace_facade";
import type { SpanMatch } from "../../../api/trace_match";
import type { AssistantRow, ThinkRow, ToolRow, TrajectoryRow } from "../../../api/trajectory_rows";
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

/** 账本投影(spec §九 D2)一步一条 assistant 行 —— 模型 / tokens 从 think 行
 *  搬到了它身上,SSE 列必须照样取到值。 */
function assistantRow(over: Partial<AssistantRow> = {}): AssistantRow {
  return {
    id: "assistant:1",
    kind: "assistant",
    seq: 1,
    step: 1,
    status: "ok",
    durationMs: 1500,
    eventIndexes: [0],
    serverMs: 1_700_000_000_123,
    text: "结论",
    reasoning: "想一想",
    model: "gpt-a",
    inputTokens: 200,
    outputTokens: 60,
    finishReason: "stop",
    toolCallCount: 1,
    toolCalls: [],
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

  it("incomplete (B-201): 数据不完整 + 去 Langfuse 查看 link, no refresh; no link without a URL", () => {
    const { rerender } = render(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={{ status: "incomplete" }}
        traceLoading={false}
        onRefreshTrace={() => {}}
        langfuseUrl="https://lf/trace/t1"
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain("Incomplete data");
    const link = screen.getByTestId("console-timing-langfuse");
    expect(link).toHaveAttribute("href", "https://lf/trace/t1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.textContent).toContain("View in Langfuse");
    expect(screen.queryByTestId("console-timing-refresh")).not.toBeInTheDocument();

    // Non-admins get no deep link (Langfuse has no tenant isolation) — the message alone.
    rerender(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={{ status: "incomplete" }}
        traceLoading={false}
        onRefreshTrace={() => {}}
        langfuseUrl={null}
      />,
    );
    expect(screen.getByTestId("console-detail-timing").textContent).toContain("Incomplete data");
    expect(screen.queryByTestId("console-timing-langfuse")).not.toBeInTheDocument();

    // The link belongs to `incomplete` only: `not_ready` keeps just its refresh button.
    rerender(
      <RowDetailTiming
        row={thinkRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={{ status: "not_ready" }}
        traceLoading={false}
        onRefreshTrace={() => {}}
        langfuseUrl="https://lf/trace/t1"
      />,
    );
    expect(screen.queryByTestId("console-timing-langfuse")).not.toBeInTheDocument();
  });

  it("incomplete shows 数据不完整 / 去 Langfuse 查看 in zh-CN", async () => {
    const before = i18n.language;
    await i18n.changeLanguage("zh-CN");
    try {
      render(
        <RowDetailTiming
          row={thinkRow()}
          match={{ span: null, reason: "no_trace" }}
          trace={{ status: "incomplete" }}
          traceLoading={false}
          onRefreshTrace={() => {}}
          langfuseUrl="https://lf/trace/t1"
        />,
      );
      expect(screen.getByTestId("console-detail-timing").textContent).toContain("数据不完整");
      expect(screen.getByTestId("console-timing-langfuse").textContent).toContain("去 Langfuse 查看");
    } finally {
      await i18n.changeLanguage(before);
    }
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

  it("assistant 行(账本一步一条)也在 SSE 列显示模型与 tokens", () => {
    render(
      <RowDetailTiming
        row={assistantRow()}
        match={{ span: null, reason: "no_trace" }}
        trace={null}
        traceLoading={false}
        onRefreshTrace={() => {}}
      />,
    );
    const table = screen.getByTestId("console-detail-timing");
    expect(table.textContent).toContain("gpt-a");
    expect(table.textContent).toContain("200 / 60");
  });
});

// B-202 —— 用户拍板:成本按平台价目表、人民币算。原来只取 Langfuse 的 costUsd,
// Langfuse 没数据时整行「—」。
describe("RowDetailTiming — 成本按平台价目表(B-202)", () => {
  // 3 / 0.3 / 15 元每百万 token(输入 / 缓存命中 / 输出)。
  const BOOK: RateBook = {
    agentKey: rateKey("zhipu", "glm-5.3"),
    byModel: new Map([
      [
        rateKey("zhipu", "glm-5.3"),
        {
          id: "rc-1", tenant_id: null, provider: "zhipu", model: "glm-5.3",
          input_per_mtok_micros: 3_000_000, output_per_mtok_micros: 15_000_000,
          cache_creation_per_mtok_micros: 0, cache_read_per_mtok_micros: 300_000,
        },
      ],
    ]),
  };
  /** 「成本」那一行的各个单元格文本(Langfuse 列整列一条说明时只剩 SSE 列)。 */
  function costCells(): string[] {
    const tr = screen.getByText("Cost").closest("tr");
    return [...(tr?.querySelectorAll("td") ?? [])].map((td) => td.textContent ?? "");
  }
  const toolRow: ToolRow = {
    id: "tool:1:0", kind: "tool", seq: 1, step: 1, status: "ok", durationMs: 300,
    eventIndexes: [], serverMs: null,
    entry: {
      id: "c1", rawName: "web_search", isMcp: false, server: null, toolName: "web_search",
      args: {}, status: "success", resultPreview: "ok", durationMs: 300,
    },
  };
  const noTrace = { match: { span: null, reason: "no_trace" } as SpanMatch, trace: { status: "unavailable" } as RunTrace };

  it("模型调用行:按平台价目表折成人民币,Langfuse 拿不到也有数", () => {
    render(
      <RowDetailTiming
        row={assistantRow({ inputTokens: 200_000, outputTokens: 10_000, cacheReadTokens: 100_000 })}
        {...noTrace}
        traceLoading={false}
        onRefreshTrace={() => {}}
        rateBook={BOOK}
      />,
    );
    // (200k − 100k) × 3 + 100k × 0.3 + 10k × 15,每百万 → ¥0.48
    expect(costCells()).toEqual(["¥0.4800"]);
  });

  it("没有价目表(非 system_admin)→ 「—」;非模型调用的行没有成本这一行(B-203)", () => {
    const { unmount } = render(
      <RowDetailTiming
        row={assistantRow()}
        {...noTrace}
        traceLoading={false}
        onRefreshTrace={() => {}}
        rateBook={null}
      />,
    );
    expect(costCells()).toEqual(["—"]);
    unmount();
    render(
      <RowDetailTiming
        row={toolRow}
        {...noTrace}
        traceLoading={false}
        onRefreshTrace={() => {}}
        rateBook={BOOK}
      />,
    );
    expect(screen.queryByText("Cost")).not.toBeInTheDocument();
  });
});

// B-203 —— 计时页排版:标签列有左边距;有开始时刻;tokens 标明输入 / 输出、带千分位;
// 工具行不列模型 / tokens / 成本(原来三行全是「—」),看图工具单列看图模型用量。
describe("RowDetailTiming — 排版(B-203)", () => {
  /** 与组件同一种钟面写法(本地时区 HH:mm:ss.SSS)。 */
  function clock(ms: number): string {
    const d = new Date(ms);
    const pad = (n: number, len = 2): string => String(n).padStart(len, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }
  /** 第 `label` 行的各单元格文本(th 之后的 td)。 */
  function cellsOf(label: string): string[] {
    const tr = screen.getByText(label).closest("tr");
    return [...(tr?.querySelectorAll("td") ?? [])].map((td) => td.textContent ?? "");
  }
  const tool: ToolRow = {
    id: "tool:1:0", kind: "tool", seq: 1, step: 1, status: "ok", durationMs: 300,
    eventIndexes: [], serverMs: 1_700_000_000_500,
    entry: {
      id: "c1", rawName: "web_search", isMcp: false, server: null, toolName: "web_search",
      args: {}, status: "success", resultPreview: "ok", durationMs: 300,
    },
  };
  const renderRow = (row: TrajectoryRow, extra: Partial<Parameters<typeof RowDetailTiming>[0]> = {}) =>
    render(
      <RowDetailTiming
        row={row}
        match={{ span: null, reason: "no_trace" }}
        trace={null}
        traceLoading={false}
        onRefreshTrace={() => {}}
        {...extra}
      />,
    );

  it("标签列有 14px 左边距,与概要等其它 tab 对齐", () => {
    renderRow(assistantRow());
    expect(screen.getByText("Latency").closest("th")?.style.padding).toBe("4px 8px 4px 14px");
  });

  it("开始时刻:优先账本排出的起点,否则结束 − 时长", () => {
    const row = assistantRow({ serverMs: 1_700_000_000_123, durationMs: 1_500 });
    const first = renderRow(row);
    expect(cellsOf("Started at")[0]).toBe(clock(1_700_000_000_123 - 1_500));
    expect(cellsOf("Ended at")[0]).toBe(clock(1_700_000_000_123));
    first.unmount();
    renderRow(row, { startedAtMs: 1_699_999_990_000 });
    expect(cellsOf("Started at")[0]).toBe(clock(1_699_999_990_000));
  });

  it("结束时刻一行:Langfuse 列给相对 trace 起点的结束(起点 + 时长)", () => {
    renderRow(assistantRow({ serverMs: 1_700_000_000_123 }), {
      match: { span: span({ startMs: 1_000, latencyMs: 2_000 }), reason: "matched" },
    });
    expect(cellsOf("Ended at")).toEqual([clock(1_700_000_000_123), "End (from trace start): 3.0s"]);
  });

  it("tokens 标明输入 / 输出并带千分位(SSE 与 Langfuse 两列)", () => {
    renderRow(assistantRow({ inputTokens: 57_803, outputTokens: 2_364 }), {
      match: { span: span({ inputTokens: 57_810, outputTokens: 2_364 }), reason: "matched" },
    });
    expect(cellsOf("Tokens (in / out)")).toEqual(["57,803 / 2,364", "57,810 / 2,364"]);
  });

  it("工具行:只有开始 / 结束 / 时长,不列模型 / tokens / 成本", () => {
    const { container } = renderRow(tool);
    expect(screen.queryByText("Model")).not.toBeInTheDocument();
    expect(screen.queryByText("Tokens (in / out)")).not.toBeInTheDocument();
    expect(screen.queryByText("Cost")).not.toBeInTheDocument();
    // Langfuse 那列的整列说明跨的行数跟着变(3 行),不多跨出空行。
    expect(container.querySelector("td[rowspan]")?.getAttribute("rowspan")).toBe("3");
  });

  it("看图工具:单列一行看图模型的 tokens(只有 token 数,模型名不在帧里)", () => {
    const { container } = renderRow({ ...tool, entry: { ...tool.entry, toolName: "ask_image", rawName: "ask_image", vlUsage: { input: 1_234, output: 56 } } });
    expect(cellsOf("Vision model tokens (in / out)")[0]).toBe("1,234 / 56");
    expect(screen.queryByText("Model")).not.toBeInTheDocument();
    expect(container.querySelector("td[rowspan]")?.getAttribute("rowspan")).toBe("4");
  });

  it("模型调用行 6 行都在;配上 LLM span 的辅助记录也给模型 / tokens / 成本(Langfuse 列有数)", () => {
    const first = renderRow(assistantRow());
    expect(first.container.querySelector("td[rowspan]")?.getAttribute("rowspan")).toBe("6");
    first.unmount();
    const reflect: TrajectoryRow = {
      id: "reflect:3", kind: "reflect", seq: 3, step: null, status: "ok", durationMs: 800,
      eventIndexes: [], serverMs: 1_700_000_001_000, verdict: "pass", detail: {},
    };
    renderRow(reflect, { match: { span: span({ model: "glm-judge" }), reason: "matched" } });
    expect(cellsOf("Model")).toEqual(["—", "glm-judge"]);
  });
});
