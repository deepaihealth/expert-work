/**
 * useUploadQueue tests — B-175 L3-T1.
 *
 * The fake ``upload`` hands back a deferred promise per call so each test
 * controls exactly when an upload finishes; ``calls`` records the file,
 * the AbortSignal and the progress callback the hook passed in.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useUploadQueue, type UploadQueueItem } from "../useUploadQueue";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface Call {
  file: File;
  signal: AbortSignal;
  onProgress: (percent: number) => void;
  d: Deferred<{ id: string }>;
}

function makeUpload() {
  const calls: Call[] = [];
  const upload = vi.fn(
    (file: File, opts: { signal: AbortSignal; onProgress: (percent: number) => void }) => {
      const d = deferred<{ id: string }>();
      calls.push({ file, signal: opts.signal, onProgress: opts.onProgress, d });
      return d.promise;
    },
  );
  return { calls, upload };
}

function pdf(name: string, size = 3): File {
  const file = new File(["abc"], name, { type: "application/pdf" });
  if (size !== 3) Object.defineProperty(file, "size", { value: size });
  return file;
}

const MIB = 1024 * 1024;

function statuses(items: UploadQueueItem<unknown>[]): string[] {
  return items.map((i) => i.status);
}

async function settle(fn: () => void): Promise<void> {
  await act(async () => {
    fn();
    // let the upload promise's then/catch run
    await Promise.resolve();
  });
}

describe("useUploadQueue", () => {
  it("never has more than `concurrency` uploads in flight (default 3)", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    const files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"].map((n) => pdf(n));

    act(() => result.current.enqueue(files));

    expect(calls).toHaveLength(3);
    expect(statuses(result.current.items)).toEqual([
      "uploading",
      "uploading",
      "uploading",
      "queued",
      "queued",
    ]);
    expect(result.current.summary).toEqual({
      queued: 2,
      uploading: 3,
      done: 0,
      failed: 0,
      cancelled: 0,
      skipped: 0,
      total: 5,
    });
  });

  it("starts the next queued item as soon as one finishes, in enqueue order", async () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    const files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"].map((n) => pdf(n));
    act(() => result.current.enqueue(files));

    await settle(() => calls[1].d.resolve({ id: "doc-b" }));

    expect(calls).toHaveLength(4);
    expect(calls[3].file).toBe(files[3]);
    expect(result.current.items[1]).toMatchObject({
      status: "done",
      progress: 100,
      result: { id: "doc-b" },
    });
    expect(statuses(result.current.items)).toEqual([
      "uploading",
      "done",
      "uploading",
      "uploading",
      "queued",
    ]);

    await settle(() => calls[0].d.resolve({ id: "doc-a" }));
    expect(calls).toHaveLength(5);
    expect(calls[4].file).toBe(files[4]);
  });

  it("reports per-item progress from the onProgress callback", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue([pdf("a.pdf")]));

    act(() => calls[0].onProgress(42));

    expect(result.current.items[0].progress).toBe(42);
  });

  it("cancel(id) aborts the in-flight upload and leaves the others alone", async () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue(["a.pdf", "b.pdf", "c.pdf", "d.pdf"].map((n) => pdf(n))));
    const ids = result.current.items.map((i) => i.id);

    act(() => result.current.cancel(ids[0]));

    expect(calls[0].signal.aborted).toBe(true);
    expect(calls[1].signal.aborted).toBe(false);
    expect(calls[2].signal.aborted).toBe(false);
    expect(result.current.items[0]).toMatchObject({ status: "failed", cancelled: true });
    // the freed slot goes to the next queued item
    expect(calls).toHaveLength(4);
    expect(result.current.items[3].status).toBe("uploading");

    // the aborted request rejecting afterwards must not overwrite the row
    await settle(() => calls[0].d.reject(new Error("canceled")));
    expect(result.current.items[0]).toMatchObject({ status: "failed", cancelled: true });
  });

  it("cancel(id) on a queued item drops it from the queue without starting it", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue(["a.pdf", "b.pdf", "c.pdf", "d.pdf"].map((n) => pdf(n))));
    const queuedId = result.current.items[3].id;

    act(() => result.current.cancel(queuedId));

    expect(result.current.items[3]).toMatchObject({ status: "failed", cancelled: true });
    expect(calls).toHaveLength(3);
  });

  it("retry(id) re-runs only failed items", async () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    const files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"].map((n) => pdf(n));
    act(() => result.current.enqueue(files));
    const ids = result.current.items.map((i) => i.id);

    await settle(() => calls[0].d.reject(new Error("boom")));
    await settle(() => calls[1].d.resolve({ id: "doc-b" }));
    expect(result.current.items[0]).toMatchObject({ status: "failed", error: "boom" });
    expect(result.current.items[1].status).toBe("done");
    const callsBefore = calls.length; // a, b, c, d all started

    act(() => result.current.retry(ids[1])); // done → no-op
    act(() => result.current.retry(ids[2])); // uploading → no-op
    expect(calls).toHaveLength(callsBefore);

    act(() => result.current.retry(ids[0])); // failed → re-queued and started
    expect(calls).toHaveLength(callsBefore + 1);
    expect(calls[callsBefore].file).toBe(files[0]);
    expect(result.current.items[0]).toMatchObject({ status: "uploading", progress: 0 });
    expect(result.current.items[0].error).toBeUndefined();
  });

  it("retry waits for a free slot when the cap is reached", async () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue(["a.pdf", "b.pdf", "c.pdf", "d.pdf"].map((n) => pdf(n))));
    const ids = result.current.items.map((i) => i.id);

    await settle(() => calls[0].d.reject(new Error("boom")));
    expect(calls).toHaveLength(4); // d took the slot
    act(() => result.current.retry(ids[0]));
    expect(calls).toHaveLength(4);
    expect(result.current.items[0].status).toBe("queued");

    await settle(() => calls[1].d.resolve({ id: "doc-b" }));
    expect(calls).toHaveLength(5);
    expect(result.current.items[0].status).toBe("uploading");
  });

  it("skips unsupported and oversized files up front; skipped rows never take a slot", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    const files = [
      pdf("setup.exe"),
      pdf("huge.pdf", 60 * MIB),
      pdf("c.pdf"),
      pdf("d.pdf"),
      pdf("e.pdf"),
      pdf("f.pdf"),
    ];

    act(() => result.current.enqueue(files));

    expect(statuses(result.current.items)).toEqual([
      "skipped",
      "skipped",
      "uploading",
      "uploading",
      "uploading",
      "queued",
    ]);
    expect(result.current.items[0].skipReason).toBe("unsupported");
    expect(result.current.items[1].skipReason).toBe("too_large");
    expect(calls.map((c) => c.file.name)).toEqual(["c.pdf", "d.pdf", "e.pdf"]);
    expect(result.current.summary).toEqual({
      queued: 1,
      uploading: 3,
      done: 0,
      failed: 0,
      cancelled: 0,
      skipped: 2,
      total: 6,
    });
  });

  it("a file of exactly the limit is accepted; one byte more is skipped", () => {
    const { upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload, maxBytes: 10 }));

    act(() => result.current.enqueue([pdf("ok.pdf", 10), pdf("big.pdf", 11)]));

    expect(statuses(result.current.items)).toEqual(["uploading", "skipped"]);
  });

  it("relativePath comes from webkitRelativePath (folder picks) and falls back to the name", () => {
    const { upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    const nested = pdf("nested.pdf");
    Object.defineProperty(nested, "webkitRelativePath", { value: "folder/sub/nested.pdf" });

    act(() => result.current.enqueue([nested, pdf("flat.pdf")]));

    expect(result.current.items.map((i) => i.relativePath)).toEqual([
      "folder/sub/nested.pdf",
      "flat.pdf",
    ]);
    expect(result.current.items.map((i) => i.size)).toEqual([3, 3]);
  });

  it("cancelAll aborts every in-flight upload and drops the queue", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue(["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.exe"].map((n) => pdf(n))));

    act(() => result.current.cancelAll());

    expect(calls.every((c) => c.signal.aborted)).toBe(true);
    expect(calls).toHaveLength(3);
    expect(statuses(result.current.items)).toEqual([
      "failed",
      "failed",
      "failed",
      "failed",
      "skipped",
    ]);
    // cancelled rows are counted apart from real failures (review #9)
    expect(result.current.summary).toEqual({
      queued: 0,
      uploading: 0,
      done: 0,
      failed: 0,
      cancelled: 4,
      skipped: 1,
      total: 5,
    });
  });

  it("clearDone removes done, skipped and cancelled rows and keeps the rest", async () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() =>
      result.current.enqueue(
        ["a.pdf", "b.exe", "c.pdf", "d.pdf", "e.pdf", "f.pdf"].map((n) => pdf(n)),
      ),
    );
    await settle(() => calls[0].d.resolve({ id: "doc-a" }));
    await settle(() => calls[1].d.reject(new Error("boom")));
    // f.pdf is queued (4th real file, cap 3) → cancel pulls it from the queue
    act(() => result.current.cancel(result.current.items[5].id));
    expect(result.current.items[5]).toMatchObject({ status: "failed", cancelled: true });

    act(() => result.current.clearDone());

    expect(result.current.items.map((i) => [i.file.name, i.status])).toEqual([
      ["c.pdf", "failed"],
      ["d.pdf", "uploading"],
      ["e.pdf", "uploading"],
    ]);
  });

  it("onSettled fires once per batch, after the last upload finishes — not per file", async () => {
    const { calls, upload } = makeUpload();
    const onSettled = vi.fn();
    const { result } = renderHook(() => useUploadQueue({ upload, onSettled }));
    act(() => result.current.enqueue(["a.pdf", "b.pdf", "c.pdf", "d.pdf"].map((n) => pdf(n))));

    await settle(() => calls[0].d.resolve({ id: "a" }));
    await settle(() => calls[1].d.reject(new Error("boom")));
    await settle(() => calls[2].d.resolve({ id: "c" }));
    expect(onSettled).not.toHaveBeenCalled();

    await settle(() => calls[3].d.resolve({ id: "d" }));
    expect(onSettled).toHaveBeenCalledTimes(1);

    // a retry opens a new batch → settles again, once
    act(() => result.current.retry(result.current.items[1].id));
    await settle(() => calls[4].d.resolve({ id: "b" }));
    expect(onSettled).toHaveBeenCalledTimes(2);
  });

  it("onSettled does not fire for a batch where nothing was uploaded", () => {
    const { upload } = makeUpload();
    const onSettled = vi.fn();
    const { result } = renderHook(() => useUploadQueue({ upload, onSettled }));

    act(() => result.current.enqueue([pdf("x.exe"), pdf("y.bin")]));

    expect(onSettled).not.toHaveBeenCalled();
    expect(result.current.summary.skipped).toBe(2);
  });

  it("honours a custom concurrency", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload, concurrency: 1 }));
    act(() => result.current.enqueue(["a.pdf", "b.pdf"].map((n) => pdf(n))));
    expect(calls).toHaveLength(1);
  });

  // ── review fixes (PR #1802) ────────────────────────────────────────────

  describe("duplicate basenames (review #1 — the server upserts on (kb, filename))", () => {
    it("the second file with the same basename in a batch is skipped as duplicate_name", () => {
      const { calls, upload } = makeUpload();
      const { result } = renderHook(() => useUploadQueue({ upload }));
      const a = pdf("x.pdf");
      Object.defineProperty(a, "webkitRelativePath", { value: "a/x.pdf" });
      const b = pdf("x.pdf");
      Object.defineProperty(b, "webkitRelativePath", { value: "b/x.pdf" });

      act(() => result.current.enqueue([a, b]));

      expect(statuses(result.current.items)).toEqual(["uploading", "skipped"]);
      expect(result.current.items[1]).toMatchObject({
        relativePath: "b/x.pdf",
        skipReason: "duplicate_name",
      });
      expect(calls).toHaveLength(1);
      expect(result.current.summary.skipped).toBe(1);
    });

    it("a basename that already finished uploading goes through again (re-upload = update)", async () => {
      const { calls, upload } = makeUpload();
      const { result } = renderHook(() => useUploadQueue({ upload }));

      act(() => result.current.enqueue([pdf("faq.pdf")]));
      await settle(() => calls[0].d.resolve({ id: "doc-faq" }));
      act(() => result.current.enqueue([pdf("faq.pdf")]));

      expect(statuses(result.current.items)).toEqual(["done", "uploading"]);
      expect(calls.map((c) => c.file.name)).toEqual(["faq.pdf", "faq.pdf"]);
    });

    it("a basename still queued / uploading from an earlier batch is never started twice", () => {
      const { calls, upload } = makeUpload();
      const { result } = renderHook(() => useUploadQueue({ upload, concurrency: 1 }));
      act(() => result.current.enqueue([pdf("a.pdf"), pdf("b.pdf")]));

      act(() => result.current.enqueue([pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf")]));

      expect(statuses(result.current.items)).toEqual([
        "uploading",
        "queued",
        "skipped",
        "skipped",
        "queued",
      ]);
      expect(calls).toHaveLength(1);
    });

    it("the duplicate check only applies to files that would otherwise be queued", () => {
      const { upload } = makeUpload();
      const { result } = renderHook(() => useUploadQueue({ upload }));

      act(() => result.current.enqueue([pdf("x.exe"), pdf("x.exe"), pdf("y.pdf")]));

      expect(result.current.items.map((i) => i.skipReason)).toEqual([
        "unsupported",
        "unsupported",
        undefined,
      ]);
    });
  });

  describe("lifetime (review #2 — the queue must not outlive its tab)", () => {
    it("unmount aborts in-flight uploads and starts nothing afterwards", async () => {
      const { calls, upload } = makeUpload();
      const { result, unmount } = renderHook(() => useUploadQueue({ upload, concurrency: 1 }));
      act(() => result.current.enqueue([pdf("a.pdf"), pdf("b.pdf")]));
      expect(calls).toHaveLength(1);

      unmount();

      expect(calls[0].signal.aborted).toBe(true);
      await act(async () => {
        calls[0].d.resolve({ id: "doc-a" });
        await Promise.resolve();
      });
      expect(calls).toHaveLength(1);
    });

    it("an item keeps the `upload` it was enqueued with, also on retry", async () => {
      const first = makeUpload();
      const second = makeUpload();
      const { result, rerender } = renderHook(
        ({ upload }: { upload: typeof first.upload }) => useUploadQueue({ upload }),
        { initialProps: { upload: first.upload } },
      );
      act(() => result.current.enqueue([pdf("a.pdf")]));
      expect(first.calls).toHaveLength(1);

      // e.g. the page switched to another knowledge base
      rerender({ upload: second.upload });
      await settle(() => first.calls[0].d.reject(new Error("boom")));

      act(() => result.current.retry(result.current.items[0].id));
      expect(first.calls).toHaveLength(2);
      expect(second.calls).toHaveLength(0);

      act(() => result.current.enqueue([pdf("b.pdf")]));
      expect(second.calls).toHaveLength(1);
      expect(second.calls[0].file.name).toBe("b.pdf");
    });

    it("registers a beforeunload guard only while something is queued or uploading", async () => {
      const added = vi.spyOn(window, "addEventListener");
      const removed = vi.spyOn(window, "removeEventListener");
      const { calls, upload } = makeUpload();
      const { result } = renderHook(() => useUploadQueue({ upload }));
      const beforeunloadAdds = () => added.mock.calls.filter((c) => c[0] === "beforeunload").length;
      const beforeunloadRemoves = () =>
        removed.mock.calls.filter((c) => c[0] === "beforeunload").length;
      expect(beforeunloadAdds()).toBe(0);

      act(() => result.current.enqueue([pdf("a.pdf")]));
      expect(beforeunloadAdds()).toBe(1);
      const handler = added.mock.calls.find((c) => c[0] === "beforeunload")?.[1] as (
        e: Event,
      ) => void;
      const event = new Event("beforeunload", { cancelable: true });
      handler(event);
      expect(event.defaultPrevented).toBe(true);

      await settle(() => calls[0].d.resolve({ id: "doc-a" }));
      expect(beforeunloadRemoves()).toBe(1);
    });
  });

  describe("timers (review #5 — batches must settle; stalled uploads must fail)", () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    it("onBatchProgress fires at most once per interval, and only after a completion", async () => {
      vi.useFakeTimers();
      const { calls, upload } = makeUpload();
      const onBatchProgress = vi.fn();
      const onSettled = vi.fn();
      const { result } = renderHook(() =>
        useUploadQueue({
          upload,
          concurrency: 1,
          onBatchProgress,
          onSettled,
          batchProgressIntervalMs: 15_000,
        }),
      );
      act(() => result.current.enqueue(["a.pdf", "b.pdf", "c.pdf"].map((n) => pdf(n))));

      act(() => vi.advanceTimersByTime(60_000));
      expect(onBatchProgress).not.toHaveBeenCalled(); // nothing completed yet

      await settle(() => calls[0].d.resolve({ id: "a" }));
      expect(onBatchProgress).not.toHaveBeenCalled(); // not immediately
      act(() => vi.advanceTimersByTime(15_000));
      expect(onBatchProgress).toHaveBeenCalledTimes(1);

      act(() => vi.advanceTimersByTime(60_000));
      expect(onBatchProgress).toHaveBeenCalledTimes(1); // no completion → no tick

      await settle(() => calls[1].d.resolve({ id: "b" }));
      act(() => vi.advanceTimersByTime(15_000));
      expect(onBatchProgress).toHaveBeenCalledTimes(2);

      await settle(() => calls[2].d.resolve({ id: "c" }));
      expect(onSettled).toHaveBeenCalledTimes(1);
      act(() => vi.advanceTimersByTime(60_000));
      expect(onBatchProgress).toHaveBeenCalledTimes(2); // settle supersedes the tick
    });

    it("an upload with no progress event for `stallTimeoutMs` is aborted and marked failed", () => {
      vi.useFakeTimers();
      const { calls, upload } = makeUpload();
      const { result } = renderHook(() =>
        useUploadQueue({ upload, stallTimeoutMs: 120_000, stallMessage: "stalled!" }),
      );
      act(() => result.current.enqueue([pdf("a.pdf"), pdf("b.pdf")]));

      act(() => vi.advanceTimersByTime(100_000));
      act(() => calls[0].onProgress(10)); // a is alive → its clock restarts
      act(() => vi.advanceTimersByTime(100_000));
      expect(result.current.items[0].status).toBe("uploading");
      expect(result.current.items[1]).toMatchObject({ status: "failed", error: "stalled!" });
      expect(calls[1].signal.aborted).toBe(true);
      expect(calls[0].signal.aborted).toBe(false);

      act(() => vi.advanceTimersByTime(20_000));
      expect(result.current.items[0]).toMatchObject({ status: "failed", error: "stalled!" });
      expect(calls[0].signal.aborted).toBe(true);
    });
  });

  it("ignores progress callbacks that do not change the rounded percent (review #6)", () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue([pdf("a.pdf")]));

    act(() => calls[0].onProgress(42.4));
    const before = result.current.items;
    act(() => calls[0].onProgress(42));
    act(() => calls[0].onProgress(41.6));

    expect(result.current.items).toBe(before); // no re-render
    expect(result.current.items[0].progress).toBe(42);
  });

  it("a synchronous throw from `upload` fails the row and frees its slot (review #10)", async () => {
    const upload = vi
      .fn()
      .mockImplementationOnce(() => {
        throw new Error("sync boom");
      })
      .mockImplementation(() => new Promise(() => {}));
    const { result } = renderHook(() => useUploadQueue({ upload, concurrency: 1 }));

    await settle(() => result.current.enqueue([pdf("a.pdf"), pdf("b.pdf")]));

    expect(result.current.items[0]).toMatchObject({ status: "failed", error: "sync boom" });
    expect(result.current.items[1].status).toBe("uploading");
    expect(upload).toHaveBeenCalledTimes(2);
  });
});
