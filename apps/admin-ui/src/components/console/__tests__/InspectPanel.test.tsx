/**
 * InspectPanel — the debug console's right-rail tab container (调试台重设计
 * PR-A Task 13). A controlled ``Segmented`` over two tabs (trajectory /
 * workspace); only the active tab's node is mounted (the trajectory panel
 * owns its own internal scroll, so it must not be kept alive off-screen).
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "../../../i18n";

import { InspectPanel } from "../InspectPanel";

describe("InspectPanel", () => {
  it("renders only the trajectory node when tab='trajectory'", () => {
    render(
      <InspectPanel
        tab="trajectory"
        onTabChange={vi.fn()}
        trajectory={<div data-testid="t-trajectory">trajectory</div>}
        workspace={<div data-testid="t-workspace">workspace</div>}
      />,
    );
    expect(screen.getByTestId("t-trajectory")).toBeInTheDocument();
    expect(screen.queryByTestId("t-workspace")).not.toBeInTheDocument();
  });

  it("renders only the workspace node when tab='workspace'", () => {
    render(
      <InspectPanel
        tab="workspace"
        onTabChange={vi.fn()}
        trajectory={<div data-testid="t-trajectory">trajectory</div>}
        workspace={<div data-testid="t-workspace">workspace</div>}
      />,
    );
    expect(screen.queryByTestId("t-trajectory")).not.toBeInTheDocument();
    expect(screen.getByTestId("t-workspace")).toBeInTheDocument();
  });

  it("clicking the workspace segment calls onTabChange('workspace')", () => {
    const onTabChange = vi.fn();
    render(
      <InspectPanel
        tab="trajectory"
        onTabChange={onTabChange}
        trajectory={<div>trajectory</div>}
        workspace={<div>workspace</div>}
      />,
    );
    fireEvent.click(screen.getByTestId("console-inspect-tab-workspace"));
    expect(onTabChange).toHaveBeenCalledWith("workspace");
  });

  it("clicking the trajectory segment calls onTabChange('trajectory')", () => {
    const onTabChange = vi.fn();
    render(
      <InspectPanel
        tab="workspace"
        onTabChange={onTabChange}
        trajectory={<div>trajectory</div>}
        workspace={<div>workspace</div>}
      />,
    );
    fireEvent.click(screen.getByTestId("console-inspect-tab-trajectory"));
    expect(onTabChange).toHaveBeenCalledWith("trajectory");
  });
});
