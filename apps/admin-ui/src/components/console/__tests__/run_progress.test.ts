/** B-269 / B-270 —— 排队 / 运行中气泡的进度文案。 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import i18n from "../../../i18n";
import type { SseEvent } from "../../../api/sessions";
import { aheadStartedAtOf, progressLine, queuedText } from "../run_progress";

let priorLang: string;
beforeAll(async () => {
  priorLang = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(priorLang);
});

const t = i18n.t.bind(i18n);

function frame(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: JSON.stringify(data), receivedAt: "" };
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
