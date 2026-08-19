/**
 * TrajectoryView 测试(PR-A.2 Task 10)—— 工具条 / 时间轴 / 账本 / 详情的组合
 * 与状态源:选中与请求详情二选一、Escape、时间轴选区 ↔ 账本 focus、搜索、两个
 * 全折、投影模式落 localStorage、`focusRequest` 联动、加载窗口与「加载更早」、
 * 窗口内 pending 轮自动回放、换会话复位、运行中、空态。
 *
 * fixture 手造 `ConsoleTurn`(三轮:history done / history pending / live
 * running),真跑 `buildLedger`;`useRunTrace` 打桩成「没有 trace」。
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import i18n from "../../../i18n";
import type { SseEvent } from "../../../api/sessions";
import type { LiveStep } from "../../../pages/agent_detail/playground/useTokenStream";
import { LANE_MODE_KEY, TrajectoryView, type TrajectoryViewProps } from "../TrajectoryView";
import type { ConsoleTurn } from "../types";

// 详情面板真跑,但 trace 拉取不该在测里发请求(useRunTrace 还要 TenantScope
// provider)—— 整体打桩成「没有 trace」,顺手把入参记下来(runId 跟着哪一轮走
// 是 Task 10 的硬要求,不记就没法断言)。
const hoisted = vi.hoisted(() => ({ runTraceArgs: vi.fn() }));
vi.mock("../useRunTrace", () => ({
  useRunTrace: (args: unknown) => {
    hoisted.runTraceArgs(args);
    return { trace: null, loading: false, refresh: () => undefined, traceId: null };
  },
}));
// ToolCallCard 的「立即触发」按钮读 useIsTenantSwitched(这里没挂 Auth /
// TenantScope provider)—— 同 RecordDetails.test.tsx 的桩。
vi.mock("../../../tenant/useIsTenantSwitched", () => ({ useIsTenantSwitched: () => false }));

// jsdom 25 没有 `PointerEvent`,testing-library 会退回基类 `Event`(静默丢掉
// `clientX`)—— 用 `MouseEvent` 派生的替身补上坐标(同 TrajectoryTimeline.test)。
class PointerEventPolyfill extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}
if (!("PointerEvent" in window)) {
  (window as unknown as { PointerEvent: unknown }).PointerEvent = PointerEventPolyfill;
}

const BASE = 1_700_000_000_000;
const RECT_1000 = {
  left: 0, top: 0, right: 1000, bottom: 50, width: 1000, height: 50, x: 0, y: 0, toJSON: () => ({}),
} as DOMRect;

let seqCounter = 0;
/** `skewMs` = 客户端收到这一帧的时刻比帧自己的服务端毫秒晚多少(时钟差)。 */
function ev(event: string, data: unknown, ms: number, skewMs = 0): SseEvent {
  seqCounter += 1;
  return {
    id: `${BASE + ms}-${seqCounter}`,
    event,
    data,
    rawData: "",
    receivedAt: new Date(BASE + ms + skewMs).toISOString(),
  };
}
function upd(node: string, channels: Record<string, unknown>, ms: number, skewMs = 0): SseEvent {
  return ev("updates", { [node]: channels }, ms, skewMs);
}
function call(id: string, name: string, args: Record<string, unknown> = {}) {
  return { id, name, args };
}
function result(id: string, name: string, durationMs: number, content = "ok") {
  return {
    type: "tool", tool_call_id: id, name, content, status: "success",
    additional_kwargs: { duration_ms: durationMs },
  };
}

function turnOf(over: Partial<ConsoleTurn> & Pick<ConsoleTurn, "key" | "seq">): ConsoleTurn {
  return {
    source: "history",
    turn: {
      id: over.key, input: "问题", attachments: [], inputs: {}, events: [],
      status: "done", error: null, approval: null,
    },
    runId: `run-${over.seq}`,
    loadState: "done",
    fallbackLines: [],
    tokens: null,
    timing: null,
    createdAt: new Date(BASE + over.seq * 10_000).toISOString(),
    ...over,
  };
}

