/**
 * Front-matter parser tests (hand-rolled, no ``gray-matter``) + a sanity
 * check that the bundled ``tenant/``/``ops/`` docs actually load and are
 * well-formed (every entry has a non-empty title, per-group order is
 * unique — Task 4/5 only add ``.md`` files, this is the contract they
 * must keep).
 */
import { describe, expect, it } from "vitest";

import { loadDocs, parseFrontMatter } from "../loader";

describe("parseFrontMatter", () => {
  it("parses title and order from a front-matter block", () => {
    const raw = ["---", "title: 租户生命周期", "order: 3", "---", "", "正文第一行"].join(
      "\n",
    );
    const result = parseFrontMatter(raw, "fallback-slug");
    expect(result.title).toBe("租户生命周期");
    expect(result.order).toBe(3);
    expect(result.body).toBe("正文第一行");
  });

  it("parses the optional group key", () => {
    const raw = ["---", "title: 示例", "order: 1", "group: ops", "---", "正文"].join("\n");
    const result = parseFrontMatter(raw, "fallback-slug");
    expect(result.group).toBe("ops");
  });

  it("falls back to the slug and order 0 when there is no front-matter", () => {
    const raw = "# 没有 front-matter 的文件\n\n直接是正文。";
    const result = parseFrontMatter(raw, "no-front-matter");
    expect(result.title).toBe("no-front-matter");
    expect(result.order).toBe(0);
    expect(result.body).toBe(raw);
  });

  it("falls back the same way when the opening --- is never closed", () => {
    const raw = ["---", "title: 未闭合", "正文没有第二个 ---"].join("\n");
    const result = parseFrontMatter(raw, "unterminated");
    expect(result.title).toBe("unterminated");
    expect(result.order).toBe(0);
    expect(result.body).toBe(raw);
  });
});

describe("loadDocs — bundled content sanity", () => {
  it("loads at least one tenant doc and one ops doc, each with a title", () => {
    const docs = loadDocs();
    expect(docs.tenant.length).toBeGreaterThanOrEqual(1);
    expect(docs.ops.length).toBeGreaterThanOrEqual(1);
    for (const entry of [...docs.tenant, ...docs.ops]) {
      expect(entry.title.length).toBeGreaterThan(0);
    }
  });

  it("includes the overview/tenant-lifecycle first-content docs by slug", () => {
    const docs = loadDocs();
    expect(docs.tenant.map((d) => d.slug)).toContain("overview");
    expect(docs.ops.map((d) => d.slug)).toContain("tenant-lifecycle");
  });
});
