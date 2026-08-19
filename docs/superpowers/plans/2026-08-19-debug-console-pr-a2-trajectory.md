# 调试台 PR-A.2 —— 轨迹视图对齐 deepseek-harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec §九(2026-08-19 修订)把调试台的「轨迹」做成 deepseek-harness `ui-trajectory` 同款:整会话账本视图(工具条 + 概览时间轴 + 账本 + 右侧详情),从右栏搬到中栏「对话 | 轨迹 | 工作区」tab,右栏检查面板退役。

**Architecture:** 全部在 `apps/admin-ui`。数据层三个纯模块:`api/trajectory_rows.ts` 新增按步的 `ASSISTANT` 投影(`ledgerRowsOf`),`components/console/ledger.ts` 把整会话的 `ConsoleTurn[]` 折成 `Ledger`(记录 / 请求 / 轮 + 绝对时序),`components/console/ledger_timeline.ts` 是 deepseek `timeline.ts` 投影模型的重写(顺序 / 时长、压空档、选区求交、缩放视口)。UI 层五个组件:`TrajectoryToolbar` / `TrajectoryTimeline` / `TrajectoryLedger`(虚拟化表格)/ `DetailsFrame` + `RecordDetails` + `RequestDetails` / `TrajectoryView`(状态源)。`ConsoleShell` 变两栏,`PlaygroundTab` 接三视图 tab;`useHistoryTurns` 加按页回放 `loadRuns`。后端零改动。

**Tech Stack:** React 18 + antd 5.29 + lucide-react + react-i18next(`console.*`)+ vitest/RTL + Playwright(e2e)。参照源码(MIT):`/Users/mac/src/github/deepseek-harness/packages/client/ui-trajectory/src/client/{timeline.ts,TrajectoryTimeline.tsx,TrajectoryTimeline.module.css,TrajectoryToolbar.tsx,TrajectoryToolbar.module.css,TrajectoryTable.tsx,TrajectoryTable.module.css}` ——**只读参照、按我们的数据层与代码规范重写,不复制粘贴**;模块 docstring 写一句「投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写」。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` **§九**(与 §二.1 / §八.6–8 冲突处以 §九为准)。设计稿(已确认)= §九 形态 + 交互样例 A–F + 逐项对照表 + 拍板 D1–D6。

## Global Constraints

- **既有 `it` 一条都不静默删除**(在 `f2fd14f6` 上 `grep -c "^\s*it("`):`api/__tests__/trajectory_rows.test.ts` 14 / `api/__tests__/trace_match.test.ts` 7 / `api/__tests__/gantt_timeline.test.ts` 8 / `pages/agent_detail/playground/__tests__/history_turns.test.ts` 7 / `components/turn/__tests__/useHistoryTurns.test.ts` 6 / `console_turns.test.ts` 3 / `live_rows.test.ts` 6 / `ConsoleShell.test.tsx` 3 / `RowDetailTiming.test.tsx` 4 / `TurnFooter.test.tsx` 6 / `ProcessStrip.test.tsx` 7 / `PlaygroundTab.test.tsx` 47。**整文件退役**的测试(`LaneStrip.test.tsx` 15 / `lane_strip_model.test.ts` 8 / `TrajectoryRows.test.tsx` 13 / `TrajectoryPanel.test.tsx` 14 / `RowDetail.test.tsx` 6 / `InspectPanel.test.tsx` 4)随源文件一起删,但 Task 11 的报告里要附一张「退役 it → 新家(哪个新测试文件的哪条覆盖了同一行为)/ 行为已不存在(依据 §九 哪句)」的对照表,控制器核。行为**有意改变**的条目改断言并在报告里逐条列「旧 → 新 → 依据(§九 哪句)」。
- **e2e 也是行为清单**:改 testid / 删按钮 / 换文案前 `grep -rn '<testid>' apps/admin-ui/e2e/`,命中的 spec 一起改;Task 11 本地跑 `pnpm exec playwright test e2e/playground-upload.spec.ts e2e/session-history.spec.ts`(浏览器已装)。
- **testid**:沿用的名字不改(`console-lane-strip` / `console-lane-block` / `console-lane-mode` / `console-traj-row` / `console-detail-header` / `console-detail-close` / `console-detail-tab-*` / `console-detail-summary|payload|result|timing|raw` / `console-inspect-run-link` / `playground-turn-langfuse` / `console-turn-inspect` / `playground-tab`);新增一律 `console-*`,每个 task 的 testid 表在任务里列全,**别自造**。
- **i18n**:新键只加 `console.*`,三处同步(`en.ts` 接口块 `console: {` + `en.ts` 值块 + `zh-CN.ts` 值块),先 grep 是否撞既有;新键**追加在 `console` 块末尾**(同波并行,控制器合并时两边都留)。删除的键只在 Task 11 做,并给出无消费者的 grep 证据。中文正文全角标点。
- **纯函数进 `api/` 或 `components/console/*.ts`**;组件进 `components/console/`;单文件 ≤ 400 行,超了拆(账本 / 时间轴 / 详情各自允许一个 `.css` 伴随文件)。
- **不动**:`TurnCard.tsx`、`TraceView.tsx`、`GanttTimeline.tsx`、`StepTimeline.tsx`、`EventCard.tsx`、`ConversationDetail.tsx`、`RunDetail.tsx`、`components/turn/*`(除 Task 1/6 点名的 `types.ts` / `useHistoryTurns.ts`)、中栏(`Transcript` / `TurnBlock` / `ProcessStrip` / `CompactRow` / `TurnFooter` 只允许 Task 11 改回调语义,不改 UI)、`StatsBar`、`SessionSidebar*`、`Composer`。`api/timeline.ts` 不动。
- **颜色只用 `--ew-*` 令牌**(§九 类型表),浅 / 深主题都要可见(选中 / 悬停描边不用纯白)。
- **测试渲染**:用到 `App.useApp()` 的组件测试包 antd `<App>`;`Tooltip` 在 jsdom 下 `mouseover` + `findByRole("tooltip")`;jsdom 没有 `scrollIntoView` / `setPointerCapture` / `ResizeObserver` —— 组件里一律可选调用(`el.scrollIntoView?.()`),测试里需要几何的地方 mock `getBoundingClientRect` / `clientHeight` / `scrollTop`。**指针捕获只在真的开始拖(位移过阈值)之后拿**(PR-A.1 Task 5 的坑:按下就捕获会吃掉块的 click),并用 spy 断言钉住。
- **前端命令**(在 `apps/admin-ui`):`pnpm exec vitest run <file>`、`pnpm typecheck`(必须用它,裸 `tsc --noEmit` 恒绿)、`pnpm build`、`pnpm build-storybook`、`pnpm exec playwright test <spec>`。**无 eslint**。
- **特性分支** `feat/debug-console-pr-a2`,worktree `.worktrees/debug-console-pr-a2`,基于 `origin/main`(`f2fd14f6`)。SDD 台账 `.superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/progress.md`。
- **并行波次**(每个 task 一个 worktree `.worktrees/pr-a2-t<N>`、分支 `feat/pr-a2-t<N>`,从特性分支当前 HEAD 切;波末控制器按 task 号 `git merge --no-ff`,合完跑 `pnpm typecheck && pnpm exec vitest run && pnpm build`):

| 波 | task | 依赖 / 文件不相交说明 |
|---|---|---|
| 0 | 1(控制器内联) | 共享类型 + `createdAt` + `originMs`,先落地 |
| 1 | 2 · 3 · 4 · 5 · 6 | T2 `api/trajectory_rows.ts` + `live_rows.ts` + `ledger.ts` + `api/trace_match.ts`;T3 `ledger_timeline.ts`;T4 `ledger_search.ts` + `ledger_collapse.ts` + `use_virtual_rows.ts`;T5 `TrajectoryToolbar`;T6 `useHistoryTurns` + `ConsoleShell` |
| 2 | 7 · 8 · 9 | T7 `TrajectoryTimeline`(需 3);T8 `TrajectoryLedger`(需 2 · 4);T9 `DetailsFrame` + `RecordDetails` + `RequestDetails`(需 2;改 `RowDetailPayloadResult` / `RowDetailTiming`) |
| 3 | 10 | `TrajectoryView`(需 5 · 7 · 8 · 9) |
| 4 | 11 | `PlaygroundTab` 接线 + 退役 + 测试 / e2e + 全门 |
| — | 12 | 合并后发布 + 真栈探针 |

  并行规矩:task 内只跑定点测试 + `pnpm typecheck`;评审包 `<切出时特性分支 HEAD>..<task 分支 HEAD>`;task 报告写到台账目录 `task-N-report.md`。

---

## 文件结构

| 文件 | 责任 | Task |
|---|---|---|
| `components/console/ledger_types.ts`(新) | `LedgerRecord` / `LedgerRequest` / `LedgerTurn` / `Ledger` / `LedgerLane` 共享类型 | 1 |
| `components/turn/types.ts` · `pages/agent_detail/playground/history_turns.ts` · `components/console/types.ts` · `console_turns.ts` | `HistoryTurn.createdAt` / `ConsoleTurn.createdAt` | 1 |
| `api/gantt_timeline.ts` | `GanttModel.originMs`(绝对 t0) | 1 |
| `api/trajectory_rows.ts` | 新 `AssistantRow`(按步)+ `ledgerRowsOf` | 2 |
| `components/console/live_rows.ts` | `liveLedgerRows` | 2 |
| `components/console/ledger.ts`(新) | `buildLedger` / `ledgerRecordId` / `absoluteSpans` | 2 |
| `api/trace_match.ts` | Rule 2 也认按步 assistant 行 | 2 |
| `components/console/ledger_timeline.ts`(新) | 投影模型 / 选区求交 / 视口缩放 | 3 |
| `components/console/ledger_search.ts`(新) | 搜索匹配集 | 4 |
| `components/console/ledger_collapse.ts`(新) | 折叠轮 / 折叠调用 → 显示行 | 4 |
| `components/console/use_virtual_rows.ts`(新) | 定高虚拟化 | 4 |
| `components/console/TrajectoryToolbar.tsx` + `trajectory_toolbar.css`(新) | 时长 / 轮次 / 调用 / 搜索 | 5 |
| `components/turn/useHistoryTurns.ts` | `loadRuns` | 6 |
| `components/console/ConsoleShell.tsx` + `console.css` | `inspect` 可选 → 两栏 | 6 |
| `components/console/TrajectoryTimeline.tsx` + `trajectory_timeline.css`(新) | 概览时间轴 | 7 |
| `components/console/TrajectoryLedger.tsx` + `trajectory_ledger.css`(新) | 账本 | 8 |
| `components/console/DetailsFrame.tsx` + `RecordDetails.tsx` + `RequestDetails.tsx` + `record_details.css`(新);`RowDetailPayloadResult.tsx` / `RowDetailTiming.tsx`(改) | 详情 | 9 |
| `components/console/TrajectoryView.tsx` + `trajectory_view.css`(新) | 轨迹视图状态源与组合 | 10 |
| `pages/agent_detail/PlaygroundTab.tsx` | 三视图 tab、两栏、联动、按页加载 | 11 |
| 退役:`InspectPanel.tsx` / `TrajectoryPanel.tsx` / `LaneStrip.tsx` + `lane_strip.css` + `lane_strip_model.ts` / `TrajectoryRows.tsx` + `trajectory_rows.css` / `RowDetail.tsx` + 各自测试;`trajectoryRowsOf` 旧签名;死 i18n 键 | 11 |
| `pages/__tests__/PlaygroundTab.test.tsx`、`e2e/playground-upload.spec.ts` | 断言更新 | 11 |
| `i18n/locales/en.ts` / `zh-CN.ts` | `console.*` 新键三处 | 各 task |

---

### Task 1: 共享类型 + `createdAt` + `originMs`(控制器内联,不派发)

**Files:**
- Create: `apps/admin-ui/src/components/console/ledger_types.ts`
- Modify: `components/turn/types.ts`(`HistoryTurn.createdAt: string | null`)、`pages/agent_detail/playground/history_turns.ts`(`buildHistoryTurns` 填 `runs[i].createdAt`)、`components/console/types.ts`(`ConsoleTurn.createdAt: string | null`)、`components/console/console_turns.ts`(history 填 `h.createdAt`,live 填 `null`)、`api/gantt_timeline.ts`(`GanttModel.originMs: number` = 内部 `t0`;无行时 `0`)
- Test: `history_turns.test.ts`(+1:createdAt 透传)、`console_turns.test.ts`(+1)、`gantt_timeline.test.ts`(+1:`originMs + rows[i].startMs === 该行绝对起点`)

**Produces:**

```ts
// components/console/ledger_types.ts
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
  /** tool / plan → 同步 assistant 记录 id;subagent → 父 tool 记录 id;其余 null。 */
  parentId: string | null;
  /** = `row.kind`(方便时间轴 / 搜索 / 样式直接读;think 永不出现)。 */
  kind: TrajectoryRow["kind"];
  lane: LedgerLane;
  isError: boolean;
  running: boolean;
  /** 绝对服务端毫秒起止;拿不到 null。 */
  startedAt: number | null;
  endedAt: number | null;
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
}

