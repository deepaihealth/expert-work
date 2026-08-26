/**
 * Pair a resumed thread's flat message history with its runs so each past
 * turn can be rebuilt as a full (lazy) TurnCard. The run event stream does
 * NOT carry the user input (it's the graph input, kept in the checkpoint),
 * so the input text comes from ``/messages`` here.
 *
 * Two pairing strategies, picked by what the data supports:
 *
 * 1. BY ``run_id`` — every message carries the run that produced it (the
 *    backend's ``expert_work_run_id`` stamp), so grouping is exact and none
 *    of the order-pairing guards below apply. This is what stops an approval
 *    from flattening the page: the paused run and its continuation own their
 *    own messages, and a continuation legitimately owns no user message at
 *    all (it resumes the paused turn's checkpoint) — an empty ``input``, not
 *    a reason to degrade.
 * 2. BY ORDER — user turn ``i`` ↔ ``runs[i]`` (runs oldest-first). The
 *    fallback for messages written before the stamp shipped; they are never
 *    backfilled, so this path has to keep working forever.
 *
 * ``is_resume`` is deliberately ignored: it means "not the thread's first
 * run", not "approval continuation", so it can't delimit turns. On the ORDER
 * path a count mismatch (an approval that split one turn across 2 runs, an
 * auto-triggered or errored run) is the honest signal that pairing is
 * unsafe — we return ``null`` and the caller falls back to flat text.
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
import { NON_TERMINAL_RUN_STATUSES } from "../../../api/runs";
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
  /** ``ThreadRunSummary.finishedAt``(ISO)—— 总耗时的墙钟终点;老后端 null。 */
  finishedAt: string | null;
  /** ``ThreadRunSummary.error`` —— INTERRUPTED 的中断原因短码 / ERROR 的异常文本。 */
  runError: string | null;
}

/** Group the messages by their owning run, or ``null`` if grouping them is
 *  not unambiguous — in which case the caller must stay on the ORDER path.
 *
 *  Two things make it ambiguous, and mixing strategies is worse than either:
 *
 *  - ANY message without a ``run_id`` (written before the stamp shipped, never
 *    backfilled): it cannot be placed in a group at all, while order pairing
 *    does have a place for it. One such row disqualifies the whole thread.
 *  - A run owning MORE THAN ONE user row: the faithful cross-tenant audit view
 *    (``include_hidden=true``) keeps the orchestrator's ``<recovery-advisory>``
 *    HumanMessage, which is stamped with the same run as the real input, so
 *    "the run's user message" no longer has one answer. Order pairing degrades
 *    to flat text there, which at least shows both rows; grouping would have
 *    to silently drop one — in the view whose whole point is faithfulness.
 *
 *  An empty message list is NOT grouped either: there is nothing to group, so
 *  it is no evidence that this thread is stamped. It happens for real — the
 *  backend's transcript read is best-effort and degrades to ``[]`` — and
 *  switching strategy there would turn a "no history" page into one empty
 *  input card per run. */
function groupMessagesByRun(
  messages: readonly HistoryMessage[],
): Map<string, HistoryMessage[]> | null {
  if (messages.length === 0) return null;
  const byRun = new Map<string, HistoryMessage[]>();
  for (const m of messages) {
    const runId = m.run_id;
    if (!runId) return null;
    const own = byRun.get(runId);
    if (own) {
      if (m.role === "user" && own.some((o) => o.role === "user")) return null;
      own.push(m);
    } else {
      byRun.set(runId, [m]);
    }
  }
  return byRun;
}

export function buildHistoryTurns(
  messages: readonly HistoryMessage[],
  runs: readonly ThreadRunSummary[],
): HistoryTurn[] | null {
  const byRun = groupMessagesByRun(messages);
  if (byRun) {
    // ``runs`` drives the rendered timeline, so it — not the message list —
    // decides which turns exist. Messages whose run is absent from it belong
    // to runs outside this page and are ignored on purpose; no run of this
    // page can lose content that way.
    return runs.map((r) => {
      const own = byRun.get(r.runId) ?? [];
      return {
        key: r.runId,
        input: own.find((m) => m.role === "user")?.content ?? "",
        fallbackLines: own
          .filter((m) => m.role !== "user")
          .map((m) => ({ text: m.content, channel: m.channel ?? null })),
        runId: r.runId,
        status: r.status,
        tokens: r.tokens,
        createdAt: r.createdAt ?? null,
        finishedAt: r.finishedAt ?? null,
        runError: r.error ?? null,
      };
    });
  }

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
  // D-5 — tolerate a TRAILING contiguous block of non-terminal runs (a
  // running new turn, a paused approval, or paused + its just-spawned
  // continuation). Such runs may not have their user message checkpointed
  // yet (running) or may own no user message at all (a continuation run
  // resumes the paused turn's checkpoint), so strict count equality would
  // needlessly flatten the whole page. A non-terminal run BEFORE the
  // trailing block (e.g. a paused run whose continuation already finished)
  // still degrades to flat — order-pairing is genuinely ambiguous there
  // (one user message ↔ two runs; folding continuation chains is a
  // follow-up, ROADMAP D-7).
  const n = runs.length;
  let tail = 0;
  while (tail < n && NON_TERMINAL_RUN_STATUSES.has(runs[n - 1 - tail].status)) tail += 1;
  for (let i = 0; i < n - tail; i += 1) {
    if (NON_TERMINAL_RUN_STATUSES.has(runs[i].status)) return null;
  }
  if (pairs.length < n - tail || pairs.length > n) return null;
  return runs.map((r, i) => ({
    key: r.runId,
    input: i < pairs.length ? pairs[i].input : "",
    fallbackLines: i < pairs.length ? pairs[i].answers : [],
    runId: r.runId,
    status: r.status,
    tokens: r.tokens,
    createdAt: r.createdAt ?? null,
    finishedAt: r.finishedAt ?? null,
    runError: r.error ?? null,
  }));
}
