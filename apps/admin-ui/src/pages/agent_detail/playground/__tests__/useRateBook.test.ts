import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { rateKey } from "../../../../api/cost";
import * as rateCardSdk from "../../../../api/rate_card";
import { useRateBook } from "../useRateBook";

const listRateCardsMock = vi.spyOn(rateCardSdk, "listRateCards");

function card(provider: string, model: string): rateCardSdk.RateCardRecord {
  return {
    id: `rc-${model}`,
    tenant_id: null,
    provider,
    model,
    input_per_mtok_micros: 1,
    output_per_mtok_micros: 1,
    cache_creation_per_mtok_micros: 0,
    cache_read_per_mtok_micros: 0,
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const AGENT = { provider: "anthropic", model: "claude-x" };
const WORKER = { provider: "zhipu", model: "glm-5.3" };

afterEach(() => {
  listRateCardsMock.mockReset();
});

describe("useRateBook", () => {
  it("keeps an in-flight card fetch alive across re-renders with a fresh (identical) models array, and fetches each model once", async () => {
    // 每收一帧 ``attributedModelsOf`` 都给一个新数组;卡是异步拉的。若拉取
    // 在下一帧到来时被「取消」而 key 又已登记为已拉,这张卡就永远不会落地
    // —— 整个会话的成本从此隐藏。
    const agentFetch = deferred<rateCardSdk.RateCardRecord[]>();
    listRateCardsMock.mockReturnValueOnce(agentFetch.promise);

    const { result, rerender } = renderHook(
      (props: { models: Array<{ provider: string; model: string }> }) =>
        useRateBook({ enabled: true, agentModel: AGENT, models: props.models }),
      { initialProps: { models: [] } },
    );
    expect(listRateCardsMock).toHaveBeenCalledTimes(1);
    expect(result.current?.byModel.size).toBe(0);

    // Two more frames before the card arrives.
    rerender({ models: [] });
    rerender({ models: [] });
    expect(listRateCardsMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      agentFetch.resolve([card(AGENT.provider, AGENT.model)]);
      await agentFetch.promise;
    });
    expect(result.current?.agentKey).toBe(rateKey(AGENT.provider, AGENT.model));
    expect(result.current?.byModel.get(rateKey(AGENT.provider, AGENT.model))?.id).toBe(
      "rc-claude-x",
    );
  });

  it("fetches a worker model's card when it first appears, and never a second time", async () => {
    listRateCardsMock.mockImplementation(async (params) =>
      params?.provider && params.model ? [card(params.provider, params.model)] : [],
    );
    const { result, rerender } = renderHook(
      (props: { models: Array<{ provider: string; model: string }> }) =>
        useRateBook({ enabled: true, agentModel: AGENT, models: props.models }),
      { initialProps: { models: [] as Array<{ provider: string; model: string }> } },
    );
    await act(async () => {});
    expect(listRateCardsMock).toHaveBeenCalledTimes(1);

    rerender({ models: [WORKER] });
    await act(async () => {});
    expect(listRateCardsMock).toHaveBeenCalledTimes(2);
    expect(listRateCardsMock).toHaveBeenLastCalledWith({ provider: "zhipu", model: "glm-5.3" });
    expect(result.current?.byModel.size).toBe(2);

    rerender({ models: [{ ...WORKER }] });
    rerender({ models: [{ ...WORKER }] });
    await act(async () => {});
    expect(listRateCardsMock).toHaveBeenCalledTimes(2);
  });

  it("fetches nothing and returns null when disabled (non-admin)", async () => {
    const { result } = renderHook(() =>
      useRateBook({ enabled: false, agentModel: AGENT, models: [WORKER] }),
    );
    await act(async () => {});
    expect(listRateCardsMock).not.toHaveBeenCalled();
    expect(result.current).toBeNull();
  });
});
