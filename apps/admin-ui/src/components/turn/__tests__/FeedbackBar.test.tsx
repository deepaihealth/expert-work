/**
 * FeedbackBar — P-2:打分按 run 走,提交体必须带 ``run_id``。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import * as sessionsSdk from "../../../api/sessions";
import { FeedbackBar } from "../FeedbackBar";

describe("FeedbackBar", () => {
  it("posts run_id with the rating", async () => {
    const spy = vi.spyOn(sessionsSdk, "submitSessionFeedback").mockResolvedValue({
      id: 1,
      thread_id: "th-1",
      run_id: "run-1",
      rating: "up",
      turn_seq: 3,
      trace_id: null,
      updated: false,
    });
    render(<FeedbackBar threadId="th-1" runId="run-1" turnSeq={3} />);
    await userEvent.setup().click(screen.getByTestId("playground-feedback-up"));
    expect(spy).toHaveBeenCalledWith("th-1", {
      rating: "up",
      comment: undefined,
      run_id: "run-1",
      turn_seq: 3,
    });
    expect(await screen.findByText(/thanks|感谢/i)).toBeInTheDocument();
  });
});
