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
  it("renders the replayed run's step timeline and tool call card", () => {
    renderCard({ readOnly: true, loadState: "done" });

    expect(screen.getByTestId("playground-turn")).toBeInTheDocument();
    expect(screen.getByText("what happened?")).toBeInTheDocument();
    expect(screen.getByText("replayed answer")).toBeInTheDocument();
    expect(screen.getByTestId("step-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("playground-tool-count")).toHaveTextContent("1");

    // Tool cards live inside a step card's collapsed body — expand the step
    // that made the call.
    fireEvent.click(screen.getAllByTestId("step-head")[0]);
    const toolCards = screen.getAllByTestId("tool-call-card");
    expect(toolCards.length).toBe(1);
    expect(within(toolCards[0]).getByText("search")).toBeInTheDocument();
  });

  it("keeps the flat fallback answer + spinner while a history turn is still loading", () => {
    renderCard({
      readOnly: true,
      loadState: "loading",
      fallbackAnswer: "stored answer",
      turn: makeTurn({ events: [] }),
    });

    expect(screen.getByText("stored answer")).toBeInTheDocument();
    expect(screen.queryByTestId("step-timeline")).not.toBeInTheDocument();
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
