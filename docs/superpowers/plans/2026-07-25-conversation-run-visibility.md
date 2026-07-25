# 对话详情页 = 只读调试台 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对话详情页的「消息记录」块换成调试台同款只读轮次卡时间线(懒加载重建),让 run 的执行过程(每步 LLM、工具调用入参出参、耗时、token、trace)在对话页直接可见。

**Architecture:** 纯前端。把调试台的 `TurnCard` 家族与 #980 历史重建流程从 `PlaygroundTab.tsx` 提取到 `components/turn/`(零逻辑改动),对话页复用。后端一行不改——`events`/`runs`/`trace` 端点全现成。spec 见 `docs/superpowers/specs/2026-07-25-conversation-run-visibility-design.md`(D1 照搬调试台形态 / D2 沿用现行降级 / D3 不做跨租户)。

**Tech Stack:** React + antd + vitest + TypeScript。

## Global Constraints

- **零回归判据(最高优先)**:提取后 `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx` 全量**不改一字**必须全绿,尤其 `:1675-1930` 的 5 个 `history lazy rebuild on resume` 用例。任何"顺手优化"逻辑一律禁止——本批提取是搬家,不是重构。
- i18n **不改键**:TurnCard 文案继续读 `playground.*` 命名空间(改键要动 en/zh-CN/interface + 全部引用,收益为零);在共享组件 docstring 注明该命名空间现为跨页共享。新增文案(如"查看运行")才走 `conversations_detail.*` 新键,en + zh-CN + interface 三处同步、先查撞键。
- 组件保持纯 props 驱动(现状零 context/store 耦合,别引入)。
- 编辑器/IDE 诊断 stale 不作数,一律以真 `pnpm -C apps/admin-ui typecheck` + vitest 定论(本仓库反复踩过)。
- 本机 `pnpm` corepack shim 可能被 SIGKILL(exit 137),绕法:`node "$(dirname "$(which pnpm)")/../lib/node_modules/corepack/dist/pnpm.js" -C apps/admin-ui <cmd>`。
- TDD:先红后绿;变异自验按各任务 brief 指定项执行并记录红/绿。
- 分支 `feat-conversation-run-visibility`,基 main(follow-up 打包合并之后)。

## 并行波次

- **波 1(2 并行 worktree,文件不相交)**:T1(提取)/ T2(fallbackAnswer 保真)
- **波 2**:T3(对话页接入,依赖 T1+T2)
- **T4 终门** + opus 全分支终审。
- worktree 从 main 切出:dispatch 第一步 `git merge --ff-only feat-conversation-run-visibility`。

---

### Task 1: 提取 TurnCard 家族 + useHistoryTurns hook(零逻辑改动)

