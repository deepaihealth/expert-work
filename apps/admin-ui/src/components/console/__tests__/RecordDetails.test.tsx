/**
 * RecordDetails 测试(PR-A.2 Task 9)—— 按记录类型分 tab、概要 `dl`(层级 /
 * 状态 / 耗时 / assistant 用量 / Run / Langfuse)、分节预览跳 tab、预览与原文、
 * 占位记录、非法 activeTab 兜底。fixture 手造 `LedgerRecord`(不跑 `buildLedger`)。
 */
import { readFileSync } from "node:fs";

import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import type { AgentToolSchema } from "../../../api/agents";
import type { SseEvent } from "../../../api/sessions";
import type {
  AssistantRow,
  MarkerRow,
  MemoryRow,
  PlanRow,
  SubagentRow,
  SystemRow,
  ToolRow,
  TrajectoryRow,
  UserRow,
} from "../../../api/trajectory_rows";
import type { LedgerRecord, LedgerRequest } from "../ledger_types";
import { RecordDetails, recordTabsOf, type RecordDetailsProps } from "../RecordDetails";
import { schemaToolNameOf } from "../RowDetailSchema";
import type { ToolSchemaState } from "../useAgentTools";

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

function systemRow(text: string): SystemRow {
  return {
    id: "system", kind: "system", seq: -1, step: null, status: "ok", durationMs: null,
    eventIndexes: [1], serverMs: 1001, text,
  };
}

function markerRow(over: Partial<MarkerRow> = {}): MarkerRow {
  return { ...ROW_BASE, id: "compaction:9", kind: "compaction", step: null, text: "压缩了 12 条消息", ...over };
}

