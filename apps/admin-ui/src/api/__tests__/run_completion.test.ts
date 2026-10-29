/**
 * B-226 ⑥ — the "finished, not completed" label for an ``end`` frame.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import i18n from "../../i18n";
import type { SseEvent } from "../sessions";
import {
  lastEndData,
  notCompletedLabel,
  runCompletionOf,
  unresolvedFailuresOf,
  unresolvedFailuresText,
} from "../run_completion";

let priorLang: string;
beforeAll(async () => {
  priorLang = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(priorLang);
});

function end(data: Record<string, unknown>): Record<string, unknown> {
  return { status: "success", run_id: "r1", ...data };
}

describe("notCompletedLabel", () => {
  it.each([
    ["max_steps", "已结束，未完成（步数用完）"],
    ["token_budget", "已结束，未完成（token 预算用完）"],
    ["no_progress", "已结束，未完成（没有进展）"],
    ["approval_rejected", "已结束，未完成（审批被拒绝）"],
    // text_response + completed=false = a tool failure was never recovered.
    ["text_response", "已结束，未完成（有未解决的失败）"],
  ])("labels exit_reason %s", (reason, label) => {
    expect(notCompletedLabel(end({ completed: false, exit_reason: reason }))).toBe(label);
  });

  it("shows an exit_reason it does not know as the raw value", () => {
    expect(notCompletedLabel(end({ completed: false, exit_reason: "constructor" }))).toBe(
      "已结束，未完成（constructor）",
    );
  });

  it("drops the parenthesis when exit_reason is absent", () => {
    expect(notCompletedLabel(end({ completed: false }))).toBe("已结束，未完成");
  });

  it("is null when the run was done or has no record", () => {
    expect(notCompletedLabel(end({ completed: true, exit_reason: "text_response" }))).toBeNull();
    // Absent = no record (an older run), never "not done".
    expect(notCompletedLabel(end({}))).toBeNull();
    expect(notCompletedLabel(null)).toBeNull();
  });

  it("is null for a non-success status — paused has its own marker", () => {
    expect(
      notCompletedLabel({ status: "paused", completed: false, exit_reason: "approval_pending" }),
    ).toBeNull();
  });

  it("has English labels", async () => {
    await i18n.changeLanguage("en");
    try {
      expect(notCompletedLabel(end({ completed: false, exit_reason: "max_steps" }))).toBe(
        "Finished, not completed (step limit reached)",
      );
    } finally {
      await i18n.changeLanguage("zh-CN");
    }
  });
});

describe("B-226 ⑥ — status incomplete and unresolved failures", () => {
  it("labels an incomplete end frame (a platform limit stopped the run)", () => {
    expect(
      notCompletedLabel({ status: "incomplete", completed: false, exit_reason: "max_steps" }),
    ).toBe("已结束，未完成（步数用完）");
    // ``incomplete`` always means not done, even if ``completed`` is missing.
    expect(notCompletedLabel({ status: "incomplete", exit_reason: "token_budget" })).toBe(
      "已结束，未完成（token 预算用完）",
    );
  });

  it("reads the failures list defensively", () => {
    expect(
      unresolvedFailuresOf({
        unresolved_failures: [
          { tool: "bash", kind: "unknown" },
          { tool: "mcp__crm__update", kind: "blocked_by_policy" },
          "not-an-object",
          { tool: 3, kind: "unknown" },
        ],
      }),
    ).toEqual([
      { tool: "bash", kind: "unknown" },
      { tool: "mcp__crm__update", kind: "blocked_by_policy" },
    ]);
    expect(unresolvedFailuresOf({ unresolved_failures: null })).toEqual([]);
    expect(unresolvedFailuresOf(undefined)).toEqual([]);
  });

  it("formats the list per locale", async () => {
    const list = [
      { tool: "bash", kind: "unknown" },
      { tool: "mcp__crm__update", kind: "blocked_by_policy" },
    ];
    expect(unresolvedFailuresText(list)).toBe(
      "未解决的失败：bash（unknown）、mcp__crm__update（blocked_by_policy）",
    );
    expect(unresolvedFailuresText([])).toBeNull();
    await i18n.changeLanguage("en");
    try {
      expect(unresolvedFailuresText(list)).toBe(
        "Unresolved failures: bash (unknown), mcp__crm__update (blocked_by_policy)",
      );
    } finally {
      await i18n.changeLanguage("zh-CN");
    }
  });
});

describe("runCompletionOf", () => {
  it("carries the row's unresolved failures (null when absent)", () => {
    const failures = [{ tool: "bash", kind: "unknown" }];
    expect(
      runCompletionOf({ status: "success", completed: false, exit_reason: "text_response", unresolved_failures: failures })
        .unresolved_failures,
    ).toEqual(failures);
    expect(runCompletionOf({ status: "success" }).unresolved_failures).toBeNull();
  });

  it("adapts a run row so the end-frame label logic reads it", () => {
    const row = { status: "success", completed: false, exit_reason: "max_steps" };
    expect(runCompletionOf(row)).toEqual({ ...row, unresolved_failures: null });
    expect(notCompletedLabel(runCompletionOf(row))).toBe("已结束，未完成（步数用完）");
  });

  it("maps absent fields to null (no record), which never qualifies", () => {
    expect(runCompletionOf({ status: "success" })).toEqual({
      status: "success",
      completed: null,
      exit_reason: null,
      unresolved_failures: null,
    });
    expect(notCompletedLabel(runCompletionOf({ status: "success" }))).toBeNull();
  });

  it("keeps the run-row status, so a paused row does not qualify", () => {
    expect(
      notCompletedLabel(
        runCompletionOf({ status: "paused", completed: false, exit_reason: "approval_pending" }),
      ),
    ).toBeNull();
  });
});

describe("lastEndData", () => {
  function ev(event: string, data: unknown): SseEvent {
    return { id: null, event, data, rawData: "", receivedAt: "t" };
  }

  it("returns the data of the last end frame", () => {
    const events = [ev("end", { status: "error" }), ev("updates", {}), ev("end", { status: "success" })];
    expect(lastEndData(events)).toEqual({ status: "success" });
  });

  it("is undefined when the turn has no end frame yet", () => {
    expect(lastEndData([ev("updates", {})])).toBeUndefined();
  });
});
