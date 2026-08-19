/**
 * RequestDetails 测试(PR-A.2 Task 9)—— 一次主模型调用的四个 tab:概要
 * (状态 / 模型 / 结束原因 / 工具调用 / 结果链接)、输入(Langfuse span 消息)、
 * 用量(本次四项 + 累计两项)、计时。fixture 手造 `LedgerRequest` / `LedgerRecord`。
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import type { TraceSpan } from "../../../api/trace_facade";
import type { AssistantRow } from "../../../api/trajectory_rows";
import type { LedgerRecord, LedgerRequest } from "../ledger_types";
import { RequestDetails, type RequestDetailsProps } from "../RequestDetails";

const BASE = 1_700_000_000_000;

function assistantRow(over: Partial<AssistantRow> = {}): AssistantRow {
  return {
    id: "assistant:1",
    kind: "assistant",
    seq: 1,
    step: 1,
    status: "ok",
    durationMs: 1200,
    eventIndexes: [0],
    serverMs: BASE + 1200,
    text: "结论",
    reasoning: "想一想",
    model: "gpt-x",
    inputTokens: 120,
    outputTokens: 30,
    reasoningTokens: 44,
    cacheReadTokens: 7,
    finishReason: "stop",
    toolCallCount: 2,
    ...over,
  };
}

function record(over: Partial<LedgerRecord> = {}): LedgerRecord {
  const row = assistantRow();
  return {
    id: "t1/assistant:1",
    index: 3,
    turnKey: "t1",
    turnSeq: 1,
    runId: "run-9",
    turnStart: false,
    turnEnd: false,
    requestNo: 4,
    ownerRequestNo: 4,
    parentId: null,
    kind: "assistant",
    lane: 1,
    isError: false,
    running: false,
    startedAt: BASE,
    endedAt: BASE + 1200,
    text: "结论",
    resultText: null,
    row,
    events: [],
    placeholder: null,
    ...over,
  };
}

function request(over: Partial<LedgerRequest> = {}): LedgerRequest {
  return {
    no: 4,
    turnKey: "t1",
    turnSeq: 1,
    step: 1,
    recordId: "t1/assistant:1",
    status: "ok",
    model: "gpt-x",
    finishReason: "stop",
    usage: { input: 120, output: 30, reasoning: 44, cacheRead: 7 },
    cumulative: { input: 320, output: 90 },
    toolCalls: 2,
    startedAt: BASE,
    endedAt: BASE + 1200,
    durationMs: 1200,
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
    model: "gpt-x",
    inputTokens: 120,
    outputTokens: 30,
    costUsd: 0.01,
    input: {
      kind: "messages",
      messages: [
        { role: "system", content: "你是助手", truncated: false, fullChars: 4, toolCalls: null },
      ],
    },
    output: null,
    level: "default",
    statusMessage: null,
    purpose: "",
    group: null,
    ...over,
  };
}

function propsOf(over: Partial<RequestDetailsProps> = {}): RequestDetailsProps {
  return {
    request: request(),
    record: record(),
    threadId: "th-1",
    isSystemAdmin: false,
    langfuseUrl: null,
    match: { span: null, reason: "no_trace" },
    trace: null,
    traceLoading: false,
    onRefreshTrace: vi.fn(),
    onOpenRecord: vi.fn(),
    activeTab: "summary",
    onTabChange: vi.fn(),
    onClose: vi.fn(),
    width: 420,
    onWidthChange: vi.fn(),
    splitWidth: 1200,
    ...over,
  };
}

function renderRequest(
  over: Partial<RequestDetailsProps> = {},
): { props: RequestDetailsProps; unmount: () => void } {
  const props = propsOf(over);
  const { unmount } = render(
    <MemoryRouter>
      <App>
        <RequestDetails {...props} />
      </App>
    </MemoryRouter>,
  );
  return { props, unmount };
}

describe("RequestDetails", () => {
  it("头部:「请求 #N」+ 「第 N 轮 · 第 M 步」;四个 tab", () => {
    renderRequest();
    const header = screen.getByTestId("console-detail-header");
    expect(header.textContent).toContain("Request #4");
    expect(header.textContent).toContain("Turn 2 · step 1");
    for (const key of ["summary", "input", "usage", "timing"]) {
      expect(screen.getByTestId(`console-detail-tab-${key}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("console-detail-tab-raw")).not.toBeInTheDocument();
  });

  it("概要:状态 / 模型 / 结束原因 / 工具调用", () => {
    renderRequest();
    const summary = screen.getByTestId("console-detail-summary");
    expect(summary.textContent).toContain("done");
    expect(summary.textContent).toContain("gpt-x");
    expect(summary.textContent).toContain("stop");
    expect(within(summary).getByText("Tool calls").nextSibling?.textContent).toBe("2");
  });

  // 终审 M6 —— 记录详情的概要有「耗时」,请求详情没有;而「这次调用花了多久」
  // 正是读者点开一次请求最先要的数。
  it("概要:有「耗时」一行;没有时长时写 —", () => {
    const first = renderRequest();
    const summary = screen.getByTestId("console-detail-summary");
    expect(within(summary).getByText("Duration").nextSibling?.textContent).toBe("1.2s");
    first.unmount();

    renderRequest({ request: request({ durationMs: null }) });
    expect(
      within(screen.getByTestId("console-detail-summary")).getByText("Duration").nextSibling?.textContent,
    ).toBe("—");
  });

  it("概要:结果按钮抛 onOpenRecord(该请求的 assistant 记录)", async () => {
    const { props } = renderRequest();
    const button = screen.getByTestId("console-detail-hier-assistant");
    expect(button.textContent).toContain("Assistant Message");
    await userEvent.click(button);
    expect(props.onOpenRecord).toHaveBeenCalledWith("t1/assistant:1");
  });

  it("输入:没配到 span → 「只在 Langfuse 精确轨迹里有」提示", () => {
    renderRequest({ activeTab: "input" });
    expect(screen.getByTestId("console-detail-input").textContent).toContain(
      "LLM input is only available from the Langfuse trace",
    );
  });

  it("输入:配到 span → 渲染 span 的消息", () => {
    renderRequest({ activeTab: "input", match: { span: span(), reason: "matched" } });
    const input = screen.getByTestId("console-detail-input");
    expect(input.textContent).toContain("system");
    expect(input.textContent).toContain("你是助手");
  });

  it("用量:本次输入 / 输出 / 思考 / 缓存读 + 累计至此输入 / 输出", () => {
    renderRequest({ activeTab: "usage" });
    const usage = screen.getByTestId("console-detail-usage");
    expect(within(usage).getAllByText("Input")[0]!.nextSibling?.textContent).toBe("120");
    expect(within(usage).getAllByText("Output")[0]!.nextSibling?.textContent).toBe("30");
    expect(within(usage).getByText("Reasoning").nextSibling?.textContent).toBe("44");
    expect(within(usage).getByText("Cache read").nextSibling?.textContent).toBe("7");
    expect(usage.textContent).toContain("Cumulative");
    expect(within(usage).getAllByText("Input")[1]!.nextSibling?.textContent).toBe("320");
    expect(within(usage).getAllByText("Output")[1]!.nextSibling?.textContent).toBe("90");
  });

  it("计时:复用 RowDetailTiming(该请求的 assistant 行)", () => {
    renderRequest({ activeTab: "timing" });
    const timing = screen.getByTestId("console-detail-timing");
    expect(timing.textContent).toContain("1.2s");
    expect(timing.textContent).toContain("gpt-x");
  });
});
