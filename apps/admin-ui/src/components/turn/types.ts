/**
 * Shared transcript types for the ``components/turn`` family — lifted verbatim
 * out of ``pages/agent_detail/PlaygroundTab.tsx`` so the conversation detail
 * page can reuse the same turn cards.
 *
 * i18n note: these components keep reading the ``playground.*`` namespace —
 * it is now a **cross-page shared** namespace, not playground-only.
 */
import type { ApprovalItem } from "../../api/approvals";
import type { SseEvent } from "../../api/sessions";

/** Re-exported so a consumer of ``useHistoryTurns`` can name the turn shape
 *  without reaching back into ``pages/agent_detail/playground`` — one
 *  components→pages edge instead of one per call site. */
export type { HistoryTurn } from "../../pages/agent_detail/playground/history_turns";

export interface Attachment {
  id: string;
  name: string;
  kind: "image" | "document";
  value: string;
}

/** One round of the conversation — the user input plus the agent's streamed
 *  frames for that turn (the thread is reused, so the backend continues the
 *  context across turns). */
export interface Turn {
  id: string;
  input: string;
  attachments: Attachment[];
  /** #10 — the jinja prompt-variable values this turn was dispatched with
   *  (``RunRequest.inputs``), kept so a retry re-sends the same variables.
   *  Absent on turns that predate the field (history/replayed turns). */
  inputs?: Record<string, string>;
  events: SseEvent[];
  status: "running" | "done" | "error";
  error: string | null;
  /** #5 — set when the run paused at an approval gate; cleared on decision. */
  approval: ApprovalItem | null;
}

/** Historical-turn lazy replay state (one per resumed run). ``pending`` = not
 *  yet scrolled into view; ``loading`` = replay in flight; ``done`` = replayed
 *  events collected (TurnCard renders the full debug panels); ``error`` =
 *  replay failed (TurnCard keeps showing the flat fallback answer). */
export type HistoryLoad =
  | { state: "pending" | "loading" | "error"; events: SseEvent[] }
  | { state: "done"; events: SseEvent[] };
