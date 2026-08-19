/**
 * useAgentTools 测试(PR-A.3 Task 10 + fix round 1)—— 懒加载 + 会话内缓存
 * (`enabled` 才发,一旦 ready 不因 enabled 反复切换而重发)、失败态 +
 * `reload()` 重发、agent identity(name/version)变了自动重置并重发、
 * disable 中途取消不卡死在 "loading"(Critical #1)、`apiTenantScope` 翻转
 * 当新 identity 重取(Important #2)。
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as agentsSdk from "../../../api/agents";
import { useAgentTools } from "../useAgentTools";

// hook 内直取 tenant scope;renderHook 不挂 Provider,mock 成可控的 home 态。
// `hoisted.apiTenantScope` 可被单条测试改写后 `rerender()`,复现
// system_admin 跨租户翻转(Important #2 的测试要用)。照 useRunTrace.test.ts。
const hoisted = vi.hoisted(() => ({ apiTenantScope: undefined as string | undefined }));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: hoisted.apiTenantScope,
  }),
}));

// 照 useRunTrace.test.ts:模块级声明一次 spy,每条测试 `mockReset()` 清掉上一条
// 留下的调用记录 / 排队实现,不然 `vi.spyOn` 会在既有 mock 上叠加。
const getAgentToolsMock = vi.spyOn(agentsSdk, "getAgentTools");

beforeEach(() => {
  getAgentToolsMock.mockReset();
  hoisted.apiTenantScope = undefined;
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

  // Fix round 1, Critical #1 —— 快速在 TOOL 行 / ASSISTANT 行之间点选时,
  // `enabled` 会在上一次 fetch 还没落地(`status === "loading"`)时就翻回
  // false;旧实现只在 identity 变化时才把 status 复位回 idle,于是这里永久
  // 卡在 "loading"(而 loading 分支没有重试按钮,用户没有出口)。用一个手动
  // resolve 的 deferred promise 控住第一次请求的时序来复现。
  it("disable 发生在 fetch 未落地时把 'loading' 复位成 idle,而不是卡死;再 enable 会重新发一次", async () => {
    let resolveFirst!: (v: { items: agentsSdk.AgentToolSchema[]; total: number }) => void;
    const first = new Promise<{ items: agentsSdk.AgentToolSchema[]; total: number }>((resolve) => {
      resolveFirst = resolve;
    });
    const spy = getAgentToolsMock
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce({ items: [ITEM], total: 1 });

    const { result, rerender } = renderHook(
      ({ enabled }) => useAgentTools({ agentName: "a", agentVersion: "1", enabled }),
      { initialProps: { enabled: true } },
    );
    await waitFor(() => expect(result.current.status).toBe("loading"));
    expect(spy).toHaveBeenCalledTimes(1);

    // 中途切走(比如点到另一条没有工具名的记录)—— enabled 翻 false,第一次
    // fetch 还悬着。
    rerender({ enabled: false });
    expect(result.current.status).toBe("idle");

    // 再切回来——必须重新发一次请求,不能因为「以为还在 loading」而卡住。
    rerender({ enabled: true });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.byName.get("bash")).toEqual(ITEM);

    // 第一个(已被放弃)请求晚到落地,不该覆盖第二次请求已经写好的结果 ——
    // `cancelled` 守卫住了那个悬着的 `.then`。
    resolveFirst({ items: [], total: 0 });
    await Promise.resolve();
    expect(result.current.status).toBe("ready");
    expect(result.current.byName.get("bash")).toEqual(ITEM);
  });

  // Fix round 1, Important #2 —— system_admin 切换跨租户视角时
  // `apiTenantScope` 会从 `undefined` 翻成 `"*"` / 具体租户 UUID
  // (TenantScopeContext.tsx:122-133);这应该跟 agent identity 变化一样重置
  // 并重取,而不是把上一个租户缓存下来的工具集继续显示成当前租户的。
  it("apiTenantScope 翻转(system_admin 跨租户)当新 identity 重置并重取", async () => {
    const spy = getAgentToolsMock.mockResolvedValue({ items: [], total: 0 });
    const { result, rerender } = renderHook(() =>
      useAgentTools({ agentName: "a", agentVersion: "1", enabled: true }),
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(spy).toHaveBeenCalledWith("a", "1", undefined);

    hoisted.apiTenantScope = "22222222-2222-2222-2222-222222222222";
    rerender();
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    expect(spy).toHaveBeenLastCalledWith("a", "1", "22222222-2222-2222-2222-222222222222");
    await waitFor(() => expect(result.current.status).toBe("ready"));
  });
});
