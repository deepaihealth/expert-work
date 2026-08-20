# 调试台重设计 PR-B(对话记录页 + Run 详情页 + 退役)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `ConversationDetail`(spec PR4)与 `RunDetail`(spec PR5)切到新 console 组件族,然后一次性退役 TurnCard 集群(≈5,900 行组件 + ≈2,100 行测试 + 死 i18n 键)。

**Architecture:** 纯前端 PR,后端零改、对外 API 零改。三波:波 1 解耦(抽 TurnCard 活导出、readOnly 穿透写按钮、数据层小修),波 2 两页面并行重写(复用 `useHistoryTurns` + `buildConsoleTurns` + `Transcript`/`TrajectoryView`/`PlanCard`),波 3 退役清扫。

**Tech Stack:** React 18 + antd + vitest + Playwright(既有栈,无新依赖)。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` §二.2(PR4)、§二.3(PR5)、§三(退役表,注意已被 §九「退役」节部分作废)、§五(PR4/PR5 验收行)。

## 事实基线(2026-08-20 对 main `138f387f` 逐项重核;旧事实表 pr-b-facts.md 基于 PR-A 时点,已过时的不再引用)

- `ConversationDetail.tsx` 574 行 / 测试 20 it;`RunDetail.tsx` 212 行 / **无页级测试**;`run_detail/` 5 组件 29 it。
- TurnCard.tsx 1257 行,console 活引用三个导出:`ApprovalGate`(TurnBlock:25)、`CommentarySegmentLine`(Transcript:25 / AnswerBubble:22)、`approvalItemFromEvent`(useRunEngine:29);`TurnCard` 组件本体唯一消费者 = ConversationDetail:37。
- `runIdOf` 在 TurnCard.tsx:128 与 console_turns.ts:18 **逐字节重复**(diff 已验证)。
- 退役集是一个闭合簇,三个活根:ConversationDetail(router:81)、RunDetail(router:79)、TurnCard 三导出。两页面换掉 + 三导出抽走 ⇒ 整簇可删。
- `pages/agent_detail/playground/` 里 5 个文件是新代码的地基,**不许动**:`duration_format.ts`/`useTokenStream.ts`/`history_turns.ts`/`untrusted_clean.ts`/`useRunEngine.ts`。
- readOnly 现状:只盖审批闸(TurnBlock:199)与反馈条(TurnFooter:114)。「立即触发」`FireNowButton`(ToolTimeline.tsx:234-290)只自查 `useIsTenantSwitched`,**三条链都能点到**:①Transcript→TurnBlock→ProcessStrip(不传)→CompactRow:127→ToolCallCard;②CompactRow:279(展开态 RowDetail 分支)→ToolCallCard;③TrajectoryView(无 readOnly prop)→RecordDetails:171→RowDetailResult(RowDetailPayloadResult:291/295/301)→ToolCallCard。
- `usePlanCard` 三源合流(GET 基线 / SSE `plan` 帧 / PUT 回显),PlanCard 已有 `readOnly` prop;PlanPanel 已复用 console 的 `PlanEditForm`;`plan-edit`/`plan-save`/`plan-step-input-N` testid 两者一致,`plan-panel`→`console-plan-card` 不一致。
- `api/conversations.ts` 的 `ConversationDetail` 含 `agent_name`/`agent_version`(:31-32)→ 两页都能喂 `TrajectoryView` 必填的 agentName/agentVersion(Schema tab 用)。
- `TurnBlock` 每 render 对同一 events 数组解析 3 次(compactRowsOf / liveSyntheticRows / liveAnswerTextOf 各一次)——`parseTimeline` 无引用缓存。
- e2e:`run_detail.spec.ts` 断 `approval-card`/`approval-approve`/`approval-reject`(保留)与 `plan-panel`/`plan-edit`/`plan-step-input-1`/`plan-save`(迁移)+ 2 处 axe;`conversations.spec.ts` 只断 /conversations 列表页文本(本 PR 不碰)。两页面自己的 testid 零 e2e 引用。
- i18n:`event_stream.*` 12 键消费者全在退役集(CompactionCard 也随之死——新 console 用 MarkerRow/CompactRow 原生渲染 compaction);`trace.*` 1 键只有 EntryBreakdown 用;`trace_toolbar.open_in_langfuse` 被 RecordDetails:293/RequestDetails:119 用(**保留**),其余 5 键死;`playground.*` 162 键混用,需逐键盘点;`session_history.*`/`tool_timeline.*`/`plan_panel.*` 均有新代码消费者(保留命名空间,只删死键)。

## 拍板(控制器判断,用户可在过目时否决)

| # | 决定 | 依据 |
|---|---|---|
| R1 | ConversationDetail **不用 ConsoleShell**(无侧栏页直接单列布局,`sidebar` 保持必填不动) | 两栏壳对无侧栏页面只剩空 rail;YAGNI |
| R2 | RunDetail 保持**可写**(ApprovalCard 照旧、PlanCard 可编辑);只有 ConversationDetail 全链 readOnly | spec §二.3「编辑能力保留」 |
| R3 | TurnMeta / RunStatusBanner **不搬 console,随 TurnCard 一起删**(现各只有 TurnCard 一个 importer;旧骨架「git mv 进 console」作废) | 重核事实 |
| R4 | RunDetail 的 plan **不做 3s 轮询**:跑动中计划更新走事件流 `plan` 帧(PR1 已落)+ PUT 回显;页级 `useStatusPolling` 只刷 run 状态 | usePlanCard 三源已覆盖 |
| R5 | 两页装载复用 `useHistoryTurns`(RunDetail 取全量后按 runId 过滤出单轮),**不写** `runConsoleTurnOf` 合成函数 | hook 现成有测试;单独合成要重做 input 配对 |
| R6 | readOnly 语义 = 写按钮**不渲染**(照 Transcript retry「不传 handler 按钮就不渲染」惯例),不是 disable | 既有惯例 |
| R7 | PlaygroundTab(740 行)不大拆,只把页内 `ViewPane` 提为共享组件 | <800 上限;避免为拆而拆 |
| R8 | 不做 `playground.*`→`console.*` 键改名(spec 原文「逐步」);只删死键 | spec §三 |
| R9 | 退役文件**直接删除**,活地基 5 文件留在 `pages/agent_detail/playground/` 原地不搬 | surgical changes |

## Global Constraints(每个 task 的隐含要求)

- 纯前端:`services/`、`docs-site/`、对外 API 一律不碰。
- 新文件 ≤400 行;既有文件改后不得突破 800。
- i18n:新键 en.ts(type 块 + value 块)+ zh-CN.ts 三处同步,加前先 grep 撞键;删键同样双文件双块都删。
- 颜色只走 `--ew-*` 令牌;不新增裸色值。
- 既有测试断言**只允许改本计划任务里明确列出的迁移/删除项**;其余一律保持。
- 改动会渲染页面的组件前,先 `grep -rn "<testid>" apps/admin-ui/e2e/`,受影响 spec 本地跑(`pnpm exec playwright test <spec>`)。
- 门:`pnpm typecheck`(tsc -b,裸 tsc 恒绿不算)、`pnpm exec vitest run <scope>`、任务末全量相关模块;vitest 全量并发下 SettingsPlatformConfig/SetupWizard/UserProfile 偶发 5s 超时,单跑绿即视为过。
- 每条新断言过 break→red→restore→green 自证;禁 `git checkout --`/stash/reset 还原(复制副本到 scratchpad)。
- subagent 测试一律前台跑。

---

### Task 1: TurnCard 活导出抽离 + readOnly 穿透三条链(波 1)

**Files:**
- Create: `apps/admin-ui/src/components/turn/ApprovalGate.tsx`(≈130 行:TurnCard.tsx 的 `approvalItemFromEvent`(:169-196)与 `ApprovalGate`(:197-283)原样搬入,连同它们的 import 与 docstring)
- Create: `apps/admin-ui/src/components/turn/CommentarySegmentLine.tsx`(≈40 行:TurnCard.tsx:94-127 原样搬入)
- Create: `apps/admin-ui/src/components/turn/__tests__/ApprovalGate.test.tsx`、`__tests__/CommentarySegmentLine.test.tsx`
- Modify: `components/turn/TurnCard.tsx`(删搬走的实现与本地 `runIdOf`,顶部加 `import { runIdOf } from "../console/console_turns"; export { ApprovalGate, approvalItemFromEvent } from "./ApprovalGate"; export { CommentarySegmentLine } from "./CommentarySegmentLine"; export { runIdOf };` —— 兼容 re-export 让 ConversationDetail 在 Task 3 前不用动)
- Modify(改 import 指到新文件): `components/console/TurnBlock.tsx:25`、`components/console/Transcript.tsx:25`、`components/console/AnswerBubble.tsx:22`、`pages/agent_detail/playground/useRunEngine.ts:29`
- Modify(readOnly 链): `components/ToolTimeline.tsx`、`components/console/ProcessStrip.tsx`、`components/console/CompactRow.tsx`、`components/console/TurnBlock.tsx`、`components/console/TrajectoryView.tsx`、`components/console/RecordDetails.tsx`、`components/console/RowDetailPayloadResult.tsx`
- Modify(顺手,两处): `RecordDetails.tsx:240-242` 占位行空 `<dt>` → `<p className="ew-detail__desc">`(类已存在,PR-A.3 follow-up 加的);`RowDetailPayloadResult.tsx:66/:72` 死导出 `planGoal`/`planSteps` 删(全仓零 importer 已核)
- Test: `components/console/__tests__/{CompactRow,RecordDetails,TurnBlock}.test.tsx` 加 readOnly 断言;`components/__tests__/ToolTimeline.test.tsx` 加 ToolCallCard readOnly 断言

**Interfaces:**
- Consumes: TurnCard.tsx 现有实现(纯搬移,不改逻辑);`console_turns.runIdOf`(与被删副本逐字节相同)。
- Produces(后续任务依赖):
  - `components/turn/ApprovalGate.tsx` → `export function approvalItemFromEvent(event: SseEvent): ApprovalItem | null`、`export function ApprovalGate(props): JSX.Element`(签名与 TurnCard 里完全一致)
  - `components/turn/CommentarySegmentLine.tsx` → `export function CommentarySegmentLine({ text }: { text: string }): JSX.Element`(以 TurnCard 现签名为准)
  - `ToolCallCard` 新 prop `readOnly?: boolean`(默认 false;true 时 `FireNowButton` 整个不渲染)
  - `ProcessStripProps.readOnly?: boolean`、`CompactRowProps.readOnly?: boolean`、`TrajectoryViewProps.readOnly?: boolean`、`RecordDetailsProps.readOnly?: boolean`、`RowDetailResult` 的 `readOnly?: boolean` —— 全部默认 false,PlaygroundTab **零改动**(不传即旧行为)

- [ ] **Step 1: 搬移三导出**。逐块剪切到新文件,TurnCard 顶部换成 re-export;`runIdOf` 本地实现删除、改 import console_turns 并 re-export。搬移前后 `git diff --stat` 确认 TurnCard 只减不增逻辑。
- [ ] **Step 2: 四个 importer 改指新文件**(TurnBlock/Transcript/AnswerBubble/useRunEngine)。跑 `pnpm typecheck` + `pnpm exec vitest run src/components/console src/components/turn src/pages/agent_detail` 全绿(行为零变化,这一步不该红)。
- [ ] **Step 3: 新测试(写在搬移后、readOnly 改动前,先绿)**——`ApprovalGate.test.tsx`:渲染 pending approval 出「批准/拒绝」按钮、`approvalItemFromEvent` 对 approval 帧返回 item / 对非 approval 帧返回 null(fixture 从 TurnCard.test.tsx 相关用例抄);`CommentarySegmentLine.test.tsx`:>240 字符钳断 + FullTextTrigger 打开全文(把 TurnCard.test.tsx:201 那条 it 的断言搬来,原文件里那条**删除**——这是本任务唯一允许动的既有 it)。
- [ ] **Step 4: readOnly 链(先写失败测试)**:
```tsx
// CompactRow.test.tsx 追加
it("readOnly 下工具行不渲染「立即触发」按钮(收起与展开两个分支)", () => {
  // manage_task 工具行 fixture(照既有 fire-now 用例),render <CompactRow readOnly ...>
  // 断言 queryByTestId("fire-now-button") 为 null;点开展开态再断一次
});
// RecordDetails.test.tsx 追加
it("readOnly 下结果 tab 的 ToolCallCard 不渲染「立即触发」", () => { /* 同上,走 RowDetailResult 分支 */ });
```
  先跑确认红(prop 尚不存在 → typecheck 红即算红)。
- [ ] **Step 5: 实现 readOnly**。`ToolTimeline.tsx` ToolCallCard 加 `readOnly = false`,`readOnly` 时跳过 FireNowButton 渲染(:213 附近);ProcessStrip/CompactRow/TurnBlock(:190-197 往 ProcessStrip 传 `readOnly={readOnly}`)/TrajectoryView(转发给 RecordDetails :171 附近)/RecordDetails/RowDetailResult 逐级转发。跑 Step 4 测试转绿。
- [ ] **Step 6: 两处顺手**(空 dt + 死导出),`RecordDetails.test.tsx` 既有 placeholder 用例如断 dt 文本需同步(只许这一条跟随调整)。
- [ ] **Step 7: 门**:`pnpm typecheck`;`pnpm exec vitest run src/components src/pages/agent_detail`;`grep -rn "fire-now" apps/admin-ui/e2e/` 确认零引用。变异自证:临时把 ToolCallCard 的 readOnly 判断去掉 → Step 4 两条红 → 恢复绿(副本放 scratchpad,不用 git checkout)。
- [ ] **Step 8: Commit** `refactor(console): 抽离 TurnCard 活导出 + readOnly 穿透立即触发三条链`

### Task 2: 数据层小修 —— parseTimeline 引用缓存 + isPlan 单源 + ViewPane 提取(波 1,与 Task 1 无共同文件)

**Files:**
- Modify: `apps/admin-ui/src/api/timeline.ts`(parseTimeline 加 WeakMap 引用缓存)
- Modify: `apps/admin-ui/src/api/trajectory_rows.ts:117-122`(私有 `asThreadPlan` 删,改 `import { isPlan } from "./plan_reducer"`,:233 调用点跟随)
- Create: `apps/admin-ui/src/components/console/ViewPane.tsx`(PlaygroundTab.tsx:100-129 原样搬出,含 `console-view-pane-{view}` testid 与首次激活闩、`scroll` prop)
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx`(删本地 ViewPane,改 import)
- Test: `api/__tests__/timeline.test.ts` 加缓存断言;`api/__tests__/trajectory_rows.test.ts` 既有 plan 用例保持绿(不许改断言)

