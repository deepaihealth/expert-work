/**
 * ProcessStrip — the console's per-turn 过程条 (PR-A.1 Task 3, spec §八.3):
 * auto-expanded while the turn runs (last 3 rows + a 「还有 N 步…」 button),
 * collapsed to one headline once settled, a manual toggle winning over both.
 * Rows come from the real ``compactRowsOf`` projection, same ``upd()`` fixture
 * style as TurnBlock.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import { compactRowsOf, type CompactRow } from "../../../api/trajectory_rows";
import { ProcessStrip, type ProcessStripProps } from "../ProcessStrip";

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

/** `n` agent steps, each with reasoning + one `web_search` call → 2n rows. */
function stepEvents(n: number): SseEvent[] {
  const out: SseEvent[] = [];
  for (let i = 1; i <= n; i += 1) {
    out.push(
      upd("agent", {
        step_count: i,
        messages: [
          {
            type: "ai",
            content: "",
            additional_kwargs: { reasoning_content: `想第 ${i} 步` },
            tool_calls: [{ id: `c${i}`, name: "web_search", args: { q: `q${i}` } }],
          },
        ],
      }),
      upd("tools", {
        messages: [
          { type: "tool", tool_call_id: `c${i}`, name: "web_search", content: "ok", status: "success" },
        ],
      }),
    );
  }
  return out;
}

function stripRows(rows: readonly CompactRow[]): ProcessStripProps["rows"] {
  return rows.map((row) => ({ row }));
}

function makeProps(over: Partial<ProcessStripProps> = {}): ProcessStripProps {
  return {
    rows: stripRows(compactRowsOf(stepEvents(1))),
    running: false,
    expandedRowIds: new Set<string>(),
    onToggleRow: vi.fn(),
    onInspectRow: vi.fn(),
    ...over,
  };
}

function renderStrip(props: ProcessStripProps) {
  return render(
    <App>
      <ProcessStrip {...props} />
    </App>,
  );
}

/** Every rendered compact row head (`console-row-<kind>`), body only. */
function visibleRowCount(): number {
  return screen.getAllByTestId(/^console-row-(think|tool)$/).length;
}

describe("ProcessStrip", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("collapses by default when settled and shows only the headline", () => {
    renderStrip(makeProps());

    const head = screen.getByTestId("console-process-head");
    expect(head).toHaveTextContent("思考 1 次 · 工具 1 次(web_search ×1)");
    expect(head).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-process-spinner")).not.toBeInTheDocument();
  });

  it("expands automatically while running and shows the last 3 rows + a 'more' button", async () => {
    renderStrip(makeProps({ rows: stripRows(compactRowsOf(stepEvents(4))), running: true }));

    expect(screen.getByTestId("console-process-head")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("console-process-spinner")).toBeInTheDocument();
    expect(screen.getByTestId("console-process-steps")).toBeInTheDocument();
    expect(visibleRowCount()).toBe(3);
    // The *last* 3 of the 8 rows — the newest step, not the oldest.
    const steps = screen.getByTestId("console-process-steps");
    expect(steps).toHaveTextContent("想第 4 步");
    expect(steps).not.toHaveTextContent("想第 1 步");
    // 8 rows total, 3 shown → 5 folded away.
    expect(screen.getByTestId("console-process-more")).toHaveTextContent("5");

    await userEvent.click(screen.getByTestId("console-process-more"));
    expect(visibleRowCount()).toBe(8);
    expect(screen.getByTestId("console-process-steps")).toHaveTextContent("想第 1 步");
    expect(screen.queryByTestId("console-process-more")).not.toBeInTheDocument();
  });

  it("a manual toggle wins over the automatic state and is kept when running flips", async () => {
    const rows = stripRows(compactRowsOf(stepEvents(1)));
    const { rerender } = renderStrip(makeProps({ rows, running: true }));
    expect(screen.getByTestId("console-process-steps")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("console-process-head"));
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();

    // The turn settles: the automatic value flips to "collapsed" too, but the
    // manual choice is what's remembered — and re-expanding stays possible.
    rerender(
      <App>
        <ProcessStrip {...makeProps({ rows, running: false })} />
      </App>,
    );
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("console-process-head"));
    expect(screen.getByTestId("console-process-steps")).toBeInTheDocument();
  });

  it("clicking a row's 轨迹 link calls onInspectRow with that row id", async () => {
    const onInspectRow = vi.fn();
    renderStrip(makeProps({ onInspectRow }));

    await userEvent.click(screen.getByTestId("console-process-head"));
    const link = screen.getAllByTestId("console-row-inspect")[1];
    // §八.3 — the row-level link reads 「轨迹」 now, not the old 「检查」.
    expect(link).toHaveTextContent("轨迹");
    await userEvent.click(link);
    expect(onInspectRow).toHaveBeenCalledWith("tool:0:0");
  });

  it("renders nothing for a turn without process rows", () => {
    renderStrip(makeProps({ rows: [] }));
    expect(screen.queryByTestId("console-process-head")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
  });
});
