/**
 * ``streamRunEvents`` 游标翻页 —— P3 PR-1 Task 6。
 *
 * 回放超过一页时后端以 ``truncated`` 帧收尾并且**不发 ``end``**(契约实况表 H:
 * 501 帧、``X-Expert-Work-Next-Seq = 499``、"有 end 帧吗:False")。SDK 必须带
 * ``next_seq`` 再拉一页,直到收到 ``end`` —— 否则调用方停在第一页,还以为流正常
 * 结束了(几处 ``frames.at(-1)?.event === "end"`` 会走进降级分支)。
 *
 * 游标口径见实况表规则 1:**到达顺序不等于 seq 顺序**,游标要取"见过的最大 seq",
 * 不是最后一帧的 seq。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { streamRunEvents } from "../runs";
import type { SseEvent } from "../sessions";

interface FrameSpec {
  /** 省略 = 这帧没有 ``id:`` 行(``end`` / ``truncated`` / ``gap`` / ``token``)。 */
  id?: string;
  event: string;
  data: unknown;
}

const MS = 1_700_000_000_000;

function upd(seq: number): FrameSpec {
  return { id: `${MS}-${seq}`, event: "updates", data: { n: seq } };
}
const END: FrameSpec = { event: "end", data: { status: "success", run_id: "r1" } };
function truncated(nextSeq: number | null): FrameSpec {
  return { event: "truncated", data: nextSeq === null ? {} : { next_seq: nextSeq } };
}

function sseText(frames: readonly FrameSpec[]): string {
  return frames
    .map(
      (f) =>
        `${f.id === undefined ? "" : `id: ${f.id}\n`}event: ${f.event}\n` +
        `data: ${JSON.stringify(f.data)}\n\n`,
    )
    .join("");
}

/** One HTTP response carrying one page of SSE frames. */
function page(frames: readonly FrameSpec[], headers: Record<string, string> = {}) {
  const encoder = new TextEncoder();
  return {
    ok: true,
    headers: new Headers(headers),
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(sseText(frames)));
        controller.close();
      },
    }),
  };
}

async function collect(threadId = "t1", runId = "r1"): Promise<SseEvent[]> {
  const out: SseEvent[] = [];
  for await (const frame of streamRunEvents(threadId, runId)) out.push(frame);
  return out;
}

function urlsOf(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map((c) => String(c[0]));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("streamRunEvents — 回放截断后的游标翻页", () => {
  it("单页走到 end 就停 —— 只发一次请求", async () => {
    const fetchMock = vi.fn().mockResolvedValue(page([upd(0), upd(1), END]));
    vi.stubGlobal("fetch", fetchMock);

    const frames = await collect();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(frames.map((f) => f.event)).toEqual(["updates", "updates", "end"]);
  });

  it("truncated 收尾就带 next_seq 再拉一页,两页帧全部到手", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(page([upd(0), upd(1), upd(2), truncated(2)]))
      .mockResolvedValueOnce(page([upd(3), upd(4), END]));
    vi.stubGlobal("fetch", fetchMock);

    const frames = await collect();

    expect(urlsOf(fetchMock)).toEqual([
      "/v1/sessions/t1/runs/r1/events",
      "/v1/sessions/t1/runs/r1/events?since_seq=2",
    ]);
    // truncated 是传输层控制帧,续拉成功就不外泄给调用方。
    expect(frames.map((f) => f.event)).toEqual([
      "updates",
      "updates",
      "updates",
      "updates",
      "updates",
      "end",
    ]);
    expect(frames.map((f) => (f.data as { n?: number }).n)).toEqual([
      0,
      1,
      2,
      3,
      4,
      undefined,
    ]);
  });

  it("游标取见过的最大 seq,不是最后一帧的 seq(实况表 B 的乱序到达)", async () => {
    // 实况表 B:客户端实际收到 0,1,2,5,6,3,4 —— 补发的洞帧排在更大 seq 之后。
    // 用最后一帧的 seq(4)当游标,重连会把 5、6 再收一遍。
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        page([upd(0), upd(1), upd(2), upd(5), upd(6), upd(3), upd(4), truncated(4)]),
      )
      .mockResolvedValueOnce(page([upd(7), END]));
    vi.stubGlobal("fetch", fetchMock);

    await collect();

    expect(urlsOf(fetchMock)[1]).toBe("/v1/sessions/t1/runs/r1/events?since_seq=6");
  });

  it("乱序到达的补发帧一帧不丢 —— 游标不是去重判据", async () => {
    // 实况表 B:0,1,2,5,6,3,4,7。3 和 4 是落库空洞被 bridge 补发的**真帧**,
    // 到它们的时候"见过的最大 seq"已经是 6 了。把游标当去重判据
    // (``seq <= maxSeen → drop``)会把它们静默扔掉 —— 正是这个 PR 要消灭的
    // 那类静默丢数据,只不过换到了前端。游标(重连用最大 seq)与去重
    // (若要做,必须是已处理 seq 的精确集合)是两件事,不能合成一个数。
    const arrival = [0, 1, 2, 5, 6, 3, 4, 7];
    const fetchMock = vi.fn().mockResolvedValue(page([...arrival.map(upd), END]));
    vi.stubGlobal("fetch", fetchMock);

    const frames = await collect();

    expect(frames.filter((f) => f.event === "updates")).toHaveLength(arrival.length);
    expect(
      frames.filter((f) => f.event === "updates").map((f) => (f.data as { n: number }).n),
    ).toEqual(arrival);
  });

  it("帧里没有 next_seq 时回落到 X-Expert-Work-Next-Seq 头", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        page([{ event: "truncated", data: {} }], { "X-Expert-Work-Next-Seq": "42" }),
      )
      .mockResolvedValueOnce(page([END]));
    vi.stubGlobal("fetch", fetchMock);

    await collect();

    expect(urlsOf(fetchMock)[1]).toBe("/v1/sessions/t1/runs/r1/events?since_seq=42");
  });

  it("翻页有上限 —— 不会无限拉,超限打日志并把 truncated 吐给调用方", async () => {
    let seq = 0;
    const fetchMock = vi.fn().mockImplementation(() => {
      seq += 1;
      return Promise.resolve(page([upd(seq), truncated(seq)]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const frames = await collect();

    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(20);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
    expect(frames.at(-1)?.event).toBe("truncated");
    expect(warn).toHaveBeenCalled();
  });

  it("游标不前进就停 —— 服务端重复同一个 next_seq 不会转成死循环", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(page([upd(7), truncated(7)])),
    );
    vi.stubGlobal("fetch", fetchMock);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const frames = await collect();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(frames.at(-1)?.event).toBe("truncated");
    expect(warn).toHaveBeenCalled();
  });

  it("翻页请求把 tenant_id 一起带上(切入态读透传不能只在第一页生效)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(page([upd(0), truncated(0)]))
      .mockResolvedValueOnce(page([END]));
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of streamRunEvents("t1", "r1", { tenantScope: "ten-9" })) {
      // drain
    }

    expect(urlsOf(fetchMock)).toEqual([
      "/v1/sessions/t1/runs/r1/events?tenant_id=ten-9",
      "/v1/sessions/t1/runs/r1/events?since_seq=0&tenant_id=ten-9",
    ]);
  });
});