**Interfaces:**
- Produces: `parseTimeline(events)` 语义不变,新增保证:**同一数组引用两次调用返回同一对象**(TurnBlock 三重解析自然合一,不改 TurnBlock);`components/console/ViewPane.tsx` → `export function ViewPane({ view, active, scroll?, children })`(签名照搬)。
- Consumes: `plan_reducer.isPlan`(:20,已有测试)。

- [ ] **Step 1: 失败测试**:
```ts
it("parseTimeline 对同一 events 引用返回缓存对象,换引用重算", () => {
  const events = [/* 既有 fixture 抄一份 */];
  const a = parseTimeline(events);
  expect(parseTimeline(events)).toBe(a);          // 同引用 → 同对象
  expect(parseTimeline([...events])).not.toBe(a); // 新引用 → 重算
});
```
- [ ] **Step 2: 实现**。`const _cache = new WeakMap<readonly SseEvent[], Timeline>();` 入口查缓存、出口写缓存(照 `ledger.ts`/`absoluteSpans` 的 WeakMap 先例;events 数组在 console 全链只替换不变异,PR-A.2 已依赖此不变式)。测试转绿。
- [ ] **Step 3: isPlan 单源**。删 `asThreadPlan`,调用点换 `isPlan`;跑 `pnpm exec vitest run src/api` 全绿(plan 谓词语义 byte-level 相同才算完成,不同则停下写差异报告)。
- [ ] **Step 4: ViewPane 搬出**。纯位移;`pnpm exec vitest run src/pages/agent_detail src/components/console` 全绿。
- [ ] **Step 5: 门 + Commit** `refactor(console): parseTimeline 引用缓存 + isPlan 单源 + ViewPane 提为共享组件`

