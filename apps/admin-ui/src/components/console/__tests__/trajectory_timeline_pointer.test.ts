/**
 * trajectory_timeline_pointer 测试 —— `tooltipLines` 纯函数(Task 11:新增
 * 「首 token」提示行)。手势状态机 / 域投影 / 命中反查等其余部件已经由
 * `TrajectoryTimeline.test.tsx` 走渲染 + 交互间接覆盖(见任务 7/9),本文件只
 * 补这条新分支 —— 直接调纯函数,不用挂载整棵组件树。
 */
import { beforeAll, describe, expect, it } from "vitest";
import i18n from "../../../i18n";

import type { TimelineModel, TimelineSpan } from "../ledger_timeline";
import type { LedgerRecord } from "../ledger_types";
import { tooltipLines } from "../trajectory_timeline_pointer";

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN");
});

const t = i18n.t.bind(i18n) as (key: string, vars?: Record<string, string>) => string;

function span(over: Partial<TimelineSpan> = {}): TimelineSpan {
  return { index: 0, lane: 1, kind: "assistant", isError: false, running: false, start: 0, end: 1, ttft: null, ...over };
}

function model(over: Partial<TimelineModel> = {}): TimelineModel {
  return { start: 0, end: 1, mode: "sequence", spans: [], turnBoundaries: [], degraded: false, ...over };
}

function record(startedAt: number | null, endedAt: number | null, firstTokenAt: number | null): LedgerRecord {
  return {
    id: "t/assistant:0", index: 0, turnKey: "t", turnSeq: 0, runId: null,
    turnStart: false, turnEnd: false, requestNo: null, ownerRequestNo: null, parentId: null,
    kind: "assistant", lane: 1, isError: false, running: false, startedAt, endedAt, firstTokenAt,
    text: "", resultText: null, row: {} as LedgerRecord["row"], events: [], placeholder: null,
  };
}

describe("tooltipLines · 首 token 行", () => {
  it("assistant 记录有首 token 时多一行「首 token 1.2s」", () => {
    const rec = record(1000, 1600, 2200); // firstTokenAt = startedAt + 1200
    const lines = tooltipLines(span(), rec, model(), t);
    expect(lines).toContain("首 token 1.2s");
  });

  // 评审 Important:startedAt=1000 时 `null >= 1000` 本来就是 false,摘掉实现
  // 里的 `firstTokenAt !== null` 判断照样绿,测不出这道门。startedAt=0(duration
  // 模式下合法的绝对时刻)让 `null >= 0` 为 true,漏了空值判断就会多算出一行
  // 「首 token 0ms」,断言才能真正逮住它。
  it("firstTokenAt 为 null 时不出该行(startedAt=0,故意撞 null >= 0 的坑)", () => {
    const rec = record(0, 600, null);
    const lines = tooltipLines(span(), rec, model(), t);
    expect(lines.some((line) => line.startsWith("首 token"))).toBe(false);
  });
});
