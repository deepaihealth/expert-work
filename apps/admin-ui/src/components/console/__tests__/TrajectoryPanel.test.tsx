/**
 * TrajectoryPanel tests — Task 18 of the debug-console PR-A plan. Nine `it`s
 * from the task-18 brief, five of them migrated (fixtures/mocking lifted)
 * from ``pages/__tests__/PlaygroundTab.test.tsx``'s timeline-banner and
 * exact-trace-view/Langfuse suites (that file is untouched — Task 19 edits
 * it). See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-18-brief.md.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";
import i18n from "../../../i18n";

import { setStoredToken } from "../../../api/client";
import * as runsSdk from "../../../api/runs";
import type { RunDetail } from "../../../api/runs";
import * as traceFacadeSdk from "../../../api/trace_facade";
import type { RunTrace } from "../../../api/trace_facade";
import type { SseEvent } from "../../../api/sessions";
import { trajectoryRowsOf } from "../../../api/trajectory_rows";
import { AuthProvider } from "../../../auth/AuthContext";
import { TenantScopeProvider } from "../../../tenant/TenantScopeContext";
import type { LiveStep } from "../../../pages/agent_detail/playground/useTokenStream";
import { buildConsoleTurns } from "../console_turns";
import type { ConsoleTurn } from "../types";
import { TrajectoryPanel, type TrajectoryPanelProps } from "../TrajectoryPanel";

// ToolCallCard's fire-now button (mounted eagerly by antd Tabs even off the
// active pane — see RowDetail.test.tsx) reads this hook; no Auth/TenantScope
// wiring needed for it here.
vi.mock("../../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => false,
}));

// jsdom 25 ships no `PointerEvent` and no `setPointerCapture` — same stand-in
// as LaneStrip.test.tsx, needed by the drag-to-filter test below.
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
/** 轨道量尺:左 0 / 宽 300px —— 域坐标 = clientX / 300 × total。 */
const RECT_300 = {
  x: 0, y: 0, left: 0, top: 0, right: 300, bottom: 14, width: 300, height: 14,
  toJSON: () => ({}),
} as DOMRect;

const getRunTraceMock = vi.spyOn(traceFacadeSdk, "getRunTrace");
const getRunMock = vi.spyOn(runsSdk, "getRun");

const THREAD_ID = "thread-1";

function jwt(roles: string[] = []): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(
    JSON.stringify({ sub: "u", tenant_id: "tenant-1", roles }),
  );
  return `${header}.${body}.`;
}

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "t" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

/** Builds one live ``ConsoleTurn`` — the brief's ``consoleTurnFrom`` helper,
 *  extended with an optional turn id (default ``"t1"``) so a test can build
 *  two turns with distinct ``key``s to exercise the turn-switch reset. */
function consoleTurnFrom(
  events: SseEvent[],
  status: "running" | "done" | "error",
  id = "t1",
): ConsoleTurn {
  const turns = buildConsoleTurns({
    historyTurns: null,
    historyLoads: {},
    liveTurns: [{ id, input: "q", attachments: [], events, status, error: null, approval: null }],
    timings: {},
  });
  const turn = turns[0];
  if (!turn) throw new Error("expected buildConsoleTurns to produce one turn");
  return turn;
}

function Wrapper(props: TrajectoryPanelProps) {
  return (
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <TrajectoryPanel {...props} />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>
  );
}

function renderPanel(overrides: Partial<TrajectoryPanelProps> = {}) {
  const props: TrajectoryPanelProps = {
    turn: null,
    threadId: THREAD_ID,
    isSystemAdmin: false,
    liveByStep: undefined,
    focusRowId: null,
    onFireResult: undefined,
    ...overrides,
  };
  const utils = render(<Wrapper {...props} />);
  const rerenderWith = (next: Partial<TrajectoryPanelProps>) => {
    const merged = { ...props, ...next };
    utils.rerender(<Wrapper {...merged} />);
  };
  return { ...utils, rerenderWith };
}

