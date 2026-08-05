import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { ToolCallCard, ToolTimeline } from "../ToolTimeline";
import { ApiError } from "../../api/client";
import type { SseEvent } from "../../api/sessions";
import type { ToolCallEntry } from "../../api/tool_timeline";
import * as triggersSdk from "../../api/triggers";
import type { FireNowResult } from "../../api/triggers";

// Cross-tenant W3 — 立即触发按钮的切入态判定 hook 会触 useAuth/useTenantScope
// (这里不挂 Provider),mock 掉;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

function updates(node: string, messages: unknown[]): SseEvent {
  return { id: null, event: "updates", data: { [node]: { messages } }, rawData: "", receivedAt: "" };
}

function baseEntry(over: Partial<ToolCallEntry> = {}): ToolCallEntry {
  return {
    id: "c1",
    rawName: "search",
    isMcp: false,
    server: null,
    toolName: "search",
    args: { q: "test" },
    status: "success",
    resultPreview: null,
    durationMs: null,
    ...over,
  };
}

/** Opens the "结果/Result" collapse panel — the badge lives in the header
 *  (always visible), but the cleaned text is inside the collapsed body. */
function openResultPanel() {
  fireEvent.click(screen.getByText(/^(Result|结果)$/));
}

/** ``ToolCallCard`` calls ``App.useApp()`` for the 立即触发 button's error
 *  toast — needs a real ``<App>`` ancestor (the antd v5 static-method
 *  fallback used outside one is an empty stub, not the real message API). */
function renderFireCard(entry: ToolCallEntry, onFireResult?: (result: FireNowResult) => void) {
  return render(
    <App>
      <ToolCallCard entry={entry} onFireResult={onFireResult} />
    </App>,
  );
}

