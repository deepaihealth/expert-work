/**
 * TurnCard — read-only smoke. The card was lifted verbatim out of
 * ``PlaygroundTab`` (its behaviour is covered end-to-end there); these tests
 * pin the contract the conversation detail page depends on: a replayed run
 * renders the full step/tool timeline, and ``readOnly`` suppresses every
 * mutating affordance (approval gate + feedback bar).
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import { TurnCard } from "../TurnCard";
import type { Turn } from "../types";
import type { ApprovalItem } from "../../../api/approvals";
import type { SseEvent } from "../../../api/sessions";
import type { LiveStep } from "../../../pages/agent_detail/playground/useTokenStream";

/** A replayed run: run metadata → one agent step calling ``search`` → the
 *  tool's result → the final answer → terminal frame. */
const replayEvents: SseEvent[] = [
  {
    id: "1",
    event: "metadata",
    data: { run_id: "run-hist-1" },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "2",
    event: "updates",
    data: {
      agent: {
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [
              {
                id: "c1",
                name: "search",
                args: { q: "expert-work" },
                type: "tool_call",
              },
            ],
          },
        ],
      },
    },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "3",
    event: "updates",
    data: {
      tools: {
        messages: [
          {
            type: "tool",
            tool_call_id: "c1",
            name: "search",
            content: "3 hits",
            status: "success",
          },
        ],
      },
    },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "4",
    event: "updates",
    data: { agent: { messages: [{ type: "ai", content: "replayed answer" }] } },
    rawData: "",
    receivedAt: "",
  },
  { id: "5", event: "end", data: {}, rawData: "", receivedAt: "" },
];

const pendingApproval: ApprovalItem = {
  id: "a1",
  tenant_id: "t",
  user_id: null,
  run_id: "run-hist-1",
  thread_id: "th-1",
  request_id: "a1",
  node: "tools",
  reason_kind: "tool_policy",
  action_summary: "run exec_python",
  proposed_args: { code: "1+1" },
  requested_at: "2026-05-25T00:00:00Z",
  timeout_at: "2026-05-25T00:10:00Z",
  status: "pending",
  decided_by: null,
  decided_at: null,
};

function makeTurn(over: Partial<Turn> = {}): Turn {
  return {
    id: "turn-1",
    input: "what happened?",
    attachments: [],
    events: replayEvents,
    status: "done",
    error: null,
    approval: null,
    ...over,
  };
}

/** ``ToolCallCard`` (reached through ``StepTimeline``) calls ``App.useApp()``
 *  and ``TurnMeta`` renders a router link — both need a real ancestor. */
function renderCard(props: Partial<Parameters<typeof TurnCard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <App>
        <TurnCard
          turn={makeTurn()}
          turnSeq={0}
          initialEventView="timeline"
          onViewChange={vi.fn()}
          threadId="th-1"
          onDownloadArtifact={vi.fn()}
          rate={null}
          onDecide={vi.fn()}
          deciding={false}
          onExport={vi.fn()}
          exporting={false}
          isSystemAdmin={false}
          {...props}
        />
      </App>
    </MemoryRouter>,
  );
}