### Task 3: ConversationDetail 切换到 console 组件(波 2;spec PR4)

**Files:**
- Rewrite: `apps/admin-ui/src/pages/ConversationDetail.tsx`(574 → 预计 ≈420 行)
- Modify: `apps/admin-ui/src/components/console/TurnFooter.tsx`(加可选 `runHref?: string`,有值渲染 `<Link data-testid="console-turn-run-link">` 「查看运行」)+ `components/console/Transcript.tsx`、`TurnBlock.tsx`(把 `runHrefOf?: (turn: ConsoleTurn) => string | null` 透传到 TurnFooter;可选,不传零变化)
- Modify: `apps/admin-ui/src/pages/__tests__/ConversationDetail.test.tsx`(按下方迁移表)
- Modify: `apps/admin-ui/src/pages/ConversationDetail.stories.tsx`(同步新结构)
- i18n: `console.view_*` 复用;新键仅 `console.turn_view_run`(「查看运行」,en+zh 三处,先 grep 撞键)

**Interfaces:**
- Consumes: `useHistoryTurns`(不改)、`buildConsoleTurns` / `statsInputOf`(console_turns)、`computeSessionStats`、`Transcript`/`TrajectoryView`(Task 1 的 `readOnly`)、`ViewPane`(Task 2)、`usePlanCard` + `PlanCard readOnly`、`ConversationDetailModel.agent_name/agent_version`。
- Produces: 页面新结构(下),testid 保留:`conversation-detail-root`/`-error`/`conversation-tokens`;废弃:`conversation-runs`/`conversation-runs-table`/`conversation-run-open-*`/`conversation-run-error-*`/`conversation-turns`/`conversation-message-*`(flat fallback 保留 `conversation-messages` 容器与 `conversation-message-${i}`)。

