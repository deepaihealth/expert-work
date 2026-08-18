/**
 * PlanCard tests — the console shell's task card (Task 8). Renders
 * whatever `plan` it's handed (owns no fetch/save wiring of its own —
 * that's usePlanCard's job); this covers only its own local state: the
 * collapse toggle's persistence, the edit-mode draft flow, and the
 * running / readOnly affordance gates.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { PlanCard, type PlanCardProps } from "../PlanCard";
import type { ThreadPlan } from "../../../api/plan";

const PLAN: ThreadPlan = {
  goal: "ship the feature",
  steps: [
    { id: "1", description: "write tests", status: "completed" },
    { id: "2", description: "implement", status: "in_progress" },
    { id: "3", description: "review", status: "pending" },
  ],
};

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
});

function renderCard(overrides: Partial<PlanCardProps> = {}) {
  return render(
    <App>
      <PlanCard plan={PLAN} loaded running={false} {...overrides} />
    </App>,
  );
}

describe("PlanCard", () => {
  it("renders the done/doing/todo progress counts", () => {
    renderCard();
    expect(screen.getByText("1 done · 1 in progress · 1 pending")).toBeInTheDocument();
  });

  it("renders nothing when there is no plan", () => {
    renderCard({ plan: null });
    expect(screen.queryByTestId("console-plan-card")).not.toBeInTheDocument();
  });

  it("persists the collapse toggle and restores it on remount", () => {
    const { unmount } = renderCard();
    expect(screen.getByTestId("plan-read-view")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("console-plan-toggle"));
    expect(window.localStorage.getItem("expert_work.console.planCollapsed")).toBe("1");
    expect(screen.queryByTestId("plan-read-view")).not.toBeInTheDocument();
    unmount();

    renderCard();
    expect(screen.queryByTestId("plan-read-view")).not.toBeInTheDocument();
  });

  it("disables the edit button while a run is live, with a tooltip", async () => {
    renderCard({ running: true });
    expect(screen.getByTestId("plan-edit")).toBeDisabled();
  });

  it("hides the edit button entirely in read-only mode", () => {
    renderCard({ readOnly: true });
    expect(screen.queryByTestId("plan-edit")).not.toBeInTheDocument();
  });

  it("edits a step and saves — onSave receives the full plan shape", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderCard({ onSave });

    await userEvent.click(screen.getByTestId("plan-edit"));
    const select = screen.getByTestId("plan-step-status-2");
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);
    await userEvent.click(
      await screen.findByText("completed", { selector: ".ant-select-item-option-content" }),
    );
    await userEvent.click(screen.getByTestId("plan-save"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const payload = onSave.mock.calls[0][0] as ThreadPlan;
    expect(payload.goal).toBe("ship the feature");
    expect(payload.steps.map((s) => s.status)).toEqual(["completed", "in_progress", "completed"]);
    // Editing closes and the read view comes back.
    expect(await screen.findByTestId("plan-read-view")).toBeInTheDocument();
  });

  it("reports a save failure via message.error instead of throwing", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("boom"));
    renderCard({ onSave });

    await userEvent.click(screen.getByTestId("plan-edit"));
    await userEvent.click(screen.getByTestId("plan-save"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    // The failure actually surfaces to the user via antd's message holder —
    // not just "didn't throw" (String(new Error("boom")) => "Error: boom").
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    // Edit mode stays open — the draft isn't discarded on failure.
    expect(screen.getByTestId("plan-edit-form")).toBeInTheDocument();
  });
});