describe("TurnCard (read-only)", () => {
  it("renders the replayed run's Gantt timeline and tool call card", () => {
    renderCard({ readOnly: true, loadState: "done" });

    expect(screen.getByTestId("playground-turn")).toBeInTheDocument();
    expect(screen.getByText("what happened?")).toBeInTheDocument();
    expect(screen.getByText("replayed answer")).toBeInTheDocument();
    // Task 3 — the timeline eventView now renders `GanttTimeline`, not
    // `StepTimeline` directly.
    expect(screen.getByTestId("gantt-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("playground-tool-count")).toHaveTextContent("1");

    // Row detail reuses the existing step card (StepTimeline, single-item
    // array) — expand the row for the step that made the call, then its
    // (collapsed-by-default) step head, to reach the nested tool call card.
    fireEvent.click(screen.getByTestId("gantt-row-item-0"));
    fireEvent.click(screen.getByTestId("step-head"));
    const toolCards = screen.getAllByTestId("tool-call-card");
    expect(toolCards.length).toBe(1);
    expect(within(toolCards[0]).getByText("search")).toBeInTheDocument();
  });

  it("keeps the flat fallback answer + spinner while a history turn is still loading", () => {
    renderCard({
      readOnly: true,
      loadState: "loading",
      fallbackLines: [{ text: "stored answer", channel: null }],
      turn: makeTurn({ events: [] }),
    });

    expect(screen.getByText("stored answer")).toBeInTheDocument();
    expect(screen.queryByTestId("step-timeline")).not.toBeInTheDocument();
  });

  // Important#1 — a fallback commentary line clamps to 240 chars with no
  // other way to read the rest (the replay that would normally render the
  // full segment never lands in this mode). The FullTextTrigger must open
  // the modal with the complete, unclamped text.
  it("fallback commentary line clamps to 240 chars but its FullTextTrigger opens the full text", () => {
    const longText = "旁白内容".repeat(80); // 320 chars, well past the 240 clamp
    renderCard({
      readOnly: true,
      loadState: "loading",
      fallbackLines: [{ text: longText, channel: "commentary" }],
      turn: makeTurn({ events: [] }),
    });

    const commentary = screen.getByTestId("turn-segment-commentary");
    expect(commentary).toHaveTextContent(`${longText.slice(0, 240)}…`);
    expect(commentary.textContent?.length ?? 0).toBeLessThan(longText.length);

    fireEvent.click(screen.getByTestId("full-text-trigger"));
    expect(screen.getByTestId("full-text-modal")).toHaveTextContent(longText);
  });

  it("hides the approval gate and the feedback bar in read-only mode", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({ approval: pendingApproval }),
    });

    expect(screen.queryByTestId("playground-approval")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("playground-turn-feedback"),
    ).not.toBeInTheDocument();
  });

  // Guards the assertion above against becoming vacuous: the very same turn
  // rendered live DOES show both controls.
  it("shows the approval gate and the feedback bar when not read-only", () => {
    renderCard({ turn: makeTurn({ approval: pendingApproval }) });

    expect(screen.getByTestId("playground-approval")).toBeInTheDocument();
    expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();
  });
});

/** A multi-step run: two LLM steps each emitting their own assistant text —
 *  the answer must aggregate BOTH (#8), not just the last one. */
const multiTextEvents: SseEvent[] = [
  {
    id: "1",
    event: "metadata",
    data: { run_id: "run-hist-2" },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "2",
    event: "updates",
    data: { agent: { messages: [{ type: "ai", content: "step one findings" }] } },
    rawData: "",
    receivedAt: "",
  },
  {
    id: "3",
    event: "updates",
    data: { agent: { messages: [{ type: "ai", content: "final summary" }] } },
    rawData: "",
    receivedAt: "",
  },
  { id: "4", event: "end", data: {}, rawData: "", receivedAt: "" },
];

/** Spec 2026-07-30 (conversation output channels) — one AI-message
 *  ``updates`` frame, matching what ``turn_summary.ts`` parses. ``toolCalls``
 *  seeds N dummy tool_calls so the structural channel rule (only the turn's
 *  last text message, when it carries no tool_calls, is "final" — everything
 *  else is "commentary") can be exercised without a full timeline. */
let aiUpdatesSeq = 0;
function aiUpdates(text: string, opts: { toolCalls?: number } = {}): SseEvent {
  aiUpdatesSeq += 1;
  const toolCalls = opts.toolCalls
    ? Array.from({ length: opts.toolCalls }, (_, i) => ({
        id: `c${aiUpdatesSeq}-${i}`,
        name: "search",
        args: {},
        type: "tool_call",
      }))
    : undefined;
  return {
    id: `u-${aiUpdatesSeq}`,
    event: "updates",
    data: {
      agent: {
        messages: [
          {
            type: "ai",
            content: text,
            ...(toolCalls ? { tool_calls: toolCalls } : {}),
          },
        ],
      },
    },
    rawData: "",
    receivedAt: "",
  };
}

/** A settled (``status: "done"``) turn built from a sequence of ``aiUpdates``
 *  frames, bracketed with ``metadata``/``end`` frames like a real replay. */
function settledTurnWith(updates: SseEvent[]): Turn {
  return makeTurn({
    events: [
      {
        id: "meta",
        event: "metadata",
        data: { run_id: "run-seg" },
        rawData: "",
        receivedAt: "",
      },
      ...updates,
      { id: "end", event: "end", data: {}, rawData: "", receivedAt: "" },
    ],
  });
}

