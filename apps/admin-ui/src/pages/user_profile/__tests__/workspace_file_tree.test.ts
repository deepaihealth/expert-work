/**
 * buildFileTree — 用户工作区文件表的目录树投影(2026-08-26 用户反馈:
 * 全路径平铺一行一条,同目录文件视觉上完全散开)。
 */
import { describe, expect, it } from "vitest";

import { buildFileTree } from "../WorkspacePane";

describe("buildFileTree", () => {
  it("nests by directory, dirs first, siblings sorted, dir size = subtree total", () => {
    const tree = buildFileTree([
      { path: "清风_20260826.json", size: 10 },
      { path: "qa/bbox.html", size: 100 },
      { path: "style/render_plan.py", size: 30 },
      { path: "qa/清风_20260826.pdf", size: 200 },
      { path: "清风_20260826.pptx", size: 20 },
    ]);

    // 顶层:目录在前(按名),根文件其后。
    expect(tree.map((n) => [n.name, n.isDir])).toEqual([
      ["qa", true],
      ["style", true],
      ["清风_20260826.json", false],
      ["清风_20260826.pptx", false],
    ]);
    const qa = tree[0];
    expect(qa.size).toBe(300); // 子树合计
    expect(qa.children!.map((n) => n.name)).toEqual(["bbox.html", "清风_20260826.pdf"]);
    // 文件节点保留完整 path(下载/删除用),key 唯一。
    expect(qa.children![0].path).toBe("qa/bbox.html");
    expect(qa.key).toBe("dir:qa");
  });

  it("handles multi-level nesting", () => {
    const tree = buildFileTree([{ path: "a/b/c.txt", size: 5 }]);
    expect(tree[0].name).toBe("a");
    expect(tree[0].children![0].name).toBe("b");
    expect(tree[0].children![0].size).toBe(5);
    expect(tree[0].children![0].children![0].path).toBe("a/b/c.txt");
  });

  it("a dir and a file with the same name at one level do not collide on key", () => {
    const tree = buildFileTree([
      { path: "report", size: 1 },
      { path: "report/inner.txt", size: 2 },
    ]);
    const keys = tree.map((n) => n.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
});