describe("ToolTimeline", () => {
  it("shows an empty state when there are no tool calls", () => {
    render(<ToolTimeline events={[]} />);
    expect(screen.getByTestId("tool-timeline-empty")).toBeInTheDocument();
  });

  it("renders an MCP call with its server + tool name and a success status", () => {
    const events = [
      updates("agent", [
        {
          type: "ai",
          content: "",
          tool_calls: [
            {
              id: "c1",
              name: "mcp__amap-maps__maps_direction_driving",
              args: { origin: "a" },
              type: "tool_call",
            },
          ],
        },
      ]),
      updates("tools", [
        { type: "tool", tool_call_id: "c1", name: null, content: "{\"d\":1}", status: "success" },
      ]),
    ];
    render(<ToolTimeline events={events} />);
    expect(screen.getByTestId("tool-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("tool-call-card")).toBeInTheDocument();
    // MCP badge carries the server name.
    expect(screen.getByText(/amap-maps/)).toBeInTheDocument();
    expect(screen.getByText("maps_direction_driving")).toBeInTheDocument();
  });

  it("renders a structured exec_python result with an exit-code chip", () => {
    const events = [
      updates("agent", [
        {
          type: "ai",
          content: "",
          tool_calls: [
            { id: "c1", name: "exec_python", args: { code: "print(1)" }, type: "tool_call" },
          ],
        },
      ]),
      updates("tools", [
        { type: "tool", tool_call_id: "c1", name: null, content: "stdout:\n1\n\nexit_code: 0", status: "success" },
      ]),
    ];
    render(<ToolTimeline events={events} />);
    // The result panel is collapsed by default — open it to reveal the
    // structured exec-result content.
    fireEvent.click(screen.getByText(/^(Result|结果)$/));
    expect(screen.getByTestId("tool-exec-result")).toBeInTheDocument();
    expect(screen.getByTestId("tool-exit-code")).toHaveTextContent("0");
  });

  it("cleans an untrusted resultPreview (fence + ▁ glyph) and shows the badge", () => {
    const dirty = "«UNTRUSTED nonce=abc»\n搜索结果▁ 有效\n«/UNTRUSTED nonce=abc»";
    render(<ToolCallCard entry={baseEntry({ resultPreview: dirty })} />);
    // The badge lives in the always-visible header label.
    expect(screen.getByTestId("tool-untrusted")).toBeInTheDocument();
    openResultPanel();
    const pre = screen.getByText(/搜索结果 有效/);
    expect(pre.textContent).not.toContain("▁");
    expect(pre.textContent).not.toContain("UNTRUSTED");
  });

  it("cleans an untrusted execResult.stdout and shows the badge", () => {
    const entry = baseEntry({
      toolName: "exec_python",
      execResult: {
        stdout: "«UNTRUSTED nonce=x»\n1▁ 2▁ 3\n«/UNTRUSTED nonce=x»",
        stderr: "",
        exitCode: 0,
      },
    });
    render(<ToolCallCard entry={entry} />);
    expect(screen.getByTestId("tool-untrusted")).toBeInTheDocument();
    openResultPanel();
    const stdout = screen.getByText(/1 2 3/);
    expect(stdout.textContent).not.toContain("▁");
    expect(stdout.textContent).not.toContain("UNTRUSTED");
  });

  it("keeps the untrusted badge when exec fields come from the artifact", () => {
    // Artifact-sourced stdout is raw (no ▁ glyph) — the badge must fall back
    // to sniffing the datamarked resultPreview.
    renderFireCard(
      baseEntry({
        rawName: "exec_python",
        toolName: "exec_python",
        resultPreview: "stdout:▁ 1▁ exit_code:▁ 0",
        execResult: { stdout: "1\n", stderr: "", exitCode: 0 },
      }),
    );
    expect(screen.getByTestId("tool-untrusted")).toBeInTheDocument();
    openResultPanel();
    expect(screen.getByTestId("tool-exit-code").textContent).toContain("0");
  });

  it("shows a timed-out tag next to the exit code when execResult.timedOut is true", () => {
    render(
      <ToolCallCard
        entry={baseEntry({
          toolName: "exec_python",
          execResult: { stdout: "", stderr: "", exitCode: -1, timedOut: true },
        })}
      />,
    );
    openResultPanel();
    expect(screen.getByTestId("tool-timed-out")).toBeInTheDocument();
  });

  it("does not show the timed-out tag when execResult.timedOut is unset", () => {
    render(
      <ToolCallCard
        entry={baseEntry({
          toolName: "exec_python",
          execResult: { stdout: "1", stderr: "", exitCode: 0 },
        })}
      />,
    );
    openResultPanel();
    expect(screen.queryByTestId("tool-timed-out")).not.toBeInTheDocument();
  });

  it("does not show the badge for a clean result, and leaves args untouched", () => {
    render(
      <ToolCallCard
        entry={baseEntry({ resultPreview: "clean result", args: { q: "▁contains-glyph" } })}
      />,
    );
    expect(screen.queryByTestId("tool-untrusted")).not.toBeInTheDocument();
    // Arguments are trusted model input — never cleaned, even if they happen
    // to contain a ▁ character.
    fireEvent.click(screen.getByText(/^(Arguments|参数)$/));
    expect(screen.getByText(/▁contains-glyph/)).toBeInTheDocument();
  });

  it("shows the badge end-to-end via the real SSE→parseToolCalls pipeline (fence already stripped upstream, ▁ glyph remains)", () => {
    const events = [
      updates("agent", [
        {
          type: "ai",
          content: "",
          tool_calls: [{ id: "c1", name: "search", args: { q: "test" }, type: "tool_call" }],
        },
      ]),
      updates("tools", [
        {
          type: "tool",
          tool_call_id: "c1",
          name: null,
          content: "«UNTRUSTED nonce=xyz»\n结果▁ 内容\n«/UNTRUSTED nonce=xyz»",
          status: "success",
        },
      ]),
    ];
    render(<ToolTimeline events={events} />);
    expect(screen.getByTestId("tool-untrusted")).toBeInTheDocument();
    openResultPanel();
    const pre = screen.getByText(/结果 内容/);
    expect(pre.textContent).not.toContain("▁");
  });
});

describe("ToolCallCard 立即触发 / run-now button (Spec 1 PR4 Task 4)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
    isTenantSwitchedMock.mockReturnValue(false);
  });

  it("shows the button for a successful manage_task create card with a triggerId", () => {
    renderFireCard(
      baseEntry({ toolName: "manage_task", triggerId: "trig-1", action: "create" }),
    );
    expect(screen.getByTestId("tool-fire-now")).toBeInTheDocument();
  });

  // Cross-tenant W3 — 立即触发是写操作(以 trigger 的 user 跑 run),切入态置灰。
  it("home 态立即触发可用;切入态置灰(两态)", () => {
    const entry = baseEntry({
      toolName: "manage_task",
      triggerId: "trig-1",
      action: "create",
    });
    const first = renderFireCard(entry);
    expect(screen.getByTestId("tool-fire-now")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    renderFireCard(entry);
    expect(screen.getByTestId("tool-fire-now")).toBeDisabled();
  });

  it("hides the button when there is no triggerId", () => {
    renderFireCard(baseEntry({ toolName: "manage_task", triggerId: null, action: "create" }));
    expect(screen.queryByTestId("tool-fire-now")).not.toBeInTheDocument();
  });

  it("hides the button for a non-manage_task tool even if it somehow carried a triggerId", () => {
    renderFireCard(baseEntry({ toolName: "search", triggerId: "trig-1", action: "create" }));
    expect(screen.queryByTestId("tool-fire-now")).not.toBeInTheDocument();
  });

  it("hides the button while the call has not yet succeeded", () => {
    renderFireCard(
      baseEntry({
        toolName: "manage_task",
        triggerId: "trig-1",
        status: "pending",
        action: "create",
      }),
    );
    expect(screen.queryByTestId("tool-fire-now")).not.toBeInTheDocument();
  });

  // PR4 Task 4 — an update card touches an existing task (no fresh trigger to
  // preview-fire); only a create card gets the shortcut.
  it("hides the button on an update card even with a triggerId and success status", () => {
    renderFireCard(
      baseEntry({ toolName: "manage_task", triggerId: "trig-1", action: "update" }),
    );
    expect(screen.queryByTestId("tool-fire-now")).not.toBeInTheDocument();
  });

  it("fires, shows a delivered status, and calls onFireResult with the response", async () => {
    const user = userEvent.setup();
    const result: FireNowResult = {
      run_id: "r1",
      thread_id: "t1",
      run_status: "success",
      trigger_run_status: "succeeded",
      delivery: "delivered",
      delivered_text: "Today's digest: …",
    };
    const fireMock = vi.spyOn(triggersSdk, "fireTriggerNow").mockResolvedValue(result);
    const onFireResult = vi.fn();
    renderFireCard(
      baseEntry({ toolName: "manage_task", triggerId: "trig-1", action: "create" }),
      onFireResult,
    );

    await user.click(screen.getByTestId("tool-fire-now"));

    await waitFor(() => expect(fireMock).toHaveBeenCalledWith("trig-1"));
    await waitFor(() =>
      expect(screen.getByTestId("tool-fire-status")).toHaveTextContent(
        /结果已落回对话|Result delivered/,
      ),
    );
    expect(onFireResult).toHaveBeenCalledWith(result);
  });

  it("shows a pending status when the bounded poll times out before completion", async () => {
    const user = userEvent.setup();
    vi.spyOn(triggersSdk, "fireTriggerNow").mockResolvedValue({
      run_id: "r1",
      thread_id: "t1",
      run_status: "running",
      trigger_run_status: "fired",
      delivery: "pending",
    });
    renderFireCard(
      baseEntry({ toolName: "manage_task", triggerId: "trig-1", action: "create" }),
    );

    await user.click(screen.getByTestId("tool-fire-now"));

    await waitFor(() =>
      expect(screen.getByTestId("tool-fire-status")).toHaveTextContent(
        /已触发,运行中|Fired, still running/,
      ),
    );
  });

  it("surfaces an error toast and resets the button after a failed fire", async () => {
    const user = userEvent.setup();
    vi.spyOn(triggersSdk, "fireTriggerNow").mockRejectedValue(
      new ApiError("agent unavailable", "TRIGGER_AGENT_UNAVAILABLE", 409),
    );
    renderFireCard(
      baseEntry({ toolName: "manage_task", triggerId: "trig-1", action: "create" }),
    );

    await user.click(screen.getByTestId("tool-fire-now"));

    // The error toast (App.useApp().message) carries the ApiError code.
    await screen.findByText(/TRIGGER_AGENT_UNAVAILABLE/);
    // Loading resets — the button falls back to its idle label.
    await waitFor(() =>
      expect(screen.getByTestId("tool-fire-now")).toHaveTextContent(/^(立即触发|Run now)$/),
    );
    // No delivery ever resolved, so no status tag renders.
    expect(screen.queryByTestId("tool-fire-status")).not.toBeInTheDocument();
  });

  // PR4 Task 4 — re-firing must not leave the previous run's status tag
  // showing while the new fire is in flight.
  it("clears the previous delivery status as soon as a re-fire starts (no stale tag)", async () => {
    const user = userEvent.setup();
    const first: FireNowResult = {
      run_id: "r1",
      thread_id: "t1",
      run_status: "success",
      trigger_run_status: "succeeded",
      delivery: "delivered",
      delivered_text: "first run",
    };
    const second: FireNowResult = {
      run_id: "r2",
      thread_id: "t1",
      run_status: "success",
      trigger_run_status: "succeeded",
      delivery: "delivered",
      delivered_text: "second run",
    };
    let resolveSecond: (result: FireNowResult) => void = () => {};
    const secondPromise = new Promise<FireNowResult>((resolve) => {
      resolveSecond = resolve;
    });
    vi.spyOn(triggersSdk, "fireTriggerNow")
      .mockResolvedValueOnce(first)
      .mockReturnValueOnce(secondPromise);
    renderFireCard(
      baseEntry({ toolName: "manage_task", triggerId: "trig-1", action: "create" }),
    );

    await user.click(screen.getByTestId("tool-fire-now"));
    await waitFor(() =>
      expect(screen.getByTestId("tool-fire-status")).toHaveTextContent(
        /结果已落回对话|Result delivered/,
      ),
    );

    // Re-fire while the second call is still pending — handleFire's
    // setDelivery(null) at the top must clear the stale "delivered" tag
    // from the first run immediately, not leave it hanging until the
    // second call resolves.
    await user.click(screen.getByTestId("tool-fire-now"));
    await waitFor(() =>
      expect(screen.queryByTestId("tool-fire-status")).not.toBeInTheDocument(),
    );

    resolveSecond(second);
    await waitFor(() =>
      expect(screen.getByTestId("tool-fire-status")).toHaveTextContent(
        /结果已落回对话|Result delivered/,
      ),
    );
  });
});
