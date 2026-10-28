/**
 * B-226 ⑥ — "finished, not completed". The ``end`` frame's ``status`` says
 * whether the run finished; ``completed`` says whether the request was done.
 * A run that used up its steps ends ``status=success`` + ``completed=false``
 * + ``exit_reason=max_steps`` — the console used to read ``status`` only and
 * paint it as a plain green success next to a red guard marker.
 *
 * The timeline end marker, the turn footer tag and the run detail header all
 * read this one helper; run rows go through {@link runCompletionOf} first.
 */
import i18n from "../i18n";
import type { SseEvent } from "./sessions";

/** ``exit_reason`` → its label key. Values not listed here show raw. */
const EXIT_REASON_KEYS: ReadonlyMap<string, string> = new Map([
  ["max_steps", "console_runtime.exit_reason_max_steps"],
  ["token_budget", "console_runtime.exit_reason_token_budget"],
  ["no_progress", "console_runtime.exit_reason_no_progress"],
  ["approval_rejected", "console_runtime.exit_reason_approval_rejected"],
  // The model stopped on its own, but a tool failure was never recovered.
  ["text_response", "console_runtime.exit_reason_text_response"],
]);

/** The warning label for a finished-but-not-completed run, or ``null``.
 *
 *  ``status === "incomplete"`` (a platform limit stopped the run; partner-facing
 *  end frames say this since B-226 ⑥) always qualifies, and so does
 *  ``status === "success"`` with ``completed === false``. An absent
 *  ``completed`` on a success is "no record" (a run from before the field
 *  existed), never "not done"; ``paused`` (``exit_reason=approval_pending``)
 *  already has its own marker. */
export function notCompletedLabel(endData: unknown): string | null {
  if (endData === null || typeof endData !== "object") return null;
  const d = endData as Record<string, unknown>;
  const notDone = d.status === "incomplete" || (d.status === "success" && d.completed === false);
  if (!notDone) return null;
  const reason = typeof d.exit_reason === "string" ? d.exit_reason : "";
  if (reason === "") return i18n.t("console_runtime.status_not_completed_plain");
  const key = EXIT_REASON_KEYS.get(reason);
  return i18n.t("console_runtime.status_not_completed", {
    reason: key === undefined ? reason : i18n.t(key),
  });
}

/** One entry of a run's ``unresolved_failures``: the tool and its error class
 *  (open set, e.g. ``unknown`` / ``blocked_by_policy``). Never any tool output. */
export interface UnresolvedFailure {
  tool: string;
  kind: string;
}

/** A run row's completion in the end frame's shape (``{status, completed,
 *  exit_reason, unresolved_failures}``) — what the helpers here read. */
export interface RunCompletion {
  status: string;
  completed: boolean | null;
  exit_reason: string | null;
  unresolved_failures: UnresolvedFailure[] | null;
}

/** Adapter for run rows: the admin run APIs return ``status`` / ``completed`` /
 *  ``exit_reason`` / ``unresolved_failures`` on the row, not in an end frame.
 *  The row's ``success`` is the end frame's ``success`` and no other status
 *  qualifies, so the status passes through unmapped. Absent fields become
 *  ``null`` (no record). */
export function runCompletionOf(run: {
  status: string;
  completed?: boolean | null;
  exit_reason?: string | null;
  unresolved_failures?: UnresolvedFailure[] | null;
}): RunCompletion {
  return {
    status: run.status,
    completed: run.completed ?? null,
    exit_reason: run.exit_reason ?? null,
    unresolved_failures: run.unresolved_failures ?? null,
  };
}

/** The ``unresolved_failures`` of an end frame or {@link RunCompletion}, read
 *  defensively: entries without a string ``tool`` / ``kind`` are skipped. */
export function unresolvedFailuresOf(data: unknown): UnresolvedFailure[] {
  if (data === null || typeof data !== "object") return [];
  const raw = (data as Record<string, unknown>).unresolved_failures;
  if (!Array.isArray(raw)) return [];
  const out: UnresolvedFailure[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object") continue;
    const { tool, kind } = entry as Record<string, unknown>;
    if (typeof tool === "string" && typeof kind === "string") out.push({ tool, kind });
  }
  return out;
}

/** "未解决的失败：bash（unknown）、…" — ``null`` for an empty list. */
export function unresolvedFailuresText(failures: readonly UnresolvedFailure[]): string | null {
  if (failures.length === 0) return null;
  const list = failures
    .map((f) => i18n.t("console_runtime.unresolved_failure_item", { tool: f.tool, kind: f.kind }))
    .join(i18n.t("console_runtime.unresolved_failure_sep"));
  return i18n.t("console_runtime.unresolved_failures", { list });
}

/** The ``data`` of the turn's last ``end`` frame; ``undefined`` before it arrives. */
export function lastEndData(events: readonly SseEvent[]): unknown {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i].event === "end") return events[i].data;
  }
  return undefined;
}
