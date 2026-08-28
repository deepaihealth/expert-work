/**
 * PlanEditForm tests — extracted from PlanPanel.tsx's edit-mode JSX
 * (Task 8) so PlanCard can share it. A pure controlled form: it must
 * never mutate `draft` in place, only call `onChange` with a new object.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { PlanEditForm, planDraftValid } from "../PlanEditForm";
import type { ThreadPlan } from "../../../api/plan";

const DRAFT: ThreadPlan = {
  goal: "ship it",
  steps: [
    { id: "1", description: "write tests", status: "pending" },
    { id: "2", description: "implement", status: "in_progress" },
  ],
};

describe("PlanEditForm", () => {
  it("edits the goal without mutating the original draft", async () => {
    const onChange = vi.fn();
    render(<PlanEditForm draft={DRAFT} onChange={onChange} />);

    await userEvent.type(screen.getByTestId("plan-goal-input"), "!");

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0] as ThreadPlan;
    expect(next).not.toBe(DRAFT);
    expect(next.goal).toBe("ship it!");
    expect(DRAFT.goal).toBe("ship it"); // original untouched
  });

  it("changes a step's status via the select, replacing only that step", async () => {
    const onChange = vi.fn();
    render(<PlanEditForm draft={DRAFT} onChange={onChange} />);

    const select = screen.getByTestId("plan-step-status-0");
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);
    await userEvent.click(
      await screen.findByText("completed", { selector: ".ant-select-item-option-content" }),
    );

    expect(onChange).toHaveBeenCalledWith({
      ...DRAFT,
      steps: [{ ...DRAFT.steps[0], status: "completed" }, DRAFT.steps[1]],
    });
    expect(DRAFT.steps[0].status).toBe("pending"); // original untouched
  });

  it("adds a step and removes a step, both via onChange", async () => {
    const onChange = vi.fn();
    const { rerender } = render(<PlanEditForm draft={DRAFT} onChange={onChange} />);

    await userEvent.click(screen.getByTestId("plan-add-step"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const afterAdd = onChange.mock.calls[0][0] as ThreadPlan;
    expect(afterAdd.steps).toHaveLength(3);
    expect(afterAdd.steps[2]).toEqual({ id: "3", description: "", status: "pending" });
    expect(DRAFT.steps).toHaveLength(2); // original untouched

    onChange.mockClear();
    rerender(<PlanEditForm draft={afterAdd} onChange={onChange} />);
    await userEvent.click(screen.getByTestId("plan-step-remove-2"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const afterRemove = onChange.mock.calls[0][0] as ThreadPlan;
    expect(afterRemove.steps).toHaveLength(2);
    expect(afterRemove.steps.map((s) => s.id)).toEqual(["1", "2"]);
  });
});

describe("planDraftValid", () => {
  it("is false for null", () => {
    expect(planDraftValid(null)).toBe(false);
  });

  it("is false when the goal is blank, there are no steps, or a step description is blank", () => {
    expect(planDraftValid({ goal: "  ", steps: DRAFT.steps })).toBe(false);
    expect(planDraftValid({ goal: "g", steps: [] })).toBe(false);
    expect(
      planDraftValid({ goal: "g", steps: [{ id: "1", description: " ", status: "pending" }] }),
    ).toBe(false);
  });

  it("is true for a fully-filled draft", () => {
    expect(planDraftValid(DRAFT)).toBe(true);
  });
});

describe("execution marker (B-35)", () => {
  it("changes a step's execution via the select, replacing only that step", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PlanEditForm draft={DRAFT} onChange={onChange} />);
    const select = screen
      .getByTestId("plan-step-execution-0")
      .querySelector(".ant-select-selector")!;
    await user.click(select);
    const option = await screen.findByText(
      (_c, el) =>
        el?.classList.contains("ant-select-item-option-content") === true &&
        el.textContent === "Delegate to workers",
    );
    await user.click(option);
    expect(onChange).toHaveBeenCalledWith({
      ...DRAFT,
      steps: [{ ...DRAFT.steps[0], execution: "delegate" }, DRAFT.steps[1]],
    });
  });

  it("a legacy step without execution renders as inline", () => {
    render(<PlanEditForm draft={DRAFT} onChange={vi.fn()} />);
    expect(
      screen.getByTestId("plan-step-execution-0").textContent,
    ).toContain("Main agent does it");
  });
});
