/**
 * configChangePoints —— 「配置在第几轮之后变过」。
 *
 * 这批用例里一半是「不该报什么」:一个会在历史数据上乱报的标记,比没有标记
 * 更糟 —— 它会把每一次复盘都引向错误的方向。
 */
import { describe, expect, it } from "vitest";

import { configChangePoints } from "../conversation_detail/configChanges";

const A = "a".repeat(64);
const B = "b".repeat(64);

function run(created: string, sha?: string | null) {
  return { created_at: created, agent_spec_sha256: sha };
}

describe("configChangePoints", () => {
  it("points at the turn after which the config changed", () => {
    expect(
      configChangePoints([
        run("2026-08-29T10:00:00Z", A),
        run("2026-08-29T10:01:00Z", A),
        run("2026-08-29T10:02:00Z", B),
      ]),
    ).toEqual([2]);
  });

  it("reports every change, not just the first", () => {
    expect(
      configChangePoints([
        run("2026-08-29T10:00:00Z", A),
        run("2026-08-29T10:01:00Z", B),
        run("2026-08-29T10:02:00Z", A),
      ]),
    ).toEqual([1, 2]);
  });

  it("says nothing when the whole conversation ran one config", () => {
    expect(
      configChangePoints([run("2026-08-29T10:00:00Z", A), run("2026-08-29T10:01:00Z", A)]),
    ).toEqual([]);
  });

  it("never treats a missing hash as a different config", () => {
    // null = 没记录(该列上线前的历史 run,或 run 在构建成功前就结束了),
    // 不是「用了另一套配置」。把它当成一个值去比,会在全部历史数据上凭空报出
    // 一堆并不存在的变更。
    expect(
      configChangePoints([
        run("2026-08-29T10:00:00Z", null),
        run("2026-08-29T10:01:00Z", A),
        run("2026-08-29T10:02:00Z", undefined),
        run("2026-08-29T10:03:00Z", A),
      ]),
    ).toEqual([]);
  });

  it("compares in chronological order, not arrival order", () => {
    // 后端目前是按时间返回的,但这个判断的正确性不该押在那上面 —— 顺序反了
    // 会把「A→B」读成「B→A」,轮次号也就跟着错位。
    expect(
      configChangePoints([
        run("2026-08-29T10:02:00Z", B),
        run("2026-08-29T10:00:00Z", A),
        run("2026-08-29T10:01:00Z", A),
      ]),
    ).toEqual([2]);
  });

  it("has nothing to say about a single-turn or empty conversation", () => {
    expect(configChangePoints([])).toEqual([]);
    expect(configChangePoints([run("2026-08-29T10:00:00Z", A)])).toEqual([]);
  });
});
