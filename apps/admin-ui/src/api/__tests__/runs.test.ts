import { describe, expect, it, vi, beforeEach } from "vitest";

import { apiClient } from "../client";
import { listThreadRuns } from "../runs";

vi.mock("../client", () => ({
  apiClient: { get: vi.fn() },
  unwrap: (envelope: any) => envelope.data,
}));

describe("listThreadRuns", () => {
  beforeEach(() => vi.mocked(apiClient.get).mockReset());

  it("GETs the thread runs endpoint and maps to camelCase", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: {
        success: true,
        data: {
          runs: [
            { run_id: "r1", status: "success", is_resume: false, created_at: "2026-01-01T00:00:00Z" },
            { run_id: "r2", status: "paused", is_resume: true, created_at: "2026-01-01T00:01:00Z" },
          ],
        },
        error: null,
      },
    });
    const runs = await listThreadRuns("t1");
    expect(apiClient.get).toHaveBeenCalledWith("/v1/sessions/t1/runs", {
      params: undefined,
    });
    expect(runs).toEqual([
      {
        runId: "r1",
        status: "success",
        isResume: false,
        createdAt: "2026-01-01T00:00:00Z",
        tokens: null,
      },
      {
        runId: "r2",
        status: "paused",
        isResume: true,
        createdAt: "2026-01-01T00:01:00Z",
        tokens: null,
      },
    ]);
  });

  it("passes tenant_id when given", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { success: true, data: { runs: [] }, error: null },
    });
    await listThreadRuns("t1", "ten-9");
    expect(apiClient.get).toHaveBeenCalledWith("/v1/sessions/t1/runs", {
      params: { tenant_id: "ten-9" },
    });
  });
});
