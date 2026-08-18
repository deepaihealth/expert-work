/**
 * usePlanCard tests — R6 three-source precedence (Task 8): GET baseline
 * on thread mount, a live `plan` stream snapshot applied at most once
 * per sourceKey, and a PUT echo as the last writer.
 */
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { usePlanCard } from "../usePlanCard";
import type { ThreadPlan } from "../../../api/plan";
import type { SseEvent } from "../../../api/sessions";

const PLAN_A: ThreadPlan = { goal: "A", steps: [{ id: "1", description: "step", status: "pending" }] };
const PLAN_B: ThreadPlan = {
  goal: "B",
  steps: [{ id: "1", description: "step", status: "in_progress" }],
};
const PLAN_C: ThreadPlan = {
  goal: "C",
  steps: [{ id: "1", description: "step", status: "completed" }],
};
const PLAN_EDITED: ThreadPlan = {
  goal: "B edited",
  steps: [{ id: "1", description: "step", status: "completed" }],
};

function planFrame(plan: ThreadPlan, id: string): SseEvent {
  return { id, event: "plan", data: plan, rawData: JSON.stringify(plan), receivedAt: "" };
}
function updatesFrame(data: unknown, id: string): SseEvent {
  return { id, event: "updates", data, rawData: JSON.stringify(data), receivedAt: "" };
}

describe("usePlanCard", () => {
  it("fetches the baseline once per thread and resets on thread change", async () => {
    const fetchPlan = vi.fn((id: string) => Promise.resolve(id === "t1" ? PLAN_A : PLAN_B));
    const savePlan = vi.fn();
    const { result, rerender } = renderHook(
      ({ threadId }) => usePlanCard({ threadId, liveEvents: [], fetchPlan, savePlan }),
      { initialProps: { threadId: "t1" } },
    );

    await waitFor(() => expect(result.current.plan).toEqual(PLAN_A));
    expect(result.current.loaded).toBe(true);
    expect(fetchPlan).toHaveBeenCalledTimes(1);
    expect(fetchPlan).toHaveBeenCalledWith("t1");

    rerender({ threadId: "t2" });
    // Reset happens synchronously on the render that observes the new threadId.
    expect(result.current.plan).toBeNull();
    expect(result.current.loaded).toBe(false);

    await waitFor(() => expect(result.current.plan).toEqual(PLAN_B));
    expect(fetchPlan).toHaveBeenCalledTimes(2);
    expect(fetchPlan).toHaveBeenCalledWith("t2");
  });

  it("applies the newest live plan snapshot once, and does not re-apply it after a PUT", async () => {
    const fetchPlan = vi.fn().mockResolvedValue(PLAN_A);
    const savePlan = vi.fn().mockResolvedValue(PLAN_EDITED);
    const events = [planFrame(PLAN_B, "id-1")];
    const { result, rerender } = renderHook(
      ({ ev }) => usePlanCard({ threadId: "t", liveEvents: ev, fetchPlan, savePlan }),
      { initialProps: { ev: events } },
    );

    // The live snapshot wins over the (slower-to-resolve) baseline fetch.
    await waitFor(() => expect(result.current.plan).toEqual(PLAN_B));

    await act(() => result.current.save(PLAN_EDITED));
    expect(result.current.plan).toEqual(PLAN_EDITED);

    // A new frame arrives but it's not a plan frame — no regression.
    rerender({ ev: [...events, updatesFrame({ agent: { messages: [] } }, "id-2")] });
    expect(result.current.plan).toEqual(PLAN_EDITED);

    // A genuinely new plan frame — overrides the PUT echo.
    rerender({ ev: [...events, planFrame(PLAN_C, "id-3")] });
    await waitFor(() => expect(result.current.plan).toEqual(PLAN_C));
  });

  it("ignores a plan snapshot that is not newer (same sourceKey) on unrelated re-renders", async () => {
    const fetchPlan = vi.fn().mockResolvedValue(null);
    const savePlan = vi.fn();
    const events = [planFrame(PLAN_A, "id-1")];
    const { result, rerender } = renderHook(
      ({ ev }) => usePlanCard({ threadId: "t", liveEvents: ev, fetchPlan, savePlan }),
      { initialProps: { ev: events } },
    );

    await waitFor(() => expect(result.current.plan).toEqual(PLAN_A));

    // New array reference, same frame id ("id-1") — must not re-apply, and
    // the plan-less baseline fetch resolving around now must not clobber it.
    rerender({ ev: [...events] });
    expect(result.current.plan).toEqual(PLAN_A);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.plan).toEqual(PLAN_A);
  });

  it("null threadId → no fetch, plan null, loaded false", () => {
    const fetchPlan = vi.fn();
    const savePlan = vi.fn();
    const { result } = renderHook(() =>
      usePlanCard({ threadId: null, liveEvents: [], fetchPlan, savePlan }),
    );

    expect(fetchPlan).not.toHaveBeenCalled();
    expect(result.current.plan).toBeNull();
    expect(result.current.loaded).toBe(false);
  });
});