describe("TurnCard answer segments (spec 2026-07-30-conversation-output-channels)", () => {
  it("答案区按段渲染:commentary 弱化行,final 走 Markdown 正文", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: settledTurnWith([
        aiUpdates("第一章资料已获取,现在撰写第一章正文。", { toolCalls: 1 }),
        aiUpdates("# 第一章\n正文内容"),
      ]),
    });

    const commentary = screen.getAllByTestId("turn-segment-commentary");
    expect(commentary).toHaveLength(1);
    expect(commentary[0]).toHaveTextContent("第一章资料已获取");
    // final 段经 MarkdownView 渲染出标题元素,且不含旁白文本
    const answer = screen.getByTestId("playground-turn-answer-scroll");
    expect(
      within(answer).getByRole("heading", { name: "第一章" }),
    ).toBeInTheDocument();
  });

  it("无 final 段(末条带 tool_calls)只渲染 commentary,不显示 no_text 占位", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: settledTurnWith([aiUpdates("先搜资料", { toolCalls: 1 })]),
    });

    expect(screen.getByTestId("turn-segment-commentary")).toHaveTextContent(
      "先搜资料",
    );
    expect(screen.queryByText(/turn_no_text/)).not.toBeInTheDocument();
  });
});

describe("TurnCard answer area (#8 / #11 / C1)", () => {
  it("aggregates every assistant text into the answer, not just the last step's", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({ events: multiTextEvents }),
    });

    const scroll = screen.getByTestId("playground-turn-answer-scroll");
    expect(within(scroll).getByText("step one findings")).toBeInTheDocument();
    expect(within(scroll).getByText("final summary")).toBeInTheDocument();
  });

  it("caps the answer block so a long answer scrolls inside its own container (#11)", () => {
    renderCard({ readOnly: true, loadState: "done" });

    expect(screen.getByTestId("playground-turn-answer-scroll")).toHaveStyle({
      maxHeight: "420px",
      overflowY: "auto",
    });
  });

  // C1 — the failure Alert is a banner ABOVE the answer, never a replacement:
  // a failed run's produced assistant text must stay readable.
  it("keeps a failed turn's answer body and shows the error banner above it", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({ status: "error", error: "boom" }),
    });

    const banner = screen.getByTestId("playground-turn-error");
    expect(banner).toHaveTextContent("This turn's run failed");
    expect(banner).toHaveTextContent("boom");
    expect(screen.getByText("replayed answer")).toBeInTheDocument();
    expect(
      screen.getByTestId("playground-turn-answer-scroll"),
    ).toBeInTheDocument();
  });

  it("renders a generic failure banner (never an empty frame) when the turn has no error text", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({
        status: "error",
        error: null,
        events: [{ id: "e", event: "end", data: {}, rawData: "", receivedAt: "" }],
      }),
    });

    expect(screen.getByTestId("playground-turn-error")).toHaveTextContent(
      "This turn's run failed",
    );
    // The banner already states the outcome — no "(no text answer)"
    // placeholder stacked underneath it.
    expect(screen.queryByText("(no text answer)")).not.toBeInTheDocument();
  });
});

/** Task 3 — the "timeline" eventView branch now renders `GanttTimeline`
 *  instead of `StepTimeline` directly; row detail expansion reuses the
 *  existing `StepTimeline` (single-item array) for the整卡 content, and a
 *  header button opens a 92vw Modal with the `expanded` variant of the same
 *  model/renderDetail. See task-3-brief.md Step 1. */
