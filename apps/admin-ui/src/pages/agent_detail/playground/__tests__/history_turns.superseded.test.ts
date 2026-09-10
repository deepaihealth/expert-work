/**
 * P-1 —— ``buildHistoryTurns`` 把 run 行的「被取代」链接与消息上的墓碑标记
 * 投影到 ``HistoryTurn``。两条路径各测一次:按 run_id 分组的路径从 run 行取
 * ``supersededBy``、从该轮自己的消息取 ``tombstone``;顺序配对路径(老消息没有
 * run 戳)取不到归属,恒给 ``null`` / ``false``。
 */
import { describe, expect, it } from "vitest";

import { buildHistoryTurns } from "../history_turns";
import type { HistoryMessage } from "../../../../api/sessions";
import type { ThreadRunSummary } from "../../../../api/runs";

function run(over: Partial<ThreadRunSummary> & { runId: string }): ThreadRunSummary {
  return {
    status: "success",
    isResume: false,
    createdAt: "2026-09-10T00:00:00Z",
    finishedAt: null,
    error: null,
    tokens: null,
    supersededBy: null,
    regeneratedFrom: null,
    ...over,
  };
}

describe("buildHistoryTurns 被取代与墓碑标记", () => {
  it("① 按 run_id 分组:supersededBy 取自 run 行,tombstone 取自该轮的消息", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: "", run_id: "r1", superseded_by: "r2", tombstone: true },
      {
        role: "assistant",
        content: "",
        channel: "final",
        run_id: "r1",
        superseded_by: "r2",
        tombstone: true,
      },
      { role: "user", content: "U", run_id: "r2" },
      { role: "assistant", content: "A", channel: "final", run_id: "r2" },
    ];
    const runs = [
      run({ runId: "r1", supersededBy: "r2" }),
      run({ runId: "r2", isResume: true, regeneratedFrom: "r1" }),
    ];

    const turns = buildHistoryTurns(messages, runs);

    expect(turns?.map((t) => [t.runId, t.supersededBy, t.tombstone])).toEqual([
      ["r1", "r2", true],
      ["r2", null, false],
    ]);
  });

  it("② 同一轮里只要有一条消息是墓碑,这一轮就算墓碑", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: "U", run_id: "r1", superseded_by: "r2" },
      {
        role: "assistant",
        content: "",
        channel: "final",
        run_id: "r1",
        superseded_by: "r2",
        tombstone: true,
      },
    ];

    const turns = buildHistoryTurns(messages, [run({ runId: "r1", supersededBy: "r2" })]);

    expect(turns?.[0].tombstone).toBe(true);
  });

  it("③ 顺序配对路径(消息没有 run 戳):supersededBy 仍取自 run 行,tombstone 只能给 false", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: "U" },
      { role: "assistant", content: "A", channel: "final" },
    ];

    const turns = buildHistoryTurns(messages, [run({ runId: "r1", supersededBy: "r2" })]);

    expect(turns?.map((t) => [t.runId, t.supersededBy, t.tombstone])).toEqual([
      ["r1", "r2", false],
    ]);
  });
});
