/**
 * B-127 —— 参数行的三种选择(自动 / 绑变量 / 固定值)的纯逻辑。
 */
import { describe, expect, it } from "vitest";

import type { ArgBindingFields } from "../../form_model";
import {
  FIXED,
  boundParamsOf,
  AUTO,
  choiceFromSelect,
  choiceOf,
  fixedValueValid,
  selectValueOf,
  setParamChoice,
} from "../mcp_arg_bindings";

describe("Select value <-> choice", () => {
  it("maps each choice to its Select value and back", () => {
    for (const choice of [
      { kind: "auto" },
      { kind: "variable", name: "project_code" },
    ] as const) {
      expect(choiceFromSelect(selectValueOf(choice), choice)).toEqual(choice);
    }
    expect(selectValueOf({ kind: "fixed", value: "brief" })).toBe(FIXED);
    expect(selectValueOf({ kind: "auto" })).toBe(AUTO);
  });

  it("picking fixed starts empty, re-picking it keeps the typed value", () => {
    expect(choiceFromSelect(FIXED, { kind: "variable", name: "pc" })).toEqual({
      kind: "fixed",
      value: "",
    });
    expect(choiceFromSelect(FIXED, { kind: "fixed", value: "brief" })).toEqual({
      kind: "fixed",
      value: "brief",
    });
  });
});

const B = (extra: Partial<ArgBindingFields>): ArgBindingFields => ({
  server: "records",
  tool: "fetch_record",
  ...extra,
});

describe("choiceOf", () => {
  const bindings = [B({ args: { code: "project_code" }, fixed: { detail_level: "brief" } })];
  it("reads each of the three choices", () => {
    expect(choiceOf(bindings, "records", "fetch_record", "code")).toEqual({
      kind: "variable",
      name: "project_code",
    });
    expect(choiceOf(bindings, "records", "fetch_record", "detail_level")).toEqual({
      kind: "fixed",
      value: "brief",
    });
    expect(choiceOf(bindings, "records", "fetch_record", "other")).toEqual({ kind: "auto" });
    expect(choiceOf(bindings, "records", "t9", "code")).toEqual({ kind: "auto" });
  });

  it("an empty fixed value is still the fixed choice (the operator is typing it)", () => {
    expect(choiceOf([B({ fixed: { detail_level: "" } })], "records", "fetch_record", "detail_level"))
      .toEqual({ kind: "fixed", value: "" });
  });
});

describe("setParamChoice", () => {
  it("a fixed value on an unbound tool creates a fixed-only binding (no empty args key)", () => {
    const out = setParamChoice([], "records", "fetch_record", "detail_level", {
      kind: "fixed",
      value: "brief",
    });
    expect(out).toEqual([B({ fixed: { detail_level: "brief" } })]);
    expect("args" in out[0]).toBe(false);
  });

  it("a variable-only binding keeps today's shape (no empty fixed key)", () => {
    const out = setParamChoice([], "records", "fetch_record", "code", {
      kind: "variable",
      name: "project_code",
    });
    expect(out).toEqual([B({ args: { code: "project_code" } })]);
    expect("fixed" in out[0]).toBe(false);
  });

  it("switching a parameter moves it between args and fixed, never both", () => {
    const start = [B({ args: { code: "project_code", detail_level: "lvl" } })];
    const out = setParamChoice(start, "records", "fetch_record", "detail_level", {
      kind: "fixed",
      value: "brief",
    });
    expect(out).toEqual([
      B({ args: { code: "project_code" }, fixed: { detail_level: "brief" } }),
    ]);
    const back = setParamChoice(out, "records", "fetch_record", "detail_level", {
      kind: "variable",
      name: "lvl",
    });
    expect(back).toEqual([B({ args: { code: "project_code", detail_level: "lvl" } })]);
  });

  it("auto on the last platform-owned parameter removes the whole binding", () => {
    const out = setParamChoice(
      [B({ fixed: { detail_level: "brief" } })],
      "records",
      "fetch_record",
      "detail_level",
      { kind: "auto" },
    );
    expect(out).toEqual([]);
  });

  it("leaves other tools' bindings and its input untouched", () => {
    const other = B({ tool: "t2", args: { code: "project_code" } });
    const start = [other, B({ fixed: { detail_level: "brief" } })];
    const snapshot = JSON.parse(JSON.stringify(start));
    const out = setParamChoice(start, "records", "fetch_record", "detail_level", {
      kind: "fixed",
      value: "full",
    });
    expect(out).toContainEqual(other);
    expect(out).toContainEqual(B({ fixed: { detail_level: "full" } }));
    expect(start).toEqual(snapshot);
  });

  it("the fixed sentinel can never be a declared variable name", () => {
    // 变量名受 ``^[a-zA-Z_][a-zA-Z0-9_]*$`` 约束(PromptVariableSpec.name)。
    expect(/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(FIXED)).toBe(false);
    expect(FIXED).not.toBe("");
  });
});

describe("boundParamsOf", () => {
  it("lists variable-bound then fixed parameters, tolerating a missing args key", () => {
    expect(
      boundParamsOf(B({ args: { code: "project_code" }, fixed: { detail_level: "brief" } })),
    ).toEqual([
      { param: "code", kind: "variable", name: "project_code" },
      { param: "detail_level", kind: "fixed", value: "brief" },
    ]);
    expect(boundParamsOf(B({ fixed: { detail_level: "brief" } }))).toEqual([
      { param: "detail_level", kind: "fixed", value: "brief" },
    ]);
  });
});

describe("fixedValueValid", () => {
  it("rejects empty and leading/trailing whitespace, accepts inner spaces", () => {
    expect(fixedValueValid("brief")).toBe(true);
    expect(fixedValueValid("very brief")).toBe(true);
    for (const bad of ["", " ", "brief ", " brief", "\tbrief", "brief\n"]) {
      expect(fixedValueValid(bad)).toBe(false);
    }
  });
});