describe("TurnCard timeline view → Gantt (Task 3)", () => {
  it("renders GanttTimeline (not StepTimeline) for the timeline view", () => {
    renderCard({ readOnly: true, loadState: "done" });

    expect(screen.getByTestId("gantt-timeline")).toBeInTheDocument();
    expect(screen.queryByTestId("step-timeline")).not.toBeInTheDocument();
  });

  it("放大按钮打开 92vw Modal,内渲 expanded variant 的 GanttTimeline", () => {
    renderCard({ readOnly: true, loadState: "done" });

    fireEvent.click(screen.getByTestId("playground-gantt-expand"));

    const instances = screen.getAllByTestId("gantt-timeline");
    const expanded = instances.find((el) => el.getAttribute("data-variant") === "expanded");
    expect(expanded).toBeInTheDocument();
    // The embedded copy stays mounted behind the modal (only the modal's
    // visibility toggles), so both variants coexist while open.
    expect(instances.some((el) => el.getAttribute("data-variant") === "embedded")).toBe(true);
  });

  it("点击 Gantt 行展开出现有 StepTimeline 整卡内容(AgentStepCard 稳定 testid)", () => {
    renderCard({ readOnly: true, loadState: "done" });

    const [firstAgentRow] = screen.getAllByTestId(/^gantt-row-item-/);
    fireEvent.click(firstAgentRow);

    expect(screen.getByTestId("step-card")).toBeInTheDocument();
  });

  it("degraded(帧 id 缺失)时显示提示文案", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({ events: replayEvents.map((e) => ({ ...e, id: null })) }),
    });

    expect(
      screen.getByText(
        "Timeline approximated by event order (server timestamps unavailable)",
      ),
    ).toBeInTheDocument();
  });

  it("running 时,进行中(无 duration)的行渲染生长条 class", () => {
    const runningEvents: SseEvent[] = [
      {
        id: "1722400000000-1",
        event: "metadata",
        data: { run_id: "run-live-1" },
        rawData: "",
        receivedAt: new Date().toISOString(),
      },
      {
        id: "1722400000500-2",
        event: "updates",
        data: {
          agent: {
            step_count: 1,
            _duration_ms: 400,
            messages: [
              {
                type: "ai",
                content: "",
                tool_calls: [{ id: "c1", name: "search", args: {}, type: "tool_call" }],
              },
            ],
          },
        },
        rawData: "",
        receivedAt: new Date().toISOString(),
      },
      // No RESULT frame for `c1` — the tool call is still pending, so its
      // Gantt row gets `durationMs: null` (the growing-bar case).
    ];
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({ events: runningEvents, status: "running" }),
    });

    expect(screen.getByTestId("gantt-bar-tool-c1").className).toContain(
      "ew-gantt-bar--running",
    );
  });

  // M1 — every other fixture in this describe block uses ids "1"~"5", which
  // fail `serverMsOf`'s 10+-digit check and so run entirely on the degraded
  // (sequential-placement) path — a blind spot that never exercised real
  // start/duration → percentage geometry. This fixture uses the real
  // `"{epoch_ms}-{seq}"` shape (see task-1-brief.md / gantt_timeline.test.ts)
  // so the assertions below are pinned to the non-degraded arithmetic.
  const REAL_ID_BASE_MS = 1_722_400_000_000;
  const realIdEvents: SseEvent[] = [
    {
      id: `${REAL_ID_BASE_MS}-1`,
      event: "metadata",
      data: { run_id: "run-real-1" },
      rawData: "",
      receivedAt: "",
    },
    {
      id: `${REAL_ID_BASE_MS + 500}-2`,
      event: "updates",
      data: {
        agent: {
          step_count: 1,
          _duration_ms: 500,
          messages: [
            {
              type: "ai",
              content: "",
              tool_calls: [{ id: "c1", name: "search", args: {}, type: "tool_call" }],
            },
          ],
        },
      },
      rawData: "",
      receivedAt: "",
    },
    {
      id: `${REAL_ID_BASE_MS + 1500}-3`,
      event: "updates",
      data: {
        tools: {
          messages: [
            {
              type: "tool",
              tool_call_id: "c1",
              name: "search",
              content: "3 hits",
              status: "success",
              additional_kwargs: { duration_ms: 1000 },
            },
          ],
        },
      },
      rawData: "",
      receivedAt: "",
    },
    {
      id: `${REAL_ID_BASE_MS + 1600}-4`,
      event: "updates",
      data: {
        agent: {
          step_count: 2,
          _duration_ms: 100,
          messages: [{ type: "ai", content: "final answer" }],
        },
      },
      rawData: "",
      receivedAt: "",
    },
    { id: `${REAL_ID_BASE_MS + 1700}-5`, event: "end", data: {}, rawData: "", receivedAt: "" },
  ];

  it("真实 epoch-ms id fixture:渲染真实(非退化)几何,不显示 degraded 提示(M1)", () => {
    renderCard({
      readOnly: true,
      loadState: "done",
      turn: makeTurn({ events: realIdEvents }),
    });

    expect(screen.queryByTestId("playground-gantt-degraded")).not.toBeInTheDocument();

    // t0 = step1's start (id_ms − durationMs = +0); totalMs = 1600 (step2's
    // end). All three rows resolve to exact binary fractions of 1600, so the
    // percentages below are exact, not rounded.
    const stepBar = screen.getByTestId("gantt-bar-item-0");
    expect(stepBar.style.left).toBe("0%");
    expect(stepBar.style.width).toBe("31.25%");

    const toolBar = screen.getByTestId("gantt-bar-tool-c1");
    expect(toolBar.style.left).toBe("31.25%");
    expect(toolBar.style.width).toBe("62.5%");

    // Settled + no tool_calls on the last step → relabeled "final", same key.
    const finalBar = screen.getByTestId("gantt-bar-item-1");
    expect(finalBar.style.left).toBe("93.75%");
    expect(finalBar.style.width).toBe("6.25%");
  });
});