**页面结构(实现骨架):**
```tsx
// PageHeader(原样)→ 摘要卡(原样,含 conversation-tokens)→
// stats 行:computeSessionStats(consoleTurns.map(statsInputOf), null) → <StatsBar stats isSystemAdmin/>
// 视图区(仅当配对成功 historyTurns !== null):
//   <Segmented data-testid="console-view-tabs" options={[chat, trajectory]}/>(无 workspace)
//   <ViewPane view="chat" active>
//     <Transcript turns={consoleTurns} readOnly isTenantSwitched={false}
//       onDecide={noop} deciding={false} taskResults={[]} liveByStep={EMPTY_MAP}
//       streamTurnKey={null} flatHistory={[]} registerHistoryRow={registerRow(...)}
//       onExport={handleExport} exportingKey={exportingId}
//       onDownloadArtifact={handleDownloadArtifact}
//       runHrefOf={(t) => t.runId ? `/runs/${threadId}/${t.runId}` : null} .../>
//   </ViewPane>
//   <ViewPane view="trajectory">
//     <TrajectoryView turns={consoleTurns} threadId agentName={convo.agent_name ?? ""}
//       agentVersion={convo.agent_version ?? ""} readOnly streamTurnKey={null}
//       liveByStep={EMPTY_MAP} running={false} visible={view==="trajectory"}
//       isSystemAdmin focusRequest={focusRequest} onEnsureLoaded={...}/>
//   </ViewPane>
//   底部:<PlanCard plan loaded running={false} readOnly/>(usePlanCard({threadId, liveEvents});GET 404 → plan null → 不渲染)
// 配对失败 fallback:原 flat HistoryMessage 块原样保留;Runs 表整卡删除。
```
`EMPTY_MAP` 用模块级常量(引用稳定,防每帧重建账本——PR-A.2 教训)。

