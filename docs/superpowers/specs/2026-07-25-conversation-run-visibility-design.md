# 对话详情页 = 只读调试台:run 执行过程可见 — 设计文档

## 背景(侦察结论,2026-07-25 按 main@8d3a71f1)

用户实测反馈:"对话记录里完全没有办法看到整个 run 的运行情况,跟调试台的能力差太多"。三层缺口:

1. **消息被后端过滤成断层**:`transcript.py:63-77` `read_turns` 只保留 `human`/`ai` 且 `content` 非空的消息 —— 所有 tool 消息被丢弃,**纯 tool_calls 的助手轮次整条消失**。前端 `HistoryMessage` 只有 `{role, content}`(`api/sessions.ts:114`),连字段都拿不到。截图里"我来搜索…"→"已找到基本信息"之间真正干活的搜索/抓页/跑 Python 全部不可见。
2. **能下钻但没人知道**:`ConversationDetail.tsx:329-335` 的 run 行已 `navigate('/runs/{thread}/{run}')`,但除 cursor 外零视觉提示。
3. **下钻页也弱一档**:`RunDetail` 只有事件流卡片;调试台独有的 `StepTimeline`/`TraceView`/trace 树/`AgentStatePanels` 一个都没复用。

**复用可行性(关键结论)**:
- 调试台的历史轮重建链路(#980)**页面无关**:只吃 `thread_id` → `Promise.all([getSessionMessages, listThreadRuns])` → `buildHistoryTurns` 配对 → 滚入视口时 `streamRunEvents(thread, run)` replay → 渲染只读 `TurnCard`。与 playground 的唯一绑定是"resume 一个 thread 继续聊"这个**动作语义**,对话页只需改成"挂载即以 `useParams().threadId` 启动"。
- 组件层**零 context/store 耦合**(全量 grep 只命中两处 antd `App.useApp()`)。TurnCard 17 个 props 纯数据驱动,`readOnly`/`loadState`/`fallbackAnswer` 三个 props 本来就是为这个只读场景设计的。
- 真正的障碍只是**符号可见性**:`TurnCard`(:1957-2634,678 行)、`ApprovalGate`、`FeedbackBar`、`TaskResultCard`、`HistoryDivider`、`runIdOf`、`approvalItemFromEvent` 与 `Turn`/`Attachment`/`HistoryLoad` 类型全是 `PlaygroundTab.tsx`(2634 行)内未导出的 file-local 符号;重建流程(state + fetch + IntersectionObserver + 3 个 ref)也没有 hook 封装。
- 后端**全部现成**:`GET /v1/sessions/{t}/runs/{r}/events`(终态 run 一次性 replay + `end` 帧)、`GET /v1/sessions/{t}/runs`、`GET …/trace`。无需任何后端改动。

## 用户拍板(2026-07-25)

> "效果跟调试台做成一致不就行了?"

据此锁定三条,**不发明新设计**:

| # | 决策 | 结论 |
|---|------|------|
| D1 | 呈现形态 | 消息记录块整体换成**调试台同款轮次卡时间线**(懒加载重建),不是"保留文本+run 行展开",也不是"只强化下钻页" |
| D2 | 降级 | 沿用调试台现行四层降级(配对失败→扁平文本;replay 空/截断→保 fallback),**不自造新规则** |
| D3 | 跨租户 | 调试台本身就是租户内视图,故本批**不做**;system_admin 跨租户抽查时自动退回现有扁平文本(replay/trace 端点无 `tenant_id` 参数是既有事实,不在本批开这个口子) |

## 设计

### A. 提取:TurnCard 家族 + 重建 hook → 共享目录

**零逻辑改动的纯搬家**,搬完 `PlaygroundTab.tsx` 从 2634 行降到约 1650 行(顺带解掉"单文件过大"的既有债)。

1. 新目录 `apps/admin-ui/src/components/turn/`(与既有跨页共享件 `ToolTimeline`/`EventCard`/`MarkdownView` 同级先例):
   - `TurnCard.tsx` — `TurnCard` + `ApprovalGate` + `runIdOf` + `approvalItemFromEvent`;
   - `FeedbackBar.tsx` / `TaskResultCard.tsx` / `HistoryDivider.tsx`;
   - `types.ts` — `Turn` / `Attachment` / `HistoryLoad`;
   - 全部 export。`PlaygroundTab.tsx` 改为 import,**其余一行不动**。
2. `useHistoryTurns(threadId: string | null)` hook(落 `components/turn/useHistoryTurns.ts`):把 #980 的重建流程整体搬出——`historyTurns`/`historyLoads` state、`Promise.all` 拉取、`buildHistoryTurns` 配对、`replayHistoryRun`、`registerHistoryRow`(共享 IntersectionObserver + `runIdByEl` 去重 + `startedHistoryRunsRef` one-shot 守卫)、陈旧请求 abort 守卫。
   - 返回 `{ turns, loads, registerRow, reload }`。
   - playground 侧行为不变:`handleResume` 改为调 hook 的 `reload(threadId)`;**既有 5 个 history 测试(`PlaygroundTab.test.tsx:1675-1930`)必须全绿**,这是"零回归"的判据。
3. i18n:TurnCard 文案现处 `playground.*` 命名空间。**不改键**(改键要动两个 locale + interface + 全部引用,收益为零),在共享组件里继续读 `playground.*`,docstring 注明该命名空间现为跨页共享。

### B. 对话详情页接入

1. `ConversationDetail.tsx` 的「消息记录」卡内容替换:
   - `useHistoryTurns(threadId)` → `turns !== null` 时逐轮渲染 `<TurnCard readOnly loadState={loads[runId]} fallbackAnswer=… />`,外包一层带 `registerRow` ref 的 div(滚入视口才 replay);
   - `turns === null`(配对失败/`listThreadRuns` 失败)→ **保留今天的扁平文本渲染**(D2);
   - 回调传 no-op(`onDecide`/`onFireResult`),`rate: null`,`isSystemAdmin` 来自 `useAuth()` —— 与 playground 历史轮渲染处(`:1483`)同款。
2. 跨租户(D3):`getConversation` 拿到的 `tenant_id` 与当前租户不一致时,直接走扁平路径(不发 replay 请求),避免必然 404 的噪音。
3. run 表格:每行加"查看运行"链接列(下钻仍指 `/runs/{thread}/{run}`),解决"能点但没人知道"。行级 onClick 保留。

### C. fallbackAnswer 保真(唯一一处**有意偏离**纯照搬)

`history_turns.ts:buildHistoryTurns` 现在的 `fallbackAnswer` 只取用户消息**紧邻的下一条** assistant 消息。而一个 run 常产出多条 assistant 消息(截图那轮就有 7 条)。后果:配对成功但 replay 失败时,只显示第一条,**比今天的扁平视图信息更少**。

改为:收集该用户消息之后、下一条用户消息之前的**全部** assistant 消息,以空行拼接。`pairs.length !== runs.length → null` 的配对判据**不变**。playground 与对话页同时受益(共享 helper),是保真而非行为发明。

## 错误处理

- 沿用 #980 四层降级(spec 背景已列),不新增分支。
- replay 请求失败/超时:该轮标 `error`,显示 input + fallbackAnswer,其余轮不受影响(逐轮独立)。
- 跨租户:不发请求,整页走扁平(B2)。

## 测试

- **零回归判据**:`PlaygroundTab.test.tsx` 全量(尤其 `:1675-1930` 的 5 个 history 用例)在提取后不改一字全绿。
- 提取后新增 `components/turn/__tests__/`:TurnCard 只读渲染冒烟(现无 TurnCard 级 story/测试,提取后补一个)。
- `useHistoryTurns` hook 单测:配对成功/失败、懒触发只跑一次、陈旧请求丢弃。
- `ConversationDetail.test.tsx` 新增:配对成功渲染轮次卡、滚入视口触发 replay、配对失败退扁平、跨租户退扁平、run 行"查看运行"链接。既有 8 例保持绿。
- `history_turns.test.ts`:多条 assistant 消息全部进 fallbackAnswer;配对判据不变(计数不等仍 null)。
- 变异自验:去掉 `registerRow` 的 one-shot 守卫 → 重复 replay 测试红;把 C 的多消息收集改回只取下一条 → 保真测试红。

## 范围外

- 后端 `read_turns` 过滤规则(它是"人类可读 transcript"的语义,另有消费方;本方案绕开它走事件流重建,不动)。
- replay/trace 端点的 `tenant_id` 跨租户参数(D3)。
- `RunDetail` 页升级为调试台同款(本方案让对话页直接内联可见,下钻页保持现状;若后续仍需要,复用同一批提取出的组件即可)。
- token 流式打字机(`StreamingStepCard`/`useTokenStream`)——历史轮是终态 replay,无 live token 流。