export interface LedgerTurn {
  key: string;
  seq: number;
  runId: string | null;
  status: "running" | "done" | "error";
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
```

- [ ] 写类型文件 + 三处 `createdAt` + `originMs`,补三条测试,`pnpm typecheck` + 定点测试绿,commit `chore(console): PR-A.2 地基 —— 账本共享类型 / createdAt / GanttModel.originMs`。

---

### Task 2: 按步 ASSISTANT 投影 + 账本构建器 + trace 配对

**Files:**
- Modify: `apps/admin-ui/src/api/trajectory_rows.ts`、`components/console/live_rows.ts`、`api/trace_match.ts`
- Create: `components/console/ledger.ts`
- Test: `api/__tests__/trajectory_rows.test.ts`(14 → ≥ 20)、`components/console/__tests__/live_rows.test.ts`(6 → ≥ 8)、`api/__tests__/trace_match.test.ts`(7 → ≥ 8)、`components/console/__tests__/ledger.test.ts`(新,≥ 12)

**Interfaces:**
- Consumes: `parseTimeline`(`api/timeline.ts`)、`buildGanttRows`(含 Task 1 的 `originMs`)、`ConsoleTurn`(含 `createdAt`)、`LiveStep`、`ledger_types.ts`。
- Produces:

```ts
// api/trajectory_rows.ts —— AssistantRow 换成按步形态(旧的 `{kind:"assistant"; text}` 只此一处消费者 = 旧 trajectoryRowsOf,本 task 同步改它填新字段)
export type AssistantRow = RowBase & {
  kind: "assistant";
  /** 该步正文;没有 ""。 */
  text: string;
  /** 该步思考;没有 ""。 */
  reasoning: string;
  model: string | null;
  inputTokens: number; outputTokens: number;
  reasoningTokens?: number; cacheReadTokens?: number;
  finishReason: string | null;
  /** 该步发起的工具调用数(含 update_plan)。 */
  toolCallCount: number;
};
/** 账本投影:`user` + 每个 agent 步一条 `assistant`(id `assistant:${seq}`,`step`=stepCount,`seq`=item.seq,`status`=hasError?error:ok,`durationMs`/`serverMs`/`eventIndexes` 同原 think 行)+ 其余紧凑行(tool / plan / subagent / memory / reflect / marker,与 compactRowsOf 同源同 id)。**不再有** think 行、也不再有末尾合成 assistant 行。 */
export function ledgerRowsOf(events: readonly SseEvent[], input: TrajectoryInput): TrajectoryRow[];
// 旧 trajectoryRowsOf(events, input, answer, turnStatus) 保留到 Task 11 删除;本 task 让它的末尾 assistant 行填新字段(reasoning "" / model null / tokens 0 / toolCallCount 0)。

// components/console/live_rows.ts
/** 账本用的 live 合成行:每个未落帧的步 **总是**一条 assistant(id `live-assistant:${step}`,seq -1,step,status running,text=live.content,reasoning=live.reasoning,tokens 0,toolCallCount=live.toolNames.size)+ 每个已命名工具一条 tool(同 liveSyntheticRows)。 */
export function liveLedgerRows(events: readonly SseEvent[], liveByStep: ReadonlyMap<number, LiveStep> | undefined): TrajectoryRow[];

// api/trace_match.ts —— Rule 2 的「think 行」扩成「think 行 或 step 非 null 的 assistant 行」(旧的末尾 assistant 行 step 为 null,不受影响)。

// components/console/ledger.ts
export function ledgerRecordId(turnKey: string, rowId: string): string;   // `${turnKey}/${rowId}`;rowId 以 `think:` 开头时换成 `assistant:`(中栏过程条的 id → 账本 id)
/** 一轮的行 → 绝对起止(服务端 ms)。gantt `degraded` → null。有 gantt 时序的行取 `originMs + startMs` 起、`+ durationMs` 止(同一行多条 gantt 行取并集,`resolveGanttKey` 映射);没有 gantt 命中但有 `serverMs` 的行 → 点 `[serverMs, serverMs]`;user 行 → 本轮最早起点(没有任何有时序的行时 → `fallbackStart` 或 null)。 */
export function absoluteSpans(rows: readonly TrajectoryRow[], events: readonly SseEvent[], fallbackStart: number | null): Map<string, { start: number; end: number }> | null;
export function buildLedger(args: {
  turns: readonly ConsoleTurn[];                 // 加载窗口内的轮,按 seq 升序
  streamTurnKey: string | null;
  liveByStep?: ReadonlyMap<number, LiveStep>;
  /** 运行中尾块的 endedAt(调用方按 lastKnownFrame 校准过的服务端 now;没法校准传 Date.now()) */
  nowMs: number;
}): Ledger;
/** 从 lane_strip_model 原样搬来(该文件 Task 11 删):最后一条带合法 id 的帧的 (serverMs, receivedAtMs)。 */
export function lastKnownFrame(events: readonly SseEvent[]): { serverMs: number; receivedAtMs: number } | null;
```

**行为(`buildLedger`)**:
- 逐轮:`loadState !== "done"` 且 `source === "history"` → 两条记录:USER(`row` = `ledgerRowsOf([], input)[0]`,`text` = 输入首行)+ 占位 ASSISTANT(`row` = 合成 `AssistantRow{id:"assistant:placeholder", text: fallbackLines 首行文本, …}`,`placeholder` = loadState `error` → `"error"`,否则 `"loading"`),两条 `startedAt = endedAt = Date.parse(createdAt)`(解析失败 null)。否则 `rows = ledgerRowsOf(events, {text: turn.turn.input, attachmentNames, inputs})`,再拼 `liveLedgerRows(events, liveByStep)`(仅 `turn.key === streamTurnKey`)。
- 每行 → 记录:`lane` 按 §九 类型表(memory 按 direction 分道;think 永不出现——若出现视为 bug,`throw`);`isError` = `row.status === "error"`;`running` = `row.status === "running"`;`text` / `resultText` 按 §九「内容」规则(tool: `${entry.toolName} ${JSON.stringify(entry.args)}`.slice(0,400),resultText = 结果首行 或 null;plan(update_plan): `update_plan ${JSON.stringify({goal, steps: stepsTotal, reason})}`,resultText = `row.plan` 有则 `${stepsTotal} 步` 摘要否则 null;memory: text = 空串、resultText = 首条 memory content 首行;reflect: text = verdict、resultText = critique 首行;compaction/marker: text = row.text;user: text = 输入首行;assistant: text = 正文首行,resultText null;subagent: text = `${worker.label} ${taskExcerpt}`,resultText = summary 有则 `${llmCallCount} 次模型调用 · ${wallClockMs}ms` 否则 null);`startedAt/endedAt` 来自 `absoluteSpans(rows, events, Date.parse(createdAt))`,运行中的尾行(最后一条 `running` 记录)`endedAt = nowMs`;live 合成行(seq -1)没有时序 → 取本轮最后一条有时序记录的 `endedAt` 作起点、`nowMs` 作终点。
- 请求:每条非占位 assistant 记录一个 `LedgerRequest`(`no` 从 1 递增;`usage` 从 row tokens;`cumulative` 累加;`toolCalls` = 同轮 `seq` 相同(或 live 时 `step` 相同)的 tool + plan 行数;`status` = running / error / ok;时序同记录);记录的 `requestNo` = 该请求 no;同轮同 seq(或同 step)的 tool / plan / subagent 记录 `ownerRequestNo` = 之;`parentId`:tool / plan → 该 assistant 记录 id,subagent → `entry.id === row.parentEntryId` 的 tool 记录 id。
- `turnStart` = 该轮首条,`turnEnd` = 该轮末条;`turns[]` 每轮一条;`timed` = 全部记录 `startedAt !== null`。

- [ ] **Step 1: 写失败测试**

`trajectory_rows.test.ts`(新增;fixture 沿用现有的多步多工具 EVENTS):
```ts
it("ledgerRowsOf: user first, then one assistant per agent step (id assistant:<seq>, step, text=content, reasoning, tokens, finishReason, toolCallCount)", …);
it("ledgerRowsOf: no think rows and no trailing synthetic assistant", …);
it("ledgerRowsOf: tools / plan / subagent / memory / reflect / marker rows keep the same ids as compactRowsOf", …);
it("ledgerRowsOf: a step with no content yields text '' (caller renders tool-call-only)", …);
it("ledgerRowsOf: step error → assistant status error", …);
it("trajectoryRowsOf (legacy) still returns the trailing assistant with the new fields zeroed", …);
```
`live_rows.test.ts`(+2):`liveLedgerRows` 每个未落帧步一条 assistant(即使 reasoning 为空)+ 命名工具;已落帧的步不出。
`trace_match.test.ts`(+1):按步 assistant 行与 main llm span 按序配对;count 不等全 mismatch。
`ledger.test.ts`(fixture:两轮 history done + 一轮 live running;`ConsoleTurn` 手工造):
```ts
it("records are in turn order with turnStart/turnEnd and 0-based index", …);
it("assistant records get requestNo 1..n across turns; tools/plans/subagents get ownerRequestNo and parentId", …);
it("requests carry usage, cumulative sums, toolCalls and status", …);
it("lane by kind: user/memory-recall → 0, assistant/reflect/compaction → 1, tool/subagent/plan/memory-writeback/markers → 2", …);
it("tool text is `name argsJSON` (≤400 chars) and resultText the first result line; error rows are isError", …);
it("startedAt/endedAt come from gantt absolute spans; user takes the turn's earliest start; timed=true when every record is timed", …);
it("a turn whose gantt is degraded → its records have null times and ledger.timed=false", …);
it("history turn not yet replayed → USER + placeholder assistant (loading/error) at createdAt", …);
it("live turn: unsettled steps append live-assistant/live-tool records with running=true and endedAt=nowMs", …);
it("ledgerRecordId maps think:<seq> to assistant:<seq>", …);
it("lastKnownFrame returns the last frame with a parseable id", …);
```

- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现**(`rowsOf` 加 `projection: "compact" | "ledger"` 分支;`AssistantRow` 类型换新;`ledger.ts` ≤ 400 行,超了把 `absoluteSpans` + `lastKnownFrame` 拆到 `ledger_timing.ts`)。
- [ ] **Step 4: 跑绿** + `pnpm typecheck`(`RowDetail.tsx` / `RowDetailPayloadResult.tsx` / `TrajectoryPanel.tsx` 等旧消费者对 `AssistantRow` 的用法只读 `text`,类型加字段不破;若 typecheck 报旧文件,只做最小适配并在报告注明)。
- [ ] **Step 5: Commit** `feat(console): 账本数据层 —— 按步 ASSISTANT 投影 / buildLedger / trace 配对扩展`。

---

### Task 3: 时间轴投影模型 `ledger_timeline.ts`

**Files:**
- Create: `apps/admin-ui/src/components/console/ledger_timeline.ts`
- Test: `components/console/__tests__/ledger_timeline.test.ts`(新,≥ 12)
- 参照:deepseek `timeline.ts`(全文 200 行)与 `TrajectoryTimeline.tsx` 里的 `centeredRange` / `rangeFraction` / wheel 缩放 / 边缘平移常量。

**Produces:**

```ts
export type TimelineMode = "sequence" | "duration";
export interface TimeRange { start: number; end: number }
/** 只依赖这几个字段,`LedgerRecord` 结构上满足。 */
export interface TimelineSpanInput {
  index: number; lane: LedgerLane; kind: TrajectoryRow["kind"]; isError: boolean; running: boolean;
  turnSeq: number; turnStart: boolean; startedAt: number | null; endedAt: number | null;
}
export interface TimelineSpan extends TimeRange { index: number; lane: LedgerLane; kind: TrajectoryRow["kind"]; isError: boolean; running: boolean }
export interface TimelineModel extends TimeRange {
  mode: TimelineMode;
  spans: TimelineSpan[];
  turnBoundaries: { turnSeq: number; time: number }[];   // 每轮首条记录的 time(首轮也含,渲染时过滤 time > start)
  /** 要求 duration 但有记录没时序 → 按 sequence 排布并置 true。 */
  degraded: boolean;
}
export function deriveTimeline(records: readonly TimelineSpanInput[], mode: TimelineMode): TimelineModel | null;  // 无记录 → null
export function focusIndexes(model: TimelineModel, range: TimeRange): ReadonlySet<number>;   // span.start <= range.end && span.end >= range.start
export function nearestSpan(model: TimelineModel, time: number): TimelineSpan | null;
export function minimumSelection(model: TimelineModel, domainDuration: number): number;    // min(domainDuration, (end-start)/spans.length)
export function orderedRange(a: number, b: number): TimeRange;
export function centeredRange(center: number, width: number, min: number, max: number): TimeRange;
export function clampFraction(v: number): number;
/** 滚轮:以 anchorFraction 为锚按 exp(deltaY*0.0015) 缩放当前视口(null=全景);最小宽度 sequence 4 / duration 20;≥ 全景 99.9% → null。 */
export function zoomViewport(model: TimelineModel, viewport: TimeRange | null, anchorFraction: number, deltaY: number): TimeRange | null;
/** 平移:视口整体挪 deltaDomain,夹在 [model.start, model.end]。 */
export function panViewport(model: TimelineModel, viewport: TimeRange, deltaDomain: number): TimeRange;
/** 选中项在视口外时把视口挪到能看见它(deepseek 的 selectedIndex effect):在里面 → 原样返回。 */
export function revealInViewport(model: TimelineModel, viewport: TimeRange | null, index: number): TimeRange | null;
export function formatClock(ms: number): string;      // HH:MM:SS.mmm(本地时区)
```

**行为**:
- sequence:`spans[i] = { start: i, end: i+1 }`(按 records 顺序),`start 0 / end n`,`turnBoundaries` = 每个 `turnStart` 记录的 i。
- duration:任一 `startedAt === null` → 退到 sequence 排布 + `degraded: true`(`mode` 仍报 `"duration"`);否则原始 span `[startedAt, endedAt ?? startedAt]`,按 start 排序压空档:`coveredUntil` 之后才开始的 span 把 `start - coveredUntil` 累进 `removedIdle`,每个 span 减去它自己的累计;`turnBoundaries` = 每轮最小 start(压过的);`start = min`,`end = max`。
- `zoomViewport` 常量与 deepseek 一致(`0.0015`、`MINIMUM_ZOOM_OPERATIONS 4`、时长 20)。

- [ ] **Step 1: 写失败测试**(fixture 手造 `TimelineSpanInput[]`,三轮 9 条):
```ts
it("sequence: unit spans, boundaries at each turnStart, start 0 end n", …);
it("duration: spans use absolute times, idle gaps between spans are removed, boundaries at each turn's first start", …);
it("duration: overlapping spans (parallel tools) keep their overlap after compression", …);
it("duration with a null startedAt → sequence layout + degraded=true", …);
it("focusIndexes returns spans intersecting the inclusive range (a point span on the boundary counts)", …);
it("nearestSpan picks the closest by distance to [start,end]", …);
it("minimumSelection = min(domain, full/spans)", …);
it("centeredRange clamps to [min,max] keeping width", …);
it("zoomViewport: wheel down zooms in around the anchor; below the minimum width clamps; zooming out past 99.9% returns null", …);
it("panViewport clamps to the model bounds", …);
it("revealInViewport: keeps the viewport when the span is visible, else moves the minimal distance", …);
it("formatClock renders HH:MM:SS.mmm", …);
```
- [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**(纯函数,无 React,无 i18n)。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 时间轴投影模型 —— 顺序 / 时长压空档 / 选区求交 / 缩放视口`。

---

### Task 4: 搜索 / 折叠 / 虚拟化三个小模块

**Files:**
- Create: `components/console/ledger_search.ts`、`ledger_collapse.ts`、`use_virtual_rows.ts`
- Test: `__tests__/ledger_search.test.ts`(≥ 4)、`__tests__/ledger_collapse.test.ts`(≥ 7)、`__tests__/use_virtual_rows.test.ts`(≥ 4,`renderHook`)

**Produces:**

```ts
// ledger_search.ts
/** 空白查询 → null;否则按空白切词,每个词都得命中(大小写不敏感)才算;命中域 = kind 大写名(TOOL / ASSISTANT …,subagent 记 SUBTOOL,compaction 记 COMPACTED)+ text + resultText + (tool 的 entry.toolName / server)。返回记录 index 集合。 */
export function searchLedger(records: readonly LedgerRecord[], query: string): ReadonlySet<number> | null;

// ledger_collapse.ts
import type { ProcessSummary } from "./process_summary";
export type DisplayRow =
  | { kind: "record"; record: LedgerRecord }
  | { kind: "turn-summary"; turnKey: string; turnSeq: number; runId: string | null; summary: ProcessSummary; hasError: boolean; anchorIndex: number }
  | { kind: "calls-summary"; ownerId: string; turnKey: string; count: number; toolBreakdown: string };
/** 一轮记录的过程摘要:assistant 计入 think 数(§九:一步一条 ASSISTANT 顶替 THINK),tool 计 tools 与 toolBreakdown,其余非 user 记 other;failed = 非 assistant 的 isError 数;durationMs = 各记录 (endedAt-startedAt) 之和(都没有 → null)。 */
export function turnSummaryOf(records: readonly LedgerRecord[]): ProcessSummary;
/** 折叠规则:有 `matches` → 只留 index ∈ matches 的记录行,不折叠;否则 collapsedTurns 里的轮 → 该轮 USER 记录 + 一条 turn-summary;collapsedOwners 里的 assistant → 它本身 + 一条 calls-summary(它的 tool/plan/subagent 子记录不出)。 */
export function displayRowsOf(ledger: Ledger, opts: { collapsedTurns: ReadonlySet<string>; collapsedOwners: ReadonlySet<string>; matches: ReadonlySet<number> | null }): DisplayRow[];
/** 有 ≥ 2 条非 user 记录的轮 key。 */
export function collapsibleTurnKeys(ledger: Ledger): string[];
/** 有 ≥ 1 个子记录(parentId 指向它,或子记录的父 tool 属于它)的 assistant 记录 id。 */
export function collapsibleOwnerIds(ledger: Ledger): string[];

// use_virtual_rows.ts
export interface VirtualWindow { start: number; end: number; topPad: number; bottomPad: number }
/** 定高窗口:监听 scrollRef 的 scroll + ResizeObserver(没有则 window resize),`start = floor(scrollTop/rowHeight) - overscan`,`end = ceil((scrollTop+clientHeight)/rowHeight) + overscan`,夹在 [0,count];clientHeight 为 0(jsdom / 未挂载)时窗口 = 全部。 */
export function useVirtualRows(args: { scrollRef: React.RefObject<HTMLElement | null>; count: number; rowHeight: number; overscan?: number }): VirtualWindow;
```

- [ ] **Step 1: 写失败测试**(fixture 复用 Task 2 的造法——为了不跨 task 依赖,测试里手造最小 `LedgerRecord`,只填用到的字段并 `as LedgerRecord`):
```ts
// search
it("blank / whitespace query → null", …);
it("matches are case-insensitive over kind label, text, resultText and tool name; every term must hit", …);
it("subagent matches SUBTOOL and compaction matches COMPACTED", …);
it("no hit → empty set (not null)", …);
// collapse
it("no collapse → one record row per record in order", …);
it("collapsed turn → USER row + one turn-summary with process counts / failed / duration", …);
it("collapsed owner → assistant row + one calls-summary listing children by tool name ×count; children hidden", …);
it("matches present → only matching records, collapse ignored", …);
it("turnSummaryOf counts assistant as think and excludes assistant errors from failed", …);
it("collapsibleTurnKeys excludes single-record turns; collapsibleOwnerIds lists assistants with children", …);
it("subagent under a collapsed owner is hidden too (parent tool belongs to that owner)", …);
// virtual
it("clientHeight 0 → whole range", …);
it("windows by scrollTop with overscan and pads", …);
it("count shrink clamps the window", …);
it("scroll event updates the window", …);
```
- [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 账本搜索 / 折叠 / 定高虚拟化`。

---

### Task 5: 工具条 `TrajectoryToolbar`

**Files:**
- Create: `components/console/TrajectoryToolbar.tsx` + `trajectory_toolbar.css`
- Test: `__tests__/TrajectoryToolbar.test.tsx`(≥ 7)
- i18n 新键(zh / en):`console.toolbar_duration`(`时长` / `Duration`)、`console.toolbar_duration_title_on`(`改回等宽` / `Use equal widths`)、`console.toolbar_duration_title_off`(`按真实时长` / `Use recorded durations`)、`console.toolbar_degraded`(`时长不可用` / `No timing`)、`console.toolbar_turns`(`轮次` / `Turns`)、`console.toolbar_collapse_turns`(`折叠所有轮` / `Collapse all turns`)、`console.toolbar_expand_turns`(`展开所有轮` / `Expand all turns`)、`console.toolbar_calls`(`调用` / `Calls`)、`console.toolbar_collapse_calls`(`折叠所有工具调用` / `Collapse all tool calls`)、`console.toolbar_expand_calls`(`展开所有工具调用` / `Expand all tool calls`)、`console.toolbar_search`(`搜索` / `Search`)、`console.toolbar_search_count`(`{{n}} 条匹配` / `{{n}} matches`)、`console.toolbar_aria`(`轨迹工具条` / `Trajectory toolbar`)。

**Produces:**
```ts
export interface TrajectoryToolbarProps {
  mode: TimelineMode; onModeChange: (m: TimelineMode) => void;
  /** 时长投影退化(ledger.timed === false):按钮仍可切,旁边标「时长不可用」。 */
  degraded: boolean;
  allTurnsCollapsed: boolean; onToggleAllTurns: () => void; turnsCollapsible: boolean;
  allCallsCollapsed: boolean; onToggleAllCalls: () => void; callsCollapsible: boolean;
  query: string; onQueryChange: (q: string) => void;
  /** null = 无查询;否则匹配数。 */
  matchCount: number | null;
}
```
**行为 / 结构**:`<div role="toolbar" aria-label data-testid="console-traj-toolbar" class="ew-tbar">`;「◷ 时长」`<button data-testid="console-lane-mode" aria-pressed={mode==="duration"} title=…>`(点 → 另一模式);`degraded` → `<span data-testid="console-traj-degraded">`;「⊟ 轮次」`<button data-testid="console-traj-collapse-turns" aria-pressed={allTurnsCollapsed} disabled={!turnsCollapsible}>` 图标 `allTurnsCollapsed ? "⊞" : "⊟"`;「⊟ 调用」同理 `console-traj-collapse-calls`;右侧 `<input type="search" data-testid="console-traj-search" aria-label placeholder>` + `matchCount !== null` 时 `<span data-testid="console-traj-search-count">`。CSS 按 deepseek Toolbar(高 28px、按钮 20px、`aria-pressed` 底色 `--ew-surface-hover`,搜索框 164px `--ew-surface-raised` 边框 `--ew-border-subtle`,聚焦 `--ew-border-focus`)。

- [ ] **Step 1: 写失败测试**:渲染三按钮一输入;`aria-pressed` 跟 props;点击回调各触发;`degraded` 出标签;`disabled` 跟 collapsible;输入触发 `onQueryChange`;`matchCount` 显示。 - [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 轨迹工具条 —— 时长 / 轮次 / 调用 / 搜索`。

---

### Task 6: `useHistoryTurns.loadRuns` + `ConsoleShell` 两栏

**Files:**
- Modify: `components/turn/useHistoryTurns.ts`、`components/console/ConsoleShell.tsx`、`console.css`
- Test: `components/turn/__tests__/useHistoryTurns.test.ts`(6 → ≥ 9)、`components/console/__tests__/ConsoleShell.test.tsx`(3 → ≥ 5)

**Produces:**
```ts
// useHistoryTurns —— 返回值加:
/** 主动回放一批 run(轨迹视图按页用):跳过已开始的,最多 4 路并发,全部结束(成败都算)才 resolve;不抛。 */
loadRuns: (runIds: readonly string[], threadId: string) => Promise<void>;

// ConsoleShell —— `inspect?: ReactNode`;缺省不渲染 `.ew-console__inspect`,根节点加 `ew-console--two`(grid `264px 1fr`,<1200px `48px 1fr`)。
```
- [ ] **Step 1: 写失败测试**:`loadRuns` 对未开始的 run 各回放一次,重复调用不重放,并发 ≤ 4(mock `streamRunEvents` 记录同时在飞的数量),失败的 run 落 `error` 状态、其它照常;`ConsoleShell` 无 `inspect` 时不渲染右列且带修饰类,有 `inspect` 时三栏照旧。 - [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**(`loadRuns` 复用内部 `replayHistoryRun`;`PlaygroundTab` 暂不改)。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): useHistoryTurns.loadRuns 按页回放 + ConsoleShell 两栏形态`。

---

### Task 7: 概览时间轴 `TrajectoryTimeline`

**Files:**
- Create: `components/console/TrajectoryTimeline.tsx` + `trajectory_timeline.css`
- Test: `__tests__/TrajectoryTimeline.test.tsx`(≥ 16)
- i18n 新键:`console.timeline_aria`(`轨迹时间轴` / `Trajectory timeline`)、`console.timeline_track_aria`(`时间轴总览;横向拖动聚焦记录` / `Timeline overview; drag horizontally to focus events`)、`console.timeline_empty`(`没有时序数据` / `No timing data`)、`console.timeline_load_earlier`(`加载更早的历史` / `Load earlier history`)、`console.timeline_loading_earlier`(`正在加载更早的历史…` / `Loading earlier history…`)、`console.timeline_tip_total`(`总计 {{d}}` / `Total {{d}}`)、`console.timeline_tip_started`(`开始于 {{t}}` / `Started {{t}}`)。
- 参照:deepseek `TrajectoryTimeline.tsx` + `.module.css`(全文)。

**Produces:**
```ts
export interface TrajectoryTimelineProps {
  model: TimelineModel | null;
  /** 提示文案用(类型 / 起止 / 时长):按 index 取。 */
  records: readonly LedgerRecord[];
  range: TimeRange | null; onRangeChange: (r: TimeRange | null) => void;
  selectedIndex: number | null;
  hoveredIndex: number | null; onHoverIndex: (i: number | null) => void;
  /** 点块。 */ onSelectRecord: (index: number) => void;
  /** 点空白:最近记录滚进账本视口(不打开详情)。 */ onFocusRecord: (index: number) => void;
  searchMatches: ReadonlySet<number> | null;
  hasEarlier: boolean; loadingEarlier: boolean; onLoadEarlier: () => void;
}
```
**结构 / testid**:根 `<section data-testid="console-lane-strip" data-mode={model?.mode} data-degraded aria-label>`;标签列三行 `t("console.lane_input|lane_model|lane_tools")`;轨道 `<div data-testid="console-lane-track" tabIndex=0 aria-label>`;块 `<span data-testid="console-lane-block" data-index data-lane data-kind data-error data-live(running) data-current(=selectedIndex) data-hovered data-search-match="true|false"(无查询不设) data-selected="true|false"(有 range 时按是否在 focus 集内;无 range 不设)>`(`aria-hidden`,不是 button——整个轨道靠指针事件,点击由 `data-index` 反查);选区 `console-lane-range`(压暗外侧用左右两块 `console-lane-dim`)+ 边条 `console-lane-range-edges`(`data-dragging`);悬停线 `console-lane-hover-line`;轮边界 `console-lane-turn-boundary`(`data-turn`);「…」`<button data-testid="console-lane-earlier" aria-disabled>`;空态 `console-lane-empty`。提示:每块 antd `Tooltip`(`mouseEnterDelay 0.5`)文案三行:类型标签 / (时长模式且有时序:`formatClock(startedAt) → formatClock(endedAt)`;仅起点:`timeline_tip_started`)/ `timeline_tip_total`(有 duration 时)。
**交互(全部按 §九「概览时间轴」与 deepseek 源码)**:左键按下记 anchor(域坐标 + clientX + 命中块 index),`setDraft`;移动 ≥ 3px 才 `setPointerCapture`(spy 断言);移动更新草稿 `orderedRange`,缩放态贴边 8% 自动 `panViewport`;抬起:位移 < 3px 且命中块 → `onRangeChange(null)` + `onSelectRecord(index)`;位移 < 3px 且空白 → `centeredRange(点, minimumSelection)` 提交 + `onFocusRecord(nearestSpan)`;否则宽度不足 minimumSelection 的按中心补足后提交;右键按下 = 平移开始(仅 viewport 非 null),右键抬起未移动 → 清选区;`onContextMenu` preventDefault;双击 / `Escape` → 清选区;wheel(`passive:false`)→ `zoomViewport`;`selectedIndex` 变化 → `revealInViewport`(`data-animate-viewport` 180ms);`onPointerLeave` 无拖 / 平移时清 hover;`pointercancel` 全清。视口非全景时块 / 边界的 `left/width` 用 CSS 变量 `--traj-domain-left/--traj-domain-width` 投影(deepseek `projectedDomainStyle`)。
**CSS**:照 deepseek 几何(plot 50px、标签 44px、泳道 top 7/21/35、块 8px、缝 `min(8%,1px)`、选区 12%/18%、边 3px/2px、外侧压暗 58% 用两块绝对定位 div、悬停线 2px、`data-selected=false` 0.2、`data-search-match=false` 0.14、`data-current` 双环 `0 0 0 1px var(--ew-surface-base), 0 0 0 2px var(--ew-color-brand-500)`、`data-hovered` 同色 80%),颜色按 §九 类型表,`data-live` 呼吸 + reduced-motion 关。

- [ ] **Step 1: 写失败测试**(轨道 `getBoundingClientRect` mock 成 `{left:0,width:1000}`;`model` 用 `deriveTimeline` 真算):三泳道 + 每记录一块 + `data-lane/kind/index`;错误块 `data-error`;`selectedIndex` → `data-current`;`hoveredIndex` ↔ `onHoverIndex`(块 `pointermove` 命中 → 回调 index,离开 → null);`range` 内外块 `data-selected` true/false + 选区元素几何;`searchMatches` → `data-search-match`;拖 ≥ 3px → `onRangeChange` 收到有序区间,`setPointerCapture` 只在过阈值后调用(spy);点块(< 3px)→ `onRangeChange(null)` + `onSelectRecord(i)`;点空白 → `onRangeChange(最小选区)` + `onFocusRecord(nearest)`;双击 / Escape / 右键单击 → `onRangeChange(null)`;wheel deltaY<0 → 块 `--traj-domain-width` > 100%;`hasEarlier` → 「…」按钮,点击 → `onLoadEarlier`,`loadingEarlier` → `aria-disabled`;`model === null` → 空态;提示气泡含类型 + 总计。
- [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**(≤ 400 行;超了把 tooltip 文案 / 指针状态机拆 `trajectory_timeline_pointer.ts`)。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 概览时间轴 —— 三泳道 / 选区 / 缩放平移 / 提示 / 联动`。

---

### Task 8: 账本 `TrajectoryLedger`

**Files:**
- Create: `components/console/TrajectoryLedger.tsx` + `trajectory_ledger.css`
- Test: `__tests__/TrajectoryLedger.test.tsx`(≥ 16)
- i18n 新键:`console.ledger_aria`(`轨迹账本` / `Trajectory ledger`)、`console.ledger_turn_label`(`第 {{n}} 轮` / `Turn {{n}}`)、`console.ledger_turn_compact`(`#{{n}}`)、`console.ledger_request_label`(`请求 #{{n}} · 第 {{turn}} 轮 · 第 {{step}} 步` / `Request #{{n}} · Turn {{turn}} · Step {{step}}`)、`console.ledger_tool_call_only`(`(仅工具调用)` / `(tool call only)`)、`console.ledger_no_output`(`(无输出)` / `(no output)`)、`console.ledger_calls_collapsed`(`(调用已折叠,点击展开)` / `(calls collapsed, click to expand)`)、`console.ledger_load_earlier`(`加载更早的历史(还有 {{n}} 轮)` / `Load earlier history ({{n}} more turns)`)、`console.ledger_loading_earlier`(`正在加载更早的历史…` / `Loading earlier history…`)、`console.ledger_loading`(`正在加载轨迹…` / `Loading trajectory…`)、`console.ledger_placeholder_loading`(`正在回放…` / `Replaying…`)、`console.ledger_placeholder_error`(`回放失败,以下为文本降级` / `Replay failed; text fallback`)、`console.ledger_kind_subtool`(`SUBTOOL`)、`console.ledger_kind_compacted`(`COMPACTED`)。类型标签其它复用 `console.traj_kind_*`(`traj_kind_think` 本组件不用)。

**Produces:**
```ts
export interface TrajectoryLedgerProps {
  rows: readonly DisplayRow[];
  requestsByRecordId: ReadonlyMap<string, LedgerRequest>;
  selectedId: string | null; onSelect: (id: string) => void;
  selectedRequestNo: number | null; onSelectRequest: (no: number) => void;
  hoveredId: string | null; onHover: (id: string | null) => void;
  /** 时间轴选区求交的记录 index 集;null = 无选区。 */
  focusIndexes: ReadonlySet<number> | null;
  /** 当前轮(选中记录所在轮,或最新轮)。 */
  activeTurnKey: string | null;
  onToggleTurn: (turnKey: string) => void; onToggleOwner: (ownerId: string) => void;
  running: boolean;
  hasEarlier: boolean; earlierCount: number; loadingEarlier: boolean; onLoadEarlier: () => void;
  /** 一次性滚动请求(nonce 变才动)。 */
  scrollTo: { id: string; nonce: number } | null;
  loading: boolean;
}
```
**结构 / testid**:滚动容器 `<div data-testid="console-traj-ledger" class="ew-ledger" onScroll>`;`loading` → 顶部粘性 `console-traj-loading`;`<table role="grid" aria-label aria-rowcount>` + `<colgroup>`(122px | auto);`hasEarlier` → 首行按钮 `console-traj-load-earlier`(`disabled` = loadingEarlier,带转圈);虚拟化上下 spacer 行(`data-virtual-spacer="top|bottom"`,高度 CSS 变量;`useVirtualRows({rowHeight: 27, overscan: 12})`);记录行 `<tr tabIndex=0 role="row" aria-selected data-testid="console-traj-row" data-record-id data-kind data-index data-turn-start data-turn-end data-error data-running data-selected data-hovered data-focus="inside|outside"(仅有 focusIndexes 时) data-placeholder>`:事件槽 `<td class="ew-ledger__event">`(`turnStart` → `<span data-testid="console-traj-turn-label" data-active>` 全文 / 紧凑两份;当前轮 `.ew-ledger__turn-rail`;选中 `.ew-ledger__sel-rail`;`requestsByRecordId.get(id)` 命中 → `<button data-testid="console-traj-request-dot" data-no data-status data-active aria-label={ledger_request_label} title>`(`onClick stopPropagation → onSelectRequest`);类型标签 `<span data-testid="console-traj-kind" class="ew-kt ew-kt--<kind>">`)+ 内容 `<td data-testid="console-traj-content">`(§九「内容」:tool `<span class="ew-ledger__nm">name</span> <span class="ew-ledger__args">args</span> <span class="ew-ledger__arr">→</span> <span class="ew-ledger__res" data-error>result | (无输出)</span>`;assistant `text` 空 → `ledger_tool_call_only`;placeholder → 前缀 `ledger_placeholder_*`;整行 `title` = 全文);`turn-summary` 行 `<tr data-testid="console-traj-turn-summary" data-turn-key>`(内容 `… ${processHeadline(summary, t)}${durationMs? " · "+fmtDuration : ""}`,点击 → `onToggleTurn`);`calls-summary` 行 `console-traj-calls-summary`(内容 `… ${toolBreakdown} ${ledger_calls_collapsed}`,点击 → `onToggleOwner`)。
**交互**:行 click / Enter / Space → `onSelect(id)`;dblclick:所在轮可折叠 → `onToggleTurn`,否则若是有子记录的 assistant → `onToggleOwner`;容器 keydown `ArrowUp/Down` 在可见记录行间移动选择(无选中从首行起);`mouseenter/leave` → `onHover`;`focusIndexes` 变化 → 首个 inside 行 `scrollIntoView?.({block:"nearest"})`;`scrollTo` nonce 变 → 该行 `scrollIntoView?.({block:"center"})`;尾随:`rows.length` 增长且 `running` 且离底 ≤ 80px → 滚到底(与 `Transcript` 同规则);`selectedId` 变化 → `scrollIntoView?.({block:"nearest"})`。
**CSS**:行 27px、12px 字、事件槽 `padding-left 36px`、轮标签 mono 8px 左上角、请求圆点 16px `top:-8px left:12px`(状态色 / 悬停放大 / 提示)、轮起点 2px `--ew-border-default` 分隔、`data-focus=outside` 0.24、选中 `--ew-surface-selected`、悬停 `--ew-surface-hover`、类型标签 19px 10px 粗体色按 §九 表(底色 = 同色 14% `color-mix`)、tool 名 / 参数 mono、失败 `--ew-text-danger`。

- [ ] **Step 1: 写失败测试**(fixture 手造 `DisplayRow[]`;容器 `clientHeight` mock 500):每记录一行 + data 属性;轮标签只在 turnStart;请求圆点只在 assistant 且 `data-status`;点行 / Enter / Space → `onSelect`;点圆点 → `onSelectRequest` 且不冒泡到 `onSelect`;dblclick 轮首行 → `onToggleTurn`;dblclick 有子记录的 assistant → `onToggleOwner`;ArrowDown 从选中行移到下一可见行;`focusIndexes` → `data-focus` inside/outside;`hoveredId` → `data-hovered`,`mouseenter` → `onHover`;tool 内容渲染 name/args/→/result 且错误标红;assistant 空文 → 「(仅工具调用)」;placeholder 前缀;turn-summary / calls-summary 行文案与点击;`hasEarlier` 首行按钮 + `earlierCount` + `loadingEarlier` disabled;虚拟化:100 行时 DOM 行数 < 100 且有 spacer;`scrollTo` 触发 `scrollIntoView` spy。
- [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**(≤ 400 行;行渲染拆 `TrajectoryLedgerRow.tsx` 若超)。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 账本 —— 事件槽 / 请求圆点 / 内容列 / 折叠 / 虚拟化 / 键盘 / 尾随`。

---

### Task 9: 详情 `DetailsFrame` + `RecordDetails` + `RequestDetails`

**Files:**
- Create: `components/console/DetailsFrame.tsx`、`RecordDetails.tsx`、`RequestDetails.tsx`、`record_details.css`
- Modify: `RowDetailPayloadResult.tsx`(assistant 的 Result = 正文 Markdown,前面「▸ 思考(N tokens)」折叠段;新增导出 `AssistantPreview` / `AssistantRawText` 两个小组件,并把 `RenderedIo` 改成导出;think 分支保留不删)、`RowDetailTiming.tsx`(`sseModel / sseTokens` 对 assistant 行也取值)
- Test: `__tests__/RecordDetails.test.tsx`(≥ 12)、`__tests__/RequestDetails.test.tsx`(≥ 6)、`__tests__/DetailsFrame.test.tsx`(≥ 5)、`RowDetailTiming.test.tsx`(4 → 5)
- i18n 新键:`console.detail_tab_preview`(`预览` / `Preview`)、`console.detail_tab_rawtext`(`原文` / `Raw`)、`console.detail_tab_input`(`输入` / `Input`)、`console.detail_tab_usage`(`用量` / `Usage`)、`console.detail_hierarchy`(`层级` / `Hierarchy`)、`console.detail_hier_request`(`请求 #{{n}}` / `Request #{{n}}`)、`console.detail_hier_assistant`(`Assistant Message`)、`console.detail_hier_tool`(`Tool Call`)、`console.detail_run`(`Run`)、`console.detail_open_tab`(`打开 {{tab}}` / `Open {{tab}}`)、`console.detail_thinking`(`思考({{n}} tokens)` / `Thinking ({{n}} tokens)`)、`console.detail_thinking_none`(`思考` / `Thinking`)、`console.detail_usage_input`(`输入` / `Input`)、`console.detail_usage_output`(`输出` / `Output`)、`console.detail_usage_reasoning`(`思考` / `Reasoning`)、`console.detail_usage_cache_read`(`缓存读` / `Cache read`)、`console.detail_usage_cumulative`(`累计至此` / `Cumulative`)、`console.detail_tool_calls`(`工具调用` / `Tool calls`)、`console.detail_request_title`(`请求 #{{n}}` / `Request #{{n}}`)、`console.detail_resize`(`拖动调整详情宽度,双击复位` / `Drag to resize; double-click to reset`)、`console.detail_placeholder`(`该轮尚未回放,只有文本降级` / `Turn not replayed yet; text fallback only`)。`detail_tab_payload` 中文改成 `载荷`,`detail_tab_timing` 改 `计时`,`detail_tab_raw` 改 `原始`(en 不变)。

**Produces:**
```ts
// DetailsFrame.tsx —— 右侧 aside 壳:拖宽手柄 + 头部 + tab 条 + 内容
export interface DetailsFrameProps {
  width: number; onWidthChange: (w: number | null) => void;   // null = 复位默认
  /** 容器可用宽(夹取上限用):clamp(w, 320, min(720, splitWidth - 280)) */
  splitWidth: number;
  header: ReactNode;               // 类型标签 + 位置文案
  tabs: { key: string; label: string; testId: string }[];
  activeTab: string; onTabChange: (k: string) => void;
  onClose: () => void;
  children: ReactNode;
}
export const DETAILS_DEFAULT_WIDTH = 420, DETAILS_MIN_WIDTH = 320, DETAILS_MAX_WIDTH = 720, LEDGER_MIN_WIDTH = 280, DETAILS_RESIZE_STEP = 16;

// RecordDetails.tsx
export type RecordTab = "summary" | "payload" | "result" | "preview" | "rawtext" | "timing" | "raw";
export function recordTabsOf(record: LedgerRecord): RecordTab[];   // user: summary/preview/rawtext/raw;assistant: summary/preview/rawtext/timing/raw;其它: summary/payload/result/timing/raw
export interface RecordDetailsProps {
  record: LedgerRecord;
  ownerRequest: LedgerRequest | null;   // ownerRequestNo 对应
  parent: LedgerRecord | null;          // parentId 对应
  threadId: string | null; isSystemAdmin: boolean; langfuseUrl: string | null;
  match: SpanMatch; trace: RunTrace | null; traceLoading: boolean; onRefreshTrace: () => void;
  onOpenRecord: (id: string) => void; onOpenRequest: (no: number) => void;
  onFireResult?: (r: FireNowResult) => void;
  activeTab: RecordTab; onTabChange: (t: RecordTab) => void;
  onClose: () => void;
  width: number; onWidthChange: (w: number | null) => void; splitWidth: number;
}
// RequestDetails.tsx
export type RequestTab = "summary" | "input" | "usage" | "timing";
export interface RequestDetailsProps {
  request: LedgerRequest; record: LedgerRecord;   // 该请求的 assistant 记录
  threadId; isSystemAdmin; langfuseUrl; match; trace; traceLoading; onRefreshTrace;
  onOpenRecord: (id: string) => void;
  activeTab: RequestTab; onTabChange; onClose; width; onWidthChange; splitWidth;
}
```
**行为**:
- Frame:`<aside data-testid="console-detail-aside" style={{width}}>`;手柄 `<div role="separator" aria-orientation="vertical" tabIndex=0 data-testid="console-detail-resize" title>`:pointerdown 记 startX/startWidth + `setPointerCapture`,move → `onWidthChange(clamp(startWidth + startX - clientX))`,dblclick → `onWidthChange(null)`,`ArrowLeft/Right` → ±16;头部 `console-detail-header` + 关闭 `console-detail-close`;tab 条 `role="tablist"`,每个 `<button role="tab" aria-selected data-testid={tab.testId}>`;内容 `role="tabpanel"`。
- RecordDetails 头部:`<span class="ew-kt ew-kt--<kind>">标签</span>` + `第 N 轮 · 第 M 步`(`row.step === null` → 只轮);tab testid = `console-detail-tab-<key>`;`activeTab` 不在 `recordTabsOf` 里时显示 summary(不改父状态,渲染层兜底)。**概要**(`console-detail-summary`):`dl`——层级(assistant 且 ownerRequest → 按钮 `console-detail-hier-request` 「请求 #N ›」→ `onOpenRequest`;tool / plan / memory-writeback 且 parent → `console-detail-hier-assistant` 「Assistant Message ›」→ `onOpenRecord(parent.id)`;subagent 且 parent → `console-detail-hier-tool` 「Tool Call ›」)、状态(`traj_status_*`)、耗时(`endedAt-startedAt` 或 `row.durationMs`)、assistant:模型 / 输入 / 输出 / 思考 / 缓存读、Run(`threadId && runId` → `<Link data-testid="console-inspect-run-link" to={/runs/${threadId}/${runId}}>`)、Langfuse(`langfuseUrl` → `<a data-testid="playground-turn-langfuse" target=_blank>`);`placeholder` 记录 → 提示 `detail_placeholder` 且没有分节;分节(`console-detail-section-<payload|result|preview|timing>`,标题按钮 `detail_open_tab` → `onTabChange`,内容 = 对应 tab 组件包在 `.ew-detail__preview`(max-height 120px + 渐隐)):非 user/assistant → 载荷 + 结果 + 计时;assistant → 预览 + 计时;user → 预览。**载荷 / 结果** = `RowDetailPayload` / `RowDetailResult`(传 `record.row` / `record.events`);**预览** = `AssistantPreview`(user 用同组件:无思考段)—— `<details data-testid="console-detail-thinking">` 折叠段(有 reasoning 才出,`summary` 文案 `detail_thinking` n = reasoningTokens ?? 字数为 0 时用 `detail_thinking_none`)+ `MarkdownView` 正文;**原文** = `AssistantRawText`(思考 `<pre>` + 正文 `<pre>`,`FullTextTrigger` 超 2000 字);**计时** = `RowDetailTiming`;**原始** = 现 `RawTab` 逻辑(搬进 RecordDetails:`row.eventIndexes` → `EventCard`)。
- RequestDetails 头部:蓝点 + `detail_request_title` + `第 N 轮 · 第 M 步`;tab `console-detail-tab-summary|input|usage|timing`;概要:状态 / 模型 / finish_reason(`detail_finish_reason`)/ 工具调用 / 结果(按钮 `console-detail-hier-assistant` → `onOpenRecord(record.id)`)/ Run / Langfuse;输入(`console-detail-input`)= `match.span?.input` 有 → `RenderedIo`(从 `RowDetailPayloadResult` 导出)否则 `detail_need_langfuse`;用量(`console-detail-usage`)= 本次四行 + 累计两行;计时 = `RowDetailTiming(record.row)`。

- [ ] **Step 1: 写失败测试**(fixture 手造 `LedgerRecord` / `LedgerRequest`,`match` 用 `{span:null, reason:"no_trace"}`):Frame 三件(拖宽 clamp / 双击复位 / 键盘步进);RecordDetails:tool 五 tab、assistant 五 tab(preview/rawtext)、user 四 tab;头部标签 + 位置;层级按钮三种各触发对应回调;Run 链接 href;Langfuse 链接仅 admin;分节标题跳 tab;assistant 预览含思考折叠段 + 正文;placeholder 提示;`activeTab` 不合法回概要;RequestDetails:概要字段、输入无 span 提示、用量数字、结果按钮回调;`RowDetailTiming` 对 assistant 行显示模型 / tokens。
- [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**(每文件 ≤ 400 行)。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`(`RowDetail.tsx` 仍存在、仍编译;不改它)。
- [ ] **Step 5: Commit** `feat(console): 详情侧栏 —— 可拖宽壳 / 记录详情按类型分 tab / 请求详情`。

---

### Task 10: `TrajectoryView` 组合与状态源

**Files:**
- Create: `components/console/TrajectoryView.tsx` + `trajectory_view.css`(如超 400 行拆 `use_trajectory_state.ts`)
- Test: `__tests__/TrajectoryView.test.tsx`(≥ 16)
- i18n 新键:`console.trajectory_empty`(`还没有轨迹` / `No trajectory yet`)。

**Produces:**
```ts
export interface TrajectoryViewProps {
  turns: readonly ConsoleTurn[];
  threadId: string | null;
  streamTurnKey: string | null;
  liveByStep: ReadonlyMap<number, LiveStep>;
  running: boolean;
  isSystemAdmin: boolean;
  /** 中栏「查看轨迹」/ 过程条「轨迹」:rowId null = 该轮最后一条 assistant;nonce 变才处理。 */
  focusRequest: { turnKey: string; rowId: string | null; nonce: number } | null;
  /** 让父级回放这些 run(未回放的);resolve 表示都结束了。 */
  onEnsureLoaded: (runIds: readonly string[]) => Promise<void>;
  onFireResult?: (r: FireNowResult) => void;
}
export const TRAJECTORY_PAGE_TURNS = 20;
export const LANE_MODE_KEY = "expert_work.console.lane_mode";   // 从 TrajectoryPanel 搬来
```
**状态**:`mode`(localStorage,`storedLaneMode` 搬来)/ `range` / `selectedId` / `selectedRequestNo`(二选一,选一个清另一个)/ `hoveredId` / `collapsedTurns` / `collapsedOwners` / `query` / `windowStart`(初值 `max(0, turns.length - 20)`;`turns.length` 变化保持已展开的窗口——live 新轮永远在窗口里)/ `loadingEarlier` / `nowTick`(running 时每秒)/ `scrollTo` / `detailsWidth` / `recordTab` / `requestTab` / `splitWidth`(`ResizeObserver` 量根节点,没有则 window resize)。
**派生**:`windowTurns = turns.slice(windowStart)`;`nowMs` = 用最新轮事件 `lastKnownFrame` 校准的服务端 now(没有 → `Date.now()`);`ledger = buildLedger({turns: windowTurns, streamTurnKey, liveByStep, nowMs})`;`timeline = deriveTimeline(ledger.records, mode)`;`focus = range && timeline ? focusIndexes(timeline, range) : null`;`matches = searchLedger(records, query)`;`displayRows = displayRowsOf(ledger, {collapsedTurns, collapsedOwners, matches})`;`requestsByRecordId`;`selectedRecord` / `selectedRequest`(+ 它的 assistant 记录);`useRunTrace({threadId, runId: 选中记录的 runId, enabled: !!选中, turnStatus, wantTraceId: isSystemAdmin})`;`matches = matchTraceSpans(该轮的 records.map(r=>r.row), trace)` → `match`;`langfuseUrl = buildLangfuseTraceUrl(traceId)`(admin)。
**副作用**:窗口内 `loadState === "pending"` 的 history run → `onEnsureLoaded(runIds)`(去重,进行中不重复发);`onLoadEarlier` → `windowStart -= 20` + 标 loading 直到 `onEnsureLoaded` resolve;`focusRequest` nonce 变 → 目标轮不在窗口 → 先扩窗口到含它;`rowId` → `ledgerRecordId(turnKey, rowId)`,null → 该轮最后一条 assistant 记录;设 `selectedId` + `scrollTo` + 清 range / 请求;换 `threadId` → 全部状态复位(窗口重算);`Escape`(根节点 keydown)→ 有详情关详情,否则清 range;选中记录不在 `matches`/`focus` 里时不自动清(deepseek:点账本行若在选区外 → 清选区,照做:`onSelect` 时若 focus 存在且不含该记录 → `setRange(null)`)。
**布局**(`.ew-traj`):`TrajectoryToolbar` / `TrajectoryTimeline` / `.ew-traj__split { display:flex; flex:1; min-height:0 }` = `TrajectoryLedger`(flex 1)+(有选中)`RecordDetails` 或 `RequestDetails`;`turns.length === 0` → `console-trajectory-empty`(`Empty`)。根 `data-testid="console-trajectory-panel"`(沿用)。
**activeTurnKey** = 选中记录 / 请求所在轮,否则最新轮。**allTurnsCollapsed** = `collapsibleTurnKeys` 非空且全在集合;`allCallsCollapsed` 同理。

- [ ] **Step 1: 写失败测试**(fixture:三轮 `ConsoleTurn`(两 history done + 一 live),`onEnsureLoaded` mock resolve;mock `useRunTrace` 返回 no trace):渲染工具条 / 时间轴 / 账本三件;点账本行 → 详情打开且时间轴块 `data-current`;点请求圆点 → 请求详情;`Escape` 关详情;时间轴拖选 → 账本 `data-focus`;搜索 → 账本只剩匹配行 + 时间轴 `data-search-match`;「轮次」全折 → 账本出 turn-summary;「调用」全折 → calls-summary;时长按钮切模式并写 localStorage;`focusRequest` 指向 `think:<seq>` → 选中 `assistant:<seq>` 记录 + `scrollIntoView` spy;`focusRequest` rowId null → 该轮最后一条 assistant;窗口:25 轮 history 只渲染最后 20 轮 + 「加载更早(还有 5 轮)」,点后 `onEnsureLoaded` 收到前 5 轮 runId 且全部渲染;pending run 在窗口内自动 `onEnsureLoaded`;换 `threadId` 状态复位;`running` 时账本 running 行 + 时间轴 `data-live`;`turns` 空 → 空态。
- [ ] **Step 2: 跑红**。 - [ ] **Step 3: 实现**。 - [ ] **Step 4: 跑绿** + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): TrajectoryView —— 工具条 / 时间轴 / 账本 / 详情的状态源与组合`。

---

### Task 11: `PlaygroundTab` 接线 + 退役 + 测试 / e2e + 全门

**Files:**
- Modify: `pages/agent_detail/PlaygroundTab.tsx`、`pages/__tests__/PlaygroundTab.test.tsx`、`e2e/playground-upload.spec.ts`(+ grep 命中的其它 spec)、`components/console/TurnFooter.tsx` / `ProcessStrip.tsx` / `TurnBlock.tsx` / `Transcript.tsx`(仅当回调签名需要;UI 不动)、`api/trajectory_rows.ts`(删旧 `trajectoryRowsOf`;`resolveGanttKey` 保留)、i18n 三处(删死键)
- Delete: `components/console/InspectPanel.tsx` / `TrajectoryPanel.tsx` / `LaneStrip.tsx` / `lane_strip.css` / `lane_strip_model.ts` / `TrajectoryRows.tsx` / `trajectory_rows.css` / `RowDetail.tsx` + 对应 `__tests__/*`
- i18n 新键:`console.view_chat`(`对话` / `Chat`)、`console.view_trajectory`(`轨迹` / `Trajectory`)、`console.view_workspace`(`工作区` / `Workspace`)、`console.view_aria`(`视图` / `View`)。删除(先 grep 无消费者):`console.inspect_trajectory / inspect_workspace / inspect_turn_header / inspect_no_turn / traj_col_idx / traj_col_kind / traj_col_summary / traj_col_in / traj_col_out / traj_col_think / traj_col_duration / traj_filter / traj_filter_clear / traj_list_label / lane_user / lane_mode_sequence / lane_mode_duration / lane_tip_hint / lane_tip_range / traj_llm_call / traj_kind_think`(`traj_kind_think` 若 `RowDetailPayloadResult` 的 think 分支或 ProcessStrip 仍用则留)。

**行为**:
- 头部一行(`MAIN_HEAD_STYLE`):会话 id(`console-thread-id`)· antd `Segmented`(`data-testid="console-view-tabs"`,选项 `console-view-tab-chat / -trajectory / -workspace`,`aria-label={view_aria}`)· 轮数;`view` state 初值 `"chat"`,`resetDraft` / `handleResume` 复位 `"chat"`。
- 主区按 `view`:`chat` → 芯片行 + `Transcript`(不变);`trajectory` → `<TrajectoryView turns={consoleTurns} threadId streamTurnKey={streamTurnId} liveByStep={tokenStream.liveByStep} running isSystemAdmin focusRequest onEnsureLoaded={(ids)=>loadRuns(ids, thread.thread_id)} onFireResult />`(芯片行也保留在上方);`workspace` → `<WorkspacePanel running readOnly />`。三个 tab 下输入区块(`COMPOSER_STYLE` 那段)照旧钉底。
- `ConsoleShell` 不传 `inspect`;`InspectPanel` / `inspectTab` / `inspectRow` / `selectedTurnKey`(仍用于中栏轮高亮)—— `selectedTurnKey` 保留,`inspectRow` 改成 `focusRequest`(`{turnKey,rowId,nonce}`,nonce 自增)。
- `handleSelectTurn(key)`(脚注「查看轨迹」)→ `setSelectedTurnKey(key)` + `setView("trajectory")` + `setFocusRequest({turnKey:key, rowId:null, nonce+1})`;`handleInspectRow(turnKey,rowId)` → 同上带 rowId。
- 删旧文件后 grep 全仓无引用;`api/trajectory_rows.ts` 删 `trajectoryRowsOf` + `TrajectoryInput` 若仍被 `ledgerRowsOf` 用则留。
- **PlaygroundTab.test.tsx**(47 条):引用右栏的条目改到新家(`console-inspect-tab-workspace` → `console-view-tab-workspace`;`console-inspect-turn-header` 断言 → 轨迹 tab 打开后 `console-traj-turn-label` / 详情头部;`console-inspect-run-link` → 点块或行后详情概要里;`console-trajectory-panel` 仍是 TrajectoryView 根),新增:三 tab 切换、轨迹 tab 下 Composer 仍在、「查看轨迹」切 tab 并选中记录、过程条「轨迹」定位记录。报告附对照表(全局约束第一条)。
- **e2e `playground-upload.spec.ts`**:run 结束后 → 点 `console-view-tab-trajectory` → `console-lane-block` 首块可见 → 点它 → `console-detail-header` 可见 + `console-inspect-run-link` href → axe 扫描(空态一次 + 轨迹态一次);`grep -rn "console-inspect\|console-lane\|console-traj\|console-detail" e2e/` 全部核过。
- 全门:`pnpm typecheck && pnpm exec vitest run && pnpm build && pnpm build-storybook && pnpm exec playwright test e2e/playground-upload.spec.ts e2e/session-history.spec.ts`。

- [ ] **Step 1**:接线 + 删除 + i18n。 - [ ] **Step 2**:测试 / e2e 更新到绿。 - [ ] **Step 3**:全门。
- [ ] **Step 4: Commit** `feat(console): 调试台三视图 tab —— 轨迹进中栏、右栏检查面板退役、旧泳道 / 行表 / 详情清扫`。

---

### Task 12: 上线与真栈冒烟(合并后)

- [ ] `scripts/release.sh test` 到 PR-A.2 的合并 commit;记录 PR(`infra/k8s/overlays/test/kustomization.yaml` newTag)。
- [ ] Playwright 无头 + 用户登录态(方法见记忆 `live-console-smoke-via-playwright-storage`;登录态文件只在 scratchpad,用完删):三 tab 切换;轨迹 tab:工具条三按钮 + 搜索;时间轴块数 = 账本记录数;拖选 → 压暗 + 边条 + 账本 outside 行变淡;点空白 → 最小选区;点块 → 详情;请求圆点 → 请求详情(用量 / 计时);滚轮缩放 + 右键平移;搜索过滤;折叠轮 / 调用;时长模式(历史会话)不退化;拖宽详情;发一轮 → 账本尾随、live 块呼吸、结束后请求圆点变正常;「查看轨迹」/ 过程条「轨迹」定位;浅 / 深主题描边可见;<1200px 左栏折叠不影响轨迹。
- [ ] 结果写进记录 PR;更新记忆 `debug-console-redesign-program.md`。