**20 条既有 it 迁移表(逐条对号,评审按此验收):**

| # | it | 处置 |
|---|---|---|
| 1,2 | tenant scope / "*" 聚合 | 原样保留 |
| 3 | summary + run list | 改:摘要断言保留;run list 断言改为「每轮脚注有 `console-turn-run-link` 且 href=/runs/{t}/{r}」 |
| 4 | SDK error alert | 原样保留 |
| 5,6,7 | flat transcript / 隐藏面板 / 空态 | 原样保留(fallback 路径未动) |
| 8,9,10,11 | back link ×4 | 原样保留 |
| 12 | read-only turn cards | 改写:断 `console-view-tabs` 存在、TurnBlock 渲染、且 `fire-now-button`/`plan-edit`/审批按钮/FeedbackBar 全部 queryBy 为 null |
| 13 | 滚动进视口回放 | 保留机制断言,元素断言改 console 结构(工具行文本) |
| 14 | 导出真发请求 | 保留(TurnFooter onExport) |
| 15 | 失败轮 error banner | 改写:TurnBlock 的错误呈现 |
| 16 | error/timeout→failed 映射 | 原样保留(buildHistoryTurns 未动) |
| 17 | 计数不配对走 flat | 原样保留 |
| 18,19 | H-1 跨租户 reset / D3 own-tenant | 原样保留(数据流未动) |
| 20 | run 行钻取链接 | 改写:点 `console-turn-run-link` 导航恰一次 |