/** Critical (gantt-execution-timeline review) — the Gantt view's sibling
 *  `StepTimeline` call (the one that surfaces the live typewriter alongside
 *  the Gantt axis) passes `items={[]}`, so `StepTimeline`'s own internal
 *  `settled` set (computed only from `items`) is always empty there.
 *  `useTokenStream`'s `liveByStep` accumulates per step and never clears a
 *  step's buffer once it settles — by design, it relies on the consumer
 *  filtering via `settled`. Before the fix, that meant an already-settled
 *  step's stale live buffer rendered forever, duplicating its own real Gantt
 *  row (and, once `finalized`, painting it "interrupted" even though it
 *  finished normally). `TurnCard` now pre-filters `liveByStep` against the
 *  settled step numbers found in its own `parseTimeline` result before
 *  handing it to the sibling `StepTimeline`. */
describe("TurnCard live typewriter — settled-step filtering (Critical, gantt-execution-timeline review)", () => {
  function liveStep(content: string): LiveStep {
    return { content, reasoning: "", toolNames: new Map(), reasoningMs: null };
  }

  /** One agent step (`step_count: 1`) whose authoritative content has
   *  already landed via an `updates` frame — no `end` frame (the turn is
   *  still `running`), matching a real mid-run snapshot where step 1 has
   *  settled but step 2 is still streaming. */
  const settledStepOneEvents: SseEvent[] = [
    {
      id: "1722400000000-1",
      event: "metadata",
      data: { run_id: "run-live-c1" },
      rawData: "",
      receivedAt: "",
    },
    {
      id: "1722400000500-2",
      event: "updates",
      data: {
        agent: {
          step_count: 1,
          _duration_ms: 400,
          messages: [{ type: "ai", content: "step one settled answer" }],
        },
      },
      rawData: "",
      receivedAt: "",
    },
  ];

  it("hides an already-settled step's stale live buffer while still rendering an unsettled step's live card", () => {
    renderCard({
      turn: makeTurn({ events: settledStepOneEvents, status: "running" }),
      liveByStep: new Map([
        [1, liveStep("STALE STEP1 LIVE TEXT")],
        [2, liveStep("LIVE STEP2 LIVE TEXT")],
      ]),
      finalized: false,
    });

    // Step 1 already has an authoritative Gantt row — its residual live
    // buffer must not render as a duplicate streaming card.
    expect(screen.queryByText("STALE STEP1 LIVE TEXT")).not.toBeInTheDocument();
    // Step 2 has no authoritative card yet — its live buffer still renders.
    const cards = screen.getAllByTestId("streaming-step-card");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveAttribute("data-step", "2");
    expect(within(cards[0]).getByText("LIVE STEP2 LIVE TEXT")).toBeInTheDocument();
  });

  it("does not render a settled step's stale buffer as 'interrupted' once the run finalizes", () => {
    renderCard({
      turn: makeTurn({
        events: [
          ...settledStepOneEvents,
          { id: "1722400001000-3", event: "end", data: {}, rawData: "", receivedAt: "" },
        ],
        status: "done",
      }),
      liveByStep: new Map([[1, liveStep("STALE STEP1 LIVE TEXT")]]),
      finalized: true,
    });

    expect(screen.queryByTestId("streaming-step-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interrupted-badge")).not.toBeInTheDocument();
    expect(screen.queryByText("STALE STEP1 LIVE TEXT")).not.toBeInTheDocument();
  });
});
