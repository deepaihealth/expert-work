/** B-269 / B-270 —— 排队 / 运行中气泡的进度文案。 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import i18n from "../../../i18n";
import type { SseEvent } from "../../../api/sessions";
import {
  aheadStartedAtOf,
  progressFromEvents,
  progressLine,
  queuedRunIdOf,
  queuedText,
} from "../run_progress";

let priorLang: string;
beforeAll(async () => {
  priorLang = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(priorLang);
});

const t = i18n.t.bind(i18n);

function frame(event: string, data: unknown, id: string | null = null): SseEvent {
  return { id, event, data, rawData: JSON.stringify(data), receivedAt: "" };
}

const base = {
  elapsed_s: null,
  step: 0,
  tool_calls: 0,
  active_workers: 0,
  current: null,
  last_event_at: null,
};

describe("progressLine", () => {
  it("joins every non-zero part", () => {
    expect(
      progressLine({ ...base, elapsed_s: 754, step: 12, tool_calls: 30, active_workers: 2 }, t),
    ).toBe("已进行 12 分钟 · 第 12 步 · 调用工具 30 次 · 子任务 2 个在跑");
  });

  it("leaves out parts that are zero or unknown", () => {
    expect(progressLine({ ...base, elapsed_s: 30, step: 1 }, t)).toBe("已进行不到 1 分钟 · 第 1 步");
    expect(progressLine({ ...base, tool_calls: 3 }, t)).toBe("调用工具 3 次");
  });

  it("is null when there is nothing to say", () => {
    expect(progressLine(base, t)).toBeNull();
  });
});

describe("queuedText", () => {
  const now = Date.parse("2026-11-04T08:10:30Z");

  it("names how long the previous turn has been running", () => {
    expect(queuedText("2026-11-04T08:00:00Z", now, t)).toBe("排队中，上一轮已进行 10 分钟");
  });

  it("falls back to the plain queued text when the previous turn has not started", () => {
    expect(queuedText(null, now, t)).toBe("排队中，上一轮结束后开始");
  });
});

describe("aheadStartedAtOf", () => {
  it("reads the queued frame's ahead_started_at", () => {
    const events = [frame("queued", { ahead: 1, ahead_started_at: "2026-11-04T08:00:00Z" })];
    expect(aheadStartedAtOf(events)).toBe("2026-11-04T08:00:00Z");
  });

  it("is null without a queued frame or when the head has not started", () => {
    expect(aheadStartedAtOf([])).toBeNull();
    expect(aheadStartedAtOf([frame("queued", { ahead: 1, ahead_started_at: null })])).toBeNull();
  });
});

describe("queuedRunIdOf", () => {
  it("reads this turn's run id off the queued frame", () => {
    expect(queuedRunIdOf([frame("queued", { run_id: "r-9", ahead: 1 })])).toBe("r-9");
    expect(queuedRunIdOf([])).toBeNull();
  });
});

describe("progressFromEvents", () => {
  const started = Date.parse("2026-11-04T08:00:00Z");
  const tool = (name: string) => ({ type: "tool", name, content: "x" });

  it("counts steps, tool results and running subtasks the way the server does", () => {
    const events = [
      frame("metadata", { run_id: "r" }, `${started}-0`),
      frame("updates", { agent: { messages: [], step_count: 1 } }, `${started + 1}-1`),
      frame("updates", { tools: { messages: [tool("a"), { type: "human" }, tool("b")] } }),
      frame("worker", { worker_id: "w1", kind: "start" }),
      frame("worker", { worker_id: "w2", kind: "start" }),
      frame("worker", { worker_id: "w1", kind: "end" }),
      frame("updates", { agent: { messages: [], step_count: 2 } }),
    ];
    const p = progressFromEvents(events, started + 754_000);
    expect(p).not.toBeNull();
    expect([p?.elapsed_s, p?.step, p?.tool_calls, p?.active_workers]).toEqual([754, 2, 2, 1]);
  });

  it("forgets subtasks of an earlier attempt and ignores an end without a start", () => {
    const events = [
      frame("metadata", { run_id: "r" }, `${started}-0`),
      frame("worker", { worker_id: "wA", kind: "start" }),
      frame("metadata", { run_id: "r" }, `${started + 60_000}-2`),
      frame("worker", { worker_id: "wB", kind: "start" }),
      frame("worker", { worker_id: "wC", kind: "end" }),
    ];
    const p = progressFromEvents(events, started + 120_000);
    expect(p?.active_workers).toBe(1);
    // 已进行时间从第一次开始执行算起,接管不清零。
    expect(p?.elapsed_s).toBe(120);
  });

  it("is null before the run has started", () => {
    expect(progressFromEvents([frame("queued", { ahead: 1 })], started)).toBeNull();
  });
});
