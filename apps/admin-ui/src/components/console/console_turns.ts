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
import type { StatsTurnInput } from "../../api/session_stats";
import type { SseEvent } from "../../api/sessions";
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

export function buildConsoleTurns(args: {
  historyTurns: HistoryTurn[] | null;
  historyLoads: Record<string, HistoryLoad>;
  liveTurns: readonly Turn[];
  timings: Readonly<Record<string, TurnTiming>>;
}): ConsoleTurn[] {
  const out: ConsoleTurn[] = [];
  let seq = 0;

  for (const h of args.historyTurns ?? []) {
    const load = args.historyLoads[h.runId] ?? { state: "pending" as const, events: [] };
    // Same status/error mapping as the old TurnCard call site
    // (PlaygroundTab.tsx:1337-1360) — only "error"/"timeout" map to a
    // failed turn; every other terminal ThreadRunSummary.status is "done".
    const failed = h.status === "error" || h.status === "timeout";
    out.push({
      key: h.key,
      seq,
      source: "history",
      turn: {
        id: h.key,
        input: h.input,
        attachments: [],
        events: load.events,
        status: failed ? "error" : "done",
        error: failed ? h.status : null,
        approval: null,
      },
      runId: h.runId,
      loadState: load.state,
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
