/**
 * TrajectoryLedger — PR-A.2 Task 8: 账本表格(事件槽 / 请求圆点 / 内容列 /
 * 折叠行 / 虚拟化 / 键盘 / 尾随)。fixture 手造 `DisplayRow[]`,几何(
 * `clientHeight` / `scrollHeight` / `scrollTop`)与 `scrollIntoView` 在
 * jsdom 里都不存在,逐个 mock。见 task-8-brief.md。
 */
import { readFileSync } from "node:fs";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import i18n from "../../../i18n";
import type {
  AssistantRow, MemoryRow, ReflectRow, SubagentRow, SystemRow, ToolRow, TrajectoryRow, UserRow,
} from "../../../api/trajectory_rows";
import type { DisplayRow } from "../ledger_collapse";
import type { LedgerRecord, LedgerRequest } from "../ledger_types";
import { TrajectoryLedger, type TrajectoryLedgerProps } from "../TrajectoryLedger";

const userRow: UserRow = {
  id: "user", kind: "user", seq: -1, step: null, status: "ok", durationMs: null,
  eventIndexes: [], serverMs: null, text: "帮我看看目录", attachmentNames: [], inputs: {},
};
function assistantRow(over: Partial<AssistantRow> = {}): AssistantRow {
  return {
    id: "assistant:1", kind: "assistant", seq: 1, step: 1, status: "ok", durationMs: 120,
    eventIndexes: [], serverMs: null, text: "我来查一下", reasoning: "", model: "glm-5.2",
    inputTokens: 10, outputTokens: 4, finishReason: "stop", toolCallCount: 1, ...over,
  };
}
const toolRow: ToolRow = {
  id: "tool:1:0", kind: "tool", seq: 1, step: 1, status: "ok", durationMs: 40,
  eventIndexes: [], serverMs: null,
  entry: {
    id: "call_1", rawName: "bash", isMcp: false, server: null, toolName: "bash",
    args: { cmd: "ls" }, status: "success", resultPreview: "a.txt", durationMs: 40,
  },
};

function rec(over: Partial<LedgerRecord> & Pick<LedgerRecord, "id" | "index" | "kind" | "row">): LedgerRecord {
  return {
    turnKey: "t1", turnSeq: 0, runId: "run-1", turnStart: false, turnEnd: false,
    requestNo: null, ownerRequestNo: null, parentId: null, lane: 1, isError: false,
    running: false, startedAt: null, endedAt: null, text: "", resultText: null,
    events: [], placeholder: null, firstTokenAt: null, ...over,
  };
}
function request(over: Partial<LedgerRequest> & Pick<LedgerRequest, "no" | "recordId">): LedgerRequest {
  return {
    turnKey: "t1", turnSeq: 0, step: 1, status: "ok", model: "glm-5.2", finishReason: "stop",
    usage: { input: 10, output: 4, reasoning: 0, cacheRead: 0 },
    cumulative: { input: 10, output: 4 }, toolCalls: 1,
    startedAt: null, endedAt: null, durationMs: 120, firstTokenMs: null, ...over,
  };
}

/** 两轮六条记录:t1 = user / assistant#1 / tool / assistant#2,t2 = user / assistant#3。 */
function fixtureRecords(): LedgerRecord[] {
  return [
    rec({ id: "t1/user", index: 0, kind: "user", row: userRow, turnStart: true, lane: 0, text: "帮我看看目录" }),
    rec({
      id: "t1/assistant:1", index: 1, kind: "assistant", row: assistantRow(), requestNo: 1,
      ownerRequestNo: 1, text: "我来查一下",
    }),
    rec({
      id: "t1/tool:1:0", index: 2, kind: "tool", row: toolRow, parentId: "t1/assistant:1",
      ownerRequestNo: 1, lane: 2, text: 'bash {"cmd":"ls"}', resultText: "a.txt",
    }),
    rec({
      id: "t1/assistant:2", index: 3, kind: "assistant", row: assistantRow({ id: "assistant:2", seq: 2, step: 2 }),
      requestNo: 2, ownerRequestNo: 2, text: "目录里只有 a.txt", turnEnd: true,
    }),
    rec({
      id: "t2/user", index: 4, kind: "user", row: userRow, turnKey: "t2", turnSeq: 1,
      turnStart: true, lane: 0, text: "谢谢",
    }),
    rec({
      id: "t2/assistant:1", index: 5, kind: "assistant", row: assistantRow({ seq: 3, step: 1 }),
      turnKey: "t2", turnSeq: 1, requestNo: 3, ownerRequestNo: 3, text: "不客气", turnEnd: true,
    }),
  ];
}

function recordRows(records: readonly LedgerRecord[]): DisplayRow[] {
  return records.map((record) => ({ kind: "record", record }));
}