/** 轮 A(history · done):step1 调 `query_crm`,step2 终答。 */
function turnAEvents(): SseEvent[] {
  return [
    upd("agent", { step_count: 1, _duration_ms: 400, messages: [{
      type: "ai", content: "",
      additional_kwargs: { reasoning_content: "先查客户档案" },
      usage_metadata: { input_tokens: 100, output_tokens: 20 },
      tool_calls: [call("c1", "query_crm", { id: "C-1" })],
    }] }, 1000),
    upd("tools", { messages: [result("c1", "query_crm", 300, "3 条记录")] }, 1400),
    upd("agent", { step_count: 2, _duration_ms: 500, messages: [{
      type: "ai", content: "结论",
      response_metadata: { model_name: "gpt-x", finish_reason: "stop" },
      usage_metadata: { input_tokens: 200, output_tokens: 40 },
    }] }, 2300),
  ];
}
/** 轮 C(live · running):step1 已落帧,工具 `search` 还挂着。 */
function turnCEvents(skewMs = 0): SseEvent[] {
  return [
    upd("agent", { step_count: 1, _duration_ms: 300, messages: [{
      type: "ai", content: "先搜一下",
      usage_metadata: { input_tokens: 50, output_tokens: 10 },
      tool_calls: [call("c9", "search")],
    }] }, 5000, skewMs),
  ];
}

/** 9 条记录:A(user/assistant:0/tool/assistant:1)· B(user/占位)· C(user/assistant:0/tool)。 */
function fixture(skewMs = 0): ConsoleTurn[] {
  return [
    turnOf({ key: "A", seq: 0, turn: {
      id: "A", input: "问题 A", attachments: [], inputs: {}, events: turnAEvents(),
      status: "done", error: null, approval: null,
    } }),
    turnOf({ key: "B", seq: 1, loadState: "pending",
      fallbackLines: [{ text: "历史回答", channel: "final" }] }),
    turnOf({ key: "C", seq: 2, source: "live", turn: {
      id: "C", input: "问题 C", attachments: [], inputs: {}, events: turnCEvents(skewMs),
      status: "running", error: null, approval: null,
    } }),
  ];
}

/** N 轮 history(全 pending)—— 加载窗口用。 */
function manyTurns(n: number): ConsoleTurn[] {
  return Array.from({ length: n }, (_, i) => turnOf({ key: `T${i}`, seq: i, loadState: "pending" }));
}

function baseProps(over: Partial<TrajectoryViewProps> = {}): TrajectoryViewProps {
  return {
    turns: fixture(),
    threadId: "th-1",
    streamTurnKey: "C",
    liveByStep: new Map<number, LiveStep>(),
    running: false,
    isSystemAdmin: false,
    focusRequest: null,
    onEnsureLoaded: vi.fn().mockResolvedValue(undefined),
    ...over,
  };
}

function renderView(over: Partial<TrajectoryViewProps> = {}): {
  props: TrajectoryViewProps;
  rerender: (next: Partial<TrajectoryViewProps>) => void;
} {
  const props = baseProps(over);
  const ui = (p: TrajectoryViewProps) => (
    <MemoryRouter>
      <App>
        <TrajectoryView {...p} />
      </App>
    </MemoryRouter>
  );
  const view = render(ui(props));
  return {
    props,
    rerender: (next) => view.rerender(ui({ ...props, ...next })),
  };
}

