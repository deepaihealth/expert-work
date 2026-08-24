/**
 * ``ConsoleTurn`` builder — merges lazy-loaded history turns (persisted
 * rollup, replayed on demand) with this-session live turns into one ordered
 * timeline the console renders/measures uniformly (Task 5 view model). See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-5-brief.md.
 *
 * ``runIdOf`` was copied (not imported) from ``components/turn/TurnCard.tsx``
 * per controller ruling; TurnCard has since been retired (PR-B Task 5), so
 * this module is its only surviving implementation.
 */
import type { ApprovalItem } from "../../api/approvals";
import type { StatsTurnInput } from "../../api/session_stats";
import type { SseEvent } from "../../api/sessions";
import { NON_TERMINAL_RUN_STATUSES } from "../../api/runs";
import { promptInputsOf } from "../../api/trajectory_rows";
import { approvalItemFromEvent } from "../turn/approval_item";
import type { HistoryLoad, HistoryTurn, Turn } from "../turn/types";
import type { ConsoleTurn, TurnTiming } from "./types";

/** Copied from the now-retired ``components/turn/TurnCard.tsx`` (see module
 *  docstring) — the first ``run_id`` carried by a ``metadata`` frame. */
export function runIdOf(events: readonly SseEvent[]): string | null {
  for (const e of events) {
    if (
      e.event === "metadata" &&
      e.data !== null &&
      typeof e.data === "object"
    ) {
      const rid = (e.data as Record<string, unknown>).run_id;
      if (typeof rid === "string" && rid) return rid;
    }
  }
  return null;
}

/** D-5/D-6 — the last ``approval`` frame in a replayed stream, as the
 *  ``ApprovalItem`` the in-place approve/reject card needs. ``null`` when
 *  the stream carries none (or the frame doesn't parse). */
function lastApprovalFrom(events: readonly SseEvent[]): ApprovalItem | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i].event === "approval") return approvalItemFromEvent(events[i].data);
  }
  return null;
}

export function buildConsoleTurns(args: {
  historyTurns: HistoryTurn[] | null;
  historyLoads: Record<string, HistoryLoad>;
  liveTurns: readonly Turn[];
  timings: Readonly<Record<string, TurnTiming>>;
  /** D-6 (终审 C-2) — synthesise the in-place ApprovalItem on a paused last
   *  turn. STRICTLY opt-in: only the conversation page (whose ``onDecide``
   *  targets history turns) passes true. The playground's ``onDecide``
   *  dispatches by LIVE turn id — a synthesised approval there would decide
   *  on the backend while the UI silently no-ops. */
  synthesizeApprovals?: boolean;
}): ConsoleTurn[] {
  const out: ConsoleTurn[] = [];
  let seq = 0;

  const history = args.historyTurns ?? [];
  for (let i = 0; i < history.length; i += 1) {
    const h = history[i];
    const load = args.historyLoads[h.runId] ?? { state: "pending" as const, events: [] };
    // Same status/error mapping as the old TurnCard call site
    // (PlaygroundTab.tsx:1337-1360) — only "error"/"timeout" map to a
    // failed turn; every other terminal ThreadRunSummary.status is "done".
    // D-5 — a non-terminal run renders as a running turn; its hook-internal
    // "live" load state maps to "done" (the same convention the playground's
    // live turns use: partial events render, status carries the in-flight
    // affordances).
    const failed = h.status === "error" || h.status === "timeout";
    const inFlight = NON_TERMINAL_RUN_STATUSES.has(h.status);
    // D-6 — surface the pending approval on a paused LAST turn only: a
    // paused run followed by another run already has a continuation (its
    // approval was decided), so live approve/reject there would 409.
    const isLastTurn = i === history.length - 1 && args.liveTurns.length === 0;
    const approval =
      (args.synthesizeApprovals ?? false) && h.status === "paused" && isLastTurn
        ? lastApprovalFrom(load.events)
        : null;
    out.push({
      key: h.key,
      seq,
      source: "history",
      turn: {
        id: h.key,
        input: h.input,
        attachments: [],
        // BUG-16 — 历史轮的 jinja 入参从回放的 system_prompt 帧还原
        // (live 轮在 useRunEngine 派发时就带;老 run 无帧数据 → undefined)。
        inputs:
          promptInputsOf(
            load.events.find((e) => e.event === "system_prompt")?.data ?? null,
          ) ?? undefined,
        events: load.events,
        // BUG-9 — ``interrupted`` (user cancel / stream break) must keep its
        // identity: mapping it to "done" rendered a cancelled run as
        // 「已完成 (无文本回复)」.
        status: failed
          ? "error"
          : inFlight
            ? "running"
            : h.status === "interrupted"
              ? "interrupted"
              : "done",
        error: failed ? h.status : null,
        approval,
      },
      runId: h.runId,
      loadState: load.state === "live" ? "done" : load.state,
      fallbackLines: h.fallbackLines,
      tokens: h.tokens,
      timing: null,
      createdAt: h.createdAt,
    });
    seq += 1;
  }

  for (const turn of args.liveTurns) {
    out.push({
      key: turn.id,
      seq,
      source: "live",
      turn,
      runId: runIdOf(turn.events),
      loadState: "done",
      fallbackLines: [],
      tokens: null,
      timing: args.timings[turn.id] ?? null,
      createdAt: null,
    });
    seq += 1;
  }

  return out;
}

/** ``ConsoleTurn`` → ``StatsTurnInput`` — the status bar's per-turn input. */
export function statsInputOf(t: ConsoleTurn): StatsTurnInput {
  return {
    events: t.turn.events,
    loaded: t.loadState === "done",
    status: t.turn.status,
    tokens: t.tokens,
    timing: t.timing,
  };
}
