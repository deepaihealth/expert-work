/**
 * Pair a resumed thread's flat message history with its runs so each past
 * turn can be rebuilt as a full (lazy) TurnCard. The run event stream does
 * NOT carry the user input (it's the graph input, kept in the checkpoint),
 * so the input text comes from ``/messages`` here, paired to the run that
 * produced it by ORDER — user turn ``i`` ↔ ``runs[i]`` (runs oldest-first).
 *
 * ``is_resume`` is deliberately ignored: it means "not the thread's first
 * run", not "approval continuation", so it can't delimit turns. A count
 * mismatch (an approval that split one turn across 2 runs, an auto-triggered
 * or errored run) is the honest signal that order-pairing is unsafe — we
 * return ``null`` and the caller falls back to flat text.
 *
 * A single run can emit several assistant messages (multi-step turns), so
 * ``fallbackLines`` collects every assistant message between a user turn
 * and the next one — not just the immediate next message — so the
 * replay-failure fallback doesn't show less than the flat view would. Each
 * line keeps its structural ``channel`` (spec:
 * docs/superpowers/specs/2026-07-30-conversation-output-channels-design.md)
 * so ``TurnCard`` can render it commentary-style vs. as the answer body,
 * matching the live/replayed rendering instead of flattening narration INTO
 * the answer.
 */
import type { HistoryMessage } from "../../../api/sessions";
import type { RunTokens, ThreadRunSummary } from "../../../api/runs";

export interface FallbackLine {
  text: string;
  channel: "commentary" | "final" | null;
}

export interface HistoryTurn {
  key: string;
  input: string;
  fallbackLines: FallbackLine[];
  runId: string;
  status: string;
  tokens: RunTokens | null;
  /** ``ThreadRunSummary.createdAt``(ISO);账本时长投影里未回放 / 无时序轮的兜底起点。 */
  createdAt: string | null;
}

export function buildHistoryTurns(
  messages: readonly HistoryMessage[],
  runs: readonly ThreadRunSummary[],
): HistoryTurn[] | null {
  const pairs: { input: string; answers: FallbackLine[] }[] = [];
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i];
    if (m.role !== "user") continue;
    const answers: FallbackLine[] = [];
    for (let j = i + 1; j < messages.length && messages[j].role !== "user"; j += 1) {
      answers.push({ text: messages[j].content, channel: messages[j].channel ?? null });
    }
    pairs.push({ input: m.content, answers });
  }
  if (pairs.length !== runs.length) return null;
  return pairs.map((p, i) => ({
    key: runs[i].runId,
    input: p.input,
    fallbackLines: p.answers,
    runId: runs[i].runId,
    status: runs[i].status,
    tokens: runs[i].tokens,
    createdAt: runs[i].createdAt ?? null,
  }));
}