function rows(): HTMLElement[] {
  return screen.getAllByTestId("console-traj-row");
}
function rowOf(recordId: string): HTMLElement {
  const found = rows().find((r) => r.dataset.recordId === recordId);
  if (found === undefined) throw new Error(`no ledger row for ${recordId}`);
  return found;
}
function blocks(): HTMLElement[] {
  return screen.getAllByTestId("console-lane-block");
}
function track(): HTMLElement {
  return screen.getByTestId("console-lane-track");
}
/** 轨道上从 `x0` 拖到 `x1`(过 3px 阈值 → 提交区间)。 */
function drag(x0: number, x1: number): void {
  fireEvent.pointerDown(track(), { clientX: x0, pointerId: 1, button: 0 });
  fireEvent.pointerMove(track(), { clientX: x1, pointerId: 1 });
  fireEvent.pointerUp(track(), { clientX: x1, pointerId: 1, button: 0 });
}

let scrollIntoView: ReturnType<typeof vi.fn>;
let priorLang: string;

beforeAll(async () => {
  priorLang = i18n.language;
  await i18n.changeLanguage("zh-CN");
  HTMLElement.prototype.setPointerCapture = () => undefined;
  HTMLElement.prototype.releasePointerCapture = () => undefined;
});
afterAll(async () => {
  Reflect.deleteProperty(HTMLElement.prototype, "setPointerCapture");
  Reflect.deleteProperty(HTMLElement.prototype, "releasePointerCapture");
  await i18n.changeLanguage(priorLang);
});
beforeEach(() => {
  window.localStorage.clear();
  hoisted.runTraceArgs.mockClear();
  scrollIntoView = vi.fn();
  // jsdom 没有 scrollIntoView;组件按可选调用写,这里给它一个 spy。
  (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView = scrollIntoView;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_1000);
});
afterEach(() => {
  cleanup();
  delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView;
  vi.restoreAllMocks();
});

