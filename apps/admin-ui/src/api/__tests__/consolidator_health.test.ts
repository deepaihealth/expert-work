/**
 * B-51 — 长期记忆整合健康度的归纳(纯函数)。
 *
 * 审计行是「新→旧」序;判据是从最近一次往前数的**连续**凭据失败,中间一次
 * 干净的 sweep 就断链 —— 否则一条陈年失败会永远挂着一条 banner。
 */
import { describe, expect, it } from "vitest";

import {
  summariseConsolidatorHealth,
  type ConsolidatorSweep,
} from "../memory";

function sweep(
  overrides: Partial<ConsolidatorSweep> = {},
): ConsolidatorSweep {
  return {
    occurred_at: "2026-09-11T00:00:00Z",
    consolidated: 0,
    errors: 0,
    errors_by_reason: {},
    missing_credential_providers: [],
    ...overrides,
  };
}

const credFailure = (provider = "anthropic") =>
  sweep({
    errors: 1,
    errors_by_reason: { credentials_missing: 1 },
    missing_credential_providers: [provider],
  });

describe("summariseConsolidatorHealth", () => {
  it("counts the leading streak of credential failures", () => {
    const health = summariseConsolidatorHealth([
      credFailure(),
      credFailure(),
      credFailure(),
    ]);
    expect(health.consecutiveCredentialFailures).toBe(3);
    expect(health.providers).toEqual(["anthropic"]);
  });

  it("stops at the first sweep that did not fail on credentials", () => {
    const health = summariseConsolidatorHealth([
      credFailure(),
      sweep({ consolidated: 2 }),
      credFailure(),
    ]);
    expect(health.consecutiveCredentialFailures).toBe(1);
  });

  it("a non-credential failure does not count as a credential failure", () => {
    const health = summariseConsolidatorHealth([
      sweep({ errors: 1, errors_by_reason: { other: 1 } }),
    ]);
    expect(health.consecutiveCredentialFailures).toBe(0);
    expect(health.providers).toEqual([]);
  });

  it("a healthy latest sweep reports zero even with older failures", () => {
    const health = summariseConsolidatorHealth([
      sweep({ consolidated: 5 }),
      credFailure(),
    ]);
    expect(health.consecutiveCredentialFailures).toBe(0);
  });

  it("dedupes providers across the streak", () => {
    const health = summariseConsolidatorHealth([
      credFailure("anthropic"),
      credFailure("qwen"),
      credFailure("anthropic"),
    ]);
    expect(health.providers).toEqual(["anthropic", "qwen"]);
  });

  it("no sweeps at all reports nothing", () => {
    expect(summariseConsolidatorHealth([])).toEqual({
      consecutiveCredentialFailures: 0,
      providers: [],
      lastSweepAt: null,
    });
  });
});