**Files:**
- Create: `apps/admin-ui/src/components/turn/TurnCard.tsx`(`TurnCard` + `ApprovalGate` + `runIdOf` + `approvalItemFromEvent`)
- Create: `apps/admin-ui/src/components/turn/FeedbackBar.tsx`、`TaskResultCard.tsx`、`HistoryDivider.tsx`、`types.ts`(`Turn` / `Attachment` / `HistoryLoad`)
- Create: `apps/admin-ui/src/components/turn/useHistoryTurns.ts`
- Create: `apps/admin-ui/src/components/turn/__tests__/TurnCard.test.tsx`、`useHistoryTurns.test.ts`
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx`(删除被搬走的定义、改 import、`handleResume` 改用 hook)
- Test(不得修改,零回归判据): `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx`

**Interfaces:**
- Produces:
  - `export function TurnCard(props: TurnCardProps)` —— props 保持现签名(`PlaygroundTab.tsx:1957-2021` 的 17 个),连同 `readOnly` / `loadState` / `fallbackAnswer`。
  - `export interface Turn { id; input; attachments; events; status; error; approval }`、`Attachment`、`HistoryLoad`(`pending|loading|done|error` 判别联合,原 `:144`)。
  - `export function useHistoryTurns(): { turns: HistoryTurn[] | null; loads: Record<string, HistoryLoad>; registerRow: (runId: string, threadId: string) => (el: HTMLElement | null) => void; load: (threadId: string) => Promise<void>; reset: () => void }` —— 具体形状以搬运后的真实调用面为准,**先保证 playground 用得上且行为不变**,T3 再消费。

- [ ] **Step 1: 先跑基线**——`PlaygroundTab.test.tsx` 全量跑一遍并记录通过数,作为提取后的对照(这是本任务唯一的成功判据来源)。
- [ ] **Step 2: 搬 TurnCard 家族**——把 `PlaygroundTab.tsx` 的以下 file-local 符号整体剪切到 `components/turn/`(逐字搬运,**禁止顺手改名/改逻辑/删注释**):`Attachment`(:119)、`Turn`(:129)、`HistoryLoad`(:144)、`TaskResultCard`(:1583)、`HistoryDivider`(:1673)、`FeedbackBar`(:1695)、`runIdOf`(:1829)、`approvalItemFromEvent`(:1848)、`ApprovalGate`(:1876)、`TurnCard`(:1957-2634)。`downloadJson`(:1809)/`isHiddenWorkspacePath`(:1825)按 TurnCard 是否真的用到决定搬不搬(不用到就留原处)。全部 `export`;`PlaygroundTab.tsx` 顶部改 import。
- [ ] **Step 3: 跑测试确认零回归**——`PlaygroundTab.test.tsx` 通过数与 Step 1 基线**完全一致**;`pnpm -C apps/admin-ui typecheck` 0 错。不一致就是搬错了,回去对齐,**不许改测试**。
- [ ] **Step 4: 抽 useHistoryTurns hook**——把 `PlaygroundTab.tsx` 里 #980 的重建流程整体搬进 hook:state `historyTurns`/`historyLoads`(:240-243)、`handleResume` 内的 `Promise.all([getSessionMessages, listThreadRuns.catch(()=>null)])`(:373-376)与 `buildHistoryTurns` 配对(:384)、`replayHistoryRun`(:420-451)、`registerHistoryRow`(:466)与其三个 ref(`runIdByEl` :265、`startedHistoryRunsRef` :272、`historyAbortRef`)。四层降级与陈旧请求守卫**逐字保留**。`handleResume` 改为调 hook 的 `load(thread_id)`,渲染处(:1447-1497)改用 hook 返回值。
- [ ] **Step 5: 再跑零回归**——同 Step 3 判据。5 个 `history lazy rebuild on resume` 用例是核心,必须全绿。
- [ ] **Step 6: 补新测试**——`TurnCard.test.tsx`:只读模式冒烟(给一组 replay 事件,断言渲染出步骤时间线与工具调用卡;`readOnly` 下 `ApprovalGate`/`FeedbackBar` 不渲染)。`useHistoryTurns.test.ts`:配对成功/计数不等返 null/`registerRow` 对同一 runId 只触发一次 replay/陈旧 `load` 的结果被丢弃。测试需 antd `App` wrapper(照 `components/__tests__/ToolTimeline.test.tsx:39` 写法)。
- [ ] **Step 7: 变异自验**——去掉 `startedHistoryRunsRef` 的 one-shot 守卫 → "只触发一次 replay"测试红;恢复绿。记录。
- [ ] **Step 8: Commit** `refactor(admin-ui): TurnCard 家族 + 历史重建 hook 提取到 components/turn(零逻辑改动)`

### Task 2: fallbackAnswer 收全该轮所有助手消息(保真)

**背景:** 一个 run 常产出多条 assistant 消息(实测截图那轮 7 条),现在 `fallbackAnswer` 只取紧邻的下一条。配对成功但 replay 失败时,显示的信息**比今天的扁平视图更少**。

**Files:**
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts`
- Test: `apps/admin-ui/src/pages/agent_detail/playground/__tests__/history_turns.test.ts`

- [ ] **Step 1: 写失败测试**:一条 user 消息后跟 3 条 assistant 消息、再一条 user 消息 + 1 条 assistant(runs 长度 2)→ 第一轮 `fallbackAnswer` 含全部 3 条内容(以空行分隔)、第二轮含 1 条;**配对判据不变**的既有用例(计数不等返 null)保持绿。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——`buildHistoryTurns` 内层循环:收集当前 user 消息之后、下一条 `role === "user"` 之前的全部 `role === "assistant"` 消息,`join("\n\n")` 作为 `answer`。`pairs.length !== runs.length → null` 判据与 `is_resume` 被忽略的语义**一字不动**(docstring 相应补一句多消息拼接的说明)。
- [ ] **Step 4: 确认绿**——本文件测试 + `PlaygroundTab.test.tsx` 全量(共享 helper,playground 也吃这个改动)。
- [ ] **Step 5: 变异自验**——改回只取下一条 → 保真测试红;恢复绿。记录。
- [ ] **Step 6: Commit** `fix(admin-ui): 历史轮 fallbackAnswer 收全该轮助手消息(replay 失败时不丢内容)`