- [ ] **Step 1**: TurnFooter `runHref` + Transcript/TurnBlock 透传(先写 TurnFooter 测试:有 runHref 渲染链接、无则不渲染;先红后绿)。
- [ ] **Step 2**: 按迁移表改测试(红)→ 重写页面(绿)。逐条核对表,不许静默丢 it。
- [ ] **Step 3**: story 同步;`pnpm exec vitest run src/pages/__tests__/ConversationDetail.test.tsx src/components/console` 全绿;`pnpm typecheck`;`pnpm build`。
- [ ] **Step 4**: Commit `feat(conversation-detail): 对话记录页切 console 组件(只读)+ Runs 表撤 + 轮脚注查看运行`

### Task 4: RunDetail 切换到 console 轨迹(波 2;spec PR5;与 Task 3 不同文件)

**Files:**
- Rewrite: `apps/admin-ui/src/pages/RunDetail.tsx`(212 → 预计 ≈260 行)
- Create: `apps/admin-ui/src/pages/__tests__/RunDetail.test.tsx`(新;页面此前零页级测试)
- Delete(本任务只删测试与 story,组件文件留给 Task 5 统一删): `pages/__tests__/{PlanPanel,EventStreamPanel,TraceToolbar}.test.tsx`、`run_detail/{EventStreamPanel,TraceToolbar}.stories.tsx` **不删**——留给 Task 5;本任务只把 RunDetail.tsx 的 import 全部切走
- Modify: `apps/admin-ui/e2e/run_detail.spec.ts`(下表)

**Interfaces:**
- Consumes: `getRun` + `useStatusPolling`(原样)、`ApprovalCard`/`RunSummaryPanel`(原样)、`getConversation`(取 `agent_name`/`agent_version`)、`useHistoryTurns`(全量加载后 `turns.filter(t => runIdOf(t.events) === runId)`——用 `loads` 的 `loadRuns([runId])` 只回放目标 run,别全会话回放)、`buildConsoleTurns`、`TrajectoryView`(readOnly=false;`onFireResult` 照 PlaygroundTab 接可选)、`PlanCard` + `usePlanCard({threadId, liveEvents: 已加载 events})`。
- Produces: 页面结构 = PageHeader → ApprovalCard(条件)→ 元数据卡 → RunSummaryPanel → PlanCard(可编辑)→ TrajectoryView(单轮)。testid 保留 `run-detail-root`/`run-detail-error`。

