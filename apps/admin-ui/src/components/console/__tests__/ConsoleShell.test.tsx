/**
 * ConsoleShell — the debug console's three-column grid shell (调试台重设计
 * PR-A Task 13). Renders the caller's ``sidebar`` / ``main`` / ``inspect``
 * slots verbatim; the <1200px column-collapse is CSS-only (jsdom always
 * keeps the wide grid, so layout is never asserted here) — the one bit of
 * behaviour that *is* testable in jsdom is the icon-rail button that opens
 * a ``Drawer`` re-rendering the same ``sidebar`` node.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ConsoleShell } from "../ConsoleShell";

describe("ConsoleShell", () => {
  it("renders all three slots and the root testid", () => {
    render(
      <ConsoleShell
        sidebar={<div data-testid="t-sidebar">sidebar</div>}
        main={<div data-testid="t-main">main</div>}
        inspect={<div data-testid="t-inspect">inspect</div>}
        sidebarLabel="Open session list"
      />,
    );
    expect(screen.getByTestId("playground-tab")).toBeInTheDocument();
    expect(screen.getByTestId("t-sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("t-main")).toBeInTheDocument();
    expect(screen.getByTestId("t-inspect")).toBeInTheDocument();
  });

  it("opens a Drawer re-rendering the sidebar node on rail-button click", () => {
    render(
      <ConsoleShell
        sidebar={<div data-testid="probe">sidebar</div>}
        main={<div>main</div>}
        inspect={<div>inspect</div>}
        sidebarLabel="Open session list"
      />,
    );
    // Only the always-rendered sidebar column copy exists before opening.
    expect(screen.getAllByTestId("probe")).toHaveLength(1);

    const railButton = screen.getByTestId("console-sidebar-rail-open");
    expect(railButton).toHaveAttribute("aria-label", "Open session list");
    fireEvent.click(railButton);

    // The Drawer renders the same `sidebar` node a second time.
    expect(screen.getAllByTestId("probe")).toHaveLength(2);
  });
});
