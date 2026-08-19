/**
 * useAgentTools 测试(PR-A.3 Task 10)—— 懒加载 + 会话内缓存(`enabled` 才发,
 * 一旦 ready 不因 enabled 反复切换而重发)、失败态 + `reload()` 重发、agent
 * identity(name/version)变了自动重置并重发。
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as agentsSdk from "../../../api/agents";
import { useAgentTools } from "../useAgentTools";

// hook 内直取 tenant scope;renderHook 不挂 Provider,mock 成 home 态
// (apiTenantScope undefined)。照 useRunTrace.test.ts。
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
}));

// 照 useRunTrace.test.ts:模块级声明一次 spy,每条测试 `mockReset()` 清掉上一条
// 留下的调用记录 / 排队实现,不然 `vi.spyOn` 会在既有 mock 上叠加。
const getAgentToolsMock = vi.spyOn(agentsSdk, "getAgentTools");

beforeEach(() => {
  getAgentToolsMock.mockReset();
});
afterEach(() => {
  vi.clearAllMocks();
});

const ITEM: agentsSdk.AgentToolSchema = {
  name: "bash",
  description: "run",
  parameters: { type: "object" },
  source: "builtin",
  from_skill: null,
  deferred: false,
};

describe("useAgentTools", () => {
  it("stays idle until enabled, then fetches once and indexes by name", async () => {
    const spy = getAgentToolsMock.mockResolvedValue({ items: [ITEM], total: 1 });
    const { result, rerender } = renderHook(
      ({ enabled }) => useAgentTools({ agentName: "a", agentVersion: "1", enabled }),
      { initialProps: { enabled: false } },
    );
    expect(result.current.status).toBe("idle");
    expect(spy).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.byName.get("bash")).toEqual(ITEM);

    rerender({ enabled: false });
    rerender({ enabled: true });
    expect(spy).toHaveBeenCalledTimes(1); // 整个会话复用,不因 enabled 抖动重发
  });

  it("error → status error; reload() refetches", async () => {
    const spy = getAgentToolsMock
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce({ items: [], total: 0 });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { result } = renderHook(() =>
      useAgentTools({ agentName: "a", agentVersion: "1", enabled: true }),
    );
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(warn).toHaveBeenCalledTimes(1);

    act(() => {
      result.current.reload();
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(spy).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });

  it("agent identity change resets and refetches", async () => {
    const spy = getAgentToolsMock.mockResolvedValue({ items: [], total: 0 });
    const { result, rerender } = renderHook(
      (p: { v: string }) => useAgentTools({ agentName: "a", agentVersion: p.v, enabled: true }),
      { initialProps: { v: "1" } },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(spy).toHaveBeenCalledWith("a", "1", undefined);

    rerender({ v: "2" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    expect(spy).toHaveBeenLastCalledWith("a", "2", undefined);
    await waitFor(() => expect(result.current.status).toBe("ready"));
  });
});
