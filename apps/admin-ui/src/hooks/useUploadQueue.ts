/**
 * Upload queue hook — B-175 L3-T1 (batch / folder import).
 *
 * A bounded-concurrency queue over a caller-supplied ``upload`` function.
 * Semantics:
 *
 * - ``enqueue`` classifies every file once: unsupported extension or over
 *   ``maxBytes`` → ``skipped`` (never takes a slot, never retried); a
 *   basename that repeats within the batch, is already queued / uploading,
 *   or is in the caller's ``existingNames`` → ``skipped`` with
 *   ``duplicate_name`` (the server upserts on ``(kb, filename)``, so two
 *   uploads with one name would silently replace each other); the rest →
 *   ``queued`` in input order.
 * - Each item keeps the ``upload`` function that was current when it was
 *   enqueued (also for ``retry``), so a batch keeps going to the base it
 *   was picked for even if the page re-renders with another one.
 * - At most ``concurrency`` items are ``uploading`` at a time; when one
 *   settles the first ``queued`` item in list order starts.
 * - ``cancel`` aborts the item's ``AbortSignal`` (in flight) or pulls it
 *   from the queue; either way the row becomes ``failed`` with
 *   ``cancelled: true`` so it stays visible and retryable. ``summary``
 *   counts cancelled rows apart from real failures.
 * - An in-flight upload with no progress event for ``stallTimeoutMs`` is
 *   aborted and marked ``failed`` with ``stallMessage``.
 * - ``retry`` only acts on ``failed`` rows (re-queued at their original
 *   position); ``done`` / ``uploading`` / ``skipped`` / ``queued`` are no-ops.
 * - ``onBatchProgress`` fires at most once per ``batchProgressIntervalMs``
 *   while a batch is open, and only if an upload completed since the last
 *   call — a cheap "something landed" signal for long batches.
 * - ``onSettled`` fires once per batch: the first time no item is queued or
 *   uploading after at least one upload started since the last settle. A
 *   batch where everything was skipped never settles (nothing changed on
 *   the server, so callers skip the refresh).
 * - Unmount aborts every in-flight upload and stops the scheduler; while
 *   anything is queued or uploading a ``beforeunload`` guard asks before the
 *   page is closed.
 *
 * The scheduler works off a ref mirror of the items so a cancel / retry /
 * enqueue issued in the same tick as a completion sees the latest state;
 * every update builds a new array (no in-place mutation).
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { isSupportedDocument, KNOWLEDGE_DOCUMENT_MAX_BYTES } from "../api/knowledge";

export type UploadQueueStatus = "queued" | "uploading" | "done" | "failed" | "skipped";
export type UploadSkipReason = "unsupported" | "too_large" | "duplicate_name";

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
  /** ``failed`` rows that were not cancelled. */
  failed: number;
  /** ``failed`` rows with ``cancelled: true``. */
  cancelled: number;
  skipped: number;
  total: number;
}

export interface UploadQueueInput {
  file: File;
  relativePath?: string;
}

export interface EnqueueOptions {
  /** Document names already on the server; a pick with one of these
   *  basenames is skipped as ``duplicate_name``. */
  existingNames?: ReadonlySet<string>;
}

export type UploadFn<T> = (
  file: File,
  opts: { signal: AbortSignal; onProgress: (percent: number) => void },
) => Promise<T>;

export interface UseUploadQueueOptions<T> {
  upload: UploadFn<T>;
  /** Max in-flight uploads; default 3. */
  concurrency?: number;
  /** Size cap; default ``KNOWLEDGE_DOCUMENT_MAX_BYTES``. */
  maxBytes?: number;
  /** Extension whitelist predicate; default ``isSupportedDocument``. */
  isSupported?: (filename: string) => boolean;
  /** Called once when a batch with at least one real upload settles. */
  onSettled?: (items: UploadQueueItem<T>[]) => void;
  /** Throttled "at least one upload completed" signal while a batch is open. */
  onBatchProgress?: (items: UploadQueueItem<T>[]) => void;
  /** Min gap between two ``onBatchProgress`` calls; default 15 s. */
  batchProgressIntervalMs?: number;
  /** No progress event for this long → abort + ``failed``; default 120 s;
   *  ``0`` disables. */
  stallTimeoutMs?: number;
  /** Row message for a stalled upload. */
  stallMessage?: string;
}

export interface UseUploadQueueResult<T> {
  items: UploadQueueItem<T>[];
  enqueue: (files: ReadonlyArray<File | UploadQueueInput>, opts?: EnqueueOptions) => void;
  cancel: (id: string) => void;
  cancelAll: () => void;
  retry: (id: string) => void;
  clearDone: () => void;
  summary: UploadQueueSummary;
}

