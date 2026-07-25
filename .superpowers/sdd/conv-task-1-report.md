# Task 1 报告 — 提取 TurnCard 家族 + useHistoryTurns hook(零逻辑改动)

SDD:「对话详情页 = 只读调试台」波 1 / Task 1
分支:`fix-deletion-hygiene-pr5` worktree,已 `git merge --ff-only feat-conversation-run-visibility`(e9ecfcc1 → ec0abbe4,fast-forward 成功)。

---

## 1. 零回归对照(唯一成功判据)

`apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx` **一个字都没改**——
`git diff --stat -- apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx` 输出为空,
`git status --short` 里也不出现该文件。

| 阶段 | 命令 | 结果 |
|---|---|---|
| **Step 1 基线**(提取前) | `vitest run src/pages/__tests__/PlaygroundTab.test.tsx` | `Test Files 1 passed (1)` / **`Tests 45 passed (45)`** |
| **Step 3**(搬完 TurnCard 家族) | 同上 | `Test Files 1 passed (1)` / **`Tests 45 passed (45)`** |
| **Step 5**(搬完 useHistoryTurns) | 同上 | `Test Files 1 passed (1)` / **`Tests 45 passed (45)`** |
| Step 5 核心子集 | 同上 `-t "history lazy rebuild on resume"` | **`Tests 5 passed | 40 skipped (45)`** |
| **最终合跑** | `vitest run src/components/turn/__tests__ src/pages/__tests__/PlaygroundTab.test.tsx` | `Test Files 3 passed (3)` / **`Tests 53 passed (53)`**(45 旧 + 8 新) |

Typecheck:`./node_modules/.bin/tsc -b --noEmit --force` → **exit 0**(Step 3 后、Step 5 后、终态各跑一次,均 0 错;
`--force` 是为了绕开 `.tsbuildinfo` 增量,确保真的全量扫过)。
`tsconfig.app.json` 开了 `noUnusedLocals` / `noUnusedParameters`,所以搬家后残留的死 import 会直接编译报错——这是本次清理 import 的兜底。

> 环境注:worktree 里没有 `node_modules`,先跑了 `pnpm install --frozen-lockfile`(3.2s,lockfile 未变动)。
> 本机 corepack shim 路径为 `/Users/mac/.nvm/versions/node/v22.15.0/lib/node_modules/corepack/dist/pnpm.js`;
> 装完后直接用 `apps/admin-ui/node_modules/.bin/vitest` / `.bin/tsc`,没再经 pnpm shim(避免 exit 137)。

---

## 2. 导出的签名(后续任务契约)

### 2.1 `components/turn/TurnCard.tsx`

```ts
export function runIdOf(events: readonly SseEvent[]): string | null;
export function approvalItemFromEvent(data: unknown): ApprovalItem | null;

export function ApprovalGate(props: {
  approval: ApprovalItem;
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}): JSX.Element;

export interface TurnCardProps {
  turn: Turn;
  /** The turn's index in the transcript — sent as feedback ``turn_seq``. */
  turnSeq: number;
  /** Seed for this turn's own view state (persisted global default). */
  initialEventView: "timeline" | "raw" | "exact";
  onViewChange: (view: "timeline" | "raw" | "exact") => void;
  threadId: string | null;
  onDownloadArtifact: (name: string) => Promise<void>;
  rate: RateCardRecord | null;
  onDecide: (
    turnId: string,
    approval: ApprovalItem,
    decision: "approve" | "reject",
  ) => void;
  deciding: boolean;
  onExport: (turn: Turn) => void;
  exporting: boolean;
  /** item 15 — gates the "open in Langfuse" deep link (system_admin only). */
  isSystemAdmin: boolean;
  /** Historical-turn rendering: hides the approval gate and feedback bar. */
  readOnly?: boolean;                                     // default false
  /** Historical-turn lazy load state — drives the placeholder. */
  loadState?: "pending" | "loading" | "done" | "error";   // default "done"
  /** Assistant text shown before this historical turn's events replay. */
  fallbackAnswer?: string;
  /** 流式打字机(3a)— live token buffers by step, forwarded to StepTimeline. */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  ttftMs?: number | null;                                 // default null
  finalized?: boolean;                                    // default false
  /** Spec 1 PR4 Task 5 — 「立即触发」 结果上报。 */
  onFireResult?: (result: FireNowResult) => void;
}

export function TurnCard(props: TurnCardProps): JSX.Element;
```

