/**
 * P-1 —— 对话详情页里「已被取代」的一轮:默认折叠、带跳到新轮的链接;墓碑轮
 * 多一枚「内容已清理」标签;普通轮不套折叠壳。折叠只是外层壳,里面的 TurnBlock
 * 照常渲染(``console-turn`` 条数不变)。
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import { Transcript, type TranscriptProps } from "../Transcript";
import type { ConsoleTurn } from "../types";

function makeTurn(over: Partial<ConsoleTurn> = {}): ConsoleTurn {
  const key = over.key ?? "h1";
  return {
    key,
    seq: 0,
    source: "history",
    turn: {
      id: key,
      input: `input-${key}`,
      attachments: [],
      events: [],
      status: "done",
      error: null,
      approval: null,
    },
    runId: key,
    loadState: "done",
    fallbackLines: [],
    tokens: null,
    timing: null,
    createdAt: null,
    finishedAt: null,
    runError: null,
    supersededBy: null,
    tombstone: false,
    ...over,
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
    onInspectTurn: vi.fn(),
    onInspectRow: vi.fn(),
    streamTurnKey: null,
    liveByStep: new Map(),
    registerHistoryRow: vi.fn(() => vi.fn()),
    rateBook: null,
    isSystemAdmin: false,
    readOnly: true,
    isTenantSwitched: false,
    onDecide: vi.fn(),
    deciding: false,
    onExport: vi.fn(),
    exportingKey: null,
    onDownloadArtifact: vi.fn().mockResolvedValue(undefined),
    runHrefOf: (turn: ConsoleTurn) => (turn.runId ? `/runs/th-1/${turn.runId}` : null),
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

describe("Transcript 被取代轮的折叠态", () => {
  it("① 被取代的一轮默认折叠,标签链接指向取代它的那一轮", () => {
    renderTranscript(
      makeBaseProps({
        turns: [
          makeTurn({ key: "r1", runId: "r1", supersededBy: "r2" }),
          makeTurn({ key: "r2", runId: "r2", seq: 1 }),
        ],
      }),
    );

    const fold = screen.getByTestId("superseded-fold");
    expect(fold).not.toHaveAttribute("open");
    expect(screen.getByTestId("superseded-fold-link")).toHaveAttribute(
      "href",
      "/runs/th-1/r2",
    );
    // 折叠只是外层壳:两轮的 TurnBlock 都还在。
    expect(screen.getAllByTestId("console-turn")).toHaveLength(2);
  });

  it("② 取代它的那一轮不在这一页时只显示 run id,不渲染链接", () => {
    renderTranscript(
      makeBaseProps({
        turns: [makeTurn({ key: "r1", runId: "r1", supersededBy: "r9" })],
      }),
    );

    const link = screen.getByTestId("superseded-fold-link");
    expect(link).not.toHaveAttribute("href");
    expect(link).toHaveTextContent("r9");
  });

  it("③ 墓碑轮多一枚「内容已清理」标签", () => {
    renderTranscript(
      makeBaseProps({
        turns: [makeTurn({ key: "r1", runId: "r1", supersededBy: "r2", tombstone: true })],
      }),
    );

    expect(screen.getByTestId("tombstone-label")).toBeInTheDocument();
  });

  it("④ 普通轮不套折叠壳,也没有墓碑标签", () => {
    renderTranscript(makeBaseProps({ turns: [makeTurn()] }));

    expect(screen.queryByTestId("superseded-fold")).toBeNull();
    expect(screen.queryByTestId("tombstone-label")).toBeNull();
    expect(screen.getAllByTestId("console-turn")).toHaveLength(1);
  });
});
