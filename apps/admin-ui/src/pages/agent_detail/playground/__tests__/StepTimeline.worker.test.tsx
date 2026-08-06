import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

import type { WorkerTimeline } from "../../../../api/worker_timeline";
import { WorkerSubTimeline } from "../StepTimeline";

function worker(over: Partial<WorkerTimeline> = {}): WorkerTimeline {
  return {
    workerId: "w-1",
    parentWorkerId: null,
    parentToolCallId: "call-1",
    label: "spawn_worker",
    agentRef: "dynamic:research",
    depth: 1,
    role: "research",
    taskExcerpt: "research X",
    maxSteps: 32,
    status: "success",
    steps: [
      {
        wseq: 1,
        node: "agent",
        stepCount: 1,
        durationMs: 120,
        messages: [{ type: "ai", contentExcerpt: "thinking", toolCalls: [{ name: "http_request", argsExcerpt: "{}" }] }],
      },
    ],
    children: [],
    summary: { iterationUsed: 2, llmCallCount: 1, wallClockMs: 900 },
    ...over,
  };
}

describe("WorkerSubTimeline", () => {
  it("renders header collapsed with role, status and summary", () => {
    render(<WorkerSubTimeline workers={[worker()]} />);
    const root = screen.getByTestId("worker-subtimeline");
    expect(root.textContent).toContain("research");
    expect(root.textContent).toContain("done");
    expect(screen.getByTestId("worker-summary").textContent).toContain("2 steps");
    expect(screen.queryByTestId("worker-step")).toBeNull(); // 默认收起
  });

  it("expands to steps on click", async () => {
    render(<WorkerSubTimeline workers={[worker()]} />);
    await userEvent.click(screen.getByTestId("worker-subtimeline-header"));
    expect(screen.getAllByTestId("worker-step")).toHaveLength(1);
    expect(screen.getByTestId("worker-step").textContent).toContain("http_request");
  });

  it("renders running status without summary and nests children recursively", async () => {
    const child = worker({ workerId: "w-2", depth: 2, status: "running", summary: null, steps: [], role: null });
    render(<WorkerSubTimeline workers={[worker({ children: [child] })]} />);
    await userEvent.click(screen.getByTestId("worker-subtimeline-header"));
    expect(screen.getAllByTestId("worker-subtimeline")).toHaveLength(2);
    expect(screen.getAllByTestId("worker-subtimeline")[1].textContent).toContain("running");
    // "without summary":父有 summary 行,running 子 worker 没有 → 恰好 1 个
    expect(screen.getAllByTestId("worker-summary")).toHaveLength(1);
  });

  it("appends a timeout marker to a worker's exec line (PR-D final-review fix wave)", async () => {
    const timedOutWorker = worker({
      steps: [
        {
          wseq: 1,
          node: "tools",
          stepCount: 1,
          durationMs: 5,
          messages: [
            {
              type: "tool",
              name: "bash",
              exec: { exitCode: -1, timedOut: true, stdoutExcerpt: "", stderrExcerpt: "" },
            },
          ],
        },
      ],
    });
    render(<WorkerSubTimeline workers={[timedOutWorker]} />);
    await userEvent.click(screen.getByTestId("worker-subtimeline-header"));
    expect(screen.getByTestId("worker-step").textContent).toContain("exit -1 · timeout");
  });
});
