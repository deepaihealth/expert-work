/**
 * 账本构建器(spec §九,PR-A.2 Task 2)—— 把调试台加载窗口内的
 * `ConsoleTurn[]` 折成一本会话级 `Ledger`:一串按轮、按事件序排好的
 * `LedgerRecord`,外加每次主模型调用一条 `LedgerRequest` 和每轮一条
 * `LedgerTurn`。时间轴、账本表、详情面板都只吃这本账。
 *
 * 纯函数,不渲染、不持状态。投影模型 / 交互参照 deepseek-harness
 * ui-trajectory(MIT)重写。
 */
import { ledgerRowsOf, type TrajectoryInput, type TrajectoryRow } from "../../api/trajectory_rows";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { absoluteSpans } from "./ledger_timing";
import { liveLedgerRows } from "./live_rows";
import type { Ledger, LedgerLane, LedgerRecord, LedgerRequest, LedgerTurn } from "./ledger_types";
import type { ConsoleTurn } from "./types";

export { absoluteSpans, lastKnownFrame } from "./ledger_timing";

/** 中栏过程条的行 id → 账本记录 id。过程条投影里每步是一条 `think:<seq>` 行,
 *  账本里同一步是 `assistant:<seq>`(spec §九「联动」);其余 id 两边同名。 */
export function ledgerRecordId(turnKey: string, rowId: string): string {
  const id = rowId.startsWith("think:") ? `assistant:${rowId.slice("think:".length)}` : rowId;
  return `${turnKey}/${id}`;
}

/** 行种类 → 泳道(spec §九 类型表)。显式 `Record<…>` 标注就是编译期穷尽闸:
 *  `TrajectoryRow` 新增一个 kind 而这里没跟上,`pnpm typecheck` 直接红。
 *  `memory` 这一格是占位 —— 真正的分道在 `laneOf` 里按 `direction` 走
 *  (召回进「输入」、写回进「工具」),永远走不到表里这一格。 */
const LANE_OF_KIND: Record<Exclude<TrajectoryRow["kind"], "think">, LedgerLane> = {
  user: 0, memory: 0,
  assistant: 1, reflect: 1, compaction: 1,
  tool: 2, subagent: 2, plan: 2,
  retry: 2, error: 2, approval: 2, guard: 2, gap: 2,
};

function laneOf(row: TrajectoryRow): LedgerLane {
  if (row.kind === "think") throw new Error(`ledger: think row ${row.id} must not reach the ledger projection`);
  if (row.kind === "memory") return row.direction === "recall" ? 0 : 2;
  return LANE_OF_KIND[row.kind];
}

function firstLine(text: string): string {
  const idx = text.indexOf("\n");
  return idx === -1 ? text : text.slice(0, idx);
}
function firstLineOrNull(text: string | null): string | null {
  if (text === null) return null;
  const line = firstLine(text);
  return line === "" ? null : line;
}
/** 记忆行的「→ 结果」= 首条记忆的正文首行(拿不到 → null)。 */
function firstMemoryLine(detail: Record<string, unknown>): string | null {
  const memories = detail.memories;
  if (!Array.isArray(memories) || memories.length === 0) return null;
  const first: unknown = memories[0];
  if (first === null || typeof first !== "object") return null;
  const content = (first as Record<string, unknown>).content;
  return typeof content === "string" ? firstLineOrNull(content) : null;
}

/** 工具行参数 JSON 的显示上限(spec §九「内容」)。 */
const TOOL_TEXT_MAX = 400;

/** 一行 → 内容列的正文与「→ 结果」预览(spec §九「内容」)。 */
function contentOf(row: TrajectoryRow): { text: string; resultText: string | null } {
  switch (row.kind) {
    case "think":
      throw new Error(`ledger: think row ${row.id} must not reach the ledger projection`);
    case "user":
    case "assistant":
      return { text: firstLine(row.text), resultText: null };
    case "tool":
      return {
        text: `${row.entry.toolName} ${JSON.stringify(row.entry.args)}`.slice(0, TOOL_TEXT_MAX),
        resultText: firstLineOrNull(row.entry.resultPreview),
      };
    case "plan": {
      const name = row.source === "update_plan" ? "update_plan" : "planner";
      const args = JSON.stringify({ goal: row.goal, steps: row.stepsTotal, reason: row.reason });
      return { text: `${name} ${args}`.slice(0, TOOL_TEXT_MAX), resultText: row.plan ? `${row.stepsTotal} 步` : null };
    }
    case "memory":
      // 正文留空:类型标签已经写着 MEMORY,内容列只值得放召回 / 写回的第一条。
      return { text: "", resultText: firstMemoryLine(row.detail) };
    case "reflect":
      return {
        text: row.verdict,
        resultText: typeof row.detail.critique === "string" ? firstLineOrNull(row.detail.critique) : null,
      };
    case "subagent": {
      const s = row.worker.summary;
      return {
        text: `${row.worker.label} ${row.worker.taskExcerpt}`.slice(0, TOOL_TEXT_MAX),
        resultText: s ? `${s.llmCallCount} 次模型调用 · ${s.wallClockMs}ms` : null,
      };
    }
    default:
      // 标记行(compaction / retry / error / approval / guard / gap)。
      return { text: firstLine(row.text), resultText: null };
  }
}