**e2e `run_detail.spec.ts` 迁移表:**

| 用例 | 处置 |
|---|---|
| approval approve / reject ×2 | 不动(ApprovalCard 保留) |
| plan panel shows goal/steps + PUT | `plan-panel`→`console-plan-card`;`plan-edit`/`plan-step-input-1`/`plan-save` 同 id 不动;PUT 断言不动 |
| plan edit locked while live | 核对 PlanCard 的 running 态实现(disabled 还是不渲染),断言随实现(`toBeDisabled()` 或 `not.toBeVisible()`),**先在本地跑红再改** |
| axe | 不动,必须过(TrajectoryView 已在调试台过 axe) |

- [ ] **Step 1**: 新 `RunDetail.test.tsx`(先写,红):①渲染头部/元数据/RunSummaryPanel;②pending_approval 出 ApprovalCard;③PlanCard 渲染且 running=run 非终态时锁编辑;④轨迹区渲染该 run 的工具行(mock streamRunEvents 喂 fixture,断 `console-traj-ledger` 出现 + 行内容);⑤**单 run 过滤**:mock 会话含两个 run,只出目标 run 的轮;⑥getRun/getConversation 都带 tenant scope。
- [ ] **Step 2**: 重写页面(绿)。运行中 run:`streamRunEvents` replay+live 接合由服务端处理,`useStatusPolling` 原样保留刷状态。
- [ ] **Step 3**: e2e 按表改,`pnpm exec playwright test e2e/run_detail.spec.ts` 本地过。
- [ ] **Step 4**: 门(typecheck/vitest scope/build)+ Commit `feat(run-detail): Run 详情页换 PlanCard + console 轨迹视图`

### Task 5: 退役清扫(波 3,单任务)

**Files(全部 Delete,含各自测试/story;删前 grep 全仓确认零 importer):**

| 组 | 文件 |
|---|---|
| turn | `components/turn/TurnCard.tsx`(先把 Task 1 的 re-export 消费者清零:ConversationDetail 已在 Task 3 改走)、`components/turn/__tests__/TurnCard.test.tsx`、`components/turn/GanttTimeline.tsx`、`GanttTimeline.css`、`__tests__/GanttTimeline.test.tsx` |
| playground | `TraceView.tsx`、`StepTimeline.tsx`、`StreamingStepCard.tsx`、`AgentStatePanels.tsx`、`TurnMeta.tsx`、`RunStatusBanner.tsx`、`EntryBreakdown.tsx`、`entry_breakdown.ts`、`trace_tree.ts`、`trace_purpose.ts`、`trace_banner.ts`、`timeline_banner.ts` + 对应 14 个测试文件(TraceView/StepTimeline×2/StreamingStepCard/AgentStatePanels/TurnMeta/RunStatusBanner/EntryBreakdown×2/entry_breakdown/trace_tree/trace_purpose/trace_banner/timeline_banner) |
| run_detail | `EventStreamPanel.tsx` + `.stories.tsx` + `pages/__tests__/EventStreamPanel.test.tsx`、`TraceToolbar.tsx` + `.stories.tsx` + `pages/__tests__/TraceToolbar.test.tsx`、`PlanPanel.tsx` + `.stories.tsx` + `pages/__tests__/PlanPanel.test.tsx` |
| components | `CompactionCard.tsx`(importer 只剩 TurnCard/EventStreamPanel,同批删)、`EventCard.tsx` **保留**(RecordDetails:27 在用) |
| ToolTimeline | 文件保留;`ToolTimeline` 导出删(唯一消费者 EventStreamPanel 已死),`ToolCallCard` 留;`components/__tests__/ToolTimeline.test.tsx` 修剪 ToolTimeline 部分的 it |

**保留(明确不删,防评审误报):** `pages/agent_detail/playground/{duration_format,useTokenStream,history_turns,untrusted_clean,useRunEngine}.*` 及其 4 个测试、`api/gantt_timeline.ts`(ledger_timing:12 在用)+ 测试、`components/turn/{types,useHistoryTurns,download_json,FeedbackBar,FullTextModal,TaskResultCard,HistoryDivider,ApprovalGate,CommentarySegmentLine}.*`、`run_detail/{ApprovalCard,RunSummaryPanel}.*` + 测试。

