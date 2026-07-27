/**
 * EntryBreakdown tests — Task 6.
 *
 * buildBreakdown's segmenting is covered by entry_breakdown.test.ts; this
 * file covers the one thing that test can't: that clicking a rendered
 * segment actually reports the right span id. That wiring is invisible in
 * a screenshot — a swapped index or a closure over the wrong segment still
 * *looks* like a working breakdown bar, it just selects the wrong row.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import "../../../../i18n";

import { EntryBreakdown } from "../EntryBreakdown";
import type { TraceSpan } from "../../../../api/trace_facade";

const span = (o: Partial<TraceSpan>): TraceSpan => ({
  id: "x", parentId: null, kind: "span", label: "l", detail: null,
  startMs: 0, latencyMs: 0, model: null, inputTokens: null, outputTokens: null,
  costUsd: null, input: null, output: null, level: "default",
  statusMessage: null, purpose: "", group: null, ...o,
});

describe("EntryBreakdown", () => {
  it("reports the clicked segment's own span id, not another segment's", () => {
    const spans = [
      span({ id: "r", label: "记忆召回", group: "entry", startMs: 0, latencyMs: 2000 }),
      span({ id: "l", kind: "llm", label: "LLM 调用", startMs: 2000, latencyMs: 600 }),
    ];
    const onSelect = vi.fn();
    render(<EntryBreakdown spans={spans} selectedId={null} onSelect={onSelect} />);

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(2);

    buttons[1].click();
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("l");

    buttons[0].click();
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenLastCalledWith("r");
  });

  it("renders nothing when the trace has no entry spans", () => {
    render(
      <EntryBreakdown
        spans={[span({ id: "l", kind: "llm", label: "LLM 调用" })]}
        selectedId={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("entry-breakdown")).not.toBeInTheDocument();
  });

  // Regression guard for a dark-theme contrast bug: the segment used to be
  // an opaque `--ew-trace-entry` fill with hardcoded white text (1.99:1
  // against dark theme's light indigo — WCAG AA wants ≥3:1). The fix is a
  // translucent fill (composites with whatever surface sits behind it,
  // matching TraceView.tsx's own kindBarColor pattern) plus theme-aware
  // text. EntryBreakdown.contrast.test.ts checks the resulting numbers
  // clear 3:1 in both themes; this checks the *component* still emits that
  // shape (not a hardcoded "#fff") — the two together close the gap either
  // one alone leaves open (tokens.css drifting vs. the component reverting).
  it("renders segments with a translucent entry-tinted background and theme-aware text (not a solid fill + hardcoded white)", () => {
    const spans = [span({ id: "r", label: "记忆召回", group: "entry", latencyMs: 2000 })];
    render(<EntryBreakdown spans={spans} selectedId={null} onSelect={vi.fn()} />);

    const button = screen.getByRole("button");
    expect(button.style.color).toBe("var(--ew-text-primary)");
    expect(button.style.color).not.toBe("#fff");
    expect(button.style.background).toContain("color-mix");
    expect(button.style.background).toContain("var(--ew-trace-entry");
    expect(button.style.background).toContain("62%");
    expect(button.style.background).toContain("transparent");
  });
});