字段名 / 顺序 / 默认值 / 注释与提取前 `PlaygroundTab.tsx:1957-2021` 逐字一致。
T3 只读接入的最小 props 集:`turn / turnSeq / initialEventView / onViewChange / threadId /
onDownloadArtifact / rate / onDecide / deciding / onExport / exporting / isSystemAdmin`
(必填,只读场景 `onDecide` 传 `() => {}`、`deciding=false`),再加 `readOnly / loadState / fallbackAnswer`。

### 2.2 `components/turn/useHistoryTurns.ts`

```ts
export interface UseHistoryTurns {
  /** Flat ``/messages`` text — the always-available degradation payload. */
  messages: HistoryMessage[];
  /** Count-paired historical turns (null = 未配对/计数不等 → 用 messages 扁平渲染)。 */
  turns: HistoryTurn[] | null;
  /** Each turn's lazy replay state, keyed by runId. */
  loads: Record<string, HistoryLoad>;
  /** Curried ref callback — 注册到共享 IntersectionObserver,进视口触发 replay。 */
  registerRow: (runId: string, threadId: string) => (el: HTMLElement | null) => void;
  /** 拉取 + 配对。永不 reject(内部降级)。 */
  load: (threadId: string) => Promise<void>;
  /** 清空重建结果 + 拆 observer。刻意不 abort 在途请求(照搬 resetDraft)。 */
  reset: () => void;
}

export function useHistoryTurns(): UseHistoryTurns;
```

### 2.3 其余导出

```ts
// components/turn/types.ts
export interface Attachment { id: string; name: string; kind: "image" | "document"; value: string }
export interface Turn { id; input; attachments: Attachment[]; events: SseEvent[];
                        status: "running" | "done" | "error"; error: string | null;
                        approval: ApprovalItem | null }
export type HistoryLoad =
  | { state: "pending" | "loading" | "error"; events: SseEvent[] }
  | { state: "done"; events: SseEvent[] };

// components/turn/HistoryDivider.tsx
export function HistoryDivider(): JSX.Element;
// components/turn/FeedbackBar.tsx
export function FeedbackBar(props: { threadId: string; turnSeq: number }): JSX.Element;
// components/turn/TaskResultCard.tsx
export function TaskResultCard(props: { result: FireNowResult }): JSX.Element;
```

---

## 3. 变异自验(Step 7)

变异点:删掉 `useHistoryTurns.ts` `replayHistoryRun` 开头的 one-shot 守卫
(`if (startedHistoryRunsRef.current.has(runId)) return;`),只保留 `.add(runId)`。

**红(变异后)** — `vitest run src/components/turn/__tests__/useHistoryTurns.test.ts`:

```
 ❯ src/components/turn/__tests__/useHistoryTurns.test.ts (4 tests | 1 failed) 14ms
     × replays a runId only once even when two rows register it 5ms

 FAIL ... > useHistoryTurns > replays a runId only once even when two rows register it
AssertionError: expected "streamRunEvents" to be called 1 times, but got 2 times
 ❯ src/components/turn/__tests__/useHistoryTurns.test.ts:157:33

 Test Files  1 failed (1)
      Tests  1 failed | 3 passed (4)
```

**绿(恢复守卫后)** — 最终合跑 `Tests 53 passed (53)`,其中 `useHistoryTurns.test.ts` 4/4。