### Task 3: 对话详情页接入(依赖 T1+T2)

**Files:**
- Modify: `apps/admin-ui/src/pages/ConversationDetail.tsx`(消息记录块 :275-315;run 表格列 :95-155)
- Modify: i18n 三处(`i18n/locales/en.ts`、`zh-CN.ts`、interface):`conversations_detail.*` 新键(如 `view_run`)
- Test: `apps/admin-ui/src/pages/__tests__/ConversationDetail.test.tsx`(既有 8 例保持绿)

**Interfaces:**
- Consumes: T1 的 `useHistoryTurns` / `TurnCard` / `HistoryLoad`;T2 的多消息 `fallbackAnswer`。

- [ ] **Step 1: 写失败测试**:①配对成功 → 渲染轮次卡(不再是扁平气泡);②滚入视口(mock IntersectionObserver,照 `PlaygroundTab.test.tsx:1675-1930` 的现成写法)→ 触发 `streamRunEvents` 并渲染出工具调用;③配对失败(runs 数与 user 消息数不等)→ 退回扁平文本(今天的渲染);④跨租户(`convo.tenant_id !== 当前租户`)→ 直接扁平且**不发** replay 请求;⑤run 行有"查看运行"链接,点击 navigate 到 `/runs/{thread}/{run}`。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——
  - 消息记录块:`const { turns, loads, registerRow, load } = useHistoryTurns()`,挂载时 `load(threadId)`;`turns !== null` → `turns.map(t => <div ref={registerRow(t.runId, threadId)}><TurnCard readOnly turn={{id:t.key, input:t.input, attachments:[], events:[], status:"done", error:null, approval:null}} turnSeq={i} threadId={threadId} loadState={loads[t.runId]} fallbackAnswer={t.fallbackAnswer} rate={null} onDecide={noop} deciding={false} onFireResult={noop} …/></div>)`;`turns === null` → 保留既有扁平渲染分支一字不动。具体 props 以 T1 导出的真实签名为准。
  - 跨租户守卫:`loaded?.tenant_id` 与当前租户不同 → 不调 `load`,直接扁平(spec D3/B2)。当前租户来源照本文件既有 `tenant` 上下文用法(`TenantScopeContext`)。
  - run 表格:新增一列渲染 `<Link to={`/runs/${threadId}/${run.run_id}`}>` + i18n `view_run`;既有行级 onClick 保留。
- [ ] **Step 4: 确认绿**——`ConversationDetail.test.tsx` 全量(既有 8 + 新 5)、`pnpm typecheck` 0 错。
- [ ] **Step 5: 变异自验**——把跨租户守卫改成恒假(总是 replay)→ 用例④红;恢复绿。记录。
- [ ] **Step 6: Commit** `feat(admin-ui): 对话详情页内联调试台同款轮次时间线(run 执行过程可见)`

### Task 4: 终门

- [ ] `pnpm -C apps/admin-ui typecheck`(0 错)
- [ ] `pnpm -C apps/admin-ui test`(全量 vitest;`PlaygroundTab.test.tsx` 与 `ConversationDetail.test.tsx` 重点看)
- [ ] `pnpm -C apps/admin-ui build`(CI 跑 vite build,本地过一遍免得 CI 才发现)
- [ ] 后端未改动 → 后端门只需 `uv run ruff check .` 确认无误伤
- [ ] 全绿后 opus 全分支终审(`review-package $(git merge-base main HEAD) HEAD`),重点核:提取零回归(diff 里 TurnCard 主体应为纯位移)、hook 四层降级逐字保留、对话页降级分支未被削弱

## Self-Review 记录

- Spec 覆盖:§A=T1,§B=T3,§C=T2,§D3 跨租户守卫在 T3 Step 3。
- 零回归判据贯穿 T1(Step 1 基线 / Step 3 / Step 5)与 T4。
- 波 1 两任务文件不相交(T1: PlaygroundTab.tsx + components/turn/*;T2: playground/history_turns.ts + 其测试)。
- 类型一致:`HistoryLoad` / `Turn` 由 T1 导出,T3 消费同一定义,不重复声明。
