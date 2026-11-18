/**
 * B-318 —— 工具参数的可选值。与后端 ``orchestrator/tools/arg_bindings.py``
 * ``fixed_value_options`` 同一套规则、同一组用例
 * (``services/orchestrator/tests/test_arg_bindings.py``),改一边就要改另一边。
 */
import { describe, expect, it } from "vitest";

import { formatParamOptions, paramOptionsOf } from "../mcp_param_options";

const HANDOFF_SCHEMA = {
  type: "object",
  properties: {
    this_turn: {
      type: "string",
      oneOf: [
        { const: "reply", title: "本轮回复" },
        { const: "silent", title: "本轮不回" },
      ],
    },
    level: { type: "string", enum: ["brief", "full"] },
    mode: { anyOf: [{ type: "string", const: "a" }, { const: "b", title: "" }] },
    keyword: { type: "string" },
  },
};

describe("paramOptionsOf", () => {
  it("oneOf 的 const 带标题就是可选值", () => {
    expect(paramOptionsOf(HANDOFF_SCHEMA, "this_turn")).toEqual([
      { value: "reply", title: "本轮回复" },
      { value: "silent", title: "本轮不回" },
    ]);
  });

  it("字符串 enum 给出没有标题的可选值", () => {
    expect(paramOptionsOf(HANDOFF_SCHEMA, "level")).toEqual([
      { value: "brief", title: null },
      { value: "full", title: null },
    ]);
  });

  it("anyOf 的 const 也算;空标题当没有标题", () => {
    expect(paramOptionsOf(HANDOFF_SCHEMA, "mode")).toEqual([
      { value: "a", title: null },
      { value: "b", title: null },
    ]);
  });

  it("普通参数、不存在的参数没有可选值", () => {
    expect(paramOptionsOf(HANDOFF_SCHEMA, "keyword")).toBeNull();
    expect(paramOptionsOf(HANDOFF_SCHEMA, "nope")).toBeNull();
  });

  it("重复值按顺序只留一个", () => {
    expect(paramOptionsOf({ properties: { p: { enum: ["b", "a", "b"] } } }, "p")).toEqual([
      { value: "b", title: null },
      { value: "a", title: null },
    ]);
  });

  it("第一个出现的键说了算,形状不对就没有可选值(不退到下一个键去猜)", () => {
    expect(
      paramOptionsOf({ properties: { p: { enum: ["a", 1], oneOf: [{ const: "a" }] } } }, "p"),
    ).toBeNull();
  });

  it("白名单之外的形状都没有可选值", () => {
    const badProps: unknown[] = [
      { enum: [] },
      { enum: ["a", 1] },
      { enum: "a" },
      { enum: ["a", null] },
      { oneOf: [{ const: "a" }, { type: "null" }] },
      { oneOf: [{ const: 1 }] },
      { oneOf: ["a"] },
      { oneOf: [] },
      { anyOf: { const: "a" } },
      { const: "a" },
      ["a"],
      "a",
    ];
    for (const prop of badProps) {
      expect(paramOptionsOf({ properties: { p: prop } }, "p"), JSON.stringify(prop)).toBeNull();
    }
    expect(paramOptionsOf({ properties: ["p"] }, "p")).toBeNull();
    expect(paramOptionsOf({}, "p")).toBeNull();
    expect(paramOptionsOf(undefined, "p")).toBeNull();
  });
});

describe("formatParamOptions", () => {
  it("有标题写「标题 值」,没有标题只写值,用 / 隔开", () => {
    expect(formatParamOptions(paramOptionsOf(HANDOFF_SCHEMA, "this_turn") ?? [])).toBe(
      "本轮回复 reply / 本轮不回 silent",
    );
    expect(formatParamOptions(paramOptionsOf(HANDOFF_SCHEMA, "level") ?? [])).toBe(
      "brief / full",
    );
  });
});