> 探针设计要点:测试用**两个不同的 DOM 元素**注册同一个 `runId`。
> 如果用同一个元素注册两次,`runIdByEl` 那道守卫会先拦住,变异下测试照样绿(空转探针)。
> 两个不同元素时 `runIdByEl` 拦不住,只有 `startedHistoryRunsRef` 能拦——这才真正杀掉变异。

---

## 4. 搬运时的偏离及理由

1. **hook 多返回一个 `messages`**(brief 草签只有 5 个键)。
   `history: HistoryMessage[]` 这个 state 在 PlaygroundTab 里**只被重建流程写**
   (声明 + `resetDraft` + `handleResume` 的 `.then`/`.catch`),而 `setHistory(messages)`
   ——注释写着「降级路径永远有数据」——正躺在被搬走的那条 `Promise.all` 链里。
   不一起搬就得让 hook 把原始 messages 吐出来、或者让调用方重复发一次 `getSessionMessages`。
   一起搬,以 `messages` 暴露。四层降级因此在 hook 内自洽,T3 直接能用。

2. **`registerRow` 的元素类型 `HTMLDivElement | null` → `HTMLElement | null`**。
   brief 的 Interfaces 段就是这么写的。逆变位置上 `HTMLDivElement` 可赋给 `HTMLElement`,
   所以 `<div ref={registerRow(...)}>` 照样通过(tsc 0 错验证);T3 换别的元素也不用改签名。

3. **TurnCard 的内联 props 对象类型抽成 exported `interface TurnCardProps`**。
   brief 的 Interfaces 段写的就是 `TurnCard(props: TurnCardProps)`。字段名 / 顺序 / 可选性 /
   默认值 / 注释逐字保留,纯类型层,零运行时影响;不抽的话 T3 没法引用这个形状。

4. **`TRACE_NOT_READY_MAX_RETRIES` / `TRACE_NOT_READY_RETRY_MS`(原 `:152-153`)被迫一起搬**。
   brief 的符号清单里没列它们,但它们只被 TurnCard 的 trace 轮询 effect 消费,留在 PlaygroundTab
   会立刻触发 `noUnusedLocals`。搬进 `TurnCard.tsx`,值和注释不动。

5. **`const { Text } = Typography;` 在 4 个新文件里各写了一份**。这是 antd 的 file-local 别名,
   不是共享符号,复制是唯一无改动的搬法。

6. **`load()` 内部 `await` 了那条链,原代码是 `void Promise.all(...)`**。
   调用方写的是 `void loadHistory(picked.thread_id)`,所以同步前半段(清 state / abort / 换 AC /
   拆 observer)和异步后半段的时序完全不变;`await` 只是给 hook 一个可测的完成信号。
   链尾有 `.catch`,`load` 永不 reject。
   另:原 `handleResume` 里 `setHistory([])` 排在 `setThread(picked)` **之前**,现在它挪进
   `load()` 因而排在**之后**——两者仍在同一个同步事件处理器里,React 批处理,渲染结果一致。

7. **`downloadJson`(`:1809`)和 `isHiddenWorkspacePath`(`:1825`)留在原处**。
   grep 确认 TurnCard 一个都没用到:前者服务 `handleExport`,后者服务左栏工作区文件浏览器。

8. **`reset()` 刻意不 abort 在途请求**。原 `resetDraft` 从来没碰过 `historyAbortRef`
   (只有 `handleResume` 碰),逐字保留,并在 hook docstring 里写明这是有意的。

9. **components → pages 的反向依赖(遗留,建议 follow-up)**。
   `components/turn/TurnCard.tsx` 从 `pages/agent_detail/playground/*` import 了 10 个模块
   (`AgentStatePanels` / `duration_format` / `RunStatusBanner` / `StepTimeline` / `useTokenStream`
   / `TimelineFilterBar` / `timeline_banner` / `trace_banner` / `TraceView` / `trace_purpose` /
   `TurnMeta`),外加 `pages/run_detail/PlanPanel`;`useHistoryTurns.ts` import 了
   `pages/agent_detail/playground/history_turns`。
   本任务是搬家不是重构,这些被依赖模块**没有**反向 import PlaygroundTab(无环,tsc 通过),
   所以原样保留。要把 `playground/*` 也挪进 `components/turn/` 是独立的一批改动。

