/**
 * Upload queue hook — B-175 L3-T1 (batch / folder import).
 *
 * A bounded-concurrency queue over a caller-supplied ``upload`` function.
 * Semantics:
 *
 * - ``enqueue`` classifies every file once: unsupported extension or over
 *   ``maxBytes`` → ``skipped`` (never takes a slot, never retried); the rest
 *   → ``queued`` in input order.
 * - At most ``concurrency`` items are ``uploading`` at a time; when one
 *   settles the first ``queued`` item in list order starts.
 * - ``cancel`` aborts the item's ``AbortSignal`` (in flight) or pulls it
 *   from the queue; either way the row becomes ``failed`` with
 *   ``cancelled: true`` so it stays visible and retryable.
 * - ``retry`` only acts on ``failed`` rows (re-queued at their original
 *   position); ``done`` / ``uploading`` / ``skipped`` / ``queued`` are no-ops.
 * - ``onSettled`` fires once per batch: the first time no item is queued or
 *   uploading after at least one upload started since the last settle. A
 *   batch where everything was skipped never settles (nothing changed on
 *   the server, so callers skip the refresh).
 *
 * The scheduler works off a ref mirror of the items so a cancel / retry /
 * enqueue issued in the same tick as a completion sees the latest state;
 * every update builds a new array (no in-place mutation).
 */
import { useCallback, useMemo, useRef, useState } from "react";

import { isSupportedDocument, KNOWLEDGE_DOCUMENT_MAX_BYTES } from "../api/knowledge";

export type UploadQueueStatus = "queued" | "uploading" | "done" | "failed" | "skipped";
export type UploadSkipReason = "unsupported" | "too_large";

export interface UploadQueueItem<T = unknown> {
  id: string;
  file: File;
  /** ``webkitRelativePath`` for folder picks / drops, else the file name. */
  relativePath: string;
  size: number;
  status: UploadQueueStatus;
  /** 0–100; 100 once ``done``. */
  progress: number;
  /** Set when ``status === "skipped"``. */
  skipReason?: UploadSkipReason;
  /** Human-readable failure (upload rejection message). */
  error?: string;
  /** True when the failure came from ``cancel`` / ``cancelAll``. */
  cancelled?: boolean;
  result?: T;
}

export interface UploadQueueSummary {
  queued: number;
  uploading: number;
  done: number;
  failed: number;
  skipped: number;
  total: number;
}

export interface UploadQueueInput {
  file: File;
  relativePath?: string;
}

export interface UseUploadQueueOptions<T> {
  upload: (
    file: File,
    opts: { signal: AbortSignal; onProgress: (percent: number) => void },
  ) => Promise<T>;
  /** Max in-flight uploads; default 3. */
  concurrency?: number;
  /** Size cap; default ``KNOWLEDGE_DOCUMENT_MAX_BYTES``. */
  maxBytes?: number;
  /** Extension whitelist predicate; default ``isSupportedDocument``. */
  isSupported?: (filename: string) => boolean;
  /** Called once when a batch with at least one real upload settles. */
  onSettled?: (items: UploadQueueItem<T>[]) => void;
}

export interface UseUploadQueueResult<T> {
  items: UploadQueueItem<T>[];
  enqueue: (files: ReadonlyArray<File | UploadQueueInput>) => void;
  cancel: (id: string) => void;
  cancelAll: () => void;
  retry: (id: string) => void;
  clearDone: () => void;
  summary: UploadQueueSummary;
}

export const DEFAULT_UPLOAD_CONCURRENCY = 3;

let nextQueueId = 0;

function newId(): string {
  nextQueueId += 1;
  return `upl-${nextQueueId}`;
}

function toInput(entry: File | UploadQueueInput): UploadQueueInput {
  return entry instanceof File ? { file: entry } : entry;
}

function relativePathOf(input: UploadQueueInput): string {
  if (input.relativePath) return input.relativePath;
  const webkitPath = (input.file as File & { webkitRelativePath?: string }).webkitRelativePath;
  return webkitPath || input.file.name;
}

function failureMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

