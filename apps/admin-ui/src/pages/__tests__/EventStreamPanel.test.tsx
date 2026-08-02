/**
 * EventStreamPanel tests — Stream H.3 PR 4.
 *
 * The streamRunEvents SDK is mocked (it would otherwise hit fetch); we
 * drive it as an async generator from the test body.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";
import i18n from "../../i18n";

import * as runsSdk from "../../api/runs";
import { EventStreamPanel } from "../run_detail/EventStreamPanel";
import type { SseEvent } from "../../api/sessions";

// Track C W2 — 组件内直取 tenant scope;这些测试不挂 Provider,mock 成
// home 态(apiTenantScope undefined)。
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

const streamMock = vi.spyOn(runsSdk, "streamRunEvents");

beforeEach(() => {
  streamMock.mockReset();
  if (typeof window !== "undefined") window.localStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
});

function makeStream(events: SseEvent[]): AsyncGenerator<SseEvent, void, void> {
  return (async function* () {
    for (const e of events) yield e;
  })();
}

describe("EventStreamPanel", () => {
  it("starts collapsed by default — no stream connection", () => {
    render(<EventStreamPanel threadId="t-1" runId="r-1" />);
    expect(screen.getByTestId("event-stream-toggle")).toHaveAttribute("aria-expanded", "false");
    expect(streamMock).not.toHaveBeenCalled();
  });

  it("expanding opens the stream and renders frames", async () => {
    const user = userEvent.setup();
    streamMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "metadata",
          data: { run_id: "r-1" },
          rawData: "",
          receivedAt: "2026-05-26T08:00:00Z",
        },
        {
          id: "2",
          event: "updates",
          data: { agent: "stub" },
          rawData: "",
          receivedAt: "2026-05-26T08:00:01Z",
        },
        {
          id: "3",
          event: "end",
          data: null,
          rawData: "null",
          receivedAt: "2026-05-26T08:00:02Z",
        },
      ]),
    );

    render(<EventStreamPanel threadId="t-1" runId="r-1" />);
    await user.click(screen.getByTestId("event-stream-toggle"));
    // Raw-event view to assert individual frames (default is the timeline).
    await user.click(screen.getByText(i18n.t("event_stream.view_raw")));

    await screen.findByTestId("event-card-metadata");
    expect(screen.getByTestId("event-card-updates")).toBeInTheDocument();
    expect(screen.getByTestId("event-card-end")).toBeInTheDocument();
  });

  it("renders a compaction summary card in the timeline view", async () => {
    const user = userEvent.setup();
    streamMock.mockReturnValue(
      makeStream([
        {
          id: "1",
          event: "compaction",
          data: { passes: 1, tokens_before: 12000, tokens_after: 3400, summary_chars: 890 },
          rawData: "",
          receivedAt: "2026-05-26T08:00:00Z",
        },
        {
          id: "2",
          event: "end",
          data: null,
          rawData: "null",
          receivedAt: "2026-05-26T08:00:02Z",
        },
      ]),
    );

    render(<EventStreamPanel threadId="t-1" runId="r-1" />);
    // Timeline is the default view — no view switch needed.
    await user.click(screen.getByTestId("event-stream-toggle"));

    const card = await screen.findByTestId("compaction-card");
    expect(card).toHaveTextContent(i18n.t("event_stream.compaction_label"));
    expect(card).toHaveTextContent("12,000");
    expect(card).toHaveTextContent("3,400");
    // reduction = (12000 - 3400) / 12000 ≈ 71.67% → 72%
    expect(card).toHaveTextContent("72");
  });

  it("persists the expanded preference via localStorage", async () => {
    const user = userEvent.setup();
    streamMock.mockReturnValue(makeStream([]));
    render(<EventStreamPanel threadId="t-1" runId="r-1" />);
    await user.click(screen.getByTestId("event-stream-toggle"));
    await waitFor(() => {
      expect(window.localStorage.getItem("expert_work.runDetail.eventStream.expanded")).toBe("1");
    });
  });

  it("surfaces a stream-failure alert when streamRunEvents throws", async () => {
    const user = userEvent.setup();
    streamMock.mockImplementation(() => {
      return (async function* () {
        throw new Error("boom");
      })();
    });
    render(<EventStreamPanel threadId="t-1" runId="r-1" />);
    await user.click(screen.getByTestId("event-stream-toggle"));
    const alert = await screen.findByTestId("event-stream-error");
    expect(alert).toHaveTextContent("boom");
  });

  it("collapsing aborts the in-flight stream", async () => {
    const user = userEvent.setup();
    // A stream that never ends — caller must abort.
    streamMock.mockImplementation((_t: string, _r: string, opts: { signal?: AbortSignal } = {}) => {
      return (async function* () {
        // Park until aborted; the test then collapses the panel.
        await new Promise<void>((resolve, reject) => {
          opts.signal?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          });
          setTimeout(resolve, 5000);
        });
      })();
    });

    render(<EventStreamPanel threadId="t-1" runId="r-1" />);
    await user.click(screen.getByTestId("event-stream-toggle"));
    await waitFor(() => expect(streamMock).toHaveBeenCalled());
    // Collapse — the generator should be aborted and no error surfaces.
    await user.click(screen.getByTestId("event-stream-toggle"));
    await waitFor(() => {
      expect(screen.getByTestId("event-stream-toggle")).toHaveAttribute(
        "aria-expanded",
        "false",
      );
    });
    expect(screen.queryByTestId("event-stream-error")).not.toBeInTheDocument();
  });
});
