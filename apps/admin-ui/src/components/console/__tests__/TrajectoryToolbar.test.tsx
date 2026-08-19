/**
 * TrajectoryToolbar — PR-A.2 Task 5: the trajectory panel's sticky toolbar
 * (timeline mode / turn fold / call fold / ledger search), a pure display
 * component. Interaction model projected from deepseek-harness ui-trajectory
 * (MIT) TrajectoryToolbar.tsx — see task-5-brief.md.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import i18n from "../../../i18n";

import { TrajectoryToolbar, type TrajectoryToolbarProps } from "../TrajectoryToolbar";

function baseProps(over: Partial<TrajectoryToolbarProps> = {}): TrajectoryToolbarProps {
  return {
    mode: "sequence",
    onModeChange: vi.fn(),
    degraded: false,
    allTurnsCollapsed: false,
    onToggleAllTurns: vi.fn(),
    turnsCollapsible: true,
    allCallsCollapsed: false,
    onToggleAllCalls: vi.fn(),
    callsCollapsible: true,
    query: "",
    onQueryChange: vi.fn(),
    matchCount: null,
    ...over,
  };
}

describe("TrajectoryToolbar", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders the toolbar landmark with its aria-label and testid", () => {
    render(<TrajectoryToolbar {...baseProps()} />);
    const toolbar = screen.getByRole("toolbar", { name: "轨迹工具条" });
    expect(toolbar).toBe(screen.getByTestId("console-traj-toolbar"));
  });

  it("reflects the duration mode via aria-pressed/title and toggles it on click", () => {
    const onModeChange = vi.fn();
    const { rerender } = render(
      <TrajectoryToolbar {...baseProps({ mode: "sequence", onModeChange })} />,
    );
    const button = screen.getByTestId("console-lane-mode");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveAttribute("title", "按真实时长");

    fireEvent.click(button);
    expect(onModeChange).toHaveBeenCalledWith("duration");

    rerender(<TrajectoryToolbar {...baseProps({ mode: "duration", onModeChange })} />);
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAttribute("title", "改回等宽");
  });

  it("shows the degraded label only when timing is degraded", () => {
    const { rerender } = render(<TrajectoryToolbar {...baseProps({ degraded: false })} />);
    expect(screen.queryByTestId("console-traj-degraded")).not.toBeInTheDocument();

    rerender(<TrajectoryToolbar {...baseProps({ degraded: true })} />);
    expect(screen.getByTestId("console-traj-degraded")).toHaveTextContent("时长不可用");
  });

  it("reflects turn-fold state/disabled-ness and toggles on click", () => {
    const onToggleAllTurns = vi.fn();
    const { rerender } = render(
      <TrajectoryToolbar
        {...baseProps({ allTurnsCollapsed: false, turnsCollapsible: true, onToggleAllTurns })}
      />,
    );
    const button = screen.getByTestId("console-traj-collapse-turns");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).not.toBeDisabled();
    expect(button).toHaveTextContent("⊟");

    fireEvent.click(button);
    expect(onToggleAllTurns).toHaveBeenCalledTimes(1);

    rerender(
      <TrajectoryToolbar
        {...baseProps({ allTurnsCollapsed: true, turnsCollapsible: true, onToggleAllTurns })}
      />,
    );
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveTextContent("⊞");
  });

  it("disables the turn-fold button when turnsCollapsible is false and blocks clicks", () => {
    const onToggleAllTurns = vi.fn();
    render(
      <TrajectoryToolbar {...baseProps({ turnsCollapsible: false, onToggleAllTurns })} />,
    );
    const button = screen.getByTestId("console-traj-collapse-turns");
    expect(button).toBeDisabled();

    fireEvent.click(button);
    expect(onToggleAllTurns).not.toHaveBeenCalled();
  });

  it("reflects call-fold state/disabled-ness independently of the turn-fold button", () => {
    const onToggleAllCalls = vi.fn();
    const { rerender } = render(
      <TrajectoryToolbar
        {...baseProps({ allCallsCollapsed: false, callsCollapsible: false, onToggleAllCalls })}
      />,
    );
    const button = screen.getByTestId("console-traj-collapse-calls");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("⊟");

    rerender(
      <TrajectoryToolbar
        {...baseProps({ allCallsCollapsed: true, callsCollapsible: true, onToggleAllCalls })}
      />,
    );
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveTextContent("⊞");

    fireEvent.click(button);
    expect(onToggleAllCalls).toHaveBeenCalledTimes(1);
  });

  it("reflects the query value and emits onQueryChange as the user types", () => {
    const onQueryChange = vi.fn();
    render(<TrajectoryToolbar {...baseProps({ query: "abc", onQueryChange })} />);
    const input = screen.getByTestId("console-traj-search") as HTMLInputElement;
    expect(input.value).toBe("abc");

    fireEvent.change(input, { target: { value: "abcd" } });
    expect(onQueryChange).toHaveBeenCalledWith("abcd");
  });

  it("shows the match count only when matchCount is not null", () => {
    const { rerender } = render(<TrajectoryToolbar {...baseProps({ matchCount: null })} />);
    expect(screen.queryByTestId("console-traj-search-count")).not.toBeInTheDocument();

    rerender(<TrajectoryToolbar {...baseProps({ matchCount: 5 })} />);
    expect(screen.getByTestId("console-traj-search-count")).toHaveTextContent("5 条匹配");

    rerender(<TrajectoryToolbar {...baseProps({ matchCount: 0 })} />);
    expect(screen.getByTestId("console-traj-search-count")).toHaveTextContent("0 条匹配");
  });
});
