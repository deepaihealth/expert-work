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
  /** B-73 ② —— 这一轮里平台自己贴的隐藏行(B-67「本轮输入」段、恢复建议)。
   *  **只有跨租户审计视图拿得到**(后端只在那一路带 ``hidden: true``);同租户
   *  视图恒为空数组。它们既不是这一轮的输入、也不是回复的一部分,所以单独一份,
   *  由 ``TurnBlock`` 渲染成一段标了「平台自动生成」的折叠块 —— 此前是直接丢掉,
   *  于是审计视图特意要来的忠实记录在界面上又不见了。 */
  platformLines: string[];
  runId: string;
  status: string;
  tokens: RunTokens | null;
  /** ``ThreadRunSummary.createdAt``(ISO);账本时长投影里未回放 / 无时序轮的兜底起点。 */
  createdAt: string | null;
  /** ``ThreadRunSummary.finishedAt``(ISO)—— 总耗时的墙钟终点;老后端 null。 */
  finishedAt: string | null;
  /** ``ThreadRunSummary.error`` —— INTERRUPTED 的中断原因短码 / ERROR 的异常文本。 */
  runError: string | null;
  /** P-1 —— 这一轮被哪个新 run 取代(``ThreadRunSummary.supersededBy``);
   *  ``null`` = 未被取代。run 行是权威,两条配对路径都从它取。 */
  supersededBy: string | null;
  /** P-1 —— 这一轮的消息已置墓碑(正文清理)。只有按 run_id 分组的路径能判:
   *  顺序配对路径下消息归不到具体的一轮,恒 ``false``。 */
  tombstone: boolean;
}

/** A row that can be a turn's input: a user row that is not platform
 *  scaffolding. Hidden rows (B-67 inputs block, advisories) are neither an
 *  input nor part of the reply, so the turn views leave them out — the
 *  assistant's fallback lines would otherwise show platform text as if the
 *  agent had said it. */
function isInputRow(m: HistoryMessage): boolean {
  return m.role === "user" && m.hidden !== true;
}

/** 平台自己贴进检查点的行。只有 user 角色会被标 ``hidden``(B-67 的「本轮输入」
 *  段、CM-1 的恢复建议都是 HumanMessage),助手行不在其列。 */
function isPlatformRow(m: HistoryMessage): boolean {
  return m.role === "user" && m.hidden === true;
}

/** Group the messages by their owning run, or ``null`` if grouping them is
 *  not unambiguous — in which case the caller must stay on the ORDER path.
 *
 *  Two things make it ambiguous, and mixing strategies is worse than either:
 *
 *  - ANY message without a ``run_id`` (written before the stamp shipped, never
 *    backfilled): it cannot be placed in a group at all, while order pairing
 *    does have a place for it. One such row disqualifies the whole thread.
 *  - A run owning MORE THAN ONE non-hidden user row. The faithful cross-tenant
 *    audit view (``include_hidden=true``) also carries platform scaffolding —
 *    the B-67 inputs block, the ``<recovery-advisory>`` — stamped with the same
 *    run as the real input. The backend marks those ``hidden: true`` and they
 *    are never a turn's input (see :func:`isInputRow`), so they don't count
 *    here. Rows from a backend that predates the flag can't be told apart:
 *    "the run's user message" has no single answer, so degrade to flat text.
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
      if (isInputRow(m) && own.some(isInputRow)) return null;
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
        input: own.find(isInputRow)?.content ?? "",
        fallbackLines: own
          .filter((m) => m.role !== "user")
          .map((m) => ({ text: m.content, channel: m.channel ?? null })),
        platformLines: own.filter(isPlatformRow).map((m) => m.content),
        runId: r.runId,
        status: r.status,
        tokens: r.tokens,
        createdAt: r.createdAt ?? null,
        finishedAt: r.finishedAt ?? null,
        runError: r.error ?? null,
        supersededBy: r.supersededBy ?? null,
        tombstone: own.some((m) => m.tombstone === true),
      };
    });
  }

  const pairs: { input: string; answers: FallbackLine[]; platform: string[] }[] = [];
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i];
    if (!isInputRow(m)) continue;
    const answers: FallbackLine[] = [];
    const platform: string[] = [];
    for (let j = i + 1; j < messages.length && !isInputRow(messages[j]); j += 1) {
      if (isPlatformRow(messages[j])) {
        platform.push(messages[j].content);
        continue;
      }
      if (messages[j].role === "user") continue; // 不认识的 user 行,照旧跳过
      answers.push({ text: messages[j].content, channel: messages[j].channel ?? null });
    }
    pairs.push({ input: m.content, answers, platform });
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
    platformLines: i < pairs.length ? pairs[i].platform : [],
    runId: r.runId,
    status: r.status,
    tokens: r.tokens,
    createdAt: r.createdAt ?? null,
    finishedAt: r.finishedAt ?? null,
    runError: r.error ?? null,
    // P-1 —— run 行与这一路径的轮仍是 1:1(``runs.map``),所以「被取代」照常
    // 取得到;墓碑要看这一轮自己的消息,而这条路径上的消息正是归不到具体一轮
    // 的那种(没有 run 戳),只能给 false。
    supersededBy: r.supersededBy ?? null,
    tombstone: false,
  }));
}
