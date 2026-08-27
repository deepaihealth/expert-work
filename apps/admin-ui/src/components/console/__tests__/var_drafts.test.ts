/**
 * var_drafts — 变量草稿记忆的纯函数层(调试台侧栏重设计,规格 C)。
 *
 * 关键不变式:预填**仅对空字段**生效 —— 已有值绝不被草稿覆盖;保存是
 * 合并语义 —— 本次没提交的变量保留旧草稿。
 */
import { beforeEach, describe, expect, it } from "vitest";

import { prefillEmptyValues, readVarDrafts, saveVarDrafts } from "../var_drafts";

const KEY = "expert_work.console.varDrafts.demo-agent";

beforeEach(() => {
  window.localStorage.clear();
});

describe("readVarDrafts / saveVarDrafts", () => {
  it("round-trips submitted values per agent", () => {
    saveVarDrafts("demo-agent", { persona: "顾问", city: "杭州" });
    expect(readVarDrafts("demo-agent")).toEqual({ persona: "顾问", city: "杭州" });
    // 别的 agent 互不串台。
    expect(readVarDrafts("other-agent")).toEqual({});
  });

  it("merges with earlier drafts instead of replacing them", () => {
    saveVarDrafts("demo-agent", { persona: "顾问", city: "杭州" });
    // 第二次提交只带 persona —— city 的旧草稿必须保留。
    saveVarDrafts("demo-agent", { persona: "医生" });
    expect(readVarDrafts("demo-agent")).toEqual({ persona: "医生", city: "杭州" });
  });

  it("does not write an entry for an empty submission", () => {
    saveVarDrafts("demo-agent", {});
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it("degrades corrupt / non-object storage to an empty table", () => {
    window.localStorage.setItem(KEY, "not-json{");
    expect(readVarDrafts("demo-agent")).toEqual({});
    window.localStorage.setItem(KEY, JSON.stringify(["a"]));
    expect(readVarDrafts("demo-agent")).toEqual({});
    // 非字符串值逐项丢弃,不拖垮整表。
    window.localStorage.setItem(KEY, JSON.stringify({ a: "x", b: 3 }));
    expect(readVarDrafts("demo-agent")).toEqual({ a: "x" });
  });
});

describe("prefillEmptyValues", () => {
  it("fills only empty fields from drafts", () => {
    const out = prefillEmptyValues(
      { persona: "", city: "北京" },
      { persona: "顾问", city: "杭州" },
      ["persona", "city"],
    );
    expect(out).toEqual({ persona: "顾问", city: "北京" });
  });

  it("never overwrites a non-empty value", () => {
    const out = prefillEmptyValues(
      { persona: "医生" },
      { persona: "顾问" },
      ["persona"],
    );
    expect(out).toEqual({ persona: "医生" });
  });

  it("only fills declared names (a removed variable's draft stays dead)", () => {
    const out = prefillEmptyValues({}, { ghost: "旧值", persona: "顾问" }, ["persona"]);
    expect(out).toEqual({ persona: "顾问" });
  });

  it("returns the original reference when nothing was filled", () => {
    const values = { persona: "医生" };
    expect(prefillEmptyValues(values, { persona: "顾问" }, ["persona"])).toBe(values);
    expect(prefillEmptyValues(values, {}, ["persona"])).toBe(values);
  });
});
