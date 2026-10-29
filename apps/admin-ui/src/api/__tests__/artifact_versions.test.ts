/** B-235 —— 产物按版本下载:每一轮登记的版本号要一路带到下载请求上。
 *
 *  事故形状:同一用户两段会话用同一个智能体、存同一个名字;只按名字下载拿到的是最新那一版,
 *  也就是另一段会话的产出。版本号来自 end 帧的产物清单,或 save_artifact 工具结果里的
 *  ``artifact.version``(没有 end 帧的老回放)。 */
import type { InternalAxiosRequestConfig } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadArtifact } from "../artifacts";
import { apiClient } from "../client";
import { downloadSessionArtifact, type SseEvent } from "../sessions";
import { turnArtifacts } from "../tool_timeline";

function capture(): Array<Record<string, unknown> | undefined> {
  const calls: Array<Record<string, unknown> | undefined> = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push(config.params as Record<string, unknown> | undefined);
    return Promise.resolve({
      data: new Blob(["x"]),
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() });
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("download SDKs carry the version", () => {
  it("downloadSessionArtifact sends version when given and omits it otherwise", async () => {
    const calls = capture();
    await downloadSessionArtifact("t-1", "report.txt", undefined, 2);
    await downloadSessionArtifact("t-1", "report.txt");
    expect(calls[0]).toMatchObject({ version: 2 });
    expect(calls[1] ?? {}).not.toHaveProperty("version");
  });

  it("downloadArtifact sends version when given and omits it otherwise", async () => {
    const calls = capture();
    await downloadArtifact("art-1", "report.txt", "user-1", undefined, 3);
    await downloadArtifact("art-1", "report.txt", "user-1");
    expect(calls[0]).toMatchObject({ artifact_id: "art-1", version: 3 });
    expect(calls[1]).not.toHaveProperty("version");
  });
});

function updates(node: string, messages: unknown[]): SseEvent {
  return { id: null, event: "updates", data: { [node]: { messages } }, rawData: "", receivedAt: "" };
}

describe("turnArtifacts carries each artifact's version", () => {
  it("takes the version from the save_artifact result when there is no end frame", () => {
    const events = [
      updates("agent", [
        {
          type: "ai",
          content: "",
          tool_calls: [{ id: "c1", name: "save_artifact", args: { name: "report.txt" }, type: "tool_call" }],
        },
      ]),
      updates("tools", [
        {
          type: "tool",
          tool_call_id: "c1",
          name: null,
          content: "Saved",
          status: "success",
          artifact: { artifact: "report.txt", version: 4, kind: "document" },
        },
      ]),
    ];
    expect(turnArtifacts(events)).toEqual([{ name: "report.txt", kind: "other", version: 4 }]);
  });

  it("takes the version from the end frame's manifest", () => {
    const events: SseEvent[] = [
      {
        id: "e",
        event: "end",
        data: { status: "success", artifacts: [{ name: "report.txt", kind: "document", version: 7 }] },
        rawData: "",
        receivedAt: "",
      },
    ];
    expect(turnArtifacts(events)).toEqual([{ name: "report.txt", kind: "document", version: 7 }]);
  });

  it("ignores a version-looking key on other tools' results", () => {
    const events = [
      updates("agent", [
        {
          type: "ai",
          content: "",
          tool_calls: [{ id: "c1", name: "save_artifact", args: { name: "a.txt" }, type: "tool_call" }],
        },
      ]),
      updates("tools", [
        { type: "tool", tool_call_id: "c1", name: null, content: "Saved", status: "success" },
      ]),
    ];
    expect(turnArtifacts(events)).toEqual([{ name: "a.txt", kind: "other" }]);
  });
});
