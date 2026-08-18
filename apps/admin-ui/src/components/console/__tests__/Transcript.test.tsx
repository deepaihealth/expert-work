/**
 * Transcript — the console's middle-column turn timeline (Task 11): history
 * turns, a divider, live turns, the flat-history degradation block, task
 * result cards, and selection.
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import { Transcript, type TranscriptProps } from "../Transcript";
import type { ConsoleTurn } from "../types";
import type { Turn } from "../../turn/types";
import type { HistoryMessage } from "../../../api/sessions";
import type { FireNowResult } from "../../../api/triggers";

function makeTurn(
  key: string,
  source: "history" | "live",
  seq: number,
  runId: string | null,
  turnOver: Partial<Turn> = {},
): ConsoleTurn {
  return {
    key,
    seq,
    source,
    turn: {
      id: key,
      input: `input-${key}`,
      attachments: [],
      events: [],
      status: "done",
      error: null,
      approval: null,
      ...turnOver,
    },
    runId,
    loadState: "done",
    fallbackLines: [],
    tokens: null,
    timing: null,
  };
}

function makeBaseProps(overrides: Partial<TranscriptProps> = {}): TranscriptProps {
  return {
    turns: [],
    flatHistory: [],
    taskResults: [],
    threadId: "th-1",
    selectedKey: null,
    onSelectTurn: vi.fn(),
    onInspectRow: vi.fn(),
    streamTurnKey: null,
    liveByStep: new Map(),
    registerHistoryRow: vi.fn(() => vi.fn()),
    rate: null,
    isSystemAdmin: false,
    readOnly: false,
    isTenantSwitched: false,
    onDecide: vi.fn(),
    deciding: false,
    onExport: vi.fn(),
    exportingKey: null,
    onDownloadArtifact: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function renderTranscript(props: TranscriptProps) {
  return render(
    <MemoryRouter>
      <App>
        <Transcript {...props} />
      </App>
    </MemoryRouter>,
  );
}

describe("Transcript", () => {
  it("① empty turns and history renders the empty-log placeholder", () => {
    renderTranscript(makeBaseProps());
    expect(screen.getByTestId("playground-empty-log")).toBeInTheDocument();
  });

  it("② 2 history turns + 1 live turn render as 3 console-turn blocks separated by a HistoryDivider, and the last (no selectedKey) is selected", () => {
    const turns = [
      makeTurn("h1", "history", 0, "run-h1"),
      makeTurn("h2", "history", 1, "run-h2"),
      makeTurn("l1", "live", 2, null),
    ];
    renderTranscript(makeBaseProps({ turns }));

    const blocks = screen.getAllByTestId("console-turn");
    expect(blocks).toHaveLength(3);
    expect(screen.getByText(/new messages/i)).toBeInTheDocument();
    expect(blocks[2]).toHaveTextContent("input-l1");
    expect(blocks[2]).toHaveAttribute("data-selected", "true");
    expect(blocks[0]).toHaveAttribute("data-selected", "false");
    expect(blocks[1]).toHaveAttribute("data-selected", "false");
  });

  it("③ selectedKey pointing at the first history turn marks only that block selected", () => {
    const turns = [
      makeTurn("h1", "history", 0, "run-h1"),
      makeTurn("h2", "history", 1, "run-h2"),
      makeTurn("l1", "live", 2, null),
    ];
    renderTranscript(makeBaseProps({ turns, selectedKey: "h1" }));

    const blocks = screen.getAllByTestId("console-turn");
    expect(blocks[0]).toHaveAttribute("data-selected", "true");
    expect(blocks[1]).toHaveAttribute("data-selected", "false");
    expect(blocks[2]).toHaveAttribute("data-selected", "false");
  });

  it("④ a non-empty flatHistory with no history-sourced turns renders the degraded flat block (commentary + final)", () => {
    const flatHistory: HistoryMessage[] = [
      { role: "user", content: "旧消息" },
      { role: "assistant", content: "旁白内容", channel: "commentary" },
      { role: "assistant", content: "旧回答", channel: "final" },
    ];
    renderTranscript(makeBaseProps({ turns: [], flatHistory }));

    const block = screen.getByTestId("playground-history");
    expect(within(block).getByText("旧消息")).toBeInTheDocument();
    expect(within(block).getByTestId("turn-segment-commentary")).toHaveTextContent("旁白内容");
    expect(within(block).getByText("旧回答")).toBeInTheDocument();
    expect(screen.queryByTestId("console-turn")).not.toBeInTheDocument();
  });

  it("⑤ taskResults render as TaskResultCard", () => {
    const taskResults: FireNowResult[] = [
      {
        run_id: "r1",
        thread_id: "th-2",
        run_status: "done",
        trigger_run_status: "succeeded",
        delivery: "delivered",
        delivered_text: "已完成",
      },
    ];
    renderTranscript(makeBaseProps({ taskResults }));
    expect(screen.getByTestId("playground-task-result")).toBeInTheDocument();
  });

  it("⑥ a history turn's container gets the ref returned by registerHistoryRow(runId, threadId)", () => {
    const rowRefFn = vi.fn();
    const registerHistoryRow = vi.fn(() => rowRefFn);
    const turns = [makeTurn("h1", "history", 0, "run-h1")];
    renderTranscript(makeBaseProps({ turns, registerHistoryRow, threadId: "th-9" }));

    expect(registerHistoryRow).toHaveBeenCalledWith("run-h1", "th-9");
    expect(rowRefFn).toHaveBeenCalledTimes(1);
    expect(rowRefFn.mock.calls[0][0]).toBe(screen.getByTestId("console-turn"));
  });
});