describe("TrajectoryView · 组合", () => {
  it("一个根节点里装出工具条 / 时间轴 / 账本三件", () => {
    renderView();
    expect(screen.getByTestId("console-trajectory-panel")).toBeInTheDocument();
    expect(screen.getByTestId("console-traj-toolbar")).toBeInTheDocument();
    expect(screen.getByTestId("console-lane-strip")).toBeInTheDocument();
    expect(screen.getByTestId("console-traj-ledger")).toBeInTheDocument();
    expect(rows().map((r) => r.dataset.recordId)).toEqual([
      "A/user", "A/assistant:0", "A/tool:0:0", "A/assistant:1",
      "B/user", "B/assistant:placeholder",
      "C/user", "C/assistant:0", "C/tool:0:0",
    ]);
    // 详情未选中时不占位。
    expect(screen.queryByTestId("console-detail-aside")).not.toBeInTheDocument();
    // 三轮记录都有时序 → 不报「时长不可用」。
    expect(screen.queryByTestId("console-traj-degraded")).not.toBeInTheDocument();
  });

  it("有记录拿不到时序 → 工具条标「时长不可用」", () => {
    // 未回放又没有 createdAt 的轮:占位记录落不到绝对时刻上。
    const turns = fixture();
    renderView({ turns: [turns[0], { ...turns[1], createdAt: null }, turns[2]] });
    expect(screen.getByTestId("console-traj-degraded")).toHaveTextContent("时长不可用");
  });

  it("useRunTrace 的 runId 跟着详情那条记录所在的轮走", () => {
    const view = renderView();
    const lastArgs = (): Record<string, unknown> =>
      hoisted.runTraceArgs.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(lastArgs()).toMatchObject({ runId: null, enabled: false });

    fireEvent.click(rowOf("A/tool:0:0"));
    expect(lastArgs()).toMatchObject({ runId: "run-0", enabled: true, turnStatus: "done" });

    fireEvent.click(rowOf("C/assistant:0"));
    expect(lastArgs()).toMatchObject({ runId: "run-2", enabled: true, turnStatus: "running" });

    // 选中请求圆点走的是同一条路(请求详情落在它的 assistant 记录上)。
    fireEvent.click(screen.getAllByTestId("console-traj-request-dot")[0]);
    expect(lastArgs()).toMatchObject({ runId: "run-0", enabled: true });

    view.rerender({ isSystemAdmin: true });
    expect(lastArgs()).toMatchObject({ wantTraceId: true });
  });

  it("turns 为空 → 空态,不渲染工具条 / 账本", () => {
    renderView({ turns: [] });
    expect(screen.getByTestId("console-trajectory-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("console-traj-ledger")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-traj-toolbar")).not.toBeInTheDocument();
  });

  it("点账本行 → 记录详情打开,时间轴对应块标 data-current", () => {
    renderView();
    fireEvent.click(rowOf("A/tool:0:0"));

    expect(screen.getByTestId("console-detail-aside")).toBeInTheDocument();
    expect(screen.getByTestId("console-detail-tab-payload")).toBeInTheDocument();
    expect(rowOf("A/tool:0:0")).toHaveAttribute("data-selected", "true");
    const current = blocks().filter((b) => b.getAttribute("data-current") === "true");
    expect(current.map((b) => b.getAttribute("data-index"))).toEqual(["2"]);
  });

  it("账本悬停 ↔ 时间轴块高亮;当前轮默认最新轮,选中后跟着选中走", () => {
    renderView();
    const activeFlags = (): (string | undefined)[] =>
      screen.getAllByTestId("console-traj-turn-label").map((l) => l.dataset.active);
    expect(activeFlags()).toEqual([undefined, undefined, "true"]);

    fireEvent.mouseEnter(rowOf("A/tool:0:0"));
    const hovered = blocks().filter((b) => b.getAttribute("data-hovered") === "true");
    expect(hovered.map((b) => b.getAttribute("data-index"))).toEqual(["2"]);
    fireEvent.mouseLeave(rowOf("A/tool:0:0"));
    expect(blocks().some((b) => b.getAttribute("data-hovered") === "true")).toBe(false);

    fireEvent.click(rowOf("A/tool:0:0"));
    expect(activeFlags()).toEqual(["true", undefined, undefined]);
  });

  it("点请求圆点 → 请求详情(与记录详情二选一)", () => {
    renderView();
    fireEvent.click(rowOf("A/assistant:0"));
    // assistant 记录的 tab 集:概要 / 预览 / 原文 / 计时 / 原始。
    expect(screen.getByTestId("console-detail-tab-preview")).toBeInTheDocument();

    const dots = screen.getAllByTestId("console-traj-request-dot");
    fireEvent.click(dots[0]);
    expect(screen.getByTestId("console-detail-tab-usage")).toBeInTheDocument();
    expect(screen.queryByTestId("console-detail-tab-preview")).not.toBeInTheDocument();
    expect(dots[0]).toHaveAttribute("data-active", "true");
    expect(rowOf("A/assistant:0")).not.toHaveAttribute("data-selected");
  });

  it("Escape:有详情先关详情,没有详情再清选区", () => {
    renderView();
    fireEvent.click(rowOf("A/tool:0:0"));
    drag(100, 400);
    expect(rowOf("A/user")).toHaveAttribute("data-focus", "inside");

    const root = screen.getByTestId("console-trajectory-panel");
    fireEvent.keyDown(root, { key: "Escape" });
    expect(screen.queryByTestId("console-detail-aside")).not.toBeInTheDocument();
    // 详情关掉了,选区还在。
    expect(rowOf("A/user")).toHaveAttribute("data-focus", "inside");

    fireEvent.keyDown(root, { key: "Escape" });
    expect(rowOf("A/user")).not.toHaveAttribute("data-focus");
  });

  it("焦点在时间轴轨道上按 Escape:只清选区,详情不跟着一起关", () => {
    renderView();
    fireEvent.click(rowOf("A/tool:0:0"));
    drag(100, 400);
    expect(rowOf("A/user")).toHaveAttribute("data-focus", "inside");

    // 轨道自己吃掉这一下(清选区并 preventDefault),根节点必须让位。
    fireEvent.keyDown(track(), { key: "Escape" });
    expect(rowOf("A/user")).not.toHaveAttribute("data-focus");
    expect(screen.getByTestId("console-detail-aside")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId("console-trajectory-panel"), { key: "Escape" });
    expect(screen.queryByTestId("console-detail-aside")).not.toBeInTheDocument();
  });

  it("时间轴拖选 → 账本行分 inside / outside", () => {
    renderView();
    // 轨道 1000px / 顺序域 [0,9):x=100 → 0.9,x=400 → 3.6 → 命中 0..3。
    drag(100, 400);
    expect(rows().map((r) => r.dataset.focus)).toEqual([
      "inside", "inside", "inside", "inside", "outside", "outside", "outside", "outside", "outside",
    ]);
  });

  it("点选区外的账本行 → 清掉选区", () => {
    renderView();
    drag(100, 400);
    expect(rowOf("B/assistant:placeholder")).toHaveAttribute("data-focus", "outside");

    fireEvent.click(rowOf("B/assistant:placeholder"));
    expect(rowOf("B/assistant:placeholder")).not.toHaveAttribute("data-focus");
    expect(rowOf("B/assistant:placeholder")).toHaveAttribute("data-selected", "true");
  });

  it("搜索:账本只剩匹配行,时间轴块标 data-search-match,工具条报条数", () => {
    renderView();
    fireEvent.change(screen.getByTestId("console-traj-search"), { target: { value: "query_crm" } });

    expect(rows().map((r) => r.dataset.recordId)).toEqual(["A/tool:0:0"]);
    expect(screen.getByTestId("console-traj-search-count")).toHaveTextContent("1 条匹配");
    const matched = blocks().filter((b) => b.getAttribute("data-search-match") === "true");
    expect(matched.map((b) => b.getAttribute("data-index"))).toEqual(["2"]);
    expect(blocks().filter((b) => b.getAttribute("data-search-match") === "false")).toHaveLength(8);
  });

  it("「轮次」全折 → 每个可折的轮缩成一条 turn-summary", () => {
    renderView();
    fireEvent.click(screen.getByTestId("console-traj-collapse-turns"));

    // A / C 各 ≥ 2 条非 USER 记录可折;B 只有一条占位,不折。
    expect(screen.getAllByTestId("console-traj-turn-summary")).toHaveLength(2);
    expect(rows().map((r) => r.dataset.recordId)).toEqual([
      "A/user", "B/user", "B/assistant:placeholder", "C/user",
    ]);
    expect(screen.getByTestId("console-traj-collapse-turns")).toHaveAttribute("aria-pressed", "true");
  });

  it("「调用」全折 → 每条有子记录的 assistant 后面缩成一条 calls-summary", () => {
    renderView();
    fireEvent.click(screen.getByTestId("console-traj-collapse-calls"));

    expect(screen.getAllByTestId("console-traj-calls-summary")).toHaveLength(2);
    expect(rows().map((r) => r.dataset.recordId)).toEqual([
      "A/user", "A/assistant:0", "A/assistant:1",
      "B/user", "B/assistant:placeholder",
      "C/user", "C/assistant:0",
    ]);
  });

  it("「时长」按钮切投影并写进 localStorage,再挂载沿用", () => {
    const first = renderView();
    expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-mode", "sequence");
    fireEvent.click(screen.getByTestId("console-lane-mode"));
    expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-mode", "duration");
    expect(window.localStorage.getItem(LANE_MODE_KEY)).toBe("duration");
    first.rerender({});

    cleanup();
    renderView();
    expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-mode", "duration");
  });

  it("切投影模式清掉选区", () => {
    // 故意用**退化**的时长投影(有记录没时序):它按顺序排布,域与顺序模式
    // 一模一样 —— 于是「域变了所以清」那条路走不到,只有「切模式就清」这条
    // 判据能让选区消失。
    const turns = fixture();
    renderView({ turns: [turns[0], { ...turns[1], createdAt: null }, turns[2]] });
    drag(100, 400);
    expect(rowOf("A/user")).toHaveAttribute("data-focus", "inside");

    fireEvent.click(screen.getByTestId("console-lane-mode"));
    expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-degraded", "true");
    expect(rowOf("A/user")).not.toHaveAttribute("data-focus");
  });

  it("focusRequest 指向 think:<seq> → 选中同一步的 assistant 记录并滚过去", () => {
    const view = renderView();
    view.rerender({ focusRequest: { turnKey: "A", rowId: "think:0", nonce: 1 } });

    expect(rowOf("A/assistant:0")).toHaveAttribute("data-selected", "true");
    expect(screen.getByTestId("console-detail-aside")).toBeInTheDocument();
    expect(scrollIntoView).toHaveBeenCalled();
  });

  it("focusRequest 的 rowId 为 null → 落到该轮最后一条 assistant 记录", () => {
    const view = renderView();
    view.rerender({ focusRequest: { turnKey: "A", rowId: null, nonce: 7 } });
    expect(rowOf("A/assistant:1")).toHaveAttribute("data-selected", "true");

    // 同一 nonce 重渲染不再处理:手动改选中后不该被拽回去。
    fireEvent.click(rowOf("A/user"));
    view.rerender({ focusRequest: { turnKey: "A", rowId: null, nonce: 7 } });
    expect(rowOf("A/user")).toHaveAttribute("data-selected", "true");
  });

  it("focusRequest 指向窗口外的轮 → 先扩窗、等回放落帧、再选中", async () => {
    const turns = manyTurns(25);
    const view = renderView({ turns, threadId: "th-many" });
    const onEnsureLoaded = view.props.onEnsureLoaded as ReturnType<typeof vi.fn>;
    expect(screen.queryByText("第 1 轮")).not.toBeInTheDocument();
    onEnsureLoaded.mockClear();

    view.rerender({ focusRequest: { turnKey: "T0", rowId: null, nonce: 3 } });
    // 窗口已扩到含 T0,并请父级回放它;该轮此刻只有占位行,选中先不落。
    expect(screen.getByText("第 1 轮")).toBeInTheDocument();
    expect(onEnsureLoaded).toHaveBeenCalledWith(["run-0"]);
    expect(rowOf("T0/assistant:placeholder")).not.toHaveAttribute("data-selected");

    // 父级回放完成 → 落到该轮最后一条 assistant 记录。
    const replayed = turns.map((turn) =>
      turn.key !== "T0" ? turn : {
        ...turn, loadState: "done" as const,
        turn: { ...turn.turn, events: turnAEvents() },
      });
    view.rerender({ turns: replayed, focusRequest: { turnKey: "T0", rowId: null, nonce: 3 } });
    await waitFor(() => {
      expect(rowOf("T0/assistant:1")).toHaveAttribute("data-selected", "true");
    });
  });

  it("加载窗口:25 轮只渲染最后 20 轮,点「加载更早」补齐前 5 轮", async () => {
    const turns = manyTurns(25);
    const props = baseProps({ turns, threadId: "th-many" });
    const onEnsureLoaded = props.onEnsureLoaded as ReturnType<typeof vi.fn>;
    render(
      <MemoryRouter>
        <App>
          <TrajectoryView {...props} />
        </App>
      </MemoryRouter>,
    );

    expect(screen.getAllByTestId("console-traj-turn-label")).toHaveLength(20);
    const earlier = screen.getByTestId("console-traj-load-earlier");
    expect(earlier).toHaveTextContent("还有 5 轮");

    onEnsureLoaded.mockClear();
    fireEvent.click(earlier);
    expect(onEnsureLoaded).toHaveBeenCalledTimes(1);
    expect(onEnsureLoaded.mock.calls[0][0]).toEqual(["run-0", "run-1", "run-2", "run-3", "run-4"]);
    await waitFor(() => {
      expect(screen.getAllByTestId("console-traj-turn-label")).toHaveLength(25);
    });
    expect(screen.queryByTestId("console-traj-load-earlier")).not.toBeInTheDocument();
  });

  it("账本加载覆盖层看的是「窗口内还有轮在回放」,不是「加载更早」按钮", () => {
    // 默认 fixture 里 B 还没回放 → 覆盖层在。
    const view = renderView();
    expect(screen.getByTestId("console-traj-loading")).toBeInTheDocument();

    const turns = fixture();
    view.rerender({ turns: [turns[0], { ...turns[1], loadState: "done" as const }, turns[2]] });
    expect(screen.queryByTestId("console-traj-loading")).not.toBeInTheDocument();
  });

  it("focusRequest 的目标轮还没进 turns → 不烧掉 nonce,轮到货后照样受理", () => {
    const view = renderView();
    view.rerender({ focusRequest: { turnKey: "D", rowId: null, nonce: 9 } });
    expect(rows().every((r) => r.dataset.selected === undefined)).toBe(true);

    const turnD = turnOf({ key: "D", seq: 3, turn: {
      id: "D", input: "问题 D", attachments: [], inputs: {}, events: turnAEvents(),
      status: "done", error: null, approval: null,
    } });
    view.rerender({ turns: [...fixture(), turnD], focusRequest: { turnKey: "D", rowId: null, nonce: 9 } });
    expect(rowOf("D/assistant:1")).toHaveAttribute("data-selected", "true");
  });

  it("轮数缩水(历史重载)→ 窗口起点夹回去,不把窗口滑空", () => {
    const view = renderView({ turns: manyTurns(25), threadId: "th-many" });
    expect(screen.getAllByTestId("console-traj-turn-label")).toHaveLength(20);
    expect(screen.getByTestId("console-traj-load-earlier")).toBeInTheDocument();

    view.rerender({ turns: manyTurns(3) });
    expect(screen.getAllByTestId("console-traj-turn-label")).toHaveLength(3);
    expect(screen.queryByTestId("console-traj-load-earlier")).not.toBeInTheDocument();
  });

  it("focusRequest 指向同一条记录的新 nonce 会再滚一次", () => {
    const view = renderView();
    view.rerender({ focusRequest: { turnKey: "A", rowId: "think:0", nonce: 1 } });
    expect(rowOf("A/assistant:0")).toHaveAttribute("data-selected", "true");
    const first = scrollIntoView.mock.calls.length;

    // 选中没变(还是同一条),只有 scrollTo 的 nonce 变了 —— 账本必须再滚一次。
    view.rerender({ focusRequest: { turnKey: "A", rowId: "think:0", nonce: 2 } });
    expect(scrollIntoView.mock.calls.length).toBeGreaterThan(first);
  });

  it("窗口内 pending 的历史轮自动回放一次(去重,不重复发)", () => {
    const view = renderView();
    const onEnsureLoaded = view.props.onEnsureLoaded as ReturnType<typeof vi.fn>;
    expect(onEnsureLoaded).toHaveBeenCalledTimes(1);
    expect(onEnsureLoaded).toHaveBeenCalledWith(["run-1"]);

    // 父级重渲染(流式每帧都换一个新的 turns 数组)不该再发一次。
    view.rerender({ turns: [...view.props.turns] });
    expect(onEnsureLoaded).toHaveBeenCalledTimes(1);
  });

  it("换 threadId → 账本整个重挂(初始尾随随会话重来)", () => {
    const view = renderView();
    const before = screen.getByTestId("console-traj-ledger");
    view.rerender({});
    expect(screen.getByTestId("console-traj-ledger")).toBe(before);
    view.rerender({ threadId: "th-2" });
    expect(screen.getByTestId("console-traj-ledger")).not.toBe(before);
  });

  it("换 threadId → 选中 / 选区 / 搜索 / 折叠全部复位", () => {
    const view = renderView();
    fireEvent.click(rowOf("A/tool:0:0"));
    drag(100, 400);
    fireEvent.change(screen.getByTestId("console-traj-search"), { target: { value: "query_crm" } });
    expect(screen.getByTestId("console-detail-aside")).toBeInTheDocument();

    view.rerender({ threadId: "th-2" });
    expect(screen.queryByTestId("console-detail-aside")).not.toBeInTheDocument();
    expect(screen.getByTestId("console-traj-search")).toHaveValue("");
    expect(rows()).toHaveLength(9);
    expect(rowOf("A/user")).not.toHaveAttribute("data-focus");
  });

  it("运行中每秒把「现在」往前推,时长投影里的尾块跟着长", () => {
    vi.useFakeTimers();
    // 帧的 `receivedAt` 与它的服务端毫秒同值 → 校准后的「现在」= 系统时钟。
    vi.setSystemTime(BASE + 6000);
    window.localStorage.setItem(LANE_MODE_KEY, "duration");
    try {
      const turns = fixture();
      renderView({
        running: true,
        turns: [turns[0], turns[2]],
        liveByStep: new Map<number, LiveStep>([
          [2, { content: "正在写", reasoning: "", toolNames: new Map(), reasoningMs: null }],
        ]),
      });
      expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-mode", "duration");
      expect(screen.getByTestId("console-lane-strip")).not.toHaveAttribute("data-degraded");

      // 尾块 = live 合成的那条 assistant 记录(它没有自己的时序,从本轮已知的
      // 最晚结束时刻长到「现在」)。
      const liveWidth = (): number => {
        const index = rowOf("C/live-assistant:2").dataset.index;
        const el = blocks().find((b) => b.getAttribute("data-index") === index);
        if (el === undefined) throw new Error(`no timeline block for index ${index}`);
        return Number.parseFloat(el.style.getPropertyValue("--traj-span-width"));
      };
      const before = liveWidth();
      act(() => {
        vi.advanceTimersByTime(2000);
      });
      expect(liveWidth()).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it("nowMs 用帧的服务端时钟校准,不是裸 Date.now()", () => {
    vi.useFakeTimers();
    vi.setSystemTime(BASE + 20_000);
    window.localStorage.setItem(LANE_MODE_KEY, "duration");
    try {
      // 同一个系统时刻、同一批帧,只有「客户端收到帧的时刻」差一个 5s 的钟差。
      // 校准过的「现在」= serverMs + (Date.now() - receivedAt),会跟着钟差往回
      // 挪;裸 `Date.now()` 完全无视 receivedAt,两次渲染只能一样宽。
      const liveWidthWithSkew = (skewMs: number): number => {
        const turns = fixture(skewMs);
        renderView({
          running: true,
          turns: [turns[0], turns[2]],
          liveByStep: new Map<number, LiveStep>([
            [2, { content: "正在写", reasoning: "", toolNames: new Map(), reasoningMs: null }],
          ]),
        });
        const index = rowOf("C/live-assistant:2").dataset.index;
        const el = blocks().find((b) => b.getAttribute("data-index") === index);
        if (el === undefined) throw new Error(`no timeline block for index ${index}`);
        const width = Number.parseFloat(el.style.getPropertyValue("--traj-span-width"));
        cleanup();
        return width;
      };
      expect(liveWidthWithSkew(5000)).toBeLessThan(liveWidthWithSkew(0));
    } finally {
      vi.useRealTimers();
    }
  });

  it("运行中:账本有 running 行,时间轴对应块标 data-live", () => {
    renderView({
      running: true,
      liveByStep: new Map<number, LiveStep>([
        [2, { content: "正在写", reasoning: "", toolNames: new Map(), reasoningMs: null }],
      ]),
    });
    expect(rows().some((r) => r.dataset.running === "true")).toBe(true);
    expect(blocks().some((b) => b.getAttribute("data-live") === "true")).toBe(true);
  });
});
