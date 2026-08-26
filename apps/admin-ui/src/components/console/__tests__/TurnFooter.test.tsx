/**
 * TurnFooter — the console turn's one-row status/meta/action footer (spec
 * §八.4, PR-A.1 Task 4).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import i18n from "../../../i18n";

import { TurnFooter } from "../TurnFooter";
import type { ConsoleTurn } from "../types";
import type { Turn } from "../../turn/types";
import type { TurnSummary } from "../../../api/turn_summary";

function makeConsoleTurn(turnOver: Partial<Turn> = {}): ConsoleTurn {
  return {
    key: "t1",
    seq: 3,
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
    createdAt: null,
    finishedAt: null,
    runError: null,
  };
}

const FULL_SUMMARY: TurnSummary = {
  finalText: "ok",
  segments: [],
  reasoning: [],
  usage: {
    inputTokens: 34408,
    outputTokens: 1622,
    totalTokens: 36030,
    cacheReadTokens: 512,
    cacheCreationTokens: 0,
    reasoningTokens: 128,
  },
  stepCount: 3,
  latencyMs: 4521,
  finishReason: "stop",
  modelName: "glm-5.2",
  perStepUsage: [],
};

const EMPTY_SUMMARY: TurnSummary = {
  finalText: null,
  segments: [],
  reasoning: [],
  usage: null,
  stepCount: null,
  latencyMs: null,
  finishReason: null,
  modelName: null,
  perStepUsage: [],
};

describe("TurnFooter", () => {
  // Locale-sensitive assertions below (查看轨迹 / 输入 / 步 text) — pin zh-CN
  // explicitly and restore afterward so it doesn't leak into other test
  // files (the i18n singleton persists its resolved language across `it`
  // blocks / files in the same worker).
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders status, a compact meta line, and the actions on one row (no TurnMeta chips)", () => {
    render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onRetry={vi.fn()}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("console-footer-meta").textContent).toMatch(
      /36,030 tok · 3 步 · .* · glm-5\.2/,
    );
    expect(screen.queryByTestId("playground-usage")).not.toBeInTheDocument();
    expect(screen.getByTestId("console-turn-inspect")).toHaveTextContent("查看轨迹");
  });

  it("meta tooltip lists input / output / cache / reasoning breakdown", async () => {
    render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    await userEvent.hover(screen.getByTestId("console-footer-meta"));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toContain("输入");
    expect(tooltip.textContent).toContain("34408");
  });

  it("总耗时 = run 行墙钟(finishedAt − createdAt),带「总耗时」标签", () => {
    // 回放帧的 receivedAt 全挤在回放一瞬间(曾把 9 分钟的 run 显示成 8ms),
    // 权威时长来自 run 行的 created_at / finished_at。
    const turn = {
      ...makeConsoleTurn({ status: "done" }),
      createdAt: "2026-08-26T00:00:00Z",
      finishedAt: "2026-08-26T00:01:44Z",
    };
    render(
      <MemoryRouter>
        <TurnFooter
          turn={turn}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("console-footer-meta").textContent).toContain("总耗时 1m44s");
  });

  it("interrupted turn: the status tag names the InterruptReason", () => {
    // 「已取消」(user_cancel)和「已中断(连接断开)」必须能分开;词表外的
    // 值落回通用「已中断」。
    const cancelled = {
      ...makeConsoleTurn({ status: "interrupted" }),
      runError: "user_cancel",
    };
    const { rerender } = render(
      <MemoryRouter>
        <TurnFooter
          turn={cancelled}
          threadId="th-1"
          summary={EMPTY_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("console-turn-status")).toHaveTextContent("已取消");

    const legacy = { ...makeConsoleTurn({ status: "interrupted" }), runError: null };
    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={legacy}
          threadId="th-1"
          summary={EMPTY_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("console-turn-status")).toHaveTextContent("已中断");
  });

  it("does not render a view-run link any more", () => {
    // A `metadata` frame carrying `run_id` is what makes the old TurnMeta
    // render its "查看运行" deep link — give the turn one so this assertion
    // is a genuine regression check, not vacuously true from a null runId.
    const turnWithRunId = makeConsoleTurn({
      status: "done",
      events: [
        {
          id: "1",
          event: "metadata",
          data: { run_id: "run-1" },
          rawData: "",
          receivedAt: "2026-01-01T00:00:00Z",
        },
      ],
    });
    render(
      <MemoryRouter>
        <TurnFooter
          turn={turnWithRunId}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/查看运行|View run/)).not.toBeInTheDocument();
  });

  it("running turn: no retry, no feedback, still 查看轨迹", () => {
    render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "running" })}
          threadId="th-1"
          summary={EMPTY_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onRetry={vi.fn()}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("playground-turn-retry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("playground-turn-feedback")).not.toBeInTheDocument();
    expect(screen.getByTestId("console-turn-inspect")).toHaveTextContent("查看轨迹");
  });

  it("feedback bar: shown for a settled, non-read-only, non-switched turn; hidden when readOnly; still shown but disabled when tenant-switched", () => {
    const { rerender } = render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("playground-turn-feedback")).not.toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();
    expect(screen.getByTestId("playground-feedback-up")).toBeDisabled();
  });

  // PR-B Task 3 — ConversationDetail's per-turn "查看运行" deep link: an
  // explicit ``runHref`` prop (not derived from the turn's own events, unlike
  // the old TurnMeta chip the test above guards against resurrecting).
  it("renders a 查看运行 link to runHref when given", () => {
    render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          runHref="/runs/th-1/run-1"
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    const link = screen.getByTestId("console-turn-run-link");
    expect(link).toHaveAttribute("href", "/runs/th-1/run-1");
    expect(link).toHaveTextContent("查看运行");
  });

  it("omits the 查看运行 link when runHref is not given", () => {
    render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("console-turn-run-link")).not.toBeInTheDocument();
  });

  it("retry button is danger-styled for a failed turn; export + inspect render regardless of onRetry", () => {
    const failedTurn = makeConsoleTurn({ status: "error", error: "boom" });
    const onRetry = vi.fn();

    const { rerender } = render(
      <MemoryRouter>
        <TurnFooter
          turn={failedTurn}
          threadId="th-1"
          summary={EMPTY_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    // No onRetry → retry hidden; export + inspect always render.
    expect(screen.queryByTestId("playground-turn-retry")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-export-json")).toBeInTheDocument();
    expect(screen.getByTestId("console-turn-inspect")).toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={failedTurn}
          threadId="th-1"
          summary={EMPTY_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onRetry={onRetry}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    const retryBtn = screen.getByTestId("playground-turn-retry");
    expect(retryBtn).toBeInTheDocument();
    expect(retryBtn.className).toContain("dangerous"); // failed turn → danger styling
  });
});


it("interrupted turn: 已中断 tag and the feedback bar stays (终审 F4)", () => {
  render(
    <MemoryRouter>
      <TurnFooter
        turn={makeConsoleTurn({ status: "interrupted" })}
        threadId="th-1"
        summary={EMPTY_SUMMARY}
        costCny={null}
        readOnly={false}
        isTenantSwitched={false}
        onExport={() => {}}
        exporting={false}
        onInspect={() => {}}
      />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("console-turn-status").textContent).toMatch(/已中断|Interrupted/);
  expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();
});
