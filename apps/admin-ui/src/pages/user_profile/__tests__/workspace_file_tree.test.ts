/**
 * buildFileTree — 用户工作区文件表的目录树投影(2026-08-26 用户反馈:
 * 全路径平铺一行一条,同目录文件视觉上完全散开)。
 */
import { describe, expect, it } from "vitest";

import { buildFileTree, groupTreeByAgent } from "../WorkspacePane";

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

describe("groupTreeByAgent", () => {
  const tree = (paths: readonly string[]) =>
    groupTreeByAgent(buildFileTree(paths.map((path) => ({ path, size: 1 }))));

  it("把 agents/<key>/ 提成顶层分组并标出它是 agent", () => {
    // 不提升的话树会多出 `agents` / `<key>` 两级,而 `plan-aaaaaaaa` 这种
    // 名字(业务名 + sha256 前 8 位)看起来只是个怪目录名 —— 没有任何东西
    // 告诉看的人这一级代表一个 agent。
    const out = tree([
      "agents/plan-aaaaaaaa/MEMORY.md",
      "agents/sop-bbbbbbbb/客户案例/秀域.md",
    ]);

    expect(out.map((n) => [n.name, n.groupKind])).toEqual([
      ["plan-aaaaaaaa", "agent"],
      ["sop-bbbbbbbb", "agent"],
    ]);
    expect(out[0].children?.map((n) => n.name)).toEqual(["MEMORY.md"]);
    // 提升之后不该再剩一个空的 `agents` 容器行。
    expect(out.some((n) => n.name === "agents")).toBe(false);
  });

  it("shared/ 单独一组并标明是归属不明的历史文件", () => {
    // `shared` 这个名字会被当成「共享目录」,而它实际是搬迁时**反推不出
    // 归属**的那批:冻结、只读、谁写的查不出来。那正是最该被标出来的一件事。
    const out = tree(["shared/style/PLAN_STYLE.md"]);

    expect(out.map((n) => [n.name, n.groupKind])).toEqual([["shared", "shared"]]);
  });

  it("agent 在前、shared 次之、搬迁前的扁平残留最后", () => {
    // 形状要能把分组排序与 buildFileTree 自己的排序**分开**。
    // buildFileTree 的根是「目录在前、同级按名排」,所以 agents / qa / shared
    // 三个目录出来就是这个字母序 —— 拿 plan-* 那种排在 shared 前面的 key 做
    // 用例,去掉 rank 排序照样过(实测零杀)。这里用 zz- 开头的 key + 一个
    // 夹在中间的普通目录 qa/,三者的字母序与目标顺序完全相反。
    const out = tree([
      "qa/bbox.html",
      "shared/MEMORY.md",
      "agents/zz-agent-cccccccc/PLAN.md",
      "未搬迁.md",
    ]);

    expect(out.map((n) => n.name)).toEqual([
      "zz-agent-cccccccc",
      "shared",
      "qa",
      "未搬迁.md",
    ]);
  });

  it("搬迁前的树原样通过 —— 没有 agents/ 时不该有任何变化", () => {
    // 搬迁跑完之前控制台照样要能用。这条钉住「分组是增量,不是前提」。
    const flat = buildFileTree([
      { path: "qa/bbox.html", size: 1 },
      { path: "报告.pptx", size: 2 },
    ]);

    expect(groupTreeByAgent(flat)).toEqual(flat);
  });

  it("名字叫 agents 的文件不当成分组容器", () => {
    // 只有**目录**才是容器。一个恰好叫 agents 的文件被提升的话,它的
    // children 是 undefined,整行会从表里消失。
    const out = tree(["agents"]);

    expect(out.map((n) => [n.name, n.isDir, n.groupKind])).toEqual([["agents", false, undefined]]);
  });
});
