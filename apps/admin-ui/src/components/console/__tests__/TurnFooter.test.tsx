/**
 * TurnFooter — the console turn's status/meta/action row (Task 10).
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import { TurnFooter } from "../TurnFooter";
import type { ConsoleTurn } from "../types";
import type { Turn } from "../../turn/types";
import { summarizeTurn } from "../../../api/turn_summary";

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
  };
}

describe("TurnFooter", () => {
  it("shows retry (danger when failed) only with onRetry and a settled turn; export + inspect always", () => {
    const failedTurn = makeConsoleTurn({ status: "error", error: "boom" });
    const summary = summarizeTurn(failedTurn.turn.events);
    const onRetry = vi.fn();

    const { rerender } = render(
      <MemoryRouter>
        <TurnFooter
          turn={failedTurn}
          threadId="th-1"
          summary={summary}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
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
          summary={summary}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onRetry={onRetry}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
        />
      </MemoryRouter>,
    );
    const retryBtn = screen.getByTestId("playground-turn-retry");
    expect(retryBtn).toBeInTheDocument();
    expect(retryBtn.className).toContain("dangerous"); // failed turn → danger styling

    const runningTurn = makeConsoleTurn({ status: "running" });
    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={runningTurn}
          threadId="th-1"
          summary={summarizeTurn(runningTurn.turn.events)}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onRetry={onRetry}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
        />
      </MemoryRouter>,
    );
    // Running turns never show retry, even when a handler is wired.
    expect(screen.queryByTestId("playground-turn-retry")).not.toBeInTheDocument();
  });

  it("feedback bar only for a settled live turn that is not read-only; disabled when tenant-switched", () => {
    const doneTurn = makeConsoleTurn({ status: "done" });
    const summary = summarizeTurn(doneTurn.turn.events);

    const { rerender } = render(
      <MemoryRouter>
        <TurnFooter
          turn={doneTurn}
          threadId="th-1"
          summary={summary}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();

    // readOnly → feedback bar hidden.
    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={doneTurn}
          threadId="th-1"
          summary={summary}
          costCny={null}
          readOnly
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("playground-turn-feedback")).not.toBeInTheDocument();

    // Still running → feedback bar hidden (only settled turns get one).
    const runningTurn = makeConsoleTurn({ status: "running" });
    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={runningTurn}
          threadId="th-1"
          summary={summarizeTurn(runningTurn.turn.events)}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("playground-turn-feedback")).not.toBeInTheDocument();

    // Not read-only but tenant-switched → bar renders, buttons disabled.
    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={doneTurn}
          threadId="th-1"
          summary={summary}
          costCny={null}
          readOnly={false}
          isTenantSwitched
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          selected={false}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();
    expect(screen.getByTestId("playground-feedback-up")).toBeDisabled();
  });
});