10. **PlaygroundTab 从 2634 行降到 1377 行**(brief 预估 ~1650)。多降的 ~270 行是因为 hook 连
    扁平 `history` state + `resetDraft` / `handleResume` 里的那段编排一起吸走了(见偏离 1)。

---

## 5. 新增文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `apps/admin-ui/src/components/turn/types.ts` | 39 | `Attachment` / `Turn` / `HistoryLoad` |
| `apps/admin-ui/src/components/turn/TurnCard.tsx` | 885 | `runIdOf` / `approvalItemFromEvent` / `ApprovalGate` / `TurnCardProps` / `TurnCard` / TRACE_NOT_READY_* |
| `apps/admin-ui/src/components/turn/FeedbackBar.tsx` | 130 | `FeedbackBar` |
| `apps/admin-ui/src/components/turn/TaskResultCard.tsx` | 109 | `TaskResultCard` |
| `apps/admin-ui/src/components/turn/HistoryDivider.tsx` | 26 | `HistoryDivider` |
| `apps/admin-ui/src/components/turn/useHistoryTurns.ts` | 234 | `useHistoryTurns` + `UseHistoryTurns` |
| `apps/admin-ui/src/components/turn/__tests__/TurnCard.test.tsx` | — | 4 用例 |
| `apps/admin-ui/src/components/turn/__tests__/useHistoryTurns.test.ts` | — | 4 用例 |

**i18n**:一个键都没改。所有搬走的组件继续读 `playground.*`;5 个新文件的模块 docstring
都注明了「``playground.*`` 现为跨页共享命名空间」(全局约束要求)。

**新测试清单**

`TurnCard.test.tsx`(需 `<MemoryRouter><App>` 双层 wrapper —— `ToolCallCard` 走 `App.useApp()`
(照 `components/__tests__/ToolTimeline.test.tsx:39` 的写法),`TurnMeta` 里有 router link):
- replay 事件 → 渲染出 `step-timeline`、工具计数 tag、展开步骤后拿到 `tool-call-card` + 工具名
- `loadState="loading"` + 空 events → 只出兜底答案,不跑完整解析机器
- `readOnly` → `playground-approval` / `playground-turn-feedback` 都不渲染
- **非** `readOnly` 同一 turn → 两者都渲染(防上一条变成空断言)

`useHistoryTurns.test.ts`:
- 计数配对成功 → `turns` 有值、`loads` 全 `pending`
- 计数不等 → `turns` 为 `null`,`messages` 仍保留(降级)
- 同一 runId 两个不同行注册 → `streamRunEvents` 只调 1 次(变异探针)
- 陈旧 `load` 的结果被新 `load` 顶掉

---

## 6. 顾虑 / 未做

- **components → pages 反向依赖**(偏离 9)。不影响编译和运行,但方向别扭;
  建议单开一批把 `playground/*` 里被共享的那部分也挪进 `components/turn/`。
- **`readOnly` 下 `onExport` / `exporting` / `onDecide` / `deciding` 仍是必填 props**。
  只读页得传哑值(`() => {}` / `false`)。改成可选属于改签名,本任务禁止,留给 T3 决定要不要提。
- **前端全量 vitest 未跑**(按任务书归终门)。本任务只跑了 `PlaygroundTab.test.tsx` +
  两个新测试文件 + 全量 `tsc`。
- **未做人工冒烟**。`PlaygroundTab.stories.tsx` 只 import `PlaygroundTab` 本身,未受影响;
  Storybook 未启动验证。
