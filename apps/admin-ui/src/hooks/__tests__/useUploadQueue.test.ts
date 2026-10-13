/**
 * useUploadQueue tests — B-175 L3-T1.
 *
 * The fake ``upload`` hands back a deferred promise per call so each test
 * controls exactly when an upload finishes; ``calls`` records the file,
 * the AbortSignal and the progress callback the hook passed in.
 */
import { describe, expect, it, vi } from "vitest";
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
    expect(result.current.summary).toEqual({
      queued: 0,
      uploading: 0,
      done: 0,
      failed: 4,
      skipped: 1,
      total: 5,
    });
  });

  it("clearDone removes done and skipped rows and keeps the rest", async () => {
    const { calls, upload } = makeUpload();
    const { result } = renderHook(() => useUploadQueue({ upload }));
    act(() => result.current.enqueue(["a.pdf", "b.exe", "c.pdf", "d.pdf", "e.pdf"].map((n) => pdf(n))));
    await settle(() => calls[0].d.resolve({ id: "doc-a" }));
    await settle(() => calls[1].d.reject(new Error("boom")));

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
});