// One agent step with a tool call (query_crm) + a second step whose AI
// message carries the turn's final answer — think/tool/think/assistant rows
// plus a lane-strip block per lane.
const SAMPLE_EVENTS: SseEvent[] = [
  ev("metadata", { run_id: "run-1" }),
  upd("agent", {
    step_count: 1,
    messages: [
      {
        type: "ai",
        content: "",
        response_metadata: { model_name: "gpt-x" },
        additional_kwargs: { reasoning_content: "先查一下" },
        tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
      },
    ],
  }),
  upd("tools", {
    messages: [{ type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" }],
  }),
  upd("agent", {
    step_count: 2,
    messages: [{ type: "ai", content: "已完成查询。" }],
  }),
  ev("end", "ok"),
];

const SAMPLE_ROWS = trajectoryRowsOf(SAMPLE_EVENTS, { text: "q", attachmentNames: [], inputs: {} }, null, "done");
const TOOL_ROW_ID = (() => {
  const row = SAMPLE_ROWS.find((r) => r.kind === "tool");
  if (!row) throw new Error("expected a tool row in the fixture");
  return row.id;
})();

function getToolRow(): HTMLElement {
  const row = screen.getAllByTestId("console-traj-row").find((el) => el.dataset.kind === "tool");
  if (!row) throw new Error("expected a tool row in the DOM");
  return row;
}

/** The `"user"` row's id is the same literal for every turn (trajectoryRowsOf
 *  always emits it) — unlike a `tool:…`/`think:…` id, it still exists after
 *  switching to an unrelated turn, so selecting it before a turn switch
 *  actually exercises the `useEffect(() => setSelectedRowId(null), [turn?.key])`
 *  reset (a `tool` row's id would already be absent from the new turn's rows,
 *  which closes the detail via the plain `rows.find(...) ?? null` derivation
 *  even if the reset effect were deleted). */
function getUserRow(): HTMLElement {
  const row = screen.getAllByTestId("console-traj-row").find((el) => el.dataset.rowId === "user");
  if (!row) throw new Error("expected the user row in the DOM");
  return row;
}

/** 与 TrajectoryPanel 的持久化键同源(改一处忘另一处 → 这条测试红)。 */
const LANE_MODE_KEY = "expert_work.console.lane_mode";

const NO_TRACE: RunTrace = { status: "no_trace" };
function baseRunDetail(runId: string, traceId: string | null): RunDetail {
  return { run_id: runId, thread_id: THREAD_ID, status: "success", pending_approval: null, trace_id: traceId };
}

beforeEach(() => {
  getRunTraceMock.mockReset();
  getRunTraceMock.mockResolvedValue(NO_TRACE);
  getRunMock.mockReset();
  getRunMock.mockResolvedValue(baseRunDetail("run-1", null));
  setStoredToken(jwt([]));
  window.localStorage.removeItem(LANE_MODE_KEY);
});

afterEach(() => {
  // 不用 `restoreAllMocks` —— 模块级的 getRunTrace / getRun 两个 spy 是整个文件
  // 共用的,restore 掉后面的用例就打真网络了。jsdom 本来就没有下面两个方法,
  // `defineProperty` 装上去的桩要手动摘。
  Reflect.deleteProperty(HTMLElement.prototype, "setPointerCapture");
  Reflect.deleteProperty(HTMLElement.prototype, "releasePointerCapture");
  window.localStorage.removeItem(LANE_MODE_KEY);
  vi.clearAllMocks();
  vi.unstubAllEnvs();
  setStoredToken(null);
});

describe("TrajectoryPanel", () => {
  it("null turn → empty state; a turn → header 第 2 轮 · 已完成 + lane strip + rows", async () => {
    renderPanel({ turn: null });
    expect(screen.getByTestId("console-trajectory-panel")).toBeInTheDocument();
    expect(screen.getByTestId("console-traj-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("console-inspect-turn-header")).not.toBeInTheDocument();
    cleanup();

    const turn = { ...consoleTurnFrom(SAMPLE_EVENTS, "done"), seq: 1 };
    renderPanel({ turn });

    const header = await screen.findByTestId("console-inspect-turn-header");
    expect(header).toHaveTextContent(
      i18n.t("console.inspect_turn_header", { n: 2, status: i18n.t("console.footer_status_done") }),
    );
    expect(screen.getByTestId("console-lane-strip")).toBeInTheDocument();
    expect(screen.getByTestId("console-traj-rows")).toBeInTheDocument();
    expect(screen.getAllByTestId("console-traj-row").length).toBeGreaterThan(0);
  });

  it("ok run: banner ok (RunStatusBanner) — migrated from PlaygroundTab.test 1280", async () => {
    const events: SseEvent[] = [
      ev("metadata", { run_id: "run-tl-ok" }),
      upd("agent", { messages: [{ type: "ai", content: "hi" }] }),
      ev("end", "ok"),
    ];
    const turn = consoleTurnFrom(events, "done");
    renderPanel({ turn });

    const banner = await screen.findByTestId("run-status-banner");
    expect(banner).toHaveTextContent(i18n.t("playground.rb_ok"));
    expect(screen.queryByTestId("run-status-jump")).not.toBeInTheDocument();
  });

  it("error run: banner error + jump selects the first error row and opens its detail — 1307", async () => {
    const events: SseEvent[] = [
      ev("metadata", { run_id: "run-tl-err" }),
      upd("agent", {
        step_count: 3,
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [{ id: "c1", name: "exec_python", args: { code: "1/0" }, type: "tool_call" }],
          },
        ],
      }),
      upd("tools", {
        messages: [{ type: "tool", tool_call_id: "c1", name: null, content: "ZeroDivisionError", status: "error" }],
      }),
      ev("end", "ok"),
    ];
    const turn = consoleTurnFrom(events, "done");
    renderPanel({ turn });

    const banner = await screen.findByTestId("run-status-banner");
    expect(banner).toHaveTextContent(i18n.t("playground.tl_step", { n: 3 }));

    const errorRow = screen.getAllByTestId("console-traj-row").find((el) => el.dataset.status === "error");
    expect(errorRow).toBeTruthy();

    fireEvent.click(screen.getByTestId("run-status-jump"));

    expect(await screen.findByTestId("console-detail-summary")).toBeInTheDocument();
    const selected = screen.getAllByTestId("console-traj-row").find((el) => el.getAttribute("aria-selected") === "true");
    expect(selected?.dataset.rowId).toBe(errorRow?.dataset.rowId);
  });

  it("a top-level error frame yields exactly one error row and one banner — 1379", async () => {
    const events: SseEvent[] = [
      ev("metadata", { run_id: "run-tl-marker" }),
      ev("error", { message: "运行崩溃了" }),
      ev("end", "ok"),
    ];
    const turn = consoleTurnFrom(events, "error");
    renderPanel({ turn });

    const banner = await screen.findByTestId("run-status-banner");
    const occurrences = (banner.textContent?.match(/运行崩溃了/g) ?? []).length;
    expect(occurrences).toBe(1);

    const errorRows = screen.getAllByTestId("console-traj-row").filter((el) => el.dataset.kind === "error");
    expect(errorRows).toHaveLength(1);
    expect(screen.getAllByTestId("run-status-banner")).toHaveLength(1);
  });

  it("clicking a row opens RowDetail (Summary tab) below; close hides it; switching turn clears the selection", async () => {
    const turn1 = consoleTurnFrom(SAMPLE_EVENTS, "done", "t1");
    const { rerenderWith } = renderPanel({ turn: turn1 });

    fireEvent.click(getToolRow());
    expect(await screen.findByTestId("console-detail-summary")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("console-detail-close"));
    expect(screen.queryByTestId("console-detail-summary")).not.toBeInTheDocument();

    // Select the "user" row (not the tool row) before switching turns — its
    // id (the literal `"user"`) exists in every turn's rows, so this pins
    // the `useEffect(() => setSelectedRowId(null), [turn?.key])` reset
    // itself: a tool/think row's id would already be absent from turn2's
    // rows, closing the detail via the plain `rows.find(...) ?? null`
    // derivation even without the reset effect.
    fireEvent.click(getUserRow());
    expect(await screen.findByTestId("console-detail-summary")).toBeInTheDocument();
    const selectedBeforeSwitch = screen
      .getAllByTestId("console-traj-row")
      .find((el) => el.getAttribute("aria-selected") === "true");
    expect(selectedBeforeSwitch?.dataset.rowId).toBe("user");

    const otherEvents: SseEvent[] = [
      ev("metadata", { run_id: "run-other" }),
      upd("agent", { messages: [{ type: "ai", content: "hi" }] }),
      ev("end", "ok"),
    ];
    const turn2 = consoleTurnFrom(otherEvents, "done", "t2");
    rerenderWith({ turn: turn2 });

    await waitFor(() => expect(screen.queryByTestId("console-detail-summary")).not.toBeInTheDocument());
    expect(
      screen.getAllByTestId("console-traj-row").some((el) => el.getAttribute("aria-selected") === "true"),
    ).toBe(false);
  });

  it("focusRowId selects that row when it changes", async () => {
    const turn = consoleTurnFrom(SAMPLE_EVENTS, "done");
    const { rerenderWith } = renderPanel({ turn, focusRowId: null });

    expect(screen.queryByTestId("console-detail-summary")).not.toBeInTheDocument();

    rerenderWith({ focusRowId: TOOL_ROW_ID });

    expect(await screen.findByTestId("console-detail-summary")).toBeInTheDocument();
    const selected = screen.getAllByTestId("console-traj-row").find((el) => el.getAttribute("aria-selected") === "true");
    expect(selected?.dataset.rowId).toBe(TOOL_ROW_ID);
  });

  it("Langfuse link hidden for non-admin — 1705; shown for system_admin via getRun trace_id — 1734", async () => {
    vi.stubEnv("VITE_LANGFUSE_BASE_URL", "https://langfuse.example.com/");
    const events: SseEvent[] = [
      ev("metadata", { run_id: "run-nolink" }),
      ev("end", "ok"),
    ];
    const turn = consoleTurnFrom(events, "done");
    renderPanel({ turn, isSystemAdmin: false });

    await waitFor(() => expect(screen.getByTestId("console-trajectory-panel")).toBeInTheDocument());
    expect(screen.queryByTestId("playground-turn-langfuse")).not.toBeInTheDocument();
    expect(getRunMock).not.toHaveBeenCalled();

    getRunMock.mockResolvedValueOnce(baseRunDetail("run-link", "tr-xyz"));
    const events2: SseEvent[] = [
      ev("metadata", { run_id: "run-link" }),
      ev("end", "ok"),
    ];
    const turn2 = consoleTurnFrom(events2, "done", "t2");
    renderPanel({ turn: turn2, isSystemAdmin: true });

    const link = await screen.findByTestId("playground-turn-langfuse");
    expect(link).toHaveAttribute("href", "https://langfuse.example.com/trace/tr-xyz");
    expect(getRunMock).toHaveBeenCalledWith(THREAD_ID, "run-link", undefined);
  });

  it("live turn: unsettled step's reasoning/tool names appear as running rows appended at the end", async () => {
    const liveByStep: ReadonlyMap<number, LiveStep> = new Map([
      [1, { content: "", reasoning: "正在思考", toolNames: new Map([[0, "search_docs"]]), reasoningMs: null }],
    ]);
    const turn = consoleTurnFrom([], "running");
    renderPanel({ turn, liveByStep });

    const rowIds = screen.getAllByTestId("console-traj-row").map((el) => el.dataset.rowId);
    expect(rowIds).toEqual(["user", "live-think:1", "live-tool:1:0"]);

    const liveRows = screen.getAllByTestId("console-traj-row").filter((el) => el.dataset.rowId?.startsWith("live-"));
    expect(liveRows.every((el) => el.dataset.status === "running")).toBe(true);
  });

  it("trace is fetched when the panel shows a turn (no view switch needed) — 1416 (fetch part)", async () => {
    getRunTraceMock.mockResolvedValue({
      status: "ok",
      trace: { name: "trace-1", latencyMs: 1000, totalCostUsd: null, spanCount: 1 },
      spans: [
        {
          id: "s1",
          parentId: null,
          kind: "llm",
          label: "LLM call",
          detail: null,
          startMs: 0,
          latencyMs: 500,
          model: "glm-4.6",
          inputTokens: 10,
          outputTokens: 20,
          costUsd: null,
          input: null,
          output: null,
          level: "default",
          statusMessage: null,
          purpose: "",
          group: null,
        },
      ],
    });
    const events: SseEvent[] = [
      ev("metadata", { run_id: "run-exact-1" }),
      upd("agent", { messages: [{ type: "ai", content: "hi" }] }),
      ev("end", "ok"),
    ];
    const turn = consoleTurnFrom(events, "done");
    renderPanel({ turn });

    await waitFor(() =>
      expect(getRunTraceMock).toHaveBeenCalledWith(THREAD_ID, "run-exact-1", undefined),
    );
  });
  it("header links to the run detail page (only when both thread and run id exist)", async () => {
    const turn = consoleTurnFrom(SAMPLE_EVENTS, "done");
    const { rerenderWith } = renderPanel({ turn });

    const link = await screen.findByTestId("console-inspect-run-link");
    expect(link).toHaveAttribute("href", `/runs/${THREAD_ID}/run-1`);
    expect(link).toHaveTextContent(i18n.t("console.inspect_run_detail"));

    rerenderWith({ threadId: null });
    expect(screen.queryByTestId("console-inspect-run-link")).not.toBeInTheDocument();
  });

  it("the lane-mode Segmented switches the strip projection and persists the choice", async () => {
    const turn = consoleTurnFrom(SAMPLE_EVENTS, "done");
    renderPanel({ turn });

    expect(await screen.findByTestId("console-lane-strip")).toHaveAttribute("data-mode", "sequence");
    // 控件本身的 testid 也钉住 —— antd 换版本把 rest props 吞了就该这里红。
    expect(screen.getByTestId("console-lane-mode")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("console-lane-mode-duration"));

    await waitFor(() =>
      expect(screen.getByTestId("console-lane-strip")).toHaveAttribute("data-mode", "duration"),
    );
    expect(window.localStorage.getItem(LANE_MODE_KEY)).toBe("duration");

    // 重挂载(= 下次打开调试台)读回持久化的投影。
    cleanup();
    renderPanel({ turn });
    expect(await screen.findByTestId("console-lane-strip")).toHaveAttribute("data-mode", "duration");
  });

  it("hovering a lane block highlights the matching row in the table", async () => {
    const turn = consoleTurnFrom(SAMPLE_EVENTS, "done");
    renderPanel({ turn });

    await screen.findByTestId("console-lane-strip");
    const block = screen
      .getAllByTestId("console-lane-block")
      .find((b) => b.getAttribute("data-row-id") === TOOL_ROW_ID);
    expect(block).toBeTruthy();

    fireEvent.mouseOver(block as HTMLElement);
    await waitFor(() => expect(getToolRow()).toHaveAttribute("data-hovered", "true"));

    fireEvent.mouseOut(block as HTMLElement);
    await waitFor(() => expect(getToolRow()).not.toHaveAttribute("data-hovered"));
  });

  it("dragging a span on the lane strip filters the row table; switching turn clears the filter", async () => {
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT_300);
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      configurable: true, writable: true, value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
      configurable: true, writable: true, value: vi.fn(),
    });

    const turn1 = consoleTurnFrom(SAMPLE_EVENTS, "done", "t1");
    const { rerenderWith } = renderPanel({ turn: turn1 });

    const allRows = screen.getAllByTestId("console-traj-row").length;
    expect(allRows).toBe(5);

    const track = await screen.findByTestId("console-lane-track");
    // 域宽 5 行 / 轨道 300px → 100px = 域 5/3、200px = 域 10/3 → 第 2/3/4 行。
    fireEvent.pointerDown(track, { clientX: 100, pointerId: 1 });
    fireEvent.pointerMove(track, { clientX: 200, pointerId: 1 });
    fireEvent.pointerUp(track, { clientX: 200, pointerId: 1 });

    const chip = await screen.findByTestId("console-traj-filter");
    expect(chip.textContent).toContain("#2–#4");
    expect(screen.getAllByTestId("console-traj-row")).toHaveLength(3);

    const otherEvents: SseEvent[] = [
      ev("metadata", { run_id: "run-other" }),
      upd("agent", { messages: [{ type: "ai", content: "hi" }] }),
      ev("end", "ok"),
    ];
    rerenderWith({ turn: consoleTurnFrom(otherEvents, "done", "t2") });
    await waitFor(() => expect(screen.queryByTestId("console-traj-filter")).not.toBeInTheDocument());
    rectSpy.mockRestore();
  });

  it("Esc inside the panel closes the row detail", async () => {
    const turn = consoleTurnFrom(SAMPLE_EVENTS, "done");
    renderPanel({ turn });

    fireEvent.click(getToolRow());
    expect(await screen.findByTestId("console-detail-summary")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId("console-trajectory-panel"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("console-detail-summary")).not.toBeInTheDocument());
  });
});
