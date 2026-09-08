import { describe, expect, it } from "vitest";

import { costCnyOfBuckets, rateKey, type RateBook } from "../cost";
import type { RateCardRecord } from "../rate_card";
import type { TurnUsage, UsageBucket } from "../turn_summary";

function card(
  provider: string,
  model: string,
  prices: { input: number; cacheRead: number; output: number },
): RateCardRecord {
  return {
    id: `rc-${provider}-${model}`,
    tenant_id: null,
    provider,
    model,
    input_per_mtok_micros: prices.input,
    output_per_mtok_micros: prices.output,
    cache_creation_per_mtok_micros: 0,
    cache_read_per_mtok_micros: prices.cacheRead,
  };
}

function usage(input: number, output: number, cacheRead = 0): TurnUsage {
  return {
    inputTokens: input,
    outputTokens: output,
    totalTokens: input + output,
    cacheReadTokens: cacheRead,
    cacheCreationTokens: 0,
    reasoningTokens: 0,
  };
}

/** 今天的算法(TurnBlock.costCnyOf,#4 cost):整轮 token × 单张卡。 */
function legacyCostCny(u: TurnUsage, rate: RateCardRecord): number {
  return (
    (Math.max(0, u.inputTokens - u.cacheReadTokens) * rate.input_per_mtok_micros +
      u.cacheReadTokens * rate.cache_read_per_mtok_micros +
      u.outputTokens * rate.output_per_mtok_micros) /
    1e12
  );
}

const AGENT = card("anthropic", "claude-sonnet-4-6", {
  input: 3_000_000,
  cacheRead: 300_000,
  output: 15_000_000,
});
const WORKER = card("zhipu", "glm-5.3", { input: 500_000, cacheRead: 50_000, output: 2_000_000 });

function book(cards: readonly RateCardRecord[], agent: RateCardRecord | null = AGENT): RateBook {
  return {
    agentKey: agent ? rateKey(agent.provider, agent.model) : null,
    byModel: new Map(cards.map((c) => [rateKey(c.provider, c.model), c])),
  };
}

const unattributed = (u: TurnUsage): UsageBucket => ({ provider: null, model: null, usage: u });
const attributed = (provider: string, model: string, u: TurnUsage): UsageBucket => ({
  provider,
  model,
  usage: u,
});

describe("costCnyOfBuckets", () => {
  it("prices each bucket at its own (provider, model) rate — a worker on another model is not billed at the agent's rate", () => {
    // B-42 的病灶:run f562fa69 里 worker 3,317,974 tok / 主线 175,137 tok,
    // 95% 的计价 token 被乘上了主 Agent 的单价。
    const main = usage(175_137, 5_137, 1_000);
    const worker = usage(3_200_000, 117_974, 900_000);
    const cost = costCnyOfBuckets(
      [unattributed(main), attributed("zhipu", "glm-5.3", worker)],
      book([AGENT, WORKER]),
    );
    expect(cost).toBe(legacyCostCny(main, AGENT) + legacyCostCny(worker, WORKER));
    // 反证:按今天的单卡算法(全部 × 主 Agent 卡)得到的是另一个数。
    const whole = usage(3_375_137, 123_111, 901_000);
    expect(cost).not.toBe(legacyCostCny(whole, AGENT));
  });

  it("is bit-identical to the single-card formula when the worker runs the agent's own model", () => {
    // 同模型 → 同一张卡 → 先并再算,和今天 ``summary.usage × rate`` 一模一样
    // (不是「约等于」:整数微元累加后只除一次 1e12,连浮点尾数都一样)。
    const main = usage(175_137, 5_137, 1_000);
    const worker = usage(3_200_000, 117_974, 900_000);
    const whole = usage(3_375_137, 123_111, 901_000);
    const cost = costCnyOfBuckets(
      [unattributed(main), attributed("anthropic", "claude-sonnet-4-6", worker)],
      book([AGENT]),
    );
    expect(cost).toBe(legacyCostCny(whole, AGENT));
  });

  it("prices an unattributed bucket (no usage_by_model on the frame) at the agent's card — today's fallback", () => {
    const main = usage(1_000_000, 100_000, 400_000);
    const worker = usage(2_000_000, 50_000, 0);
    const cost = costCnyOfBuckets([unattributed(main), unattributed(worker)], book([AGENT]));
    expect(cost).toBe(legacyCostCny(usage(3_000_000, 150_000, 400_000), AGENT));
  });

  it("returns null when no usage was reported, when there is no rate book, or when the agent's card is missing", () => {
    expect(costCnyOfBuckets([], book([AGENT]))).toBeNull();
    expect(costCnyOfBuckets([unattributed(usage(1, 1))], null)).toBeNull();
    expect(costCnyOfBuckets([unattributed(usage(1, 1))], book([], null))).toBeNull();
  });

  it("returns null while an attributed bucket's card is still unknown, rather than silently pricing it at the agent's rate", () => {
    // 卡是异步拉的:worker 的卡还没到时宁可先不显示,也不显示一个错的数。
    const cost = costCnyOfBuckets(
      [unattributed(usage(10, 1)), attributed("zhipu", "glm-5.3", usage(1_000, 100))],
      book([AGENT]),
    );
    expect(cost).toBeNull();
  });
});
