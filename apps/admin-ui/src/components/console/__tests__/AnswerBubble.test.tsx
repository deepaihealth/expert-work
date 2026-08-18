/**
 * AnswerBubble — the console's agent-answer block (Task 10). Fixtures mirror
 * ``components/turn/__tests__/TurnCard.test.tsx`` (the channelled-segments
 * and failed-turn cases are copied from there per the brief) since the JSX
 * was lifted verbatim out of TurnCard.
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { AnswerBubble } from "../AnswerBubble";
import type { ConsoleTurn } from "../types";
import type { SseEvent } from "../../../api/sessions";
import { summarizeTurn } from "../../../api/turn_summary";
import type { Turn } from "../../turn/types";

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
          { type: "ai", content: text, ...(toolCalls ? { tool_calls: toolCalls } : {}) },
        ],
      },
    },
    rawData: "",
    receivedAt: "",
  };
}

function makeConsoleTurn(
  turnOver: Partial<Turn> = {},
  over: Partial<Omit<ConsoleTurn, "turn">> = {},
): ConsoleTurn {
  return {
    key: "t1",
    seq: 0,
    source: "live",
    turn: {
      id: "t1",
      input: "hi",
      attachments: [],
      events: [],
      status: "done",
      error: null,
      approval: null,
      ...turnOver,
    },
    runId: null,
    loadState: "done",
    fallbackLines: [],
    tokens: null,
    timing: null,
    ...over,
  };
}

describe("AnswerBubble", () => {
  it("renders commentary lines de-emphasised and the final segment as markdown", () => {
    const events = [
      aiUpdates("第一章资料已获取,现在撰写第一章正文。", { toolCalls: 1 }),
      aiUpdates("# 第一章\n正文内容"),
    ];
    const turn = makeConsoleTurn({ events, status: "done" });
    const summary = summarizeTurn(events);

    render(
      <App>
        <AnswerBubble turn={turn} summary={summary} onDownloadArtifact={vi.fn()} />
      </App>,
    );

    const commentary = screen.getAllByTestId("turn-segment-commentary");
    expect(commentary).toHaveLength(1);
    expect(commentary[0]).toHaveTextContent("第一章资料已获取");
    const answer = screen.getByTestId("playground-turn-answer-scroll");
    expect(within(answer).getByRole("heading", { name: "第一章" })).toBeInTheDocument();
  });

  it("while running: settled segments as commentary, liveText appended as plain typewriter text", () => {
    const events = [
      aiUpdates("commentary text one", { toolCalls: 1 }),
      aiUpdates("commentary text two", { toolCalls: 1 }),
    ];
    const turn = makeConsoleTurn({ events, status: "running" });
    const summary = summarizeTurn(events);

    render(
      <App>
        <AnswerBubble
          turn={turn}
          summary={summary}
          liveText="streaming now"
          onDownloadArtifact={vi.fn()}
        />
      </App>,
    );

    const commentary = screen.getAllByTestId("turn-segment-commentary");
    expect(commentary).toHaveLength(1);
    expect(commentary[0]).toHaveTextContent("commentary text one");
    expect(screen.getByTestId("playground-turn-answer-scroll")).toHaveTextContent(
      "commentary text two",
    );
    expect(screen.getByTestId("console-answer-live")).toHaveTextContent("streaming now");
  });

  // Important 1 (fix round 1) — the very first LLM message of a running turn
  // has no settled segment yet (no `updates` frame has landed), so the old
  // `hasText ? … : status === "running" ? placeholder …` branch discarded
  // `liveText` entirely and showed only the generic "Running…" placeholder.
  it("running turn with no settled segments still types out liveText; placeholder only shows without liveText", () => {
    const turn = makeConsoleTurn({ events: [], status: "running" });
    const summary = summarizeTurn([]);

    const { rerender } = render(
      <App>
        <AnswerBubble turn={turn} summary={summary} liveText="partial" onDownloadArtifact={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("console-answer-live")).toHaveTextContent("partial");
    expect(screen.queryByText("Running…")).not.toBeInTheDocument();

    rerender(
      <App>
        <AnswerBubble turn={turn} summary={summary} onDownloadArtifact={vi.fn()} />
      </App>,
    );
    expect(screen.queryByTestId("console-answer-live")).not.toBeInTheDocument();
    expect(screen.getByText("Running…")).toBeInTheDocument();
  });

  // Important 2 (fix round 1) — the scroll-pin effect's dep array was
  // `[segments, status]`; a liveText-only update (the common case — new
  // characters land without a new `updates` frame settling) never re-ran it,
  // so the capped answer box stopped following the newest text mid-stream.
  it("auto-scrolls to bottom when liveText grows without a new segment landing", () => {
    const turn = makeConsoleTurn({ events: [], status: "running" });
    const summary = summarizeTurn([]);

    const { rerender } = render(
      <App>
        <AnswerBubble turn={turn} summary={summary} liveText="partial" onDownloadArtifact={vi.fn()} />
      </App>,
    );
    const scrollEl = screen.getByTestId("playground-turn-answer-scroll");

    // jsdom never lays out content, so `scrollHeight` is always 0 — stub both
    // it and `scrollTop` on the real DOM node so the effect's
    // `node.scrollTop = node.scrollHeight` assignment is observable.
    let height = 100;
    let scrollTopValue = 0;
    Object.defineProperty(scrollEl, "scrollHeight", { configurable: true, get: () => height });
    Object.defineProperty(scrollEl, "scrollTop", {
      configurable: true,
      get: () => scrollTopValue,
      set: (v: number) => {
        scrollTopValue = v;
      },
    });

    height = 250; // the live text grew, pushing the container taller
    rerender(
      <App>
        <AnswerBubble
          turn={turn}
          summary={summary}
          liveText="partial partial partial"
          onDownloadArtifact={vi.fn()}
        />
      </App>,
    );
    expect(scrollEl.scrollTop).toBe(250);
  });

  it("history turn not yet loaded: fallback lines + loading spinner; error load: fallback without spinner", () => {
    const summary = summarizeTurn([]);
    const loadingTurn = makeConsoleTurn(
      { events: [], status: "done" },
      { loadState: "loading", fallbackLines: [{ text: "stored answer", channel: null }] },
    );
    const { rerender } = render(
      <App>
        <AnswerBubble turn={loadingTurn} summary={summary} onDownloadArtifact={vi.fn()} />
      </App>,
    );
    expect(screen.getByText("stored answer")).toBeInTheDocument();
    expect(screen.getByText("Loading debug data…")).toBeInTheDocument();

    const erroredTurn = makeConsoleTurn(
      { events: [], status: "done" },
      { loadState: "error", fallbackLines: [{ text: "stored answer", channel: null }] },
    );
    rerender(
      <App>
        <AnswerBubble turn={erroredTurn} summary={summary} onDownloadArtifact={vi.fn()} />
      </App>,
    );
    expect(screen.getByText("stored answer")).toBeInTheDocument();
    expect(screen.queryByText("Loading debug data…")).not.toBeInTheDocument();
  });

  it("failed turn keeps the answer body under the error banner", () => {
    const events = [aiUpdates("replayed answer")];
    const turn = makeConsoleTurn({ events, status: "error", error: "boom" });
    const summary = summarizeTurn(events);

    render(
      <App>
        <AnswerBubble turn={turn} summary={summary} onDownloadArtifact={vi.fn()} />
      </App>,
    );

    const banner = screen.getByTestId("playground-turn-error");
    expect(banner).toHaveTextContent("This turn's run failed");
    expect(banner).toHaveTextContent("boom");
    expect(screen.getByText("replayed answer")).toBeInTheDocument();
    expect(screen.getByTestId("playground-turn-answer-scroll")).toBeInTheDocument();
  });

  it("artifact download row calls onDownloadArtifact(name)", async () => {
    const events: SseEvent[] = [
      {
        id: "1",
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
                    name: "save_artifact",
                    args: { name: "report.txt", kind: "other" },
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
        id: "2",
        event: "updates",
        data: {
          tools: {
            messages: [
              {
                type: "tool",
                tool_call_id: "c1",
                name: "save_artifact",
                content: "saved",
                status: "success",
              },
            ],
          },
        },
        rawData: "",
        receivedAt: "",
      },
    ];
    const turn = makeConsoleTurn({ events, status: "done" });
    const summary = summarizeTurn(events);
    const onDownload = vi.fn().mockResolvedValue(undefined);

    render(
      <App>
        <AnswerBubble turn={turn} summary={summary} onDownloadArtifact={onDownload} />
      </App>,
    );

    expect(screen.getByTestId("playground-turn-artifacts")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("playground-turn-artifact-download"));
    expect(onDownload).toHaveBeenCalledWith("report.txt");
  });
});
