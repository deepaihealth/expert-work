/**
 * token 帧不带 ``id:`` —— P3 PR-1 Task 6 Step 1 的实证钉子。
 *
 * 后端这一轮起,token 帧不占落库 seq、也不发 ``id:`` 行(契约实况表 D:
 * "live 里的 token 帧:无 id:,不占号,不参与去重")。计划要求**实证**(不是查
 * 记忆)token 帧会不会走到 ``sse_id.serverMsOf``,以及 ``timeline`` /
 * ``tool_timeline`` / ``worker_timeline`` / ``gantt_timeline`` 这四条路径。
 *
 * 实证结论(本文件就是证据,不是转述):
 *
 *   1. **会**走到 ``serverMsOf`` —— ``parseTimeline`` 在按帧类型分派**之前**就对
 *      每一帧调一次(``timeline.ts`` 的循环开头),token 帧也不例外;传进去的正是
 *      ``null``,返回 ``null``,落在早就存在的「id 缺失/畸形」降级路径上。
 *   2. ``tool_timeline`` / ``worker_timeline`` 的 ``serverMsOf`` 调用都在
 *      ``updates`` / ``worker`` 帧的过滤之后,token 帧**够不着**。
 *   3. token 帧不产生任何时间线条目、工具条目、worker 条目或 Gantt 行,
 *      因此也掀不翻 ``GanttModel.degraded``(只有**行**的 id 缺失才算降级)。
 *
 * 也就是说:token 帧丢掉 ``id:`` 对这四条路径是无害的 —— 但这个结论必须由测试
 * 钉住,而不是靠"记忆里说 token 帧不进 turn.events"(那是 2026-07 的结论,而且
 * 只对 PlaygroundTab 成立:EventStreamPanel / useHistoryTurns / ConversationDetail
 * 都把 token 帧原样收进 events)。
 */
import { describe, expect, it, vi } from "vitest";

// `vi.mock` 的工厂被提升到 import 之前,所以记录数组必须走 `vi.hoisted`,
// 否则工厂执行时它还在 TDZ 里。
const { serverMsCalls } = vi.hoisted(() => ({
  serverMsCalls: [] as Array<string | null>,
}));

vi.mock("../sse_id", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../sse_id")>();
  return {
    ...actual,
    serverMsOf: (id: string | null) => {
      serverMsCalls.push(id);
      return actual.serverMsOf(id);
    },
  };
});

import { buildGanttRows } from "../gantt_timeline";
import type { SseEvent } from "../sessions";
import { parseTimeline } from "../timeline";
import { parseToolCalls } from "../tool_timeline";
import { parseWorkerFrames } from "../worker_timeline";

const MS = 1_700_000_000_000;

function frame(
  event: string,
  data: unknown,
  id: string | null,
  receivedAt = "2026-08-14T00:00:00.000Z",
): SseEvent {
  return { id, event, data, rawData: "", receivedAt };
}

/** 一个带 id 的 agent ``updates`` 帧 —— 有 id 的对照组。 */
function agentUpdates(seq: number, stepCount: number): SseEvent {
  return frame(
    "updates",
    {
      agent: {
        step_count: stepCount,
        _duration_ms: 100,
        messages: [
          {
            type: "ai",
            content: "答案",
            response_metadata: { model_name: "glm-5.2", finish_reason: "stop" },
            usage_metadata: { input_tokens: 10, output_tokens: 2, total_tokens: 12 },
            tool_calls: [],
          },
        ],
      },
    },
    `${MS + seq}-${seq}`,
  );
}

/** 契约实况表 D 里那种 token 帧:无 ``id:``。 */
function tokenFrame(text: string): SseEvent {
  return frame("token", { channel: "content", text }, null);
}

describe("token 帧无 id: —— 四条 timeline 路径的实证", () => {
  it("parseTimeline 对每一帧都调 serverMsOf,token 帧传进去的是 null", () => {
    serverMsCalls.length = 0;

    parseTimeline([agentUpdates(0, 1), tokenFrame("hi"), agentUpdates(1, 2)]);

    // 三帧三次 —— token 帧没有被跳过,它确实走到了 id 解析这一步。
    expect(serverMsCalls).toEqual([`${MS}-0`, null, `${MS + 1}-1`]);
  });

  it("token 帧不产生任何时间线条目,也不污染相邻条目的 serverMs", () => {
    const items = parseTimeline([agentUpdates(0, 1), tokenFrame("hi"), agentUpdates(1, 2)]);

    expect(items).toHaveLength(2);
    expect(items.every((i) => i.kind === "agent")).toBe(true);
    expect(items.map((i) => i.serverMs)).toEqual([MS, MS + 1]);
  });

  it("token 帧进不了工具 / worker 解析(它们的 serverMsOf 在帧类型过滤之后)", () => {
    const events = [agentUpdates(0, 1), tokenFrame("hi")];

    expect(parseToolCalls(events)).toHaveLength(0);
    expect(parseWorkerFrames(events).size).toBe(0);
  });

  it("token 帧不产生 Gantt 行,也掀不翻 degraded", () => {
    const withToken = buildGanttRows([agentUpdates(0, 1), tokenFrame("hi"), agentUpdates(1, 2)]);
    const withoutToken = buildGanttRows([agentUpdates(0, 1), agentUpdates(1, 2)]);

    expect(withToken.degraded).toBe(false);
    expect(withToken.rows).toHaveLength(withoutToken.rows.length);
  });
});