function planRow(over: Partial<PlanRow> = {}): PlanRow {
  return {
    ...ROW_BASE,
    id: "plan:1:0",
    kind: "plan",
    source: "update_plan",
    callId: "c2",
    plannerSeq: null,
    stepsTotal: 2,
    goal: "先查档案再报价",
    reason: null,
    plan: null,
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
    firstTokenAt: null,
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
    firstTokenMs: null,
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
    toolSchemas: { status: "idle", byName: new Map(), reload: vi.fn() },
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
  // PR-A.3 Task 10 —— `toolRow()` 的 fixture 一直带真实 `toolName`
  // ("query_crm"),`schemaToolNameOf` 现在对它非 null,所以这条记录现在
  // 多出一个 Schema tab(概要 / 载荷 / 结果 / Schema / 计时 / 原始 六个)。
  it("tool 记录:概要 / 载荷 / 结果 / Schema / 计时 / 原始 六个 tab", () => {
    expect(recordTabsOf(rec(toolRow()))).toEqual([
      "summary",
      "payload",
      "result",
      "schema",
      "timing",
      "raw",
    ]);
    renderRecord({ record: rec(toolRow()) });
    for (const key of ["summary", "payload", "result", "schema", "timing", "raw"]) {
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

  // 终审 I3 —— 头部标签曾直接拼 `console.traj_kind_<kind>`,于是账本行写
  // SUBTOOL / COMPACTED、详情头部却写 SUBAGENT / COMPACTION(spec §九 标签列
  // 只认前者)。三处现在共用 `kind_label.ts`。
  it("头部类型标签与账本同一套:subagent → SUBTOOL、compaction → COMPACTED", () => {
    const first = renderRecord({ record: rec(subagentRow()) });
    expect(within(screen.getByTestId("console-detail-header")).getByText("SUBTOOL"))
      .toHaveClass("ew-kt", "ew-kt--subagent");
    first.unmount();

    renderRecord({ record: rec(markerRow()) });
    expect(within(screen.getByTestId("console-detail-header")).getByText("COMPACTED"))
      .toHaveClass("ew-kt", "ew-kt--compaction");
  });

  // 终审 M5 —— 失败态原先只有账本行那条选择器会把标签染红,详情头部的同一枚
  // 标签留在中性色上。
  it("失败记录:头部标签带 data-error,样式表按它染红", () => {
    renderRecord({ record: rec(toolRow({ status: "error" }), { isError: true }) });
    const tag = within(screen.getByTestId("console-detail-header")).getByText("TOOL");
    expect(tag).toHaveAttribute("data-error", "true");
    // vitest 配了 `css: false`,样式表不进 jsdom —— 规则本身在这里断言。
    const css = readFileSync("src/components/console/kind_tag.css", "utf8");
    const rule = /\.ew-kt\[data-error="true"\] \{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toContain("--ew-kt-color: var(--ew-color-danger-500)");
  });

  it("正常记录的头部标签不带 data-error", () => {
    renderRecord({ record: rec(toolRow()) });
    expect(within(screen.getByTestId("console-detail-header")).getByText("TOOL"))
      .not.toHaveAttribute("data-error");
  });

  // 终审 I4 —— REFLECT 标签是文字色,钉死的 `--ew-color-accent-600`(#9333ea)
  // 在深色底上对比度不够;换成随主题走的语义令牌。同一令牌也铺时间轴的
  // REFLECT 块,所以它在两个主题里都必须与 ASSISTANT 的 `--ew-accent-violet`
  // 错开一档,否则同泳道的两类块分不出来。
  it("REFLECT 用随主题走的 --ew-accent-reflect,两个主题里都与 --ew-accent-violet 错开", () => {
    const tags = readFileSync("src/components/console/kind_tag.css", "utf8");
    expect(/\.ew-kt--reflect \{([^}]*)\}/.exec(tags)?.[1] ?? "")
      .toContain("var(--ew-accent-reflect)");
    const timeline = readFileSync("src/components/console/trajectory_timeline.css", "utf8");
    expect(/\.ew-traj-tl__block\[data-kind="reflect"\] \{([^}]*)\}/.exec(timeline)?.[1] ?? "")
      .toContain("var(--ew-accent-reflect)");

    const tokens = readFileSync("src/theme/tokens.css", "utf8");
    const dark = /html\[data-theme="dark"\] \{([\s\S]*?)\n\}/.exec(tokens)?.[1] ?? "";
    const light = /html\[data-theme="light"\] \{([\s\S]*?)\n\}/.exec(tokens)?.[1] ?? "";
    const valueOf = (block: string, name: string): string =>
      new RegExp(`${name}:\\s*var\\((--ew-color-accent-\\d+)\\)`).exec(block)?.[1] ?? "";
    expect(valueOf(dark, "--ew-accent-reflect")).toBe("--ew-color-accent-300");
    expect(valueOf(light, "--ew-accent-reflect")).toBe("--ew-color-accent-700");
    expect(valueOf(dark, "--ew-accent-reflect")).not.toBe(valueOf(dark, "--ew-accent-violet"));
    expect(valueOf(light, "--ew-accent-reflect")).not.toBe(valueOf(light, "--ew-accent-violet"));
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

  it("分节预览区不可点:被 120px 裁一半的复制 / 工具按钮关掉指针事件", () => {
    renderRecord({ record: rec(toolRow()) });
    const preview = screen
      .getByTestId("console-detail-section-payload")
      .querySelector<HTMLElement>(".ew-detail__preview");
    expect(preview).not.toBeNull();
    // 预览区里确实有会被裁掉一半的按钮(载荷自带的复制按钮),不是空谈。
    expect(within(preview!).getAllByRole("button").length).toBeGreaterThan(0);
    // vitest 配了 `css: false`,样式表根本不进 jsdom —— 规则本身在这里断言。
    // (vitest 的 root 是 apps/admin-ui;`css: false` 让 `?raw` 也拿不到内容,只能读盘)
    const css = readFileSync("src/components/console/record_details.css", "utf8");
    const rule = /\.ew-detail__preview \{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toContain("pointer-events: none");
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

// PR-A.3 Task 10 —— Schema tab:tool / update_plan 记录懒加载出的工具契约。
describe("Schema tab (PR-A.3 §十.2)", () => {
  const ready = (items: AgentToolSchema[]): ToolSchemaState => ({
    status: "ready",
    byName: new Map(items.map((i) => [i.name, i])),
    reload: vi.fn(),
  });

  it("tool 记录的 Schema tab 排在结果与计时之间;user / assistant 记录没有", () => {
    const tool = renderRecord({ record: rec(toolRow()), toolSchemas: ready([]) });
    expect(screen.getAllByRole("tab").map((el) => el.getAttribute("data-testid"))).toEqual([
      "console-detail-tab-summary",
      "console-detail-tab-payload",
      "console-detail-tab-result",
      "console-detail-tab-schema",
      "console-detail-tab-timing",
      "console-detail-tab-raw",
    ]);
    tool.unmount();
    renderRecord({ record: rec(assistantRow()) });
    expect(screen.queryByTestId("console-detail-tab-schema")).toBeNull();
  });

  it("渲染命中工具的描述 / 来源 / 延迟挂载标记 / 参数 JSON Schema", () => {
    const item: AgentToolSchema = {
      name: "bash",
      description: "Run a shell command",
      parameters: { type: "object", properties: { command: { type: "string" } } },
      source: "mcp:gh",
      from_skill: null,
      deferred: true,
    };
    const bashRow = toolRow({
      entry: {
        id: "c9", rawName: "bash", isMcp: false, server: null, toolName: "bash",
        args: { command: "ls" }, status: "success", resultPreview: "", durationMs: 50,
      },
    });
    renderRecord({ record: rec(bashRow), toolSchemas: ready([item]), activeTab: "schema" });
    const panel = screen.getByTestId("console-detail-schema");
    expect(panel).toHaveTextContent("Run a shell command");
    expect(panel).toHaveTextContent("mcp:gh");
    expect(panel).toHaveTextContent("promoted on demand");
    expect(panel).toHaveTextContent('"command"');
  });

  // 终审 C1 —— 账本 `entry.toolName` 是剥了 `mcp__server__` 前缀的显示名
  // ("create_issue"),catalog key 是注册名("mcp__gh__create_issue",
  // `rawName`)。`schemaToolNameOf` 必须用 `rawName` 查,否则每一条 MCP 工具
  // 记录都稳定 miss、渲染成看似正常的空态。
  it("MCP 工具记录用 rawName(注册名)查 catalog,不用剥了前缀的 toolName", () => {
    const item: AgentToolSchema = {
      name: "mcp__gh__create_issue",
      description: "Create a GitHub issue",
      parameters: { type: "object", properties: {} },
      source: "mcp:gh",
      from_skill: null,
      deferred: false,
    };
    const mcpRow = toolRow({
      entry: {
        id: "c10",
        rawName: "mcp__gh__create_issue",
        isMcp: true,
        server: "gh",
        toolName: "create_issue",
        args: {},
        status: "success",
        resultPreview: "",
        durationMs: 40,
      },
    });
    renderRecord({ record: rec(mcpRow), toolSchemas: ready([item]), activeTab: "schema" });
    expect(screen.queryByTestId("console-detail-schema-missing")).toBeNull();
    expect(screen.getByTestId("console-detail-schema")).toHaveTextContent("Create a GitHub issue");
  });

  it("rawName 为空的 tool 记录不出 Schema tab", () => {
    const emptyNameRow = toolRow({
      entry: {
        id: "c11",
        rawName: "",
        isMcp: false,
        server: null,
        toolName: "",
        args: {},
        status: "success",
        resultPreview: "",
        durationMs: 10,
      },
    });
    expect(schemaToolNameOf(emptyNameRow)).toBeNull();
    expect(recordTabsOf(rec(emptyNameRow))).not.toContain("schema");
  });

  // Minor 1 —— skill 工具的「来源」应显示 `skill:<name>`,不是 registry 落的
  // 默认 `source: "builtin"`。
  it("skill 工具的来源显示 skill:<name>,不是 builtin", () => {
    const item: AgentToolSchema = {
      name: "summarize_doc",
      description: "Summarize a document",
      parameters: { type: "object", properties: {} },
      source: "builtin",
      from_skill: "doc_tools",
      deferred: false,
    };
    const skillRow = toolRow({
      entry: {
        id: "c12", rawName: "summarize_doc", isMcp: false, server: null, toolName: "summarize_doc",
        args: {}, status: "success", resultPreview: "", durationMs: 20,
      },
    });
    renderRecord({ record: rec(skillRow), toolSchemas: ready([item]), activeTab: "schema" });
    const panel = screen.getByTestId("console-detail-schema");
    expect(panel).toHaveTextContent("skill:doc_tools");
    expect(panel).not.toHaveTextContent("builtin");
  });

  it("三态:loading / error(带重试)/ missing(未命中当前工具集)", async () => {
    const reload = vi.fn();
    const loading = renderRecord({
      record: rec(toolRow()),
      toolSchemas: { status: "loading", byName: new Map(), reload },
      activeTab: "schema",
    });
    expect(screen.getByTestId("console-detail-schema")).toHaveTextContent("Loading tool schemas");
    loading.unmount();

    const errored = renderRecord({
      record: rec(toolRow()),
      toolSchemas: { status: "error", byName: new Map(), reload },
      activeTab: "schema",
    });
    await userEvent.click(screen.getByTestId("console-detail-schema-retry"));
    expect(reload).toHaveBeenCalledTimes(1);
    errored.unmount();

    renderRecord({ record: rec(toolRow()), toolSchemas: ready([]), activeTab: "schema" });
    expect(screen.getByTestId("console-detail-schema-missing")).toBeInTheDocument();
  });

  it("plan(update_plan) 记录的工具名解析成 'update_plan';planner 节点的 plan 记录不解析", () => {
    expect(schemaToolNameOf(planRow({ source: "update_plan" }))).toBe("update_plan");
    expect(schemaToolNameOf(planRow({ source: "planner" }))).toBeNull();
    expect(recordTabsOf(rec(planRow({ source: "update_plan" })))).toContain("schema");
    expect(recordTabsOf(rec(planRow({ source: "planner" })))).not.toContain("schema");
  });
});

// PR-A.3 §十.1 Task 12 —— SYSTEM 记录:概要 / 原文 / 原始 三个 tab,概要多一行
// 字数,原文 = SystemPromptPanel 全文 + 复制按钮。这个套件的默认语言是英文
// (jsdom navigator.language → i18next-browser-languagedetector 探测出
// "en",不是 zh-CN;别的既有用例同样断言英文文案,如 "Turn not replayed yet"),
// 所以 tab 文案与 brief 示例的中文不同,这里按本文件实际渲染的英文断言。
describe("SYSTEM record (PR-A.3 §十.1)", () => {
  it("tabs 概要 / 原文 / 原始;summary shows char count;原文 shows the full prompt", async () => {
    renderRecord({ record: rec(systemRow("你是评审员\n只说重点")) });
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Summary",
      "Raw",
      "Frames",
    ]);
    // "你是评审员\n只说重点".length === 10(5 + 1 换行 + 4)。
    expect(screen.getByTestId("console-detail-summary")).toHaveTextContent("10 chars");
    await userEvent.click(screen.getByTestId("console-detail-tab-rawtext"));
    expect(screen.getByTestId("console-detail-system-prompt")).toHaveTextContent("只说重点");
  });

  it("原文面板带复制按钮", () => {
    renderRecord({ record: rec(systemRow("你是评审员")), activeTab: "rawtext" });
    expect(screen.getByTestId("console-detail-system-copy")).toBeInTheDocument();
  });

  it("SYSTEM 的类型标签显式定色(kind_tag.css 不靠默认的 .ew-kt 兜底)", () => {
    const css = readFileSync("src/components/console/kind_tag.css", "utf8");
    const rule = /\.ew-kt--system \{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toContain("--ew-kt-color: var(--ew-text-secondary)");
  });
});
