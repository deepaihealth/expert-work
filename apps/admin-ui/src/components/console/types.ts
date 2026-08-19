/**
 * View-model types for the debug console's turn timeline — history turns
 * (persisted rollup, lazy-replayed) and live turns (streamed this session)
 * normalised into one ``ConsoleTurn`` shape so the console can render and
 * measure them uniformly. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-5-brief.md.
 */
import type { RunTokens } from "../../api/runs";
import type { FallbackLine } from "../../pages/agent_detail/playground/history_turns";
import type { Turn } from "../turn/types";

export type LoadState = "pending" | "loading" | "done" | "error";

/** Per-turn timing captured only while this session streamed the turn live
 *  (``useTokenStream``'s ttft / first-token / last-token wall clock). */
export interface TurnTiming {
  ttftMs: number | null;
  firstTokenAt: number | null;
  lastTokenAt: number | null;
}

/** One row of the console's turn timeline — a history turn (lazy-replayed
 *  from the persisted rollup) or a live turn (streamed this session),
 *  normalised to one shape. */
export interface ConsoleTurn {
  /** history: the source ``HistoryTurn.key``; live: ``turn.id``. */
  key: string;
  /** 0-based; history turns come first, live turns after. */
  seq: number;
  source: "history" | "live";
  /** History turns are synthesised (status/error mapped from the run
   *  summary, same as the old ``TurnCard`` call site); live turns are the
   *  playground's own ``Turn`` object. */
  turn: Turn;
  runId: string | null;
  /** Live turns are always ``"done"`` — only history turns lazy-load. */
  loadState: LoadState;
  /** Live turns are always ``[]``. */
  fallbackLines: FallbackLine[];
  /** History turns' persisted rollup; live turns are always ``null``. */
  tokens: RunTokens | null;
  /** Set only for turns this session actually streamed live. */
  timing: TurnTiming | null;
  /** History turns' ``ThreadRunSummary.createdAt``(ISO);live turns ``null``。 */
  createdAt: string | null;
}
