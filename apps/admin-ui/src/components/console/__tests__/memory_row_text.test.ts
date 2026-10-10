/** B-168 B2 —— 记忆行的文案:召回 / inline 写回 / 后台写回(已排队 / 进行中 / 完成 / 失败)。 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import i18n from "../../../i18n";
import type { MemoryRow } from "../../../api/trajectory_rows";
import { memoryRowText } from "../memory_row_text";

let priorLang: string;
beforeAll(async () => {
  priorLang = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(priorLang);
});

function row(over: Partial<MemoryRow>): MemoryRow {
  return {
    id: "memory:0", kind: "memory", seq: 0, step: null, status: "ok", durationMs: null,
    eventIndexes: [], serverMs: null, direction: "writeback", count: 0, detail: {},
    ...over,
  };
}

const t = i18n.t.bind(i18n);

describe("memoryRowText", () => {
  it("recall and inline writeback keep their existing text", () => {
    expect(memoryRowText(row({ direction: "recall", count: 3 }), t)).toBe("记忆召回 · 3 条");
    expect(memoryRowText(row({ count: 2 }), t)).toBe("记忆写回 · 2 条");
  });

  it("a queued writeback without a job result reads as queued in the background", () => {
    expect(memoryRowText(row({ detail: { queued: true } }), t)).toBe("后台记忆写回 · 已排队");
  });

  it("a finished background job shows count, queue time and run time", () => {
    const r = row({
      detail: { queued: true },
      background: { status: "done", written_count: 2, failed: false, queued_ms: 800, exec_ms: 4100 },
    });
    expect(memoryRowText(r, t)).toBe("后台记忆写回 · 2 条 · 排队 800ms · 执行 4.1s");
  });

  it("a background job that wrote nothing because of an error says so", () => {
    const r = row({
      detail: { queued: true },
      background: { status: "done", written_count: 0, failed: true, queued_ms: 500, exec_ms: 1200 },
    });
    expect(memoryRowText(r, t)).toBe("后台记忆写回 · 未写成 · 排队 500ms · 执行 1.2s");
  });

  it("pending / running / failed jobs show their status", () => {
    const base = { written_count: null, failed: null, queued_ms: null, exec_ms: null };
    const text = (status: "pending" | "running" | "failed") =>
      memoryRowText(row({ detail: { queued: true }, background: { ...base, status } }), t);
    expect(text("pending")).toBe("后台记忆写回 · 排队中");
    expect(text("running")).toBe("后台记忆写回 · 执行中");
    expect(text("failed")).toBe("后台记忆写回 · 失败");
  });
});
