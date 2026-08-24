/**
 * ledger_types — the debug console's session-wide trajectory ledger types
 * (spec §九,PR-A.2). ``buildLedger`` (``ledger.ts``) folds the console's
 * ``ConsoleTurn[]`` into one ordered ``Ledger`` of records / LLM requests /
 * turns; the timeline (``ledger_timeline.ts``), search / collapse helpers,
 * the ledger table and the details panels all read these shapes. Pure types,
 * shared here so the parallel tasks that produce and consume them agree on
 * one definition.
 */
import type { SseEvent } from "../../api/sessions";
import type { TrajectoryRow } from "../../api/trajectory_rows";

/** 0 输入 / 1 模型 / 2 工具(泳道纵向顺序)。 */
export type LedgerLane = 0 | 1 | 2;

export interface LedgerRecord {
  /** 会话内唯一:`${turnKey}/${row.id}`。 */
  id: string;
  /** 0-based,账本顺序(加载窗口内)。 */
  index: number;
  turnKey: string;
  /** `ConsoleTurn.seq`(0-based;显示 +1)。 */
  turnSeq: number;
  runId: string | null;
  turnStart: boolean;
  turnEnd: boolean;
  /** 该记录开启的 LLM 请求号(1-based,窗口内递增);只有 assistant 记录非 null。 */
  requestNo: number | null;
  /** 所属请求号:assistant 自己;同步的 tool / plan / subagent 取父 assistant 的;其余 null。 */
  ownerRequestNo: number | null;
  /** tool / plan → 同步 assistant 记录 id;subagent → 父 tool 记录 id;
   *  reflect / memory 写回 → 同轮之前最近的 assistant 记录 id(仅供详情层级
   *  链接,折叠不把它们当子调用);其余 null。 */
  parentId: string | null;
  /** = `row.kind`(方便时间轴 / 搜索 / 样式直接读;think 永不出现)。 */
  kind: TrajectoryRow["kind"];
  lane: LedgerLane;
  isError: boolean;
  running: boolean;
  /** 绝对服务端毫秒起止;拿不到 null。 */
  startedAt: number | null;
  endedAt: number | null;
  /** ASSISTANT 记录的首 token 绝对时刻(`startedAt + row.firstTokenMs`,夹到
   *  ≤ endedAt);其它 kind / 缺数据 null。 */
  firstTokenAt: number | null;
  /** 内容列正文(工具 = `名字 参数JSON`,截 400 字符);assistant 没文字时 ""。 */
  text: string;
  /** 「→ 结果」预览首行;无 null。 */
  resultText: string | null;
  row: TrajectoryRow;
  /** 该轮全部帧(原始 tab / Timing 用)。 */
  events: readonly SseEvent[];
  /** 未回放的轮的占位 assistant 记录;正常记录 null。 */
  placeholder: null | "loading" | "error";
}

export interface LedgerRequest {
  no: number;
  turnKey: string;
  turnSeq: number;
  step: number | null;
  /** 该请求的 assistant 记录 id。 */
  recordId: string;
  status: "ok" | "error" | "running";
  model: string | null;
  finishReason: string | null;
  usage: { input: number; output: number; reasoning: number; cacheRead: number };
  /** 窗口内到本请求为止的累计。 */
  cumulative: { input: number; output: number };
  toolCalls: number;
  startedAt: number | null;
  endedAt: number | null;
  durationMs: number | null;
  /** 该请求的 assistant 步首 token 毫秒(`AssistantRow.firstTokenMs`);没有 null。 */
  firstTokenMs: number | null;
}

export interface LedgerTurn {
  key: string;
  seq: number;
  runId: string | null;
  status: "running" | "done" | "error" | "interrupted";
  firstIndex: number;
  lastIndex: number;
  requestNos: number[];
}

export interface Ledger {
  records: LedgerRecord[];
  requests: LedgerRequest[];
  turns: LedgerTurn[];
  /** 每条记录都有 startedAt(时长投影可用)。 */
  timed: boolean;
}