/** 未回放的历史轮那条占位 ASSISTANT 行 —— 没有任何一步落过帧,只能拿持久化的
 *  兜底答案首行凑一行,状态写在 `LedgerRecord.placeholder` 上。 */
function placeholderAssistantRow(turn: ConsoleTurn): TrajectoryRow {
  return {
    id: "assistant:placeholder", kind: "assistant", seq: -1, step: null,
    status: turn.loadState === "error" ? "error" : "ok",
    durationMs: null, eventIndexes: [], serverMs: null,
    text: turn.fallbackLines[0]?.text ?? "",
    reasoning: "", model: null, inputTokens: 0, outputTokens: 0,
    finishReason: null, toolCallCount: 0,
  };
}

function parseCreatedAt(createdAt: string | null): number | null {
  if (createdAt === null) return null;
  const ms = Date.parse(createdAt);
  return Number.isFinite(ms) ? ms : null;
}

/** 该记录开启 / 归属的请求 —— 建记录时边走边填(assistant 行先出,它的工具行
 *  紧随其后,所以一趟就能配上)。 */
interface RequestOwner { no: number; recordId: string }

type Span = { start: number; end: number } | null;

/** 一轮的行 → 一轮的记录(不含「最后一条运行中记录长到现在」那步全局修正)。 */
function turnRecordsOf(args: {
  turn: ConsoleTurn;
  rows: readonly TrajectoryRow[];
  /** `rows[liveFrom..]` 是 live 合成行(没有自己的时序)。 */
  liveFrom: number;
  spanOf: (row: TrajectoryRow) => Span;
  liveSpan: Span;
  startIndex: number;
  nextRequestNo: () => number;
  /** 未回放的历史轮 —— 只有那条占位 ASSISTANT 记录带 `placeholder`,
   *  同轮的 USER 记录照旧是正常记录;两条都不开 LLM 请求。 */
  placeholder: LedgerRecord["placeholder"];
}): LedgerRecord[] {
  const { turn, rows, liveFrom, spanOf, liveSpan, startIndex, nextRequestNo, placeholder } = args;
  const ownerBySeq = new Map<number, RequestOwner>();
  const ownerByStep = new Map<number, RequestOwner>();
  const toolRecordByEntryId = new Map<string, string>();
  const records: LedgerRecord[] = [];

  rows.forEach((row, i) => {
    const id = ledgerRecordId(turn.key, row.id);
    // live 行按 step 认领主人(它们的 seq 全是 -1,认 seq 会把不同步串到一起)。
    const owner = row.seq >= 0 ? ownerBySeq.get(row.seq) : row.step === null ? undefined : ownerByStep.get(row.step);

    let requestNo: number | null = null;
    let ownerRequestNo: number | null = null;
    let parentId: string | null = null;
    if (row.kind === "assistant" && placeholder === null) {
      requestNo = nextRequestNo();
      ownerRequestNo = requestNo;
      const own: RequestOwner = { no: requestNo, recordId: id };
      if (row.seq >= 0) ownerBySeq.set(row.seq, own);
      if (row.step !== null) ownerByStep.set(row.step, own);
    } else if (row.kind === "tool" || row.kind === "plan") {
      ownerRequestNo = owner?.no ?? null;
      parentId = owner?.recordId ?? null;
      if (row.kind === "tool") toolRecordByEntryId.set(row.entry.id, id);
    } else if (row.kind === "subagent") {
      ownerRequestNo = owner?.no ?? null;
      parentId = toolRecordByEntryId.get(row.parentEntryId) ?? null;
    }

    const span = i >= liveFrom ? liveSpan : spanOf(row);
    const { text, resultText } = contentOf(row);
    records.push({
      id, index: startIndex + i, turnKey: turn.key, turnSeq: turn.seq, runId: turn.runId,
      turnStart: i === 0, turnEnd: i === rows.length - 1,
      requestNo, ownerRequestNo, parentId,
      kind: row.kind, lane: laneOf(row),
      isError: row.status === "error", running: row.status === "running",
      startedAt: span === null ? null : span.start,
      endedAt: span === null ? null : span.end,
      text, resultText, row, events: turn.turn.events,
      placeholder: row.kind === "assistant" ? placeholder : null,
    });
  });
  return records;
}

