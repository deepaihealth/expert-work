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
            // r1 = 新后端行(带 finished_at + error);r2 = 老后端行(缺
            // 两字段)→ 必须映射成 null 而不是 undefined。
            {
              run_id: "r1",
              status: "interrupted",
              is_resume: false,
              created_at: "2026-01-01T00:00:00Z",
              finished_at: "2026-01-01T00:00:30Z",
              error: "user_cancel",
            },
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
        status: "interrupted",
        isResume: false,
        createdAt: "2026-01-01T00:00:00Z",
        finishedAt: "2026-01-01T00:00:30Z",
        error: "user_cancel",
        tokens: null,
      },
      {
        runId: "r2",
        status: "paused",
        isResume: true,
        createdAt: "2026-01-01T00:01:00Z",
        finishedAt: null,
        error: null,
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