export const DEFAULT_UPLOAD_CONCURRENCY = 3;
export const DEFAULT_BATCH_PROGRESS_INTERVAL_MS = 15_000;
export const DEFAULT_UPLOAD_STALL_TIMEOUT_MS = 120_000;
const DEFAULT_STALL_MESSAGE = "Upload stalled: no progress for 120 s";

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

function isActive(item: UploadQueueItem<unknown>): boolean {
  return item.status === "queued" || item.status === "uploading";
}

function isCancelled(item: UploadQueueItem<unknown>): boolean {
  return item.status === "failed" && item.cancelled === true;
}

/** Invoke ``upload`` so a synchronous throw becomes a rejection instead of
 *  escaping the scheduler and leaving the slot occupied forever. */
function invokeUpload<T>(
  upload: UploadFn<T>,
  file: File,
  opts: Parameters<UploadFn<T>>[1],
): Promise<T> {
  return new Promise<T>((resolve) => resolve(upload(file, opts)));
}

function beforeUnloadGuard(event: BeforeUnloadEvent): void {
  event.preventDefault();
  // Legacy browsers need a non-empty returnValue to show the prompt.
  event.returnValue = "";
}

export function useUploadQueue<T = unknown>({
  upload,
  concurrency = DEFAULT_UPLOAD_CONCURRENCY,
  maxBytes = KNOWLEDGE_DOCUMENT_MAX_BYTES,
  isSupported = isSupportedDocument,
  onSettled,
  onBatchProgress,
  batchProgressIntervalMs = DEFAULT_BATCH_PROGRESS_INTERVAL_MS,
  stallTimeoutMs = DEFAULT_UPLOAD_STALL_TIMEOUT_MS,
  stallMessage = DEFAULT_STALL_MESSAGE,
}: UseUploadQueueOptions<T>): UseUploadQueueResult<T> {
  const [items, setItems] = useState<UploadQueueItem<T>[]>([]);
  const itemsRef = useRef<UploadQueueItem<T>[]>([]);
  const controllersRef = useRef(new Map<string, AbortController>());
  /** Per-item ``upload`` captured at enqueue time. */
  const uploadByIdRef = useRef(new Map<string, UploadFn<T>>());
  const stallTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  /** True once an upload started since the last ``onSettled``. */
  const batchOpenRef = useRef(false);
  const disposedRef = useRef(false);
  const tickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const completedSinceTickRef = useRef(false);

  const uploadRef = useRef(upload);
  const onSettledRef = useRef(onSettled);
  const onBatchProgressRef = useRef(onBatchProgress);
  const stallMessageRef = useRef(stallMessage);
  /** ``start`` → completion → ``pump`` → ``start``: break the cycle with a ref. */
  const pumpRef = useRef<() => void>(() => {});

  useLayoutEffect(() => {
    uploadRef.current = upload;
    onSettledRef.current = onSettled;
    onBatchProgressRef.current = onBatchProgress;
    stallMessageRef.current = stallMessage;
  });

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

  const clearStallTimer = useCallback((id: string) => {
    const timer = stallTimersRef.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      stallTimersRef.current.delete(id);
    }
  }, []);

  const clearTickTimer = useCallback(() => {
    if (tickTimerRef.current !== null) {
      clearTimeout(tickTimerRef.current);
      tickTimerRef.current = null;
    }
    completedSinceTickRef.current = false;
  }, []);

  const noteCompleted = useCallback(() => {
    completedSinceTickRef.current = true;
    if (tickTimerRef.current !== null) return;
    tickTimerRef.current = setTimeout(() => {
      tickTimerRef.current = null;
      if (disposedRef.current || !batchOpenRef.current || !completedSinceTickRef.current) return;
      completedSinceTickRef.current = false;
      onBatchProgressRef.current?.(itemsRef.current);
    }, batchProgressIntervalMs);
  }, [batchProgressIntervalMs]);

  const settleIfIdle = useCallback(() => {
    if (!batchOpenRef.current) return;
    if (itemsRef.current.some(isActive)) return;
    batchOpenRef.current = false;
    clearTickTimer();
    onSettledRef.current?.(itemsRef.current);
  }, [clearTickTimer]);

  const start = useCallback(
    (item: UploadQueueItem<T>) => {
      const controller = new AbortController();
      controllersRef.current.set(item.id, controller);
      batchOpenRef.current = true;
      patch(item.id, { status: "uploading", progress: 0, error: undefined, cancelled: undefined });

      const isCurrent = () => controllersRef.current.get(item.id) === controller;
      const release = () => {
        controllersRef.current.delete(item.id);
        clearStallTimer(item.id);
      };

      const armStall = () => {
        clearStallTimer(item.id);
        if (stallTimeoutMs <= 0) return;
        stallTimersRef.current.set(
          item.id,
          setTimeout(() => {
            stallTimersRef.current.delete(item.id);
            if (!isCurrent()) return;
            // Same shape as ``cancel``: free the slot now; the abort
            // rejection that follows is ignored because the controller is
            // no longer current.
            release();
            controller.abort();
            patch(item.id, { status: "failed", error: stallMessageRef.current });
            pumpRef.current();
          }, stallTimeoutMs),
        );
      };
      armStall();

      const uploadFn = uploadByIdRef.current.get(item.id) ?? uploadRef.current;
      invokeUpload(uploadFn, item.file, {
        signal: controller.signal,
        onProgress: (percent) => {
          if (!isCurrent() || controller.signal.aborted) return;
          armStall();
          const rounded = Math.round(percent);
          const current = itemsRef.current.find((i) => i.id === item.id);
          if (current && current.progress === rounded) return;
          patch(item.id, { progress: rounded });
        },
      }).then(
        (result) => {
          if (!isCurrent()) return;
          release();
          patch(item.id, { status: "done", progress: 100, result });
          noteCompleted();
          pumpRef.current();
        },
        (err: unknown) => {
          if (!isCurrent()) return;
          release();
          // ``cancel`` already marked the row; the abort rejection must not
          // overwrite it with the transport's message.
          if (!controller.signal.aborted) {
            patch(item.id, { status: "failed", error: failureMessage(err) });
          }
          pumpRef.current();
        },
      );
    },
    [clearStallTimer, noteCompleted, patch, stallTimeoutMs],
  );

  const pump = useCallback(() => {
    if (disposedRef.current) return;
    let inFlight = itemsRef.current.filter((item) => item.status === "uploading").length;
    for (const item of itemsRef.current) {
      if (inFlight >= concurrency) break;
      if (item.status !== "queued") continue;
      start(item);
      inFlight += 1;
    }
    settleIfIdle();
  }, [concurrency, start, settleIfIdle]);

  useLayoutEffect(() => {
    pumpRef.current = pump;
  }, [pump]);

  const enqueue = useCallback(
    (files: ReadonlyArray<File | UploadQueueInput>, opts: EnqueueOptions = {}) => {
      if (files.length === 0 || disposedRef.current) return;
      const batchUpload = uploadRef.current;
      const taken = new Set<string>(opts.existingNames ?? []);
      for (const item of itemsRef.current) if (isActive(item)) taken.add(item.file.name);

      const added: UploadQueueItem<T>[] = [];
      for (const entry of files) {
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
          added.push({ ...base, status: "skipped", skipReason: "unsupported" });
        } else if (file.size > maxBytes) {
          added.push({ ...base, status: "skipped", skipReason: "too_large" });
        } else if (taken.has(file.name)) {
          added.push({ ...base, status: "skipped", skipReason: "duplicate_name" });
        } else {
          taken.add(file.name);
          uploadByIdRef.current.set(base.id, batchUpload);
          added.push({ ...base, status: "queued" });
        }
      }
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
        clearStallTimer(item.id);
        return { ...item, status: "failed", cancelled: true };
      }
      if (item.status === "queued") {
        return { ...item, status: "failed", cancelled: true };
      }
      return item;
    },
    [clearStallTimer],
  );

  const cancel = useCallback(
    (id: string) => {
      const target = itemsRef.current.find((item) => item.id === id);
      if (!target || !isActive(target)) return;
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
    const next = itemsRef.current.filter(
      (item) => item.status !== "done" && item.status !== "skipped" && !isCancelled(item),
    );
    const kept = new Set(next.map((item) => item.id));
    for (const id of uploadByIdRef.current.keys()) {
      if (!kept.has(id)) uploadByIdRef.current.delete(id);
    }
    commit(next);
  }, [commit]);

  const summary = useMemo<UploadQueueSummary>(() => {
    const counts: UploadQueueSummary = {
      queued: 0,
      uploading: 0,
      done: 0,
      failed: 0,
      cancelled: 0,
      skipped: 0,
      total: items.length,
    };
    for (const item of items) {
      if (isCancelled(item)) counts.cancelled += 1;
      else counts[item.status] += 1;
    }
    return counts;
  }, [items]);

  const active = summary.queued + summary.uploading > 0;
  useEffect(() => {
    if (!active) return undefined;
    window.addEventListener("beforeunload", beforeUnloadGuard);
    return () => window.removeEventListener("beforeunload", beforeUnloadGuard);
  }, [active]);

  // Unmount: stop the scheduler, abort everything in flight, drop timers.
  // (StrictMode runs this cleanup once on mount too — re-arm on setup.)
  useEffect(() => {
    disposedRef.current = false;
    const controllers = controllersRef.current;
    const stallTimers = stallTimersRef.current;
    return () => {
      disposedRef.current = true;
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      for (const timer of stallTimers.values()) clearTimeout(timer);
      stallTimers.clear();
      if (tickTimerRef.current !== null) clearTimeout(tickTimerRef.current);
      tickTimerRef.current = null;
    };
  }, []);

  return { items, enqueue, cancel, cancelAll, retry, clearDone, summary };
}