export function useUploadQueue<T = unknown>({
  upload,
  concurrency = DEFAULT_UPLOAD_CONCURRENCY,
  maxBytes = KNOWLEDGE_DOCUMENT_MAX_BYTES,
  isSupported = isSupportedDocument,
  onSettled,
}: UseUploadQueueOptions<T>): UseUploadQueueResult<T> {
  const [items, setItems] = useState<UploadQueueItem<T>[]>([]);
  const itemsRef = useRef<UploadQueueItem<T>[]>([]);
  const controllersRef = useRef(new Map<string, AbortController>());
  /** True once an upload started since the last ``onSettled``. */
  const batchOpenRef = useRef(false);

  const uploadRef = useRef(upload);
  uploadRef.current = upload;
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  /** ``start`` → completion → ``pump`` → ``start``: break the cycle with a ref. */
  const pumpRef = useRef<() => void>(() => {});

  const commit = useCallback((next: UploadQueueItem<T>[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  const patch = useCallback(
    (id: string, change: Partial<UploadQueueItem<T>>) => {
      commit(itemsRef.current.map((item) => (item.id === id ? { ...item, ...change } : item)));
    },
    [commit],
  );

  const settleIfIdle = useCallback(() => {
    if (!batchOpenRef.current) return;
    const busy = itemsRef.current.some(
      (item) => item.status === "queued" || item.status === "uploading",
    );
    if (busy) return;
    batchOpenRef.current = false;
    onSettledRef.current?.(itemsRef.current);
  }, []);

  const start = useCallback(
    (item: UploadQueueItem<T>) => {
      const controller = new AbortController();
      controllersRef.current.set(item.id, controller);
      batchOpenRef.current = true;
      patch(item.id, { status: "uploading", progress: 0, error: undefined, cancelled: undefined });

      const isCurrent = () => controllersRef.current.get(item.id) === controller;

      uploadRef.current(item.file, {
        signal: controller.signal,
        onProgress: (percent) => {
          if (isCurrent() && !controller.signal.aborted) patch(item.id, { progress: percent });
        },
      }).then(
        (result) => {
          if (!isCurrent()) return;
          controllersRef.current.delete(item.id);
          patch(item.id, { status: "done", progress: 100, result });
          pumpRef.current();
        },
        (err: unknown) => {
          if (!isCurrent()) return;
          controllersRef.current.delete(item.id);
          // ``cancel`` already marked the row; the abort rejection must not
          // overwrite it with the transport's message.
          if (!controller.signal.aborted) {
            patch(item.id, { status: "failed", error: failureMessage(err) });
          }
          pumpRef.current();
        },
      );
    },
    [patch],
  );

  const pump = useCallback(() => {
    let inFlight = itemsRef.current.filter((item) => item.status === "uploading").length;
    for (const item of itemsRef.current) {
      if (inFlight >= concurrency) break;
      if (item.status !== "queued") continue;
      start(item);
      inFlight += 1;
    }
    settleIfIdle();
  }, [concurrency, start, settleIfIdle]);
  pumpRef.current = pump;

  const enqueue = useCallback(
    (files: ReadonlyArray<File | UploadQueueInput>) => {
      if (files.length === 0) return;
      const added: UploadQueueItem<T>[] = files.map((entry) => {
        const input = toInput(entry);
        const { file } = input;
        const base = {
          id: newId(),
          file,
          relativePath: relativePathOf(input),
          size: file.size,
          progress: 0,
        };
        if (!isSupported(file.name)) {
          return { ...base, status: "skipped", skipReason: "unsupported" };
        }
        if (file.size > maxBytes) {
          return { ...base, status: "skipped", skipReason: "too_large" };
        }
        return { ...base, status: "queued" };
      });
      commit([...itemsRef.current, ...added]);
      pump();
    },
    [commit, isSupported, maxBytes, pump],
  );

  const cancelOne = useCallback(
    (item: UploadQueueItem<T>): UploadQueueItem<T> => {
      if (item.status === "uploading") {
        controllersRef.current.get(item.id)?.abort();
        controllersRef.current.delete(item.id);
        return { ...item, status: "failed", cancelled: true };
      }
      if (item.status === "queued") {
        return { ...item, status: "failed", cancelled: true };
      }
      return item;
    },
    [],
  );

  const cancel = useCallback(
    (id: string) => {
      const target = itemsRef.current.find((item) => item.id === id);
      if (!target || (target.status !== "uploading" && target.status !== "queued")) return;
      commit(itemsRef.current.map((item) => (item.id === id ? cancelOne(item) : item)));
      pump();
    },
    [cancelOne, commit, pump],
  );

  const cancelAll = useCallback(() => {
    commit(itemsRef.current.map(cancelOne));
    pump();
  }, [cancelOne, commit, pump]);

  const retry = useCallback(
    (id: string) => {
      const target = itemsRef.current.find((item) => item.id === id);
      if (!target || target.status !== "failed") return;
      patch(id, { status: "queued", progress: 0, error: undefined, cancelled: undefined });
      pump();
    },
    [patch, pump],
  );

  const clearDone = useCallback(() => {
    commit(
      itemsRef.current.filter((item) => item.status !== "done" && item.status !== "skipped"),
    );
  }, [commit]);

  const summary = useMemo<UploadQueueSummary>(() => {
    const counts: UploadQueueSummary = {
      queued: 0,
      uploading: 0,
      done: 0,
      failed: 0,
      skipped: 0,
      total: items.length,
    };
    for (const item of items) counts[item.status] += 1;
    return counts;
  }, [items]);

  return { items, enqueue, cancel, cancelAll, retry, clearDone, summary };
}