**i18n(en.ts type+value 两块 + zh-CN.ts,共三处;删前每键 grep 消费者):**
- `event_stream.*` 12 键全删;`trace.breakdown_title` 删;`trace_toolbar.*` 留 `open_in_langfuse` 删其余 5;
- `playground.*`:写一次性脚本(node,读 locales 键名 → grep src/ 消费者)盘出零消费者键,输出清单进 commit message,逐键删;`session_history.*`/`tool_timeline.*`/`plan_panel.*` 只删脚本盘出的死键(plan_panel 预计死 PlanPanel 独占键)。

- [ ] **Step 1**: 删除顺序:先 run_detail 三组件 + CompactionCard → TurnCard + turn 附属 → playground 组;每删一组跑 `pnpm typecheck` 定位漏网 importer。
- [ ] **Step 2**: ToolTimeline 导出修剪 + 测试修剪。
- [ ] **Step 3**: i18n 脚本盘点 + 删键;`pnpm exec vitest run src/i18n` 的键一致性守卫过。
- [ ] **Step 4**: 残留 grep:`grep -rn "TurnCard\|TraceView\|StepTimeline\|EventStreamPanel\|TraceToolbar\|PlanPanel\|GanttTimeline\|TurnMeta\|RunStatusBanner\|AgentStatePanels\|EntryBreakdown\|CompactionCard\|StreamingStepCard" apps/admin-ui/src apps/admin-ui/e2e` 只允许命中注释/历史文档。
- [ ] **Step 5**: 全门:`pnpm typecheck` / `pnpm exec vitest run`(全量)/ `pnpm build` / storybook build / `pnpm exec playwright test`(全套,本地)。
- [ ] **Step 6**: Commit `chore(console): 退役 TurnCard 集群 + run_detail 旧面板 + 死 i18n 键(约 -8k 行)`

### Task 6: 终审 + 发布(控制器自己做,不派 SDD 实施者)

- [ ] 全分支终审(opus,`git merge-base` 为 base)→ 修复轮 → 复审
- [ ] push + 开 PR(合并等用户)
- [ ] 合并后 `tools/deploy/release.sh test` → 记录 PR(newTag + 注释搬回)→ Playwright 登录态冒烟:对话记录页(只读断言:无立即触发/无编辑/轨迹可用/查看运行跳转)+ Run 详情页(PlanCard 编辑、轨迹单轮、审批卡)+ 调试台回归(零变化抽查)

## 波次与冲突表

| 波 | 任务 | 文件冲突检查 |
|---|---|---|
| 1 | T1 ‖ T2 | T1 碰 console 组件 + ToolTimeline + turn 新文件;T2 碰 api/timeline、api/trajectory_rows、ViewPane(新)、PlaygroundTab —— 零交集 |
| 2 | T3 ‖ T4 | T3 碰 ConversationDetail + TurnFooter/Transcript/TurnBlock(runHref 链);T4 碰 RunDetail + e2e —— 零交集(T4 不动 TurnFooter) |
| 3 | T5 | 独占 |
| — | T6 | 控制器 |

## Self-review 记录

- spec 覆盖:PR4(T3)/PR5(T4)/退役表(T5,§三 原表已按 §九 与本次重核修正)/「立即触发 gate」(T1)/「单 run 轨迹与调试台一致」(T4 Step 1-⑤ 用同 fixture 断行)。
- 无占位符;类型/签名前后一致(readOnly 可选默认 false 贯穿;runHrefOf 命名统一)。
- 已知风险:①Transcript 必填 props 较多,T3 传空值集合要照骨架逐个给,漏传 typecheck 会拦;②`plan edit locked while live` 的 e2e 断言形态取决于 PlanCard 实现,T4 明确「先跑红再改」;③i18n 死键盘点必须脚本化,人工数 162 键必错。
