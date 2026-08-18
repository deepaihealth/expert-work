import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { parseTimeline } from "../timeline";

function ev(event: string, data: unknown, receivedAt: string): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt };
}
function upd(node: string, channels: Record<string, unknown>, at: string): SseEvent {
  return ev("updates", { [node]: channels }, at);
}

describe("parseTimeline", () => {
  it("builds an agent step with reasoning, finish/model, and its tool", () => {
    const events = [
      upd("agent", {
        step_count: 1,
        messages: [{
          type: "ai", content: "",
          additional_kwargs: { reasoning_content: "先查天气" },
          response_metadata: { finish_reason: "tool_calls", model_name: "glm-5.2" },
          usage_metadata: { input_tokens: 100, output_tokens: 10, total_tokens: 110 },
          tool_calls: [{ id: "c1", name: "exec_python", args: { code: "print(1)" } }],
        }],
      }, "t1"),
      upd("tools", {
        messages: [{ type: "tool", tool_call_id: "c1", name: "exec_python", content: "stdout:\n1\n\nexit_code: 0", status: "success" }],
      }, "t2"),
    ];
    const items = parseTimeline(events);
    const step = items.find((i) => i.kind === "agent");
    expect(step).toBeDefined();
    if (step && step.kind === "agent") {
      expect(step.reasoning).toBe("先查天气");
      expect(step.finishReason).toBe("tool_calls");
      expect(step.model).toBe("glm-5.2");
      expect(step.totalTokens).toBe(110);
      expect(step.tools).toHaveLength(1);
      expect(step.tools[0].toolName).toBe("exec_python");
      expect(step.tools[0].status).toBe("success");
      expect(step.hasError).toBe(false);
    }
  });

  it("emits aux node items for memory_recall and a revise reflect (warn tone)", () => {
    const events = [
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "住嘉兴", importance: 0.7, confidence: 0.9 }] }, "t1"),
      upd("reflect", { reflections: [{ verdict: "revise", critique: "漏了夜间" }] }, "t2"),
    ];
    const items = parseTimeline(events);
    const mem = items.find((i) => i.kind === "memory_recall");
    const ref = items.find((i) => i.kind === "reflect");
    expect(mem).toBeDefined();
    expect(ref && ref.kind === "reflect" && ref.tone).toBe("warn");
  });

  it("emits markers for compaction / retry / end in order", () => {
    const events = [
      ev("compaction", { passes: 2, tokens_before: 46000, tokens_after: 22000, summary_chars: 800 }, "t1"),
      ev("retry", { attempt: 1, error_class: "TimeoutError", backoff_s: 2 }, "t2"),
      ev("end", {}, "t3"),
    ];
    const kinds = parseTimeline(events).map((i) => i.kind);
    expect(kinds).toEqual(["compaction", "retry", "end"]);
  });

  it("assigns increasing seq in arrival order across types", () => {
    const events = [
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }] }, "t1"),
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "hi" }] }, "t2"),
      ev("end", {}, "t3"),
    ];
    const seqs = parseTimeline(events).map((i) => i.seq);
    expect(seqs).toEqual([0, 1, 2]);
  });

  it("reads node _duration_ms into agent step and aux node (null when absent)", () => {
    const events = [
      upd("agent", {
        step_count: 1,
        _duration_ms: 1200,
        messages: [{ type: "ai", content: "hi" }],
      }, "t1"),
      upd("memory_recall", {
        _duration_ms: 300,
        recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }],
      }, "t2"),
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "no-dur" }] }, "t3"),
    ];
    const items = parseTimeline(events);
    const steps = items.filter((i) => i.kind === "agent");
    const mem = items.find((i) => i.kind === "memory_recall");
    expect(steps[0].kind === "agent" && steps[0].durationMs).toBe(1200);
    expect(steps[1].kind === "agent" && steps[1].durationMs).toBe(null);
    expect(mem && mem.kind === "memory_recall" && mem.durationMs).toBe(300);
  });

  it("attaches worker timelines to the matching tool call entry", () => {
    const events = [
      upd("agent", {
        step_count: 1,
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [{ id: "call-9", name: "spawn_worker", args: { task: "x" } }],
          },
        ],
      }, "t1"),
      ev("worker", {
        worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9",
        label: "spawn_worker", agent_ref: "dynamic:general", depth: 1, kind: "start", wseq: 0,
        data: { task_excerpt: "x", role: null, max_steps: 8 },
      }, "t2"),
      ev("worker", {
        worker_id: "w-9", parent_worker_id: null, parent_tool_call_id: "call-9",
        label: "spawn_worker", agent_ref: "dynamic:general", depth: 1, kind: "end", wseq: 1,
        data: { outcome: "success", iteration_used: 1, llm_call_count: 1, wall_clock_ms: 42 },
      }, "t3"),
    ];
    const items = parseTimeline(events);
    const agent = items.find((i) => i.kind === "agent");
    expect(agent?.kind).toBe("agent");
    const tool = agent?.kind === "agent" ? agent.tools.find((t) => t.id === "call-9") : undefined;
    expect(tool?.workers).toHaveLength(1);
    expect(tool?.workers?.[0].status).toBe("success");
  });

  it("worker events do not become standalone timeline items", () => {
    const items = parseTimeline([
      ev("worker", {
        worker_id: "w", parent_worker_id: null, parent_tool_call_id: "c",
        label: "spawn_worker", agent_ref: "d", depth: 1, kind: "start", wseq: 0,
        data: { task_excerpt: "", role: null, max_steps: 1 },
      }, "t1"),
    ]);
    expect(items).toHaveLength(0);
  });

  it("renders guard frames as markers", () => {
    const items = parseTimeline([
      ev("guard", { kind: "warning", guard: "token_budget", detail: { spent: 410000, limit: 500000 } }, "t1"),
      ev("guard", { kind: "tripped", guard: "max_steps", detail: { steps: 30, max: 30 } }, "t2"),
    ]);
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "guard", tone: "warn" });
    expect(items[0].kind === "guard" && items[0].text).toContain("82%");
    expect(items[1]).toMatchObject({ kind: "guard", tone: "bad" });
    expect(items[1].kind === "guard" && items[1].text).toContain("步数耗尽");
  });

  // ——— P3 PR-1 契约变更 ———

  it("renders a gap frame as a warn marker naming the missing range", () => {
    // ``gap``(只出现在 live 分支)= 这段帧在这条连接上补不到了。既不是错误,
    // 也不能当未知帧丢掉 —— 时间线上真的缺了一段。
    const items = parseTimeline([ev("gap", { from: 3, to: 7 }, "t1")]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "gap", tone: "warn" });
    expect(items[0].kind === "gap" && items[0].text).toContain("3");
    expect(items[0].kind === "gap" && items[0].text).toContain("7");
  });

  it("reads end.status — paused/interrupted/error are not 运行完成", () => {
    const cases: Array<[string, string, string]> = [
      ["success", "运行完成", "good"],
      ["paused", "等待人工审批", "pause"],
      ["interrupted", "运行已取消", "warn"],
      ["error", "运行失败", "bad"],
    ];
    for (const [status, text, tone] of cases) {
      const items = parseTimeline([ev("end", { status, run_id: "r1" }, "t1")]);
      expect(items[0]).toMatchObject({ kind: "end", text, tone });
    }
  });

  it("falls back to 运行完成 for a legacy end frame with null data", () => {
    const items = parseTimeline([ev("end", null, "t1")]);
    expect(items[0]).toMatchObject({ kind: "end", text: "运行完成", tone: "good" });
  });

  it("items carry the index of the frame they came from (two items from one updates frame share it)", () => {
    const items = parseTimeline([
      ev("compaction", { passes: 1, tokens_before: 1, tokens_after: 1, summary_chars: 0 }, "t1"),
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "r" } }], recalled_memories: [{ id: "m" }] }, "t2"),
    ]);
    expect(items.map((it) => it.eventIndex)).toEqual([0, 1, 1]);
  });
});