function baseProps(over: Partial<TrajectoryLedgerProps> = {}): TrajectoryLedgerProps {
  const records = fixtureRecords();
  return {
    rows: recordRows(records),
    requestsByRecordId: new Map([
      ["t1/assistant:1", request({ no: 1, recordId: "t1/assistant:1" })],
      ["t1/assistant:2", request({ no: 2, recordId: "t1/assistant:2", step: 2, status: "error" })],
      ["t2/assistant:1", request({ no: 3, recordId: "t2/assistant:1", turnKey: "t2", turnSeq: 1, status: "running" })],
    ]),
    selectedId: null, onSelect: vi.fn(),
    selectedRequestNo: null, onSelectRequest: vi.fn(),
    hoveredId: null, onHover: vi.fn(),
    focusIndexes: null,
    activeTurnKey: "t1",
    onToggleTurn: vi.fn(), onToggleOwner: vi.fn(),
    running: false,
    hasEarlier: false, earlierCount: 0, loadingEarlier: false, onLoadEarlier: vi.fn(),
    scrollTo: null,
    loading: false,
    ...over,
  };
}

function mockGeometry(
  el: HTMLElement,
  g: { clientHeight?: number; scrollHeight?: number; scrollTop?: number },
): void {
  if (g.clientHeight !== undefined) {
    Object.defineProperty(el, "clientHeight", { value: g.clientHeight, configurable: true });
  }
  if (g.scrollHeight !== undefined) {
    Object.defineProperty(el, "scrollHeight", { value: g.scrollHeight, configurable: true });
  }
  if (g.scrollTop !== undefined) {
    Object.defineProperty(el, "scrollTop", { value: g.scrollTop, writable: true, configurable: true });
  }
}

let scrollIntoView: ReturnType<typeof vi.fn>;
let priorLang: string;

