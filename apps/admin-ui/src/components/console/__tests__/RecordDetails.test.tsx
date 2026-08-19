/**
 * RecordDetails 测试(PR-A.2 Task 9)—— 按记录类型分 tab、概要 `dl`(层级 /
 * 状态 / 耗时 / assistant 用量 / Run / Langfuse)、分节预览跳 tab、预览与原文、
 * 占位记录、非法 activeTab 兜底。fixture 手造 `LedgerRecord`(不跑 `buildLedger`)。
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import type {
  AssistantRow,
  MemoryRow,
  SubagentRow,
  ToolRow,
  TrajectoryRow,
  UserRow,
} from "../../../api/trajectory_rows";
import type { LedgerRecord, LedgerRequest } from "../ledger_types";
import { RecordDetails, recordTabsOf, type RecordDetailsProps } from "../RecordDetails";

// ToolCallCard 的「立即触发」按钮读 useIsTenantSwitched(这里没挂 Auth /
// TenantScope provider)—— 同 RowDetail.test.tsx 的桩。
vi.mock("../../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => false,
}));

const BASE = 1_700_000_000_000;

const ROW_BASE = {
  seq: 1,
  step: 1,
  status: "ok" as const,
  durationMs: 1200,
  eventIndexes: [0],
  serverMs: BASE + 1200,
};

function toolRow(over: Partial<ToolRow> = {}): ToolRow {
  return {
    ...ROW_BASE,
    id: "tool:1:0",
    kind: "tool",
    entry: {
      id: "c1",
      rawName: "query_crm",
      isMcp: false,
      server: null,
      toolName: "query_crm",
      args: { id: "C-1" },
      status: "success",
      resultPreview: "3 条记录",
      durationMs: 300,
    },
    ...over,
  };
}
function assistantRow(over: Partial<AssistantRow> = {}): AssistantRow {
  return {
    ...ROW_BASE,
    id: "assistant:1",
    kind: "assistant",
    text: "## 结论\n建议先补档案",
    reasoning: "先查客户档案",
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
function userRow(over: Partial<UserRow> = {}): UserRow {
  return {
    ...ROW_BASE,
    id: "user",
    step: null,
    kind: "user",
    text: "帮我查一下客户 C-1",
    attachmentNames: [],
    inputs: {},
    ...over,
  };
}
function memoryRow(over: Partial<MemoryRow> = {}): MemoryRow {
  return {
    ...ROW_BASE,
    id: "memory:2",
    kind: "memory",
    direction: "writeback",
    count: 2,
    detail: { memories: [{ id: "m1", kind: "fact", content: "客户偏好邮件" }] },
    ...over,
  };
}
function subagentRow(over: Partial<SubagentRow> = {}): SubagentRow {
  return {
    ...ROW_BASE,
    id: "subagent:1:0:0",
    kind: "subagent",
    parentEntryId: "c1",
    worker: {
      workerId: "w1",
      parentWorkerId: null,
      parentToolCallId: "c1",
      label: "researcher",
      agentRef: "researcher",
      depth: 1,
      role: null,
      taskExcerpt: "查资料",
      maxSteps: null,
      status: "success",
      steps: [],
      children: [],
      summary: null,
    },
    ...over,
  };
}

function rec(row: TrajectoryRow, over: Partial<LedgerRecord> = {}): LedgerRecord {
  return {
    id: `t1/${row.id}`,
    index: 3,
    turnKey: "t1",
    turnSeq: 1,
    runId: "run-9",
    turnStart: false,
    turnEnd: false,
    requestNo: null,
    ownerRequestNo: null,
    parentId: null,
    kind: row.kind,
    lane: 1,
    isError: false,
    running: false,
    startedAt: BASE,
    endedAt: BASE + 1200,
    text: "",
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

function propsOf(over: Partial<RecordDetailsProps> = {}): RecordDetailsProps {
  return {
    record: rec(toolRow()),
    ownerRequest: null,
    parent: null,
    threadId: "th-1",
    isSystemAdmin: false,
    langfuseUrl: null,
    match: { span: null, reason: "no_trace" },
    trace: null,
    traceLoading: false,
    onRefreshTrace: vi.fn(),
    onOpenRecord: vi.fn(),
    onOpenRequest: vi.fn(),
    activeTab: "summary",
    onTabChange: vi.fn(),
    onClose: vi.fn(),
    width: 420,
    onWidthChange: vi.fn(),
    splitWidth: 1200,
    ...over,
  };
}

function renderRecord(
  over: Partial<RecordDetailsProps> = {},
): { props: RecordDetailsProps; unmount: () => void } {
  const props = propsOf(over);
  const { unmount } = render(
    <MemoryRouter>
      <App>
        <RecordDetails {...props} />
      </App>
    </MemoryRouter>,
  );
  return { props, unmount };
}

describe("RecordDetails", () => {
  it("tool 记录:概要 / 载荷 / 结果 / 计时 / 原始 五个 tab", () => {
    expect(recordTabsOf(rec(toolRow()))).toEqual(["summary", "payload", "result", "timing", "raw"]);
    renderRecord({ record: rec(toolRow()) });
    for (const key of ["summary", "payload", "result", "timing", "raw"]) {
      expect(screen.getByTestId(`console-detail-tab-${key}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("console-detail-tab-preview")).not.toBeInTheDocument();
  });

  it("assistant 记录:概要 / 预览 / 原文 / 计时 / 原始 五个 tab", () => {
    expect(recordTabsOf(rec(assistantRow()))).toEqual([
      "summary",
      "preview",
      "rawtext",
      "timing",
      "raw",
    ]);
    renderRecord({ record: rec(assistantRow()) });
    for (const key of ["summary", "preview", "rawtext", "timing", "raw"]) {
      expect(screen.getByTestId(`console-detail-tab-${key}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("console-detail-tab-payload")).not.toBeInTheDocument();
  });

  it("user 记录:概要 / 预览 / 原文 / 原始 四个 tab(没有计时)", () => {
    expect(recordTabsOf(rec(userRow()))).toEqual(["summary", "preview", "rawtext", "raw"]);
    renderRecord({ record: rec(userRow()) });
    expect(screen.queryByTestId("console-detail-tab-timing")).not.toBeInTheDocument();
  });

  it("头部:类型标签 + 「第 N 轮 · 第 M 步」;没有 step 时只显示轮", () => {
    const first = renderRecord({ record: rec(toolRow(), { turnSeq: 2 }) });
    const header = screen.getByTestId("console-detail-header");
    expect(within(header).getByText("TOOL")).toHaveClass("ew-kt", "ew-kt--tool");
    expect(header.textContent).toContain("Turn 3 · step 1");
    first.unmount();

    renderRecord({ record: rec(userRow(), { turnSeq: 2 }) });
    const header2 = screen.getByTestId("console-detail-header");
    expect(header2.textContent).toContain("Turn 3");
    expect(header2.textContent).not.toContain("step");
  });

  it("层级:assistant + ownerRequest → 「请求 #N ›」抛 onOpenRequest", async () => {
    const { props } = renderRecord({
      record: rec(assistantRow(), { ownerRequestNo: 4, requestNo: 4 }),
      ownerRequest: request(),
    });
    const button = screen.getByTestId("console-detail-hier-request");
    expect(button.textContent).toContain("Request #4");
    await userEvent.click(button);
    expect(props.onOpenRequest).toHaveBeenCalledWith(4);
  });

  it("层级:tool / memory 写回 + parent → 「Assistant Message ›」抛 onOpenRecord", async () => {
    const parent = rec(assistantRow());
    const first = renderRecord({ record: rec(toolRow(), { parentId: parent.id }), parent });
    const button = screen.getByTestId("console-detail-hier-assistant");
    expect(button.textContent).toContain("Assistant Message");
    await userEvent.click(button);
    expect(first.props.onOpenRecord).toHaveBeenCalledWith(parent.id);
    first.unmount();

    const second = renderRecord({ record: rec(memoryRow(), { parentId: parent.id }), parent });
    await userEvent.click(screen.getByTestId("console-detail-hier-assistant"));
    expect(second.props.onOpenRecord).toHaveBeenCalledWith(parent.id);
  });

  it("层级:subagent + parent → 「Tool Call ›」抛 onOpenRecord", async () => {
    const parent = rec(toolRow());
    const { props } = renderRecord({
      record: rec(subagentRow(), { parentId: parent.id }),
      parent,
    });
    const button = screen.getByTestId("console-detail-hier-tool");
    expect(button.textContent).toContain("Tool Call");
    await userEvent.click(button);
    expect(props.onOpenRecord).toHaveBeenCalledWith(parent.id);
  });

  it("概要:Run 链接指向 /runs/{threadId}/{runId}", () => {
    renderRecord({ record: rec(toolRow(), { runId: "run-9" }), threadId: "th-1" });
    expect(screen.getByTestId("console-inspect-run-link")).toHaveAttribute(
      "href",
      "/runs/th-1/run-9",
    );
  });

  it("概要:Langfuse 链接只对系统管理员出现", () => {
    const first = renderRecord({ isSystemAdmin: false, langfuseUrl: "https://lf/t/1" });
    expect(screen.queryByTestId("playground-turn-langfuse")).not.toBeInTheDocument();
    first.unmount();

    renderRecord({ isSystemAdmin: true, langfuseUrl: "https://lf/t/1" });
    const link = screen.getByTestId("playground-turn-langfuse");
    expect(link).toHaveAttribute("href", "https://lf/t/1");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("概要分节:tool 有载荷 / 结果 / 计时三节,标题按钮跳对应 tab", async () => {
    const { props } = renderRecord({ record: rec(toolRow()) });
    for (const key of ["payload", "result", "timing"]) {
      expect(screen.getByTestId(`console-detail-section-${key}`)).toBeInTheDocument();
    }
    // 分节里还有载荷自带的复制按钮,按可访问名字点标题那颗。
    const section = screen.getByTestId("console-detail-section-payload");
    await userEvent.click(within(section).getByRole("button", { name: "Open Payload" }));
    expect(props.onTabChange).toHaveBeenCalledWith("payload");
  });

  it("assistant 预览:「思考」折叠段 + Markdown 正文", () => {
    renderRecord({ record: rec(assistantRow()), activeTab: "preview" });
    const thinking = screen.getByTestId("console-detail-thinking");
    expect(thinking.textContent).toContain("Thinking (44 tokens)");
    expect(thinking.textContent).toContain("先查客户档案");
    // Markdown 正文:`## 结论` 渲染成标题而不是原样文本。
    expect(screen.getByRole("heading", { name: "结论" })).toBeInTheDocument();
  });

  it("user 预览:没有思考折叠段", () => {
    renderRecord({ record: rec(userRow()), activeTab: "preview" });
    expect(screen.queryByTestId("console-detail-thinking")).not.toBeInTheDocument();
    expect(screen.getByText("帮我查一下客户 C-1")).toBeInTheDocument();
  });

  it("placeholder 记录:只给「尚未回放」提示,没有分节", () => {
    renderRecord({
      record: rec(assistantRow({ text: "", reasoning: "" }), { placeholder: "loading" }),
    });
    expect(screen.getByTestId("console-detail-summary").textContent).toContain(
      "Turn not replayed yet",
    );
    expect(screen.queryByTestId("console-detail-section-preview")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-detail-section-timing")).not.toBeInTheDocument();
  });

  it("activeTab 不在该记录的 tab 集里 → 渲染概要(不改父状态)", () => {
    const { props } = renderRecord({ record: rec(userRow()), activeTab: "payload" });
    expect(screen.getByTestId("console-detail-summary")).toBeInTheDocument();
    expect(screen.queryByTestId("console-detail-payload")).not.toBeInTheDocument();
    expect(props.onTabChange).not.toHaveBeenCalled();
  });

  it("原始 tab:该记录的每一帧一张 EventCard", () => {
    const events: SseEvent[] = [
      { id: null, event: "updates", data: { agent: {} }, rawData: "", receivedAt: "t" },
      { id: null, event: "end", data: {}, rawData: "", receivedAt: "t" },
    ];
    renderRecord({
      record: rec(toolRow({ eventIndexes: [0, 1] }), { events }),
      activeTab: "raw",
    });
    const raw = screen.getByTestId("console-detail-raw");
    expect(within(raw).getByTestId("event-card-updates")).toBeInTheDocument();
    expect(within(raw).getByTestId("event-card-end")).toBeInTheDocument();
  });
});