function requestsOf(records: readonly LedgerRecord[]): LedgerRequest[] {
  // 请求号 → 它名下的 tool / plan 记录数(spec §九「请求 #N」的工具调用数)。
  const toolCallsByRequest = new Map<number, number>();
  for (const r of records) {
    if ((r.kind !== "tool" && r.kind !== "plan") || r.ownerRequestNo === null) continue;
    toolCallsByRequest.set(r.ownerRequestNo, (toolCallsByRequest.get(r.ownerRequestNo) ?? 0) + 1);
  }

  const requests: LedgerRequest[] = [];
  let cumInput = 0;
  let cumOutput = 0;
  for (const record of records) {
    if (record.requestNo === null || record.row.kind !== "assistant") continue;
    const row = record.row;
    cumInput += row.inputTokens;
    cumOutput += row.outputTokens;
    const { startedAt, endedAt } = record;
    requests.push({
      no: record.requestNo,
      turnKey: record.turnKey,
      turnSeq: record.turnSeq,
      step: row.step,
      recordId: record.id,
      status: record.running ? "running" : record.isError ? "error" : "ok",
      model: row.model,
      finishReason: row.finishReason,
      usage: {
        input: row.inputTokens, output: row.outputTokens,
        reasoning: row.reasoningTokens ?? 0, cacheRead: row.cacheReadTokens ?? 0,
      },
      cumulative: { input: cumInput, output: cumOutput },
      toolCalls: toolCallsByRequest.get(record.requestNo) ?? 0,
      startedAt, endedAt,
      durationMs: row.durationMs ?? (startedAt !== null && endedAt !== null ? endedAt - startedAt : null),
    });
  }
  return requests;
}

export function buildLedger(args: {
  /** 加载窗口内的轮,按 seq 升序。 */
  turns: readonly ConsoleTurn[];
  streamTurnKey: string | null;
  liveByStep?: ReadonlyMap<number, LiveStep>;
  /** 运行中尾块的 endedAt(调用方按 `lastKnownFrame` 校准过的服务端 now;
   *  没法校准传 `Date.now()`)。 */
  nowMs: number;
}): Ledger {
  const records: LedgerRecord[] = [];
  const turns: LedgerTurn[] = [];
  let requestCounter = 0;
  const nextRequestNo = (): number => {
    requestCounter += 1;
    return requestCounter;
  };

  for (const turn of args.turns) {
    const createdMs = parseCreatedAt(turn.createdAt);
    const unreplayed = turn.source === "history" && turn.loadState !== "done";
    const input: TrajectoryInput = {
      text: turn.turn.input,
      attachmentNames: turn.turn.attachments.map((a) => a.name),
      inputs: turn.turn.inputs ?? {},
    };

    let turnRecords: LedgerRecord[];
    if (unreplayed) {
      // 一帧都没回放过:USER + 一条占位 ASSISTANT,两条都钉在 createdAt。
      const at: Span = createdMs === null ? null : { start: createdMs, end: createdMs };
      const rows = [ledgerRowsOf([], input)[0], placeholderAssistantRow(turn)];
      turnRecords = turnRecordsOf({
        turn, rows, liveFrom: rows.length, spanOf: () => at, liveSpan: null,
        startIndex: records.length, nextRequestNo,
        placeholder: turn.loadState === "error" ? "error" : "loading",
      });
    } else {
      const base = ledgerRowsOf(turn.turn.events, input);
      const live = turn.key === args.streamTurnKey ? liveLedgerRows(turn.turn.events, args.liveByStep) : [];
      const rows = [...base, ...live];
      const liveFrom = base.length;
      const spans = absoluteSpans(rows, turn.turn.events, createdMs);
      // live 合成行没有自己的时序:从本轮最后一条有时序的记录接着长到「现在」。
      let lastEnd: number | null = null;
      for (let i = 0; i < liveFrom; i += 1) {
        const end = spans?.get(rows[i].id)?.end;
        if (end !== undefined) lastEnd = end;
      }
      turnRecords = turnRecordsOf({
        turn, rows, liveFrom,
        spanOf: (row) => spans?.get(row.id) ?? null,
        liveSpan: lastEnd === null ? null : { start: lastEnd, end: args.nowMs },
        startIndex: records.length, nextRequestNo, placeholder: null,
      });
    }

    turns.push({
      key: turn.key, seq: turn.seq, runId: turn.runId, status: turn.turn.status,
      firstIndex: records.length, lastIndex: records.length + turnRecords.length - 1,
      requestNos: turnRecords.map((r) => r.requestNo).filter((no): no is number => no !== null),
    });
    records.push(...turnRecords);
  }

  // 运行中的尾记录一直长到「现在」(呼吸块);只认最后一条,前面那些运行中的
  // 单元各有自己的时序。
  const lastRunning = records.reduce((acc, r, i) => (r.running ? i : acc), -1);
  const settled = lastRunning === -1
    ? records
    : records.map((r, i) => (i === lastRunning ? { ...r, endedAt: args.nowMs } : r));

  return {
    records: settled,
    requests: requestsOf(settled),
    turns,
    timed: settled.every((r) => r.startedAt !== null),
  };
}
