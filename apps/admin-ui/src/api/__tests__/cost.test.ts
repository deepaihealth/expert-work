import { describe, expect, it } from "vitest";

import { costCnyOfBuckets, costCnyOfMainLineCall, rateKey, type RateBook } from "../cost";
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

// B-279 —— 这些 B-42 用例的主 Agent 卡原来写的是 anthropic,可期望值是按「输入含缓存」
// 厂商的公式(先扣缓存命中)算的;那正是 B-279 修掉的少算。厂商改成输入含缓存的
// deepseek,用例本意(按模型分卡计价)不变;Anthropic 的口径见文件末尾 B-279 一节。
const AGENT = card("deepseek", "deepseek-chat", {
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
      [unattributed(main), attributed("deepseek", "deepseek-chat", worker)],
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

describe("costCnyOfMainLineCall (B-202)", () => {
  it("prices one main-line call at the agent's card — the same formula the turn total uses", () => {
    const u = usage(200_000, 10_000, 100_000);
    expect(costCnyOfMainLineCall({ input: 200_000, output: 10_000, cacheRead: 100_000, cacheCreation: 0 }, book([AGENT]))).toBe(
      legacyCostCny(u, AGENT),
    );
    // A worker card in the book changes nothing: main-line usage is unattributed.
    expect(
      costCnyOfMainLineCall({ input: 200_000, output: 10_000, cacheRead: 100_000, cacheCreation: 0 }, book([AGENT, WORKER])),
    ).toBe(legacyCostCny(u, AGENT));
  });

  it("returns null without a rate book or without the agent's card", () => {
    expect(costCnyOfMainLineCall({ input: 1, output: 1, cacheRead: 0, cacheCreation: 0 }, null)).toBeNull();
    expect(costCnyOfMainLineCall({ input: 1, output: 1, cacheRead: 0, cacheCreation: 0 }, book([WORKER], null))).toBeNull();
  });
});

// B-279 —— 和月度计费(billing-rollup-job ``_uncached_input_tokens``,B-185)同一口径:
// Anthropic 的 input_tokens 本来就不含缓存读写(三个数分开报),不能再扣;别的厂商
// (OpenAI 兼容)的 input_tokens 已经包含缓存命中 / 写入,先扣掉再按各自单价算。
// 缓存写入按「缓存写入价」计。期望值全是手算的字面量,不复用被测公式。
describe("costCnyOfBuckets / costCnyOfMainLineCall — 按厂商区分缓存口径(B-279)", () => {
  // 每百万 token:输入 3 元 / 缓存命中 0.3 元 / 缓存写入 3.75 元 / 输出 15 元。
  const CLAUDE: RateCardRecord = {
    ...card("anthropic", "claude-sonnet-4-6", { input: 3_000_000, cacheRead: 300_000, output: 15_000_000 }),
    cache_creation_per_mtok_micros: 3_750_000,
  };
  // 每百万 token:输入 2 元 / 缓存命中 0.2 元 / 缓存写入 2.5 元 / 输出 8 元。
  const DEEPSEEK: RateCardRecord = {
    ...card("deepseek", "deepseek-chat", { input: 2_000_000, cacheRead: 200_000, output: 8_000_000 }),
    cache_creation_per_mtok_micros: 2_500_000,
  };
  const withCache = (input: number, output: number, cacheRead: number, cacheCreation: number): TurnUsage => ({
    ...usage(input, output, cacheRead),
    cacheCreationTokens: cacheCreation,
  });

  it("Anthropic: input is NOT reduced by cache reads", () => {
    // 输入 10k(不含缓存)+ 命中 90k + 输出 1k:
    // 10k×3 + 90k×0.3 + 1k×15 = 30000 + 27000 + 15000 → ¥0.072
    // (旧算法先扣命中:max(0, 10k − 90k) = 0 → ¥0.042,少算四成)
    const cost = costCnyOfBuckets([unattributed(withCache(10_000, 1_000, 90_000, 0))], book([CLAUDE], CLAUDE));
    expect(cost).toBeCloseTo(0.072, 12);
  });

  it("Anthropic: cache writes are priced at the cache-write rate, on top of input", () => {
    // 输入 10k + 写入 20k + 输出 1k:10k×3 + 20k×3.75 + 1k×15 = 30000 + 75000 + 15000 → ¥0.12
    const cost = costCnyOfBuckets([unattributed(withCache(10_000, 1_000, 0, 20_000))], book([CLAUDE], CLAUDE));
    expect(cost).toBeCloseTo(0.12, 12);
  });

  it("non-Anthropic: input already contains the cache hits, so they come out first (unchanged)", () => {
    // 输入 100k(其中 60k 命中)+ 输出 1k:(100k − 60k)×2 + 60k×0.2 + 1k×8 = 80000 + 12000 + 8000 → ¥0.1
    const cost = costCnyOfBuckets([unattributed(withCache(100_000, 1_000, 60_000, 0))], book([DEEPSEEK], DEEPSEEK));
    expect(cost).toBeCloseTo(0.1, 12);
  });

  it("non-Anthropic: a reported cache write also sits inside input — taken out, then priced at the write rate", () => {
    // 输入 100k(含命中 60k、写入 10k):(100k − 60k − 10k)×2 + 60k×0.2 + 10k×2.5 + 1k×8
    // = 60000 + 12000 + 25000 + 8000 → ¥0.105
    const cost = costCnyOfBuckets([unattributed(withCache(100_000, 1_000, 60_000, 10_000))], book([DEEPSEEK], DEEPSEEK));
    expect(cost).toBeCloseTo(0.105, 12);
  });

  it("mixed turn: Anthropic main line + DeepSeek worker — each bucket under its own provider's rule", () => {
    // 主线(Anthropic):10k×3 + 90k×0.3 + 20k×3.75 + 1k×15 = 147000 → ¥0.147
    // worker(DeepSeek):(100k − 60k)×2 + 60k×0.2 + 1k×8 = 100000 → ¥0.1
    const cost = costCnyOfBuckets(
      [
        unattributed(withCache(10_000, 1_000, 90_000, 20_000)),
        attributed("deepseek", "deepseek-chat", withCache(100_000, 1_000, 60_000, 0)),
      ],
      book([CLAUDE, DEEPSEEK], CLAUDE),
    );
    expect(cost).toBeCloseTo(0.247, 12);
  });

  it("one main-line call gets the same rule as the turn total", () => {
    const tokens = { input: 10_000, output: 1_000, cacheRead: 90_000, cacheCreation: 20_000 };
    // Anthropic 主 Agent:同上主线 → ¥0.147
    expect(costCnyOfMainLineCall(tokens, book([CLAUDE], CLAUDE))).toBeCloseTo(0.147, 12);
    // 同样的数放到 DeepSeek 主 Agent 上:max(0, 10k − 90k − 20k)×2 + 90k×0.2 + 20k×2.5 + 1k×8
    // = 0 + 18000 + 50000 + 8000 → ¥0.076
    expect(costCnyOfMainLineCall(tokens, book([DEEPSEEK], DEEPSEEK))).toBeCloseTo(0.076, 12);
  });
});