describe("TrajectoryLedger", () => {
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
    scrollIntoView = vi.fn();
    // jsdom 没有 scrollIntoView;组件按可选调用写,这里给它一个 spy。
    (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView = scrollIntoView;
  });
  afterEach(async () => {
    delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView;
    await i18n.changeLanguage(priorLang);
  });

  it("每条记录一行,带 record-id / kind / index / 轮起止 data 属性", () => {
    render(<TrajectoryLedger {...baseProps()} />);
    const rows = screen.getAllByTestId("console-traj-row");
    expect(rows).toHaveLength(6);
    expect(rows.map((r) => r.dataset.recordId)).toEqual([
      "t1/user", "t1/assistant:1", "t1/tool:1:0", "t1/assistant:2", "t2/user", "t2/assistant:1",
    ]);
    expect(rows[2]).toHaveAttribute("data-kind", "tool");
    expect(rows[2]).toHaveAttribute("data-index", "2");
    expect(rows[0]).toHaveAttribute("data-turn-start", "true");
    expect(rows[1]).not.toHaveAttribute("data-turn-start");
    expect(rows[3]).toHaveAttribute("data-turn-end", "true");
    expect(screen.getByRole("grid", { name: "轨迹账本" })).toHaveAttribute("aria-rowcount", "6");
  });

  it("轮标签只挂在轮首行,当前轮标 data-active", () => {
    render(<TrajectoryLedger {...baseProps({ activeTurnKey: "t2" })} />);
    const labels = screen.getAllByTestId("console-traj-turn-label");
    expect(labels).toHaveLength(2);
    expect(labels[0]).toHaveTextContent("第 1 轮");
    expect(labels[0]).toHaveTextContent("#1");
    expect(labels[0]).not.toHaveAttribute("data-active");
    expect(labels[1]).toHaveTextContent("第 2 轮");
    expect(labels[1]).toHaveAttribute("data-active", "true");
  });

  it("请求圆点只挂在有请求的记录上,带序号 / 状态 / 选中态与提示", () => {
    render(<TrajectoryLedger {...baseProps({ selectedRequestNo: 2 })} />);
    const dots = screen.getAllByTestId("console-traj-request-dot");
    expect(dots).toHaveLength(3);
    expect(dots.map((d) => d.dataset.no)).toEqual(["1", "2", "3"]);
    expect(dots.map((d) => d.dataset.status)).toEqual(["ok", "error", "running"]);
    expect(dots[1]).toHaveAttribute("data-active", "true");
    expect(dots[0]).not.toHaveAttribute("data-active");
    expect(dots[0]).toHaveAttribute("aria-label", "请求 #1 · 第 1 轮 · 第 1 步");
    expect(dots[0]).toHaveAttribute("title", "请求 #1 · 第 1 轮 · 第 1 步");
    // tool 行没有请求 → 它那格没有圆点。
    const toolRowEl = screen.getAllByTestId("console-traj-row")[2];
    expect(within(toolRowEl).queryByTestId("console-traj-request-dot")).toBeNull();
  });

  it("点击 / Enter / Space 都选中该行", () => {
    const onSelect = vi.fn();
    render(<TrajectoryLedger {...baseProps({ onSelect })} />);
    const rows = screen.getAllByTestId("console-traj-row");
    fireEvent.click(rows[2]);
    expect(onSelect).toHaveBeenCalledWith("t1/tool:1:0");

    onSelect.mockClear();
    fireEvent.keyDown(rows[1], { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("t1/assistant:1");

    onSelect.mockClear();
    fireEvent.keyDown(rows[4], { key: " " });
    expect(onSelect).toHaveBeenCalledWith("t2/user");
  });

  it("选中行标 aria-selected / data-selected", () => {
    render(<TrajectoryLedger {...baseProps({ selectedId: "t1/tool:1:0" })} />);
    const rows = screen.getAllByTestId("console-traj-row");
    expect(rows[2]).toHaveAttribute("aria-selected", "true");
    expect(rows[2]).toHaveAttribute("data-selected", "true");
    expect(rows[1]).toHaveAttribute("aria-selected", "false");
    expect(rows[1]).not.toHaveAttribute("data-selected");
  });

  it("点圆点选中请求,且不冒泡成选中行", () => {
    const onSelect = vi.fn();
    const onSelectRequest = vi.fn();
    render(<TrajectoryLedger {...baseProps({ onSelect, onSelectRequest })} />);
    fireEvent.click(screen.getAllByTestId("console-traj-request-dot")[1]);
    expect(onSelectRequest).toHaveBeenCalledWith(2);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("双击轮首行折叠该轮;双击有子记录的 assistant 折叠它的调用", () => {
    const onToggleTurn = vi.fn();
    const onToggleOwner = vi.fn();
    render(<TrajectoryLedger {...baseProps({ onToggleTurn, onToggleOwner })} />);
    const rows = screen.getAllByTestId("console-traj-row");

    fireEvent.doubleClick(rows[0]);
    expect(onToggleTurn).toHaveBeenCalledWith("t1");
    expect(onToggleOwner).not.toHaveBeenCalled();

    onToggleTurn.mockClear();
    fireEvent.doubleClick(rows[1]);
    expect(onToggleOwner).toHaveBeenCalledWith("t1/assistant:1");
    expect(onToggleTurn).not.toHaveBeenCalled();

    // 无子记录的 assistant 落到第三档:折所在轮(t1 有 3 条非 USER 记录)。
    onToggleOwner.mockClear();
    fireEvent.doubleClick(rows[3]);
    expect(onToggleOwner).not.toHaveBeenCalled();
    expect(onToggleTurn).toHaveBeenCalledWith("t1");

    // 工具行同理 —— 双击账本任意一行都该折得动所在轮,不只轮首行。
    onToggleTurn.mockClear();
    fireEvent.doubleClick(rows[2]);
    expect(onToggleTurn).toHaveBeenCalledWith("t1");

    // 但只有一条非 USER 记录的轮不值得折,双击它什么都不做。
    onToggleTurn.mockClear();
    onToggleOwner.mockClear();
    fireEvent.doubleClick(rows[5]);
    expect(onToggleTurn).not.toHaveBeenCalled();
    expect(onToggleOwner).not.toHaveBeenCalled();
  });

  // PR-A.3 §十.1 —— SYSTEM 行是上下文行(与 USER 同档),不算进「这一轮有几条
  // 值得折的记录」。轮里只有 SYSTEM + USER + 一条 assistant(唯一的非上下文
  // 记录只有 1 条,不到 2 条门槛)—— 双击 SYSTEM 行不该折;若 SYSTEM 被错当成
  // 非上下文记录计数,门槛会被凑到 2 而误折。
  it("双击 SYSTEM 行落点同 USER —— SYSTEM 不计入非上下文记录数,不够 2 条就不折", () => {
    const onToggleTurn = vi.fn();
    const onToggleOwner = vi.fn();
    const systemRow: SystemRow = {
      id: "system", kind: "system", seq: -1, step: null, status: "ok", durationMs: null, inputs: null,
      eventIndexes: [], serverMs: null, text: "你是评审员",
    };
    const rows = recordRows([
      rec({ id: "t3/system", index: 0, turnKey: "t3", turnSeq: 2, kind: "system", row: systemRow, lane: 0, turnStart: true, text: "你是评审员" }),
      rec({ id: "t3/user", index: 1, turnKey: "t3", turnSeq: 2, kind: "user", row: userRow, lane: 0, text: "帮我看看目录" }),
      rec({
        id: "t3/assistant:1", index: 2, turnKey: "t3", turnSeq: 2, kind: "assistant",
        row: assistantRow({ id: "assistant:1", seq: 1, step: 1 }), requestNo: 1, ownerRequestNo: 1, text: "我来查一下",
      }),
    ]);
    render(<TrajectoryLedger {...baseProps({ rows, onToggleTurn, onToggleOwner })} />);
    fireEvent.doubleClick(screen.getAllByTestId("console-traj-row")[0]);
    expect(onToggleTurn).not.toHaveBeenCalled();
    expect(onToggleOwner).not.toHaveBeenCalled();
  });

  // 终审 M13 收口:reflect / memory 写回带 parentId 只供详情层级链接,双击落点
  // 不能把它们当子调用 —— 否则这类 assistant 会被判成「有调用可折」而
  // collapsibleOwnerIds 又不认,双击就被静默吃掉。
  it("双击只挂着 reflect 子记录的 assistant 折所在轮,而不是折它的调用", () => {
    const onToggleTurn = vi.fn();
    const onToggleOwner = vi.fn();
    const records = fixtureRecords();
    const reflect: ReflectRow = {
      id: "reflect:9", kind: "reflect", seq: 9, step: null, status: "ok", durationMs: null,
      eventIndexes: [], serverMs: null, verdict: "pass", detail: {},
    };
    const rows = recordRows([
      ...records,
      rec({
        id: "t2/reflect:9", index: 6, kind: "reflect", row: reflect, turnKey: "t2", turnSeq: 1,
        parentId: "t2/assistant:1", text: "复盘",
      }),
    ]);
    render(<TrajectoryLedger {...baseProps({ rows, onToggleTurn, onToggleOwner })} />);
    fireEvent.doubleClick(screen.getAllByTestId("console-traj-row")[5]);
    expect(onToggleOwner).not.toHaveBeenCalled();
    expect(onToggleTurn).toHaveBeenCalledWith("t2");
  });

  it("双击已折叠那一轮里剩下的行会把它展开", () => {
    const onToggleTurn = vi.fn();
    const records = fixtureRecords();
    // 折叠态下这一轮只剩 USER 行 + 摘要行 —— 没有「≥ 2 条非 USER 记录」这条
    // 依据可用,只能靠「本轮有摘要行 = 此刻是折叠的」认出来。
    const rows: DisplayRow[] = [
      { kind: "record", record: records[0] },
      {
        kind: "turn-summary", turnKey: "t1", turnSeq: 0, runId: "run-1", hasError: false, anchorIndex: 0,
        summary: { think: 2, tools: 1, other: 0, failed: 0, toolBreakdown: "bash ×1", durationMs: 1500 },
      },
    ];
    render(<TrajectoryLedger {...baseProps({ rows, onToggleTurn })} />);
    fireEvent.doubleClick(screen.getByTestId("console-traj-row"));
    expect(onToggleTurn).toHaveBeenCalledWith("t1");
  });

  it("↑ ↓ 在记录行之间移动选择,没有选中时从首行起", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <TrajectoryLedger {...baseProps({ onSelect, selectedId: "t1/assistant:1" })} />,
    );
    const container = screen.getByTestId("console-traj-ledger");

    fireEvent.keyDown(container, { key: "ArrowDown" });
    expect(onSelect).toHaveBeenCalledWith("t1/tool:1:0");

    onSelect.mockClear();
    fireEvent.keyDown(container, { key: "ArrowUp" });
    expect(onSelect).toHaveBeenCalledWith("t1/user");

    onSelect.mockClear();
    rerender(<TrajectoryLedger {...baseProps({ onSelect, selectedId: null })} />);
    fireEvent.keyDown(container, { key: "ArrowDown" });
    expect(onSelect).toHaveBeenCalledWith("t1/user");
  });

  // 终审 M10 —— 只点了请求圆点时 `selectedId` 是 null,↑ ↓ 原先一律跳回首行,
  // 读者从「请求 #2」按一下就被扔到第 0 行。请求也有落点:它的 assistant 记录。
  it("只选中了请求时,↑ ↓ 从该请求的 assistant 记录起算", () => {
    const onSelect = vi.fn();
    render(
      <TrajectoryLedger {...baseProps({ onSelect, selectedId: null, selectedRequestNo: 2 })} />,
    );
    const container = screen.getByTestId("console-traj-ledger");

    // 请求 #2 的记录是 `t1/assistant:2`(下标 3),↓ → `t2/user`。
    fireEvent.keyDown(container, { key: "ArrowDown" });
    expect(onSelect).toHaveBeenCalledWith("t2/user");

    onSelect.mockClear();
    fireEvent.keyDown(container, { key: "ArrowUp" });
    expect(onSelect).toHaveBeenCalledWith("t1/tool:1:0");
  });

  it("focusIndexes 把段内 / 段外行分别标 inside / outside(无选区时不标)", () => {
    const props = baseProps();
    const { rerender } = render(<TrajectoryLedger {...props} />);
    expect(screen.getAllByTestId("console-traj-row")[1]).not.toHaveAttribute("data-focus");
    scrollIntoView.mockClear();

    rerender(<TrajectoryLedger {...props} focusIndexes={new Set([1, 2])} />);
    const rows = screen.getAllByTestId("console-traj-row");
    expect(rows.map((r) => r.dataset.focus)).toEqual([
      "outside", "inside", "inside", "outside", "outside", "outside",
    ]);
    // 段内首行滚进视口(spy 挂在原型上,所以要看调用它的那个元素)。
    expect(scrollIntoView.mock.instances).toContain(rows[1]);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
  });

  it("hoveredId 标 data-hovered,进出行回报悬停", () => {
    const onHover = vi.fn();
    render(<TrajectoryLedger {...baseProps({ onHover, hoveredId: "t1/tool:1:0" })} />);
    const rows = screen.getAllByTestId("console-traj-row");
    expect(rows[2]).toHaveAttribute("data-hovered", "true");
    expect(rows[1]).not.toHaveAttribute("data-hovered");

    fireEvent.mouseEnter(rows[1]);
    expect(onHover).toHaveBeenCalledWith("t1/assistant:1");
    fireEvent.mouseLeave(rows[1]);
    expect(onHover).toHaveBeenCalledWith(null);
  });

  it("工具行内容列拆成 名字 / 参数 / → / 结果,失败结果标红", () => {
    const records = fixtureRecords();
    const failing = { ...records[2], id: "t1/tool:1:1", index: 6, isError: true, resultText: "command not found" };
    render(<TrajectoryLedger {...baseProps({ rows: recordRows([records[2], failing]) })} />);
    const cells = screen.getAllByTestId("console-traj-content");

    expect(cells[0].querySelector(".ew-ledger__nm")).toHaveTextContent("bash");
    expect(cells[0].querySelector(".ew-ledger__args")).toHaveTextContent('{"cmd":"ls"}');
    expect(cells[0].querySelector(".ew-ledger__arr")).toHaveTextContent("→");
    expect(cells[0].querySelector(".ew-ledger__res")).toHaveTextContent("a.txt");
    expect(cells[0].querySelector(".ew-ledger__res")).not.toHaveAttribute("data-error");
    expect(cells[0]).toHaveAttribute("title", 'bash {"cmd":"ls"} → a.txt');

    expect(cells[1].querySelector(".ew-ledger__res")).toHaveAttribute("data-error", "true");
    expect(cells[1].querySelector(".ew-ledger__res")).toHaveTextContent("command not found");
  });

  it("工具行没结果:已结束写「(无输出)」,进行中留空", () => {
    const records = fixtureRecords();
    const done = { ...records[2], resultText: null };
    const running = { ...records[2], id: "t1/tool:1:1", index: 6, resultText: null, running: true };
    render(<TrajectoryLedger {...baseProps({ rows: recordRows([done, running]) })} />);
    const cells = screen.getAllByTestId("console-traj-content");
    expect(cells[0].querySelector(".ew-ledger__res")).toHaveTextContent("(无输出)");
    expect(cells[1].querySelector(".ew-ledger__res")).toBeNull();
    expect(cells[1].querySelector(".ew-ledger__arr")).toBeNull();
  });

  it("assistant 没文字时写「(仅工具调用)」", () => {
    const records = fixtureRecords();
    const silent = { ...records[1], id: "t1/assistant:0", index: 6, text: "" };
    render(<TrajectoryLedger {...baseProps({ rows: recordRows([silent, records[1]]) })} />);
    const cells = screen.getAllByTestId("console-traj-content");
    expect(cells[0]).toHaveTextContent("(仅工具调用)");
    expect(cells[0]).toHaveAttribute("title", "(仅工具调用)");
    expect(cells[1]).toHaveTextContent("我来查一下");
  });

  it("MEMORY 行正文写「召回 / 写回 N 条」,后面照常接结果", () => {
    // 账本数据层不碰 i18n(`ledger.ts:contentOf` 给 memory 的 text 是空串),
    // §九 要的「召回 N 条 → 首条摘要」只能在这一层拼出来。
    const recall: MemoryRow = {
      id: "memory:3", kind: "memory", seq: 3, step: null, status: "ok", durationMs: null,
      eventIndexes: [], serverMs: null, direction: "recall", count: 3, detail: {},
    };
    const writeback: MemoryRow = { ...recall, id: "memory:4", seq: 4, direction: "writeback", count: 1 };
    const rows = recordRows([
      rec({
        id: "t1/memory:3", index: 0, kind: "memory", row: recall, lane: 0,
        text: "", resultText: "上次说过要订周五的票",
      }),
      rec({ id: "t1/memory:4", index: 1, kind: "memory", row: writeback, lane: 2, text: "", resultText: null }),
    ]);
    render(<TrajectoryLedger {...baseProps({ rows })} />);
    const cells = screen.getAllByTestId("console-traj-content");

    expect(cells[0]).toHaveTextContent("记忆召回 · 3 条");
    expect(cells[0].querySelector(".ew-ledger__res")).toHaveTextContent("上次说过要订周五的票");
    expect(cells[0]).toHaveAttribute("title", "记忆召回 · 3 条 → 上次说过要订周五的票");

    expect(cells[1]).toHaveTextContent("记忆写回 · 1 条");
    // MEMORY 不是工具类 —— 没结果也不该补「(无输出)」。
    expect(cells[1].querySelector(".ew-ledger__res")).toBeNull();
  });

  it("未回放的轮:占位 assistant 带 data-placeholder 与状态前缀", () => {
    const records = fixtureRecords();
    const loading = { ...records[1], id: "h/assistant", text: "历史答案", placeholder: "loading" as const };
    const failed = { ...records[1], id: "h/assistant2", index: 7, text: "历史答案", placeholder: "error" as const };
    render(<TrajectoryLedger {...baseProps({ rows: recordRows([loading, failed]) })} />);
    const rows = screen.getAllByTestId("console-traj-row");
    expect(rows[0]).toHaveAttribute("data-placeholder", "loading");
    expect(rows[1]).toHaveAttribute("data-placeholder", "error");
    const cells = screen.getAllByTestId("console-traj-content");
    expect(cells[0]).toHaveTextContent("正在回放…");
    expect(cells[0]).toHaveTextContent("历史答案");
    expect(cells[1]).toHaveTextContent("回放失败,以下为文本降级");
  });

  it("折叠轮的摘要行:文案 = 过程摘要 + 时长,点击展开该轮", () => {
    const onToggleTurn = vi.fn();
    const records = fixtureRecords();
    const rows: DisplayRow[] = [
      { kind: "record", record: records[0] },
      {
        kind: "turn-summary", turnKey: "t1", turnSeq: 0, runId: "run-1", hasError: false, anchorIndex: 0,
        summary: { think: 2, tools: 1, other: 0, failed: 0, toolBreakdown: "bash ×1", durationMs: 1500 },
      },
    ];
    render(<TrajectoryLedger {...baseProps({ rows, onToggleTurn })} />);
    const summary = screen.getByTestId("console-traj-turn-summary");
    expect(summary).toHaveAttribute("data-turn-key", "t1");
    expect(summary).toHaveTextContent("思考 2 次 · 工具 1 次(bash ×1) · 1.5s");

    fireEvent.click(summary);
    expect(onToggleTurn).toHaveBeenCalledWith("t1");
  });

  it("折叠调用的摘要行:文案 = 工具明细 + 提示,点击展开该 assistant", () => {
    const onToggleOwner = vi.fn();
    const records = fixtureRecords();
    const rows: DisplayRow[] = [
      { kind: "record", record: records[1] },
      { kind: "calls-summary", ownerId: "t1/assistant:1", turnKey: "t1", count: 3, toolBreakdown: "bash ×2 · read_file ×1" },
    ];
    render(<TrajectoryLedger {...baseProps({ rows, onToggleOwner })} />);
    const summary = screen.getByTestId("console-traj-calls-summary");
    expect(summary).toHaveTextContent("bash ×2 · read_file ×1");
    expect(summary).toHaveTextContent("(调用已折叠,点击展开)");

    fireEvent.click(summary);
    expect(onToggleOwner).toHaveBeenCalledWith("t1/assistant:1");
  });

  it("还有更早的历史时首行是加载按钮,加载中禁用并换文案", () => {
    const onLoadEarlier = vi.fn();
    const props = baseProps({ hasEarlier: true, earlierCount: 7, onLoadEarlier });
    const { rerender } = render(<TrajectoryLedger {...props} />);
    const button = screen.getByTestId("console-traj-load-earlier");
    expect(button).toHaveTextContent("加载更早的历史(还有 7 轮)");
    expect(button).not.toBeDisabled();
    // 它排在第一条记录行之前。
    const allRows = screen.getByRole("grid").querySelectorAll("tr");
    expect(allRows[0]).toContainElement(button);
    expect(screen.getByRole("grid")).toHaveAttribute("aria-rowcount", "7");

    fireEvent.click(button);
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    rerender(<TrajectoryLedger {...props} loadingEarlier />);
    expect(screen.getByTestId("console-traj-load-earlier")).toBeDisabled();
    expect(screen.getByTestId("console-traj-load-earlier")).toHaveTextContent("正在加载更早的历史…");
  });

  it("loading 时挂粘性覆盖层,滚动容器照旧渲染", () => {
    const props = baseProps();
    const { rerender } = render(<TrajectoryLedger {...props} />);
    expect(screen.queryByTestId("console-traj-loading")).toBeNull();
    const container = screen.getByTestId("console-traj-ledger");

    rerender(<TrajectoryLedger {...props} loading />);
    expect(screen.getByTestId("console-traj-loading")).toHaveTextContent("正在加载轨迹…");
    // 同一个 DOM 节点 —— 容器不能被 loading 态换掉(虚拟化挂在它上面)。
    expect(screen.getByTestId("console-traj-ledger")).toBe(container);
    expect(screen.getAllByTestId("console-traj-row")).toHaveLength(6);
  });

  it("100 行时只渲染视口窗口,上下留 spacer 行", () => {
    const many = Array.from({ length: 100 }, (_, i) =>
      rec({
        id: `t1/row-${i}`, index: i, kind: "tool", row: toolRow,
        text: `bash {"i":${i}}`, resultText: "ok",
      }),
    );
    render(<TrajectoryLedger {...baseProps({ rows: recordRows(many) })} />);
    const container = screen.getByTestId("console-traj-ledger");
    // 未量到高度前渲染全部(useVirtualRows 的 clientHeight === 0 分支)。
    expect(screen.getAllByTestId("console-traj-row")).toHaveLength(100);

    mockGeometry(container, { clientHeight: 500, scrollTop: 0 });
    fireEvent.scroll(container);

    const rendered = screen.getAllByTestId("console-traj-row");
    expect(rendered.length).toBeLessThan(100);
    // 行高 27 + overscan 12 → ceil(500/27)+12 = 31 行。
    expect(rendered).toHaveLength(31);
    expect(document.querySelector('[data-virtual-spacer="top"]')).toBeNull();
    const bottom = document.querySelector('[data-virtual-spacer="bottom"] td');
    expect(bottom).not.toBeNull();
    expect((bottom as HTMLElement).style.height).toBe(`${(100 - 31) * 27}px`);

    mockGeometry(container, { scrollTop: 1350 });
    fireEvent.scroll(container);
    const top = document.querySelector('[data-virtual-spacer="top"] td');
    expect(top).not.toBeNull();
    // start = floor(1350/27) - 12 = 38。
    expect((top as HTMLElement).style.height).toBe(`${38 * 27}px`);
  });

  it("scrollTo 的 nonce 变化时把那一行滚到中间,nonce 不变不重复滚", () => {
    const props = baseProps();
    const { rerender } = render(<TrajectoryLedger {...props} />);
    scrollIntoView.mockClear();

    rerender(<TrajectoryLedger {...props} scrollTo={{ id: "t1/tool:1:0", nonce: 1 }} />);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.instances[0]).toBe(screen.getAllByTestId("console-traj-row")[2]);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });

    scrollIntoView.mockClear();
    rerender(<TrajectoryLedger {...props} scrollTo={{ id: "t1/tool:1:0", nonce: 1 }} hoveredId="t2/user" />);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("目标行在虚拟窗口外时改用 scrollTop 兜底(scrollIntoView 无从谈起)", () => {
    const many = Array.from({ length: 100 }, (_, i) =>
      rec({ id: `t1/row-${i}`, index: i, kind: "tool", row: toolRow, text: `bash {"i":${i}}`, resultText: "ok" }),
    );
    const props = baseProps({ rows: recordRows(many) });
    const { rerender } = render(<TrajectoryLedger {...props} />);
    const container = screen.getByTestId("console-traj-ledger");
    mockGeometry(container, { clientHeight: 500, scrollTop: 0 });
    fireEvent.scroll(container);
    const rendered = screen.getAllByTestId("console-traj-row").map((r) => r.dataset.recordId);
    expect(rendered).not.toContain("t1/row-80");
    scrollIntoView.mockClear();

    rerender(<TrajectoryLedger {...props} scrollTo={{ id: "t1/row-80", nonce: 1 }} />);
    // DOM 里没有这一行 —— 只能自己算:80 × 27 − 500 / 2 = 1910(center)。
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(container.scrollTop).toBe(1910);
  });

  it("窗口外的选中行兜底时把「加载更早」那一行算进偏移", () => {
    const many = Array.from({ length: 100 }, (_, i) =>
      rec({ id: `t1/row-${i}`, index: i, kind: "tool", row: toolRow, text: "bash {}", resultText: "ok" }),
    );
    const props = baseProps({ rows: recordRows(many), hasEarlier: true, earlierCount: 4 });
    const { rerender } = render(<TrajectoryLedger {...props} />);
    const container = screen.getByTestId("console-traj-ledger");
    mockGeometry(container, { clientHeight: 500, scrollTop: 0 });
    fireEvent.scroll(container);
    scrollIntoView.mockClear();

    rerender(<TrajectoryLedger {...props} selectedId="t1/row-80" />);
    // nearest 顶对齐,前面还压着一行「加载更早」:(1 + 80) × 27 = 2187。
    expect(container.scrollTop).toBe(2187);
  });

  it("首批行到达时跟到最新,且只做一次", () => {
    const props = baseProps({ rows: [] });
    const { rerender } = render(<TrajectoryLedger {...props} />);
    const container = screen.getByTestId("console-traj-ledger");
    mockGeometry(container, { clientHeight: 100, scrollHeight: 200, scrollTop: 0 });

    rerender(<TrajectoryLedger {...props} rows={recordRows(fixtureRecords())} />);
    expect(container.scrollTop).toBe(200);

    // 第二次长行不再自动跟(没在 running) —— 初始尾随是一次性的。
    mockGeometry(container, { scrollTop: 0, scrollHeight: 300 });
    const grown = [...fixtureRecords(), { ...fixtureRecords()[0], id: "t3/user", index: 6 }];
    rerender(<TrajectoryLedger {...props} rows={recordRows(grown)} />);
    expect(container.scrollTop).toBe(0);
  });

  it("初始尾随让位给 scrollTo 与已选中行", () => {
    const withTarget = baseProps({ rows: [], scrollTo: { id: "t1/user", nonce: 1 } });
    const { rerender } = render(<TrajectoryLedger {...withTarget} />);
    const container = screen.getByTestId("console-traj-ledger");
    mockGeometry(container, { clientHeight: 100, scrollHeight: 200, scrollTop: 0 });
    rerender(<TrajectoryLedger {...withTarget} rows={recordRows(fixtureRecords())} />);
    expect(container.scrollTop).toBe(0);

    // 换第二组 props 前先卸载 —— 两棵树同时挂着的话 testid 会撞。
    cleanup();
    const withSelection = baseProps({ rows: [], selectedId: "t1/user" });
    const second = render(<TrajectoryLedger {...withSelection} />);
    const el = second.getByTestId("console-traj-ledger");
    mockGeometry(el, { clientHeight: 100, scrollHeight: 200, scrollTop: 0 });
    second.rerender(<TrajectoryLedger {...withSelection} rows={recordRows(fixtureRecords())} />);
    expect(el.scrollTop).toBe(0);
  });

  it("子代理行名字取 worker.label(两个词也不拆),参数取 taskExcerpt", () => {
    const worker: SubagentRow["worker"] = {
      workerId: "w-1", parentWorkerId: null, parentToolCallId: "call_1", label: "research worker",
      agentRef: "researcher", depth: 1, role: null, taskExcerpt: "查一下 2026 年的票价",
      maxSteps: null, status: "success", steps: [], children: [], summary: null,
    };
    const subRow: SubagentRow = {
      id: "subagent:1:0:0", kind: "subagent", seq: 1, step: 1, status: "ok", durationMs: null,
      eventIndexes: [], serverMs: null, parentEntryId: "call_1", worker,
    };
    const rows = recordRows([
      rec({
        id: "t1/subagent:1:0:0", index: 0, kind: "subagent", row: subRow, lane: 2,
        text: "research worker 查一下 2026 年的票价", resultText: "3 次模型调用 · 900ms",
      }),
    ]);
    render(<TrajectoryLedger {...baseProps({ rows })} />);
    const cell = screen.getByTestId("console-traj-content");
    expect(cell.querySelector(".ew-ledger__nm")).toHaveTextContent("research worker");
    expect(cell.querySelector(".ew-ledger__args")).toHaveTextContent("查一下 2026 年的票价");
    expect(cell).toHaveAttribute("title", "research worker 查一下 2026 年的票价 → 3 次模型调用 · 900ms");
  });

  it("选中行变化时把它滚进视口", () => {
    const props = baseProps();
    const { rerender } = render(<TrajectoryLedger {...props} />);
    scrollIntoView.mockClear();

    rerender(<TrajectoryLedger {...props} selectedId="t2/user" />);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
    expect(scrollIntoView.mock.instances[0]).toBe(screen.getAllByTestId("console-traj-row")[4]);
  });

  it("运行中且贴底时新行到达自动滚到底,上滚超过 80px 则不跟随", () => {
    const records = fixtureRecords();
    const props = baseProps({ running: true, rows: recordRows(records.slice(0, 5)) });
    const { rerender } = render(<TrajectoryLedger {...props} />);
    const container = screen.getByTestId("console-traj-ledger");

    mockGeometry(container, { clientHeight: 100, scrollHeight: 200, scrollTop: 100 });
    rerender(<TrajectoryLedger {...props} rows={recordRows(records)} />);
    expect(container.scrollTop).toBe(200);

    // 上滚 100px(> 80px 松弛)后不再跟随。
    mockGeometry(container, { scrollTop: 0 });
    rerender(<TrajectoryLedger {...props} rows={recordRows([...records, { ...records[0], id: "t3/user", index: 6 }])} />);
    expect(container.scrollTop).toBe(0);
  });

  it("不在运行的会话不会因为行数增长而滚动", () => {
    const records = fixtureRecords();
    const props = baseProps({ running: false, rows: recordRows(records.slice(0, 5)) });
    const { rerender } = render(<TrajectoryLedger {...props} />);
    const container = screen.getByTestId("console-traj-ledger");
    mockGeometry(container, { clientHeight: 100, scrollHeight: 200, scrollTop: 100 });

    rerender(<TrajectoryLedger {...props} rows={recordRows(records)} />);
    expect(container.scrollTop).toBe(100);
  });

  it("类型标签按记录类型取文案与样式(subagent → SUBTOOL,compaction → COMPACTED)", () => {
    const marker: TrajectoryRow = {
      id: "compaction:9", kind: "compaction", seq: 9, step: null, status: "ok",
      durationMs: null, eventIndexes: [], serverMs: null, text: "压缩了 12 条消息",
    };
    const rows = recordRows([
      rec({ id: "a", index: 0, kind: "subagent", row: toolRow, text: "worker 查资料" }),
      rec({ id: "b", index: 1, kind: "compaction", row: marker, text: "压缩了 12 条消息" }),
      rec({ id: "c", index: 2, kind: "user", row: userRow, text: "问题" }),
    ]);
    render(<TrajectoryLedger {...baseProps({ rows })} />);
    const tags = screen.getAllByTestId("console-traj-kind");
    expect(tags.map((tag) => tag.textContent)).toEqual(["SUBTOOL", "COMPACTED", "USER"]);
    expect(tags[0].className).toContain("ew-kt--subagent");
    expect(tags[2].className).toContain("ew-kt--user");
  });

  // PR-A.3 §十.1 Task 12 —— SYSTEM 行:kind 标签走同一套 kindLabel(t) →
  // "SYSTEM",内容列取账本层已经折好的首行,data-kind 落 "system"。颜色
  // 显式写在 trajectory_timeline.css 里,不靠 .ew-traj-tl__block 的默认兜底
  // (那条默认值恰好也是 --ew-text-secondary,不显式写就测不出这条规则是否
  // 真的存在)。
  it("SYSTEM 行:kind 标签 SYSTEM,内容列 = 首行,data-kind=\"system\"", () => {
    const sysRow: SystemRow = {
      id: "system", kind: "system", seq: -1, step: null, status: "ok", durationMs: null, inputs: null,
      eventIndexes: [], serverMs: null, text: "你是评审员",
    };
    const rows = recordRows([
      rec({ id: "t1/system", index: 0, kind: "system", row: sysRow, lane: 0, turnStart: true, text: "你是评审员" }),
    ]);
    render(<TrajectoryLedger {...baseProps({ rows })} />);

    const tag = screen.getByTestId("console-traj-kind");
    expect(tag).toHaveTextContent("SYSTEM");
    expect(tag.className).toContain("ew-kt--system");
    expect(screen.getByTestId("console-traj-row")).toHaveAttribute("data-kind", "system");
    expect(screen.getByTestId("console-traj-content")).toHaveTextContent("你是评审员");

    const timeline = readFileSync("src/components/console/trajectory_timeline.css", "utf8");
    const rule = /\.ew-traj-tl__block\[data-kind="system"\] \{([^}]*)\}/.exec(timeline)?.[1] ?? "";
    expect(rule).toContain("background: var(--ew-text-secondary)");
  });
});
