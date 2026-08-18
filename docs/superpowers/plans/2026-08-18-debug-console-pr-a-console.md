# 调试台重设计 PR-A —— 三栏调试台(壳 + 轨迹面板)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 调试台从「左输入 / 右每轮一张大卡」的两栏页,变成「左会话列表 / 中连续对话流 / 右检查面板」的三栏客户端形态;对话流用紧凑行讲每轮发生了什么,任务卡钉在输入框上方,变量表单挡住必填未填,状态栏给会话级数字;右栏「轨迹」tab 直接是新的 `TrajectoryPanel`(三泳道时间条 + 扁平行列表 + 行详情五 tab:Summary / Payload / Result / Timing / Raw),「工作区」tab 装现有工作区面板。本计划 = spec §五 的 **PR2 + PR3**(用户 2026-08-18 拍板合成两个 PR:PR-A 本计划;PR-B = PR4 + PR5 + 全部旧组件退役)。

**Architecture:** `PlaygroundTab.tsx`(1476 行单组件)拆成「状态与请求逻辑留在 PlaygroundTab(`ensureThread / startRun / streamRun / 审批续跑 / 上传`)」+「`components/console/*` 一组纯展示 / 局部状态组件」。数据一份:每轮 `Turn.events` 走现有 `parseTimeline / summarizeTurn / buildGanttRows`;新增纯投影 `api/trajectory_rows.ts`(事件 → 中栏紧凑行 `compactRowsOf` / 右栏轨迹行 `trajectoryRowsOf`,同一 id 体系,右栏行集 ⊇ 中栏)、`api/plan_reducer.ts`(事件 → 当前计划)、`api/session_stats.ts`(事件 → 状态栏数字)、`api/trace_match.ts`(轨迹行 ↔ Langfuse span 配对,Timing 双列用)。`TurnCard / TraceView / GanttTimeline(组件)/ StepTimeline / EventCard` **本 PR 不改也不删**(对话记录页 / Run 详情页还在用,PR-B 切页时一次退役);`api/gantt_timeline.ts` 的 `buildGanttRows` 复用为泳道条数据;`api/timeline.ts` 只加一个可选字段 `eventIndex`。后端只加两处只读增强:`GET /v1/sessions?order_by=` 与 `GET /v1/sessions/{thread_id}/runs` 每行带 `tokens`。

**Tech Stack:** React 18 + antd 5.29(已装;`Splitter` 用于轨迹面板的上下分割)+ react-i18next + vitest/RTL(admin-ui);FastAPI + pytest(control-plane)。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` §一(现状)、§二.1(调试台目标形态,含右栏轨迹)、§二.4 前端段、§三(前端结构)、§四 PR2 行、§五 PR2 + PR3 行、§七 第一、三条风险。

## Global Constraints

- **行为清单迁移**(spec §七):`apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx` 现有 **54 条 `it`**(本计划「行为清单迁移表」逐条列出),每条要么在该文件里改写后仍绿,要么迁到新组件测试;**不许静默删除任何一条**。删一条要在台账写明理由。
- 现有 `data-testid` 前缀 `playground-*` 在**同一控件仍存在**时保留(输入框 / 发送 / 附件 / 变量 / 审批 / 重试 / 导出 / 工作区 / 反馈 / 元数据 / run 链接);**新增**元素一律 `console-*`。理由:54 条测试大半按 testid 查询,保留可让迁移是「改写」不是「重写」。
- i18n:新增 `console.*` 命名空间(`en.ts` 接口块 + `en.ts` 值块 + `zh-CN.ts` 值块三处);**沿用**既有 `playground.*`(输入区 / 工作区 / 元数据 / 反馈)、`session_history.*`(会话列表)、`plan_panel.*`(任务卡)键;不删旧键(PR-B 退役旧组件时一起删)。新键先 grep 是否撞既有(同 object 重复键 esbuild 静默覆盖)。
- 组件全部放 `apps/admin-ui/src/components/console/`(spec §三),纯函数放 `apps/admin-ui/src/api/`;单文件 ≤ 400 行,超了拆。
- 每条新断言先在未改代码上跑红(或跑不过编译)再改绿;纯函数测试对着真实事件形状写 fixture(`data` 里 `messages[].type/content/tool_calls/additional_kwargs/usage_metadata`、`step_count`、`_duration_ms`),不写「内层形状」fixture(PR0 教训)。
- 前端命令在 `apps/admin-ui` 下:`pnpm exec vitest run <file>`(单文件)、`pnpm typecheck`(必须用它,裸 `tsc --noEmit` 恒绿)、`pnpm exec eslint <files>`、`pnpm build`;后端在仓库根 `uv run pytest services/control-plane/tests/<file> -q`、`uv run ruff check --fix` + `uv run ruff format`、`uv run mypy services/control-plane`。
- 集成测试需要 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`(本 PR 后端改动只动 API 层,in-memory 即可)。
- 中文正文全角标点;代码 / 提交信息照常。
- 只改本 PR 要碰的文件;`TurnCard.tsx`、`TraceView.tsx`、`components/turn/GanttTimeline.tsx`、`StepTimeline.tsx`、`EventCard.tsx`、`ConversationDetail.tsx`、`RunDetail.tsx`、`api/gantt_timeline.ts` **不动**;`api/timeline.ts` 只允许加可选字段 `eventIndex?: number`(Task 4),其余不动。本 PR **不删**任何旧组件(`SessionHistoryDrawer` 除外——只有 PlaygroundTab 用它);退役全部在 PR-B。
- 特性分支 `feat/debug-console-pr-a`,worktree `.worktrees/debug-console-pr-a`,基于 `main`(PR1 #1202 / 品牌 #1203 #1204 已合;记录 PR #1206 只改 k8s overlay,与本 PR 无交集);SDD 台账 `.superpowers/sdd/2026-08-18-debug-console-pr-a-console/progress.md`;本计划文件随分支一起提交。
- 测试渲染:凡是用到 `App.useApp()` 的组件(PlanCard 的 `message`、ToolCallCard 的立即触发)测试都要包 antd `<App>`;`PlaygroundTab.test.tsx` 的 `renderPg` / `pgTree` 在 Task 19 一并加 `<App>` 包裹(现在没有——`fire-now` 那组能过是因为它们不走 message 分支)。
- **并行波次**(用户要求能并行就并行;每波并发 ≤ 6;墙钟 = 5 波而不是 19 步):

| 波 | 并行 task | 依赖 |
|---|---|---|
| 1 | 1 · 2 · 3 · 4 | 互不相依 |
| 2 | 5 · 6 · 7 · 8 · 9 · 14 | 5 需 1+4;6 需 1+2;7 / 9 需 2;8 需 2+3;14 需 4 |
| 3 | 10 · 12 · 13 · 15 · 16 · 17 | 10 需 4+5;12 需 5;13 需 2;15 / 16 需 4;17 需 4+14 |
| 4 | 11 · 18 | 11 需 5+10;18 需 5+14+15+16+17 |
| 5 | 19 | 全部 |
| — | 20 | 合并后 |

  并行规矩:①每个 task 一个 worktree `.worktrees/pr-a-t<N>`、分支 `feat/pr-a-t<N>`,从特性分支 `feat/debug-console-pr-a` **当前 HEAD** 切(dispatch 第一步 `git merge --ff-only feat/debug-console-pr-a` 确认同步);task 内只跑定点测试 + `pnpm typecheck`(前端)/ 定点 pytest + ruff + mypy(后端),**不跑全套**;②每波结束控制器按 task 号顺序 `git merge --no-ff feat/pr-a-t<N>` 进特性分支,只合评审 Approved 的,合完跑一次全门(`pnpm typecheck && pnpm exec vitest run && pnpm exec eslint src`;含 Task 1 的波再跑 `uv run pytest services/control-plane/tests/test_sessions_api.py services/control-plane/tests/test_runs_api.py -q` + ruff + mypy),红了先修再开下一波;下一波从新 HEAD 切;③i18n 漏键:实现者在 `console` 块**末尾**追加(三处同步)并在报告里列出,同一波两支同处追加的冲突由控制器合并时手解(两边都留);④评审包用 `<该 task 切出时的特性分支 HEAD>..<task 分支 HEAD>`,不用 `HEAD~1`、不与兄弟分支比;⑤实现者报告文件 `.superpowers/sdd/…/task-<N>-report.md`,同波不同 N 不撞;⑥同波多个 worktree 各自 `pnpm install --frozen-lockfile --prefer-offline`(pnpm 硬链接,秒级);后端 worktree `uv sync` 走缓存。
- 测试里遇到 antd `Splitter`(Task 18):jsdom 下面板尺寸为 0 但子节点仍在 DOM,测试只断 DOM 存在 / 文案 / 回调,不断尺寸;若实测 `Splitter.Panel` 在 jsdom 不渲染子节点,在该测试文件里 `vi.mock("antd", async (orig) => ({ ...(await orig()), Splitter: 透传容器 }))` 并在台账记一笔。

---

## 裁定(spec 没写死或写错的地方,执行前先定;用户过目时可否决)

| # | 事项 | 裁定 | 理由 / 错了的代价 |
|---|---|---|---|
| R1 | 右栏「轨迹」tab 怎么「把 TurnCard 的事件面板抽出来」 | **不改 TurnCard,也不做过渡组件**:右栏直接上新 `TrajectoryPanel`(spec §二.1 右栏形态;PR2 + PR3 合并后没有「先原样装三档」这一步);旧三档视图(`TraceView / GanttTimeline / StepTimeline / EventCard`)本 PR 只作为 `TurnCard` 的依赖继续存在,PR-B 切完对话记录页一起删;TurnCard / StepTimeline 里要复用的小逻辑(`lastKnownFrame`、`runIdOf`、trace 拉取 / 轮询、`asMemories` 等取值 helper)**复制**到新文件而不是 import,PR-B 删旧文件时不牵连;大组件(`ApprovalGate` / `CommentarySegmentLine`,现导出自 TurnCard.tsx)照旧 import,PR-B 退役时挪成独立文件 | 省掉 ~350 行过渡代码和 9 条测试二次迁移;代价:PR-A 变大(19 个代码 task),靠并行波次压墙钟 |
| R2 | 状态栏「未加载的历史轮用 `listThreadRuns` 的持久 rollup(`RunTokens`)」 | **spec 事实错**:`GET /v1/sessions/{thread_id}/runs` 只回 `run_id/status/is_resume/created_at`(`api/runs.py:1639-1647`),没有 tokens。本 PR 给它加 `tokens`(与 `get_run` 同源 `_tokens_to_dict` + `totals_by_trace_ids` 一次批量),SDK `ThreadRunSummary.tokens`、`HistoryTurn.tokens` 透传 | 不加则历史轮 token 数只能靠懒重建后的事件,状态栏「输入 K · 输出 K」在长会话上不完整。加是 ~20 行只读增强,不改契约形状(多一个键) |
| R3 | 「进行中的会话带活动点」怎么知道哪条在跑 | 活动点 = **当前会话且客户端 `running`**;不轮询后端 | 后端没有「正在跑」的会话列表字段;单用户调试台同一时刻只有当前会话在跑 |
| R4 | 「窄于 1200 px 折成图标条」的断点实现 | **CSS 媒体查询**(`console.css`),不加 JS 断点 hook;图标条点开 antd `Drawer` 装同一个 `SessionSidebar` | 测试环境 `matchMedia` 恒 `matches:false`(`test/setup.ts:21-36`),JS hook 会让所有测试落在窄屏分支;CSS 在 jsdom 不生效 → 测试恒宽屏 |
| R5 | `Enter` 发送 / `Shift+Enter` 换行 | 新增(现状没有);**IME 组合中(`nativeEvent.isComposing`)不发送** | 中文输入法回车选词会误发,必须守 |
| R6 | 计划卡三来源优先级 | 一份 `plan` 状态,按**时间顺序**被三处写入:打开会话 `GET` 基线 → live 轮事件里最新一条计划快照(`plan` 帧或 `updates.*.plan`,按 sourceKey 去重只应用一次)→ `PUT` 成功回显。历史轮懒重建出来的旧快照**不**写入 | 若历史事件也写入,会用旧快照盖掉基线;若不去重,PUT 后下一帧任意 `updates` 会把编辑结果盖回旧值 |
| R7 | resumed 提示条(`playground-resumed-notice`) | **退役**——左栏选中态已表达「你在哪个会话」 | 相应测试改断言「点会话 → 拉历史」 |
| R8 | 轮脚注 | 直接复用 `TurnMeta`(用量 / 步数 / 耗时 / 模型 / 成本 / 「查看运行」链,testid 不变)+ `FeedbackBar` + 三个按钮(重试 / 导出 / 检查) | 少写一份、测试兼容;PR-B 再统一样式 |
| R9 | 右栏跟随 | `selectedTurnKey: string \| null`;`null` = 跟随最新一轮;`startRun` 时置 `null`;点脚注「检查」或点轮块置为该轮 | 简单可测 |
| R10 | 三栏高度 | 根 `height: calc(100vh - 360px)`(与现状右栏一致),常量 `CONSOLE_HEIGHT_OFFSET_PX = 360` | 不盲调;上测试环境有截图再调 |
| R11 | 变量值「随会话保留(切会话清空)」 | `resetDraft` 与 `handleResume` 都清 `varValues` | 现状不清(跨会话残留),spec 明说要清 |
| R12 | 状态栏「缓存命中 % = cache_read ÷(input + cache_read + cache_creation)」 | **改为 `cache_read ÷ input`**:LangChain `usage_metadata.input_tokens` 是「全部输入 token 之和」,`input_token_details.cache_read / cache_creation` 是它的子集(现有 `TurnCard.costCny` 也按 `input - cache_read` 算非缓存输入);持久 rollup(`token_usage`)同源。spec 公式分母重复计了缓存 | 按 spec 算会把命中率算低一截;错了的代价:一个除法改回去 |
| R13 | 右栏轨迹行的集合与 id | 右栏行 = `user` + **每个 agent 步一条 `think`**(reasoning 为空也出,摘要显示「模型调用 · <model>」)+ 其余同中栏紧凑行 + `assistant`(答案非空时);中栏紧凑行只在 reasoning 非空时出 think 行。两投影同一套 id(`kind:seq[:idx]`),右栏 ⊇ 中栏,中栏「检查」按 id 定位右栏行 | 没有 reasoning 的模型调用(纯工具调用步、最终答案步)在右栏也得有一行挂模型 / tokens / Timing;中栏不出空思考行免噪音。代价:两条规则一处 flag 控制,一起测 |
| R14 | Raw tab 的「该行对应的原始帧」 | `parseTimeline` 每个 item 加**可选**字段 `eventIndex`(来源帧在 `events` 里的下标);行的 `eventIndexes` = 步帧 + 工具结果帧(按 `tool_call_id` 扫)+ worker 帧(按 `worker_id` 扫)+ 合并进来的 planner 帧;Raw tab 用 `EventCard` 渲染这些帧 | 不改 `tool_timeline.ts`;可选字段不动现有手写 item 字面量的测试;一行改动向后兼容 |
| R15 | Timing 双列的 span 配对规则(`api/trace_match.ts`) | 主对话 llm span(`purpose` 为 `""` / `"main"`)按顺序 ↔ think 行,**仅当数量相等**(沿用 `labelPurpose` 判据);tool span 按 `label === toolName` 同名第 n 条 ↔ 第 n 条同名 tool 行(`update_plan` 同理);aux:planner→`planner`、reflect→`reflect`、memory 写回→`memory`、memory 召回→`rerank`、compaction→`compress`,同 purpose 第 n 条;配不上 → Langfuse 列显示「无法对齐」;user / assistant / subagent / marker 行显示「无对应 span」 | Langfuse span 与 SSE 帧没有共享 id,只能按序配;错配只影响 Timing 展示,不影响其它 tab |
| R16 | trace 什么时候拉 | 轨迹面板显示某一轮即拉该轮 trace(不再等切到「精确」档);`not_ready` 6 × 1.5 s 轮询、run 结束重新武装一次、Langfuse 深链 admin-only(`getRun` 取 `trace_id`)全部照 TurnCard 现状搬进 `useRunTrace` | 五 tab 里 Timing / Payload 都要它;一轮一次拉取,量与现状「切精确档才拉」同量级 |
| R17 | 行详情区布局 | antd `Splitter layout="vertical"`(上行列表 / 下详情,默认 55 / 45,可拖);**没选中行时不渲染 Splitter**(只有列表);jsdom 下 Splitter 面板尺寸为 0 但子节点在 DOM,测试只断 DOM / 文案 / 回调 | spec 明写「下半区、中间可拖」;antd 5.29 自带,不引新依赖。代价:jsdom 若不渲染子节点则该测试文件局部 mock Splitter(全局约束已写) |
| R18 | 右栏选中态 | 轮级 `selectedTurnKey` 同 R9;面板内 `selectedRowId`,轮切换清空;中栏「检查」传 `(turnKey, rowId)` → 父级切轮 + `focusRowId` → 面板选中并滚到该行;banner 的「跳到错误」= 选中第一条 `status === "error"` 的行;泳道块点击 = 选中 `resolveGanttKey` 得到的行 | 一份选中源(rowId),三处入口(行 / 泳道 / 中栏)都写它 |
| R19 | 旧三档视图 9 条测试的去处 | timeline banner 三条 → `TrajectoryPanel.test`(`RunStatusBanner` 保留在面板头,`timelineBannerModel` 同源);exact 六条 → `useRunTrace.test`(懒拉 / `not_ready` 轮询 / 结束重新武装 / unavailable)+ `RowDetailTiming.test`(loading / not_ready 刷新 / error span 红字)+ `TrajectoryPanel.test`(Langfuse 链 admin 显隐、面板显示即拉 trace) | 行为一条不丢,只是载体换了 |
| R20 | 泳道映射与 `AgentStatePanels` | `GanttRow.kind` `aux`→输入泳道、`agent` / `final`→模型泳道、`tool` / `worker`→工具泳道;标记画竖线;运行中轴生长复用 TurnCard 的 `lastKnownFrame` 算法(复制进 `lane_strip_model.ts`)。`AgentStatePanels` / `CompactionSummaryList` **不**嵌进右栏:它们展示的召回记忆 / 工具失败 / 反思 / 压缩 / 守卫信号已分别是 memory / tool / reflect / compaction / guard 行的详情 | 与 spec §三「AgentStatePanels 的子面板(进 RowDetail)」一致的落法;不重复摆两份 |

---

## 文件结构

| 文件 | 改动 | Task |
|---|---|---|
| `services/control-plane/src/control_plane/api/sessions.py:758-811` | `order_by` 查询参数透传 store | 1 |
| `services/control-plane/src/control_plane/api/runs.py:1588-1651` | `list_thread_runs` 每行加 `tokens` | 1 |
| `services/control-plane/tests/test_sessions_api.py`、`tests/test_runs_api.py` | 各加测试 | 1 |
| `apps/admin-ui/src/api/sessions.ts:149-175` | `listSessions({ orderBy })` | 1 |
| `apps/admin-ui/src/api/runs.ts:250-281` | `ThreadRunSummary.tokens` | 1 |
| `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts` | `HistoryTurn.tokens` 透传 | 1 |
| `apps/admin-ui/src/i18n/locales/en.ts`、`zh-CN.ts` | `console.*` | 2 |
| `apps/admin-ui/src/api/plan_reducer.ts`(新)+ 测试 | `planFromEvent / reducePlan` | 3 |
| `apps/admin-ui/src/pages/agent_detail/playground/useTokenStream.ts` + 测试 | `firstTokenAt / lastTokenAt` | 3 |
| `apps/admin-ui/src/api/trajectory_rows.ts`(新)+ 测试;`api/timeline.ts` 加可选 `eventIndex` | `compactRowsOf / trajectoryRowsOf / resolveGanttKey` | 4 |
| `apps/admin-ui/src/api/session_stats.ts`(新)+ 测试 | `computeSessionStats` | 5 |
| `apps/admin-ui/src/components/console/types.ts`、`console_turns.ts`、`live_rows.ts`(新)+ 测试 | `ConsoleTurn` 视图模型 + `buildConsoleTurns` + live 合成行 | 5 |
| `apps/admin-ui/src/components/console/SessionSidebar.tsx`(新)+ 测试 | 会话列表 | 6 |
| `apps/admin-ui/src/components/console/VariablesForm.tsx`、`Composer.tsx`、`AttachmentChips.tsx`(新)+ 测试 | 输入区 | 7 |
| `apps/admin-ui/src/components/console/PlanEditForm.tsx`(新,从 PlanPanel 抽)、`PlanCard.tsx`、`usePlanCard.ts`(新)+ 测试;`pages/run_detail/PlanPanel.tsx` 改用 PlanEditForm | 任务卡 | 8 |
| `apps/admin-ui/src/components/console/WorkspacePanel.tsx`、`useUserWorkspace.ts`(新)+ 测试 | 工作区 | 9 |
| `apps/admin-ui/src/components/console/UserBubble.tsx`、`CompactRow.tsx`、`AnswerBubble.tsx`、`TurnFooter.tsx`(新)+ 测试 | 对话流叶子 | 10 |
| `apps/admin-ui/src/components/console/TurnBlock.tsx`、`Transcript.tsx`(新)+ 测试 | 对话流 | 11 |
| `apps/admin-ui/src/components/console/StatsBar.tsx`(新)+ 测试 | 状态栏 | 12 |
| `apps/admin-ui/src/components/console/ConsoleShell.tsx`、`console.css`、`InspectPanel.tsx`(新)+ 测试 | 壳 + 右栏 tab 容器 | 13 |
| `apps/admin-ui/src/api/trace_match.ts`(新)+ 测试;`components/console/useRunTrace.ts`(新)+ 测试 | 轨迹行 ↔ span 配对 + trace 拉取 hook | 14 |
| `apps/admin-ui/src/components/console/lane_strip_model.ts`、`LaneStrip.tsx`、`lane_strip.css`(新)+ 测试 | 三泳道时间条 | 15 |
| `apps/admin-ui/src/components/console/TrajectoryRows.tsx`、`trajectory_rows.css`(新)+ 测试 | 轨迹行列表 | 16 |
| `apps/admin-ui/src/components/console/RowDetail.tsx`、`RowDetailPayloadResult.tsx`、`RowDetailTiming.tsx`(新)+ 测试 | 行详情五 tab | 17 |
| `apps/admin-ui/src/components/console/TrajectoryPanel.tsx`(新)+ 测试 | 轨迹面板容器 | 18 |
| `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx` | 组装壳;`PlaygroundTab.test.tsx` 逐条迁移;`PlaygroundTab.stories.tsx` 更新;删 `components/SessionHistoryDrawer.tsx` + story + test | 19 |
| —(合并后)| 上线 + 真栈冒烟 | 20 |

依赖与并行波次见「全局约束 · 并行波次」表(五波)。

---

## 行为清单迁移表(`PlaygroundTab.test.tsx` 54 条 `it`)

「留」= 仍在 `PlaygroundTab.test.tsx`,按新 DOM 改写查询;「迁」= 搬到括号里的组件测试;每条 Task 19 收口时逐条对号。

| 行 | 现有 `it` | 去向 | 改写要点 |
|---|---|---|---|
| 235 | does not create a thread on mount; creates it lazily on first send | 留 | 不变 |
| 261 | streams events from streamRun and renders them in the log | 留 | 断言改查 `console-turn` 内的答案文本 |
| 302 | renders an inline download for an artifact the turn registered | 留 | testid `playground-turn-artifact-download` 保留(AnswerBubble) |
| 371 | exports the turn's authoritative event stream as JSON | 留 | 导出按钮在 TurnFooter,testid `playground-export-json` 保留 |
| 432 | lists artifacts with download/delete and hides dotfiles from files | 迁(WorkspacePanel.test)| 右栏「工作区」tab;PlaygroundTab.test 留一条「工作区 tab 可切」 |
| 485 | shows a stream-failure alert when streamRun throws | 留 | `playground-turn-error` 保留(AnswerBubble) |
| 501 | shows a session-failure alert when the lazy createSession rejects | 留 | `playground-session-error` 保留(中栏顶部) |
| 515 | disables Run while the input is empty | 迁(Composer.test)+ 留一条集成 | |
| 522 | uploads an attached image and sends its ref with the run | 留 | 附件按钮 testid 不变 |
| 565 | uploads a document and surfaces its workspace path in the run prompt | 留 | 同上 |
| 609 | renders declared prompt variables and sends their values as inputs | 留 | `playground-var-<name>` 保留 |
| 660 | does not treat a bare inner spec as a jinja agent | 留 | 不变 |
| 680 | shows an upload-error alert and keeps Run usable when upload fails | 留 | `playground-upload-error` 保留 |
| 699 | shows a workspace-full alert … 429 | 留 | 不变 |
| 729 | accumulates turns across runs and parses per-turn token usage | 留 | `playground-usage` 保留(TurnMeta in TurnFooter) |
| 791 | retries a turn with the same input via the per-turn retry button | 留 | `playground-turn-retry` 保留(TurnFooter) |
| 834 | does not fetch rate cards for a non-admin user | 留 | 不变 |
| 852 | shows per-turn cost + step + a run-detail link | 留 | `playground-turn-cost` / `playground-turn-run-link` 保留 |
| 934 | lists past sessions for resume and shows a resumed banner | 留(改)| 左栏直接列;点会话 → `getSessionMessages` 被调;**去掉 banner 断言**(R7) |
| 965 | shows the workspace inspector with the volume + artifacts | 迁(WorkspacePanel.test)| |
| 999 | shows 'no workspace' when the user has none | 迁(WorkspacePanel.test)| |
| 1009 | lists workspace files and downloads one on click | 迁(WorkspacePanel.test)| |
| 1028 | loads the workspace inspector without a thread (user-scoped) | 留 | 断言 `getUserWorkspace` 在挂载时被调 |
| 1057 | surfaces an approval gate, approves, and streams the continuation | 留 | `playground-approval*` 保留(TurnBlock 内联 ApprovalGate) |
| 1182 | removes an attachment when its tag is closed | 迁(AttachmentChips.test)+ 留一条集成 | |
| 1200 | thumbs-up submits feedback for the turn | 留 | FeedbackBar 在 TurnFooter |
| 1229 | thumbs-down opens a comment popover and submits rating+comment | 留 | |
| 1261 | surfaces an inline error when feedback submission fails | 留 | |
| 1280 | timeline view: ok banner | 迁(TrajectoryPanel.test)| 面板头 `RunStatusBanner` ok;右栏默认跟随最新一轮 |
| 1307 | timeline view: error banner + jump | 迁(TrajectoryPanel.test)| banner error;跳转 = 选中第一条 error 行并开详情 |
| 1379 | timeline view: top-level error once | 迁(TrajectoryPanel.test)| 顶层 error 帧只出一条 error 行 + 一个 banner |
| 1416 | exact: lazy fetch + primary reasoning label | 迁(useRunTrace.test + TrajectoryPanel.test + trace_match.test)| 面板显示该轮即拉 trace;主对话 span 按序配 think 行(数量相等才配) |
| 1493 | exact: error banner + jump | 迁(RowDetailTiming.test)| 配对 span `level=error` → Langfuse 列红字 + statusMessage |
| 1585 | exact: auto-polls not_ready | 迁(useRunTrace.test)| 6 × 1.5 s;UI 侧「入库中 + 刷新」在 RowDetailTiming.test |
| 1661 | exact: loading state | 迁(RowDetailTiming.test)| trace 未到 → Langfuse 列「加载中」 |
| 1705 | Langfuse link hidden for non-admin | 迁(TrajectoryPanel.test)| |
| 1734 | Langfuse link for system_admin | 迁(TrajectoryPanel.test)| `getRun` trace_id → `playground-turn-langfuse` |
| 1777 | replays a count-matched history run into a full TurnCard | 留(改)| 断言变成:该轮 `console-turn` 内出现紧凑工具行(`console-row-tool`) |
| 1835 | backfills the input box when a history turn's retry is clicked | 留 | |
| 1881 | falls back to the flat history block when counts don't line up | 留 | `playground-history` 保留(Transcript 降级块) |
| 1921 | keeps the fallback answer when a history run's replay fails | 留 | |
| 1956 | keeps the fallback answer when a history run replays empty | 留 | |
| 2007 | drops a stale resume's history write | 留 | |
| 2193 | fire-now: delivered text + completed chip | 留(改)| 打开路径改:点紧凑工具行 → ToolCallCard → `tool-fire-now` |
| 2217 | fire-now: pending hint | 留(改)| 同上 |
| 2245 | fire-now: clears on new session | 留 | |
| 2271 | fire-now: clears on resuming a different thread | 留(改)| 通过左栏点另一会话 |
| 2308 | fire-now: no view-run link when thread_id empty | 留(改)| |
| 2331 | fire-now: view-run link | 留(改)| |
| 2358 | 切入态:发送/运行与新建会话按钮置灰 | 留 | |
| 2366 | 归属态:新建会话可用,输入后发送可用 | 留 | |
| 2376 | 切入态:上传按钮置灰;归属态可用 | 留 | |
| 2403 | live 轮:归属态渲染重试按钮,切入态不渲染 | 留 | |
| 2433 | resume 历史轮:归属态渲染重试按钮,切入态不渲染 | 留 | |

`SessionHistoryDrawer.test.tsx` 9 条 → 全部迁 `SessionSidebar.test.tsx`(Task 6);`PlanPanel.test.tsx` 6 条不动(Task 8 只抽表单,testid 不变)。

---

### Task 1: 后端两处只读增强 + SDK 透传(`order_by` / `runs[].tokens`)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/sessions.py:758-811`
- Modify: `services/control-plane/src/control_plane/api/runs.py:1588-1651`
- Test: `services/control-plane/tests/test_sessions_api.py`(紧接 `test_list_filters_by_agent_name`,`:919` 之后)
- Test: `services/control-plane/tests/test_runs_api.py`(紧接 `test_thread_runs_lists_oldest_first`,`:1413` 之后)
- Modify: `apps/admin-ui/src/api/sessions.ts:149-175`
- Modify: `apps/admin-ui/src/api/runs.ts:250-281`
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts:27-43`
- Test: `apps/admin-ui/src/pages/agent_detail/playground/__tests__/history_turns.test.ts`(已有)

**Interfaces:**
- Produces(后端):`GET /v1/sessions?order_by=created|last_activity`(默认 `created`,与现状一致;非法值 422);`GET /v1/sessions/{thread_id}/runs` 每行多一个 `"tokens": {...} | null`(形状 = `get_run` 的 `tokens`,即 `_tokens_to_dict`)。
- Produces(前端):`listSessions({ orderBy?: "created" | "last_activity" })`;`ThreadRunSummary.tokens: RunTokens | null`;`HistoryTurn.tokens: RunTokens | null`。

- [ ] **Step 1: 后端测试先写(红)**

`services/control-plane/tests/test_sessions_api.py`,加在 `test_list_filters_by_agent_name` 之后:

```python
@pytest.mark.asyncio
async def test_list_order_by_passes_through_to_store(
    session_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?order_by=last_activity`` reaches the store as ``order_by="last_activity"``
    (the in-memory store ignores it — the SQL sort is covered in the
    persistence package — so assert the pass-through, not the order)."""
    await _create(session_client)
    app = session_client._transport.app  # type: ignore[attr-defined,union-attr]
    repo = app.state.thread_meta_repo
    seen: list[str] = []
    real = repo.list_by_tenant

    async def spy(*args: object, **kwargs: object) -> object:
        seen.append(str(kwargs.get("order_by")))
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, "list_by_tenant", spy)
    resp = await session_client.get("/v1/sessions?order_by=last_activity")
    assert resp.status_code == 200
    assert seen == ["last_activity"]

    seen.clear()
    resp = await session_client.get("/v1/sessions")
    assert resp.status_code == 200
    assert seen == ["created_at"]


@pytest.mark.asyncio
async def test_list_order_by_rejects_unknown_value(session_client: AsyncClient) -> None:
    resp = await session_client.get("/v1/sessions?order_by=title")
    assert resp.status_code == 422
```

`services/control-plane/tests/test_runs_api.py`,加在 `test_thread_runs_lists_oldest_first` 之后(seed 写法照 `test_get_run_includes_token_summary` `:729-777`):

```python
@pytest.mark.asyncio
async def test_thread_runs_carry_per_run_tokens(runs_client: AsyncClient) -> None:
    """Debug-console redesign PR-A — the thread's run list carries each run's
    token rollup (joined by trace_id, same source as ``GET .../runs/{run_id}``);
    a run without usage carries ``tokens: null``."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from expert_work.persistence.token_usage_store import TokenUsageRecord
    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    with_usage, without_usage = uuid4(), uuid4()
    trace = "feedfacefeedfacefeedfacefeedface"
    for rid, created, tid in (
        (with_usage, now, trace),
        (without_usage, now + timedelta(seconds=5), None),
    ):
        await app.state.run_store.create(
            RunInfo(
                run_id=rid,
                tenant_id=DEFAULT_DEV_TENANT_ID,
                thread_id=UUID(thread_id),
                user_id=None,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=created,
                updated_at=created,
                finished_at=created,
                trace_id=tid,
            )
        )
    await app.state.token_usage_store.insert(
        TokenUsageRecord(
            tenant_id=DEFAULT_DEV_TENANT_ID,
            agent_name="code-reviewer",
            agent_version="1.0.0",
            model="claude-sonnet-4-6",
            trace_id=trace,
            input_tokens=120,
            output_tokens=30,
        )
    )
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert [r["run_id"] for r in runs] == [str(with_usage), str(without_usage)]
    assert runs[0]["tokens"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 150,
        "llm_calls": 1,
        "models": ["claude-sonnet-4-6"],
    }
    assert runs[1]["tokens"] is None
```

- [ ] **Step 2: 跑红**

Run: `uv run pytest services/control-plane/tests/test_sessions_api.py -q -k order_by` → 期望 `test_list_order_by_passes_through_to_store` 失败(`seen == ["created_at"]` 断言不满足:参数没透传,第一段拿到 `created_at`)、`test_list_order_by_rejects_unknown_value` 失败(200 而非 422)。
Run: `uv run pytest services/control-plane/tests/test_runs_api.py -q -k carry_per_run_tokens` → 失败(`KeyError: 'tokens'`)。

- [ ] **Step 3: 后端实现**

`sessions.py` `list_sessions`:签名加一项(放在 `offset` 之后、`tenant_id` 之前):

```python
        order_by: Annotated[ThreadOrder, Query()] = "created_at",
```

`ThreadOrder` 从 `expert_work.persistence.thread_meta.base` 导入(`base.py:36`,`Literal["created_at", "last_activity"]`;`thread_meta/__init__.py` 没有再导出,所以从 `.base` 拿;放在 sessions.py:56-62 那组 persistence import 旁;FastAPI 对 Literal 自动 422)。两处 store 调用各加 `order_by=order_by,`(`list_all_tenants(...)` 与 `list_by_tenant(...)`;先例 `api/conversations.py:253/279`)。

`runs.py` `list_thread_runs`:依赖注入加 `token_usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],`(`_get_token_usage_store` 在 `:503`);`try` 块里改为:

```python
        try:
            rows = await runs.list_by_thread(thread_id=thread_id, tenant_id=target_tenant)
            # PR-A — per-run token rollup, same source as ``get_run`` (token_usage
            # joined by trace_id; one batched read for the whole list). Scoped
            # so RLS applies exactly as in ``get_conversation``.
            trace_ids = sorted({r.trace_id for r in rows if r.trace_id is not None})
            async with applied_scope(scope):
                by_trace = await token_usage.totals_by_trace_ids(trace_ids) if trace_ids else {}
            out = [
                {
                    "run_id": str(r.run_id),
                    "status": r.status.value,
                    "is_resume": r.is_resume,
                    "created_at": r.created_at.isoformat(),
                    "tokens": _tokens_to_dict(
                        by_trace.get(r.trace_id) if r.trace_id is not None else None
                    ),
                }
                for r in rows
            ]
        except Exception:
```

(`applied_scope` 已由 `control_plane.tenant_scope` 导入(runs.py:62-68);`scope` 是本 handler 上文 `ensure_tenant_scope` 解析出的单租户 scope,直接传。`_tokens_to_dict` 在 runs.py:1855。docstring 里「Returns run_id / status / is_resume / created_at only」改成加 `tokens`。)

- [ ] **Step 4: 跑绿 + 全量后端门**

Run: `uv run pytest services/control-plane/tests/test_sessions_api.py services/control-plane/tests/test_runs_api.py -q` → 全绿。
Run: `uv run ruff check services/control-plane && uv run ruff format --check services/control-plane && uv run mypy services/control-plane` → 绿。

- [ ] **Step 5: 前端 SDK(先写测试)**

`apps/admin-ui/src/api/sessions.ts` `listSessions` 参数加 `orderBy?: "created" | "last_activity";`,映射:

```ts
  if (params.orderBy === "last_activity") query.order_by = "last_activity";
```

(默认不发,保持请求形状不变。)注意后端参数值是 `created_at`,SDK 侧只暴露两个语义值,只在 `last_activity` 时发。

`apps/admin-ui/src/api/runs.ts`:

```ts
export interface ThreadRunSummary {
  runId: string;
  status: RunStatus;
  isResume: boolean;
  createdAt: string;
  /** PR-A — persisted per-run token rollup (``null`` when the run has no
   *  recorded usage; absent on old backends → treated as null). */
  tokens: RunTokens | null;
}

interface ThreadRunRow {
  run_id: string;
  status: RunStatus;
  is_resume: boolean;
  created_at: string;
  tokens?: RunTokens | null;
}
```

映射处加 `tokens: row.tokens ?? null`。

`history_turns.ts` `HistoryTurn` 加 `tokens: RunTokens | null;`,`buildHistoryTurns` 配对时 `tokens: run.tokens`。`useHistoryTurns.test.ts` 与 `history_turns.test.ts` 里的 `ThreadRunSummary` fixture 补 `tokens: null`(typecheck 会逼你补)。

`history_turns.test.ts` 加一条:

```ts
it("carries each run's persisted token rollup onto the paired turn", () => {
  const messages: HistoryMessage[] = [
    { role: "user", content: "q", channel: null },
    { role: "assistant", content: "a", channel: "final" },
  ];
  const tokens = {
    input_tokens: 10, output_tokens: 5, cache_creation_tokens: 0,
    cache_read_tokens: 0, total_tokens: 15, llm_calls: 1, models: ["m"],
  };
  const turns = buildHistoryTurns(messages, [
    { runId: "r1", status: "success", isResume: false, createdAt: "2026-01-01T00:00:00Z", tokens },
  ]);
  expect(turns?.[0]?.tokens).toEqual(tokens);
});
```

Run: `pnpm exec vitest run src/pages/agent_detail/playground/__tests__/history_turns.test.ts src/components/turn/__tests__/useHistoryTurns.test.ts && pnpm typecheck` → 绿。

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/api/sessions.py services/control-plane/src/control_plane/api/runs.py services/control-plane/tests/test_sessions_api.py services/control-plane/tests/test_runs_api.py apps/admin-ui/src/api/sessions.ts apps/admin-ui/src/api/runs.ts apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts apps/admin-ui/src/pages/agent_detail/playground/__tests__/history_turns.test.ts apps/admin-ui/src/components/turn/__tests__/useHistoryTurns.test.ts
git commit -m "feat(sessions): GET /v1/sessions 支持 order_by=last_activity;线程 runs 列表带每 run token rollup(调试台重设计 PR-A Task 1)"
```

---

### Task 2: i18n `console.*` 命名空间

**Files:**
- Modify: `apps/admin-ui/src/i18n/locales/en.ts`(`TranslationKeys` 接口块 `playground` 之前;`en` 值块 `playground` 之前)
- Modify: `apps/admin-ui/src/i18n/locales/zh-CN.ts`(值块 `playground` 之前)
- Test: `apps/admin-ui/src/i18n/__tests__/i18n.test.tsx`(已有,跑一遍)

**Interfaces:**
- Produces:下表全部键,后续 Task 只消费不新增(执行中发现漏键 → 在该 Task 补三处并在台账记一笔)。

- [ ] **Step 1: 键表(接口块 + 两份值)**

`console: { … }` 三处同步(接口块每键 `: string;`):

| 键 | zh-CN | en |
|---|---|---|
| `sidebar_title` | 会话 | Sessions |
| `sidebar_new` | 新建 | New |
| `sidebar_search` | 搜索会话… | Search sessions… |
| `sidebar_filter_active` | 活跃 | Active |
| `sidebar_filter_archived` | 已归档 | Archived |
| `sidebar_open` | 打开会话列表 | Open session list |
| `sidebar_running_dot` | 进行中 | Running |
| `thread_id_label` | 会话 | Session |
| `turn_count` | {{n}} 轮 | {{n}} turns |
| `no_turns` | 还没有对话 —— 在下面输入,或从左侧打开一个会话。 | No conversation yet — type below, or open a session on the left. |
| `row_think` | 思考 | Thinking |
| `row_think_live` | 思考中 | Thinking… |
| `row_plan_update` | 计划 · 更新为 {{n}} 步 | Plan · updated to {{n}} steps |
| `row_plan_create` | 制定计划 · {{n}} 步 | Plan drafted · {{n}} steps |
| `row_memory_recall` | 记忆召回 · {{n}} 条 | Memory recall · {{n}} |
| `row_memory_writeback` | 记忆写回 · {{n}} 条 | Memory written · {{n}} |
| `row_reflect_pass` | 反思 · 通过 | Reflection · pass |
| `row_reflect_revise` | 反思 · 修订 | Reflection · revise |
| `row_subagent` | 子代理 · {{name}} | Sub-agent · {{name}} |
| `row_tool_pending` | 执行中 | running |
| `row_tool_error` | 失败 | failed |
| `row_expand` | 展开 | Expand |
| `row_collapse` | 收起 | Collapse |
| `row_inspect` | 检查 | Inspect |
| `answer_streaming` | 生成中… | Generating… |
| `footer_inspect` | 检查 | Inspect |
| `footer_status_running` | 进行中 | Running |
| `footer_status_done` | 已完成 | Done |
| `footer_status_error` | 失败 | Failed |
| `plan_title` | 任务 | Tasks |
| `plan_progress` | {{done}} 已完成 · {{doing}} 进行中 · {{todo}} 待处理 | {{done}} done · {{doing}} in progress · {{todo}} pending |
| `plan_toggle` | 展开 / 收起任务 | Toggle tasks |
| `vars_required_missing` | 必填变量未填:{{names}} | Required variables missing: {{names}} |
| `vars_required_mark` | 必填 | required |
| `composer_hint` | Enter 发送,Shift+Enter 换行 | Enter to send, Shift+Enter for a new line |
| `stats_turns` | {{turns}} 轮 · {{steps}} 步 | {{turns}} turns · {{steps}} steps |
| `stats_llm_tools` | LLM {{llm}} · 工具 {{tools}} | LLM {{llm}} · tools {{tools}} |
| `stats_ttft` | 首 token {{v}} | First token {{v}} |
| `stats_tps` | ≈ {{v}} tok/s | ≈ {{v}} tok/s |
| `stats_cache` | 缓存 {{v}}% | Cache {{v}}% |
| `stats_tokens` | 入 {{in}} · 出 {{out}} | In {{in}} · out {{out}} |
| `stats_cost` | ≈ ¥{{v}} | ≈ ¥{{v}} |
| `stats_partial` | (仅已加载轮) | (loaded turns only) |
| `inspect_trajectory` | 轨迹 | Trajectory |
| `inspect_workspace` | 工作区 | Workspace |
| `inspect_turn_header` | 第 {{n}} 轮 · {{status}} | Turn {{n}} · {{status}} |
| `inspect_no_turn` | 选一轮查看轨迹 | Pick a turn to inspect |
| `traj_kind_user` | USER | USER |
| `traj_kind_think` | THINK | THINK |
| `traj_kind_plan` | PLAN | PLAN |
| `traj_kind_memory` | MEMORY | MEMORY |
| `traj_kind_tool` | TOOL | TOOL |
| `traj_kind_subagent` | SUBAGENT | SUBAGENT |
| `traj_kind_reflect` | REFLECT | REFLECT |
| `traj_kind_compaction` | COMPACTION | COMPACTION |
| `traj_kind_assistant` | ASSISTANT | ASSISTANT |
| `traj_kind_error` | ERROR | ERROR |
| `traj_kind_retry` | RETRY | RETRY |
| `traj_kind_approval` | APPROVAL | APPROVAL |
| `traj_kind_guard` | GUARD | GUARD |
| `traj_kind_gap` | GAP | GAP |
| `traj_llm_call` | 模型调用 · {{model}} | LLM call · {{model}} |
| `traj_status_running` | 进行中 | running |
| `traj_status_ok` | 完成 | done |
| `traj_status_error` | 失败 | failed |
| `traj_status_warn` | 注意 | warning |
| `traj_status_pause` | 等待审批 | awaiting approval |
| `lane_input` | 输入 | Input |
| `lane_model` | 模型 | Model |
| `lane_tools` | 工具 | Tools |
| `detail_tab_summary` | 概要 | Summary |
| `detail_tab_payload` | 输入 | Payload |
| `detail_tab_result` | 结果 | Result |
| `detail_tab_timing` | 耗时 | Timing |
| `detail_tab_raw` | 原始帧 | Raw |
| `detail_close` | 关闭详情 | Close detail |
| `detail_level` | 第 {{turn}} 轮 · 第 {{step}} 步 | Turn {{turn}} · step {{step}} |
| `detail_level_turn_only` | 第 {{turn}} 轮 | Turn {{turn}} |
| `detail_status` | 状态 | Status |
| `detail_duration` | 时长 | Duration |
| `detail_model` | 模型 | Model |
| `detail_tokens` | 入 {{in}} · 出 {{out}} | In {{in}} · out {{out}} |
| `detail_finish_reason` | 结束原因 | Finish reason |
| `detail_tool` | 工具 | Tool |
| `detail_server` | MCP 服务 | MCP server |
| `detail_steps_total` | 步数 | Steps |
| `detail_goal` | 目标 | Goal |
| `detail_count` | 条数 | Count |
| `detail_attachments` | 附件 | Attachments |
| `detail_variables` | 变量 | Variables |
| `detail_chars` | 字数 | Characters |
| `detail_worker` | 子代理 | Sub-agent |
| `detail_worker_steps` | 子代理步数 | Sub-agent steps |
| `detail_need_langfuse` | 模型输入只在 Langfuse 精确轨迹里有;本行还没配到 span。 | LLM input is only available from the Langfuse trace; no span matched this row yet. |
| `detail_no_frames` | 该行没有对应的原始帧。 | No raw frames for this row. |
| `detail_none` | — | — |
| `timing_col_sse` | SSE 时戳 | SSE timestamps |
| `timing_col_langfuse` | Langfuse 精确 | Langfuse (exact) |
| `timing_row_end` | 结束时刻 | Ended at |
| `timing_row_start` | 开始(相对 trace 起点) | Start (from trace start) |
| `timing_row_latency` | 时长 | Latency |
| `timing_row_model` | 模型 | Model |
| `timing_row_tokens` | tokens | tokens |
| `timing_row_cost` | 成本 | Cost |
| `timing_loading` | 加载中… | Loading… |
| `timing_not_ready` | Langfuse 入库中,稍后自动重试 | Langfuse still ingesting — retrying shortly |
| `timing_unavailable` | trace 不可用 | Trace unavailable |
| `timing_no_trace` | 本次运行没有 trace | No trace for this run |
| `timing_mismatch` | 无法与 span 对齐 | Could not align with a span |
| `timing_unsupported` | 该行没有对应 span | No span for this row |

- [ ] **Step 2: 三处落键 + 跑 i18n 测试 + typecheck**

Run: `pnpm exec vitest run src/i18n && pnpm typecheck` → 绿(`zhCN: TranslationKeys` 少一键会红,借此对齐)。
Run: `grep -n "^  console:" src/i18n/locales/en.ts src/i18n/locales/zh-CN.ts` → 各恰一处(接口块的 `console:` 在 en.ts 也算,一共 en 两处、zh 一处)。

- [ ] **Step 3: Commit**

```bash
git add apps/admin-ui/src/i18n/locales/en.ts apps/admin-ui/src/i18n/locales/zh-CN.ts
git commit -m "feat(i18n): 调试台三栏壳 console.* 命名空间(zh-CN + en)"
```

---

### Task 3: 纯函数 `api/plan_reducer.ts` + `useTokenStream` 记 `firstTokenAt / lastTokenAt`

**Files:**
- Create: `apps/admin-ui/src/api/plan_reducer.ts`
- Test: `apps/admin-ui/src/api/__tests__/plan_reducer.test.ts`(目录已存在,`timeline.test.ts` 等 17 个都在这)
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/useTokenStream.ts`
- Test: `apps/admin-ui/src/pages/agent_detail/playground/__tests__/useTokenStream.test.ts`

**Interfaces:**
- Produces:

```ts
// api/plan_reducer.ts
import type { ThreadPlan } from "./plan";
import type { SseEvent } from "./sessions";

export interface PlanSnapshot {
  plan: ThreadPlan;
  /** 去重键:帧 id;无 id 时 `${index}` 兜底(调用方传 index)。 */
  sourceKey: string;
}

/** 单帧 → 计划快照。`plan` 顶层帧(PR1)直接取 data;`updates` 帧取任一节点值里非空的 `plan` 键(多个节点取最后一个)。其它帧 null。 */
export function planFromEvent(evt: SseEvent, index: number): PlanSnapshot | null;

/** 一段帧 → 最后一份计划快照(按数组顺序,最后非 null 者胜);没有 → null。 */
export function reducePlan(events: readonly SseEvent[]): PlanSnapshot | null;
```

- `TokenStreamState` 加 `firstTokenAt: number | null; lastTokenAt: number | null;`(epoch ms,`Date.now()`;任一频道 token 都算;`reset` 清空;`finalize` 保留)。

- [ ] **Step 1: 测试(红)**

`plan_reducer.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { planFromEvent, reducePlan } from "../plan_reducer";
import type { SseEvent } from "../sessions";

const PLAN_A = {
  goal: "给客户 C-1024 出续约建议",
  steps: [
    { id: "1", description: "查档案", status: "completed" },
    { id: "2", description: "分析工单", status: "in_progress" },
  ],
};
const PLAN_B = { goal: PLAN_A.goal, steps: [...PLAN_A.steps, { id: "3", description: "出建议", status: "pending" }] };

function evt(event: string, data: unknown, id: string | null = null): SseEvent {
  return { id, event, data, rawData: JSON.stringify(data), receivedAt: "" };
}

describe("planFromEvent", () => {
  it("reads a top-level plan frame (PR1) verbatim, keyed by its frame id", () => {
    expect(planFromEvent(evt("plan", PLAN_A, "1755500000000-7"), 3)).toEqual({
      plan: PLAN_A,
      sourceKey: "1755500000000-7",
    });
  });
  it("reads updates.<node>.plan (pre-PR1 persisted runs), last node wins", () => {
    const e = evt("updates", { tools: { plan: PLAN_A }, planner: { plan: PLAN_B } }, null);
    expect(planFromEvent(e, 5)).toEqual({ plan: PLAN_B, sourceKey: "5" });
  });
  it("ignores updates without a plan key / null plan / other events", () => {
    expect(planFromEvent(evt("updates", { agent: { messages: [] } }), 0)).toBeNull();
    expect(planFromEvent(evt("updates", { tools: { plan: null } }), 0)).toBeNull();
    expect(planFromEvent(evt("token", { step: 1, channel: "content", text: "x" }), 0)).toBeNull();
    expect(planFromEvent(evt("plan", "not-an-object"), 0)).toBeNull();
    expect(planFromEvent(evt("plan", { goal: "g" }), 0)).toBeNull(); // no steps array → not a plan
  });
});

describe("reducePlan", () => {
  it("returns the last snapshot in order (plan frame after updates.plan wins)", () => {
    const r = reducePlan([
      evt("updates", { tools: { plan: PLAN_A } }, "1"),
      evt("updates", { agent: { messages: [] } }, "2"),
      evt("plan", PLAN_B, "3"),
    ]);
    expect(r).toEqual({ plan: PLAN_B, sourceKey: "3" });
  });
  it("returns null for a stream with no plan", () => {
    expect(reducePlan([evt("metadata", { run_id: "r" }), evt("end", "ok")])).toBeNull();
  });
});
```

`useTokenStream.test.ts` 加两条(文件已有 helper:`contentFrame(step, text)` / `reasoningFrame(step, text)` / `flushRaf()`;`Date.now` 用 `vi.spyOn(Date, "now").mockReturnValue(...)` 逐次改——文件里 TTFT 那条怎么控时间就照它):

```ts
it("records firstTokenAt / lastTokenAt (epoch ms) across channels and keeps them after finalize", () => {
  const now = vi.spyOn(Date, "now");
  now.mockReturnValue(1_000);
  const { result } = renderHook(() => useTokenStream());
  act(() => result.current.reset());
  now.mockReturnValue(1_250);
  act(() => result.current.push(reasoningFrame(1, "…")));
  now.mockReturnValue(2_000);
  act(() => result.current.push(contentFrame(1, "hi")));
  act(() => flushRaf());
  expect(result.current.firstTokenAt).toBe(1_250);
  expect(result.current.lastTokenAt).toBe(2_000);
  act(() => result.current.finalize());
  expect(result.current.firstTokenAt).toBe(1_250);
  expect(result.current.lastTokenAt).toBe(2_000);
});

it("reset clears firstTokenAt / lastTokenAt", () => {
  const { result } = renderHook(() => useTokenStream());
  act(() => result.current.reset());
  act(() => result.current.push(contentFrame(1, "a")));
  act(() => result.current.reset());
  expect(result.current.firstTokenAt).toBeNull();
  expect(result.current.lastTokenAt).toBeNull();
});
```

(第一条测试结束 `now.mockRestore()`。)

- [ ] **Step 2: 跑红**

Run: `pnpm exec vitest run src/api/__tests__/plan_reducer.test.ts src/pages/agent_detail/playground/__tests__/useTokenStream.test.ts` → 前者模块不存在红;后者 `firstTokenAt` undefined 红。

- [ ] **Step 3: 实现**

`plan_reducer.ts`:

```ts
function isPlan(v: unknown): v is ThreadPlan {
  return (
    v !== null && typeof v === "object" &&
    typeof (v as { goal?: unknown }).goal === "string" &&
    Array.isArray((v as { steps?: unknown }).steps)
  );
}

export function planFromEvent(evt: SseEvent, index: number): PlanSnapshot | null {
  const key = evt.id ?? String(index);
  if (evt.event === "plan") return isPlan(evt.data) ? { plan: evt.data, sourceKey: key } : null;
  if (evt.event !== "updates" || evt.data === null || typeof evt.data !== "object") return null;
  let last: ThreadPlan | null = null;
  for (const value of Object.values(evt.data as Record<string, unknown>)) {
    if (value === null || typeof value !== "object") continue;
    const p = (value as { plan?: unknown }).plan;
    if (isPlan(p)) last = p;
  }
  return last === null ? null : { plan: last, sourceKey: key };
}

export function reducePlan(events: readonly SseEvent[]): PlanSnapshot | null {
  let last: PlanSnapshot | null = null;
  events.forEach((e, i) => {
    const s = planFromEvent(e, i);
    if (s !== null) last = s;
  });
  return last;
}
```

`useTokenStream.ts`:`firstTokenAtRef / lastTokenAtRef` 两个 ref;`push` 里 `parseToken` 非空后:`const now = Date.now(); if (firstTokenAtRef.current === null) firstTokenAtRef.current = now; lastTokenAtRef.current = now;`(ttft 那段用同一个 `now`);`flush` / `finalize` 的 snapshot 带上两值;`reset` 清空 + 初始 state 两值 null;`TokenStreamState` 接口加字段并写 docstring。

- [ ] **Step 4: 跑绿 + 既有 13 条不变**

Run: 同 Step 2 命令 → 全绿(useTokenStream 15 条)。Run: `pnpm typecheck` → 绿(TurnCard 等消费方不读新字段,不受影响)。

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/plan_reducer.ts apps/admin-ui/src/api/__tests__/plan_reducer.test.ts apps/admin-ui/src/pages/agent_detail/playground/useTokenStream.ts apps/admin-ui/src/pages/agent_detail/playground/__tests__/useTokenStream.test.ts
git commit -m "feat(console): plan_reducer(事件→当前计划)+ useTokenStream 记首/末 token 时刻"
```

---

### Task 4: 纯函数 `api/trajectory_rows.ts` —— 事件 → 紧凑行 / 轨迹行(两投影一套 id)+ `parseTimeline` 补 `eventIndex`

**Files:**
- Modify: `apps/admin-ui/src/api/timeline.ts`(`AgentStep / AuxNodeItem / MarkerItem` 各加**可选**字段 `eventIndex?: number`;`parseTimeline` 里照 `evtServerMs` 的写法加一个 `evtIndex`,`push` 一并塞进去。可选是为了不动 `StepTimeline.test / timeline_banner.test / GanttTimeline.test` 里手写的 item 字面量)
- Create: `apps/admin-ui/src/api/trajectory_rows.ts`
- Test: `apps/admin-ui/src/api/__tests__/trajectory_rows.test.ts`(fixture 写法照 `timeline.test.ts` 的 `ev(...)` / `upd(node, channels, at)`);`api/__tests__/timeline.test.ts` 追加一条 `eventIndex` 断言

**Interfaces:**
- Consumes:`parseTimeline / TimelineItem`(`api/timeline.ts`)、`ToolCallEntry`(`api/tool_timeline.ts`)、`WorkerTimeline`(`api/worker_timeline.ts`)、`ThreadPlan`(`api/plan.ts`)、`isPlan`(Task 3 的 `plan_reducer.ts`)、`GanttRow["key"]` 的三种前缀(`item-<seq>` / `tool-<toolCallId>` / `worker-<workerId>-<wseq>`,见 `api/gantt_timeline.ts:167-227`)。
- Produces(Task 10 / 11 吃 `CompactRow`;Task 14 / 15 / 16 / 17 / 18 吃 `TrajectoryRow` 与 `resolveGanttKey`):

```ts
// api/trajectory_rows.ts
export type RowStatus = "running" | "ok" | "error" | "warn" | "pause";

interface RowBase {
  /** 轮内稳定 id:`${kind}:${seq}[:${toolIdx}[:${workerIdx}]]`;中栏与右栏同一套,右栏行集 ⊇ 中栏(中栏「检查」按 id 定位右栏行)。user / assistant 行 id 固定 `"user"` / `"assistant"`。 */
  id: string;
  /** 来源 `TimelineItem.seq`;user / assistant 行为 -1。 */
  seq: number;
  /** 所属 agent 步号(`AgentStep.stepCount`);aux / marker / user / assistant 为 null。 */
  step: number | null;
  status: RowStatus;
  durationMs: number | null;
  /** 该行对应的原始帧在 `events` 里的下标(Raw tab 用);来源 item 没带 `eventIndex` 时为 []。 */
  eventIndexes: number[];
  /** 帧 id 里的服务端毫秒(≈ 该单元结束时刻;Timing tab SSE 列用):agent / aux / marker 行取 `item.serverMs`,tool / plan(update_plan)行取 `entry.serverMs`;subagent / user / assistant 为 null。 */
  serverMs: number | null;
}
export type ThinkRow = RowBase & { kind: "think"; text: string; content: string | null; model: string | null; inputTokens: number; outputTokens: number; finishReason: string | null };
export type ToolRow = RowBase & { kind: "tool"; entry: ToolCallEntry };
export type SubagentRow = RowBase & { kind: "subagent"; worker: WorkerTimeline; parentEntryId: string };
export type PlanRow = RowBase & {
  kind: "plan"; source: "update_plan" | "planner";
  /** `update_plan` 的 tool_call_id;planner 行 null。 */
  callId: string | null;
  /** 被合并进来的 planner item 的 seq(`resolveGanttKey` 用);没合并 null。 */
  plannerSeq: number | null;
  stepsTotal: number; goal: string | null; reason: string | null; plan: ThreadPlan | null;
};
export type MemoryRow = RowBase & { kind: "memory"; direction: "recall" | "writeback"; count: number; detail: Record<string, unknown> };
export type ReflectRow = RowBase & { kind: "reflect"; verdict: "pass" | "revise"; detail: Record<string, unknown> };
export type MarkerRow = RowBase & { kind: "compaction" | "retry" | "error" | "approval" | "guard" | "gap"; text: string };
export type CompactRow = ThinkRow | ToolRow | SubagentRow | PlanRow | MemoryRow | ReflectRow | MarkerRow;

export type UserRow = RowBase & { kind: "user"; text: string; attachmentNames: string[]; inputs: Record<string, string> };
export type AssistantRow = RowBase & { kind: "assistant"; text: string };
export type TrajectoryRow = UserRow | CompactRow | AssistantRow;

/** 中栏紧凑行:顺序 = parseTimeline 顺序;`end` 不出行(脚注表达状态);think 只在 reasoning 非空时出。 */
export function compactRowsOf(events: readonly SseEvent[]): CompactRow[];

export interface TrajectoryInput { text: string; attachmentNames: string[]; inputs: Record<string, string> }
/** 右栏轨迹行:`user` + 每个 agent 步一条 think(reasoning 为空也出,`text: ""`,UI 显示「模型调用 · <model>」)+ 其余同紧凑行 + `assistant`(`answer` 非空时;`status` = turnStatus running→running / error→error / done→ok)。 */
export function trajectoryRowsOf(
  events: readonly SseEvent[], input: TrajectoryInput, answer: string | null, turnStatus: "running" | "done" | "error",
): TrajectoryRow[];

/** `GanttRow.key` → 轨迹行 id(泳道块点击定位用);找不到 → null。 */
export function resolveGanttKey(rows: readonly TrajectoryRow[], key: string): string | null;
```

规则(每条都有测试;两投影共用一个内部 builder `rowsOf(events, { everyStepThinks: boolean })`):
1. `AgentStep`:`reasoning !== null || everyStepThinks` → 一条 `think`(`text = reasoning ?? ""`,`content = item.content`,`step = stepCount`,`model / inputTokens / outputTokens / finishReason` 直接抄 item,`status = hasError ? "error" : "ok"`,`durationMs = item.durationMs`,`eventIndexes = idx(item)`)。然后按 `tools[]` 顺序:`toolName === "update_plan"` → `plan` 行(`source: "update_plan"`,`callId = entry.id`,`plannerSeq: null`,`stepsTotal = Array.isArray(args.steps) ? args.steps.length : 0`,`goal = typeof args.goal === "string" ? args.goal : null`,`reason = typeof args.reason === "string" ? args.reason : null`,`plan: null`,`status` 按工具状态);否则 `tool` 行(`status`:`pending`→`running`、`success`→`ok`、`error`→`error`、`pending_approval`→`pause`;`durationMs = entry.durationMs`);两者 `eventIndexes = [...idx(item), ...resultIdx(events, entry.id)]`(`resultIdx` = 第一条 `updates` 帧、任一节点 `messages[]` 里 `type === "tool" && tool_call_id === entry.id` 的下标;找不到不加)。每个 `entry.workers?.[i]` 紧随其后追加一条 `subagent` 行(`status`:`running`→`running`、`success`→`ok`、其它→`warn`;`durationMs = worker.summary?.wallClockMs ?? null`;`eventIndexes` = 所有 `event === "worker"` 且 `data.worker_id === worker.workerId` 的帧下标)。
2. `AuxNodeItem`:`memory_recall` / `memory_writeback` → `memory`(`count = Array.isArray(detail.memories) ? detail.memories.length : 0` —— **parseTimeline 两种都放在 `detail.memories`**,见 `timeline.ts:206-208 / 233-235`);`reflect` → `reflect`(`verdict = detail.verdict === "revise" ? "revise" : "pass"`,`status = verdict === "revise" ? "warn" : "ok"`);`planner`:**若 `node === "tools"` 且此前已有一条 `source: "update_plan"` 且 `plan === null` 的行(从末尾往前找第一条)→ 合并进它**(填 `plan = detail.plan`,`stepsTotal = plan.steps.length`,`goal = plan.goal`,`plannerSeq = item.seq`,`eventIndexes` 追加 `idx(item)`(已含则不重复);不新增行);否则新增 `plan` 行(`source: "planner"`,`callId: null`,`plannerSeq: null`,`plan = detail.plan`,`stepsTotal`,`goal`,`reason: null`)。`workspace_ingest`(声明了但 parseTimeline 从不发)→ 忽略。aux 行 `durationMs = item.durationMs`,`eventIndexes = idx(item)`。
3. `MarkerItem`:`compaction / retry / error / approval / guard / gap` → 同名行,`text = item.text`,`status`:`tone` `bad`→`error`、`warn`→`warn`、`pause`→`pause`、`good`→`ok`,`durationMs: null`,`eventIndexes = idx(item)`;`end` → 不出行。
4. `idx(item)` = `item.eventIndex === undefined ? [] : [item.eventIndex]`。
5. `trajectoryRowsOf`:`[userRow, ...rowsOf(events, { everyStepThinks: true }), ...(answer ? [assistantRow] : [])]`;`userRow = { id: "user", kind: "user", seq: -1, step: null, status: "ok", durationMs: null, eventIndexes: [], text: input.text, attachmentNames: input.attachmentNames, inputs: input.inputs }`;`assistantRow.eventIndexes` = 最后一条 `content !== null` 的 AgentStep 的 `idx`。
6. `resolveGanttKey`:`item-<seq>` → 第一条 `row.seq === seq && kind ∉ {tool, subagent}` 的行,找不到再找 `kind === "plan" && plannerSeq === seq`;`tool-<id>` → `kind === "tool" && entry.id === id` 或 `kind === "plan" && callId === id`;`worker-<workerId>-<wseq>`(正则 `/^worker-(.+)-\d+$/`)→ 第一条 `kind === "subagent" && worker.workerId === workerId`;都没有 → null。

- [ ] **Step 1: 测试(红)**

```ts
import { describe, expect, it } from "vitest";

import type { SseEvent } from "../sessions";
import { compactRowsOf, resolveGanttKey, trajectoryRowsOf } from "../trajectory_rows";

function ev(event: string, data: unknown, at = "t"): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: at };
}
function upd(node: string, channels: Record<string, unknown>, at = "t"): SseEvent {
  return ev("updates", { [node]: channels }, at);
}
const PLAN = { goal: "出建议", steps: [
  { id: "1", description: "查档案", status: "completed" },
  { id: "2", description: "分析", status: "in_progress" },
  { id: "3", description: "出建议", status: "pending" },
] };
const INPUT = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };

describe("compactRowsOf", () => {
  it("agent step → think row then one row per tool, in order; think carries model/tokens", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, _duration_ms: 900, messages: [{
        type: "ai", content: "",
        response_metadata: { model_name: "gpt-x" }, usage_metadata: { input_tokens: 120, output_tokens: 30 },
        additional_kwargs: { reasoning_content: "先查客户档案\n再看工单" },
        tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
      }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" }] }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(["think", "tool"]);
    expect(rows[0]).toMatchObject({ kind: "think", step: 1, text: "先查客户档案\n再看工单", status: "ok", model: "gpt-x", inputTokens: 120, outputTokens: 30, durationMs: 900, eventIndexes: [0] });
    expect(rows[1]).toMatchObject({ kind: "tool", step: 1, status: "ok", eventIndexes: [0, 1] });
    if (rows[1].kind === "tool") {
      expect(rows[1].entry.toolName).toBe("query_crm");
      expect(rows[1].entry.resultPreview).toContain("3 条记录");
    }
  });

  it("a step without reasoning has NO think row in the compact projection", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(["tool"]);
  });

  it("tool statuses map: no result yet→running, error→error; approval marker → pause row", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [
        { id: "a", name: "t1", args: {} }, { id: "b", name: "t2", args: {} },
      ] }] }),
      upd("tools", { messages: [
        { type: "tool", tool_call_id: "b", name: "t2", content: "boom", status: "error" },
      ] }),
      ev("approval", { tool_call_id: "a" }),
    ]);
    const tools = rows.filter((r) => r.kind === "tool");
    expect(tools.map((r) => r.status)).toEqual(["running", "error"]);
    expect(rows.at(-1)).toMatchObject({ kind: "approval", status: "pause", text: "等待人工审批", eventIndexes: [2] });
  });

  it("update_plan call + the tools node's plan snapshot merge into ONE plan row (callId / plannerSeq / both frames)", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "", tool_calls: [
        { id: "p1", name: "update_plan", args: { goal: "出建议", steps: PLAN.steps, reason: "档案查完了,细化后两步" } },
      ] }] }),
      upd("tools", {
        messages: [{ type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" }],
        plan: PLAN,
      }),
    ]);
    const plans = rows.filter((r) => r.kind === "plan");
    expect(plans).toHaveLength(1);
    expect(plans[0]).toMatchObject({ kind: "plan", source: "update_plan", callId: "p1", plannerSeq: 1, stepsTotal: 3, goal: "出建议", reason: "档案查完了,细化后两步", plan: PLAN, step: 2, eventIndexes: [0, 1] });
  });

  it("two update_plan calls in one batch: snapshot merges into the LAST one, the earlier keeps args-only", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [
        { id: "p1", name: "update_plan", args: { goal: "g", steps: [{ id: "1", description: "a", status: "pending" }] } },
        { id: "p2", name: "update_plan", args: { goal: "g", steps: PLAN.steps } },
      ] }] }),
      upd("tools", { messages: [
        { type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" },
        { type: "tool", tool_call_id: "p2", name: "update_plan", content: "ok", status: "success" },
      ], plan: PLAN }),
    ]);
    const plans = rows.filter((r) => r.kind === "plan");
    expect(plans).toHaveLength(2);
    expect(plans[0]).toMatchObject({ stepsTotal: 1, plan: null, plannerSeq: null });
    expect(plans[1]).toMatchObject({ stepsTotal: 3, plan: PLAN });
  });

  it("planner node's plan (no preceding update_plan) is its own 'planner' row", () => {
    const rows = compactRowsOf([upd("planner", { plan: PLAN, _duration_ms: 1200 })]);
    expect(rows).toEqual([expect.objectContaining({ kind: "plan", source: "planner", callId: null, stepsTotal: 3, goal: "出建议", plan: PLAN, durationMs: 1200, step: null, eventIndexes: [0] })]);
  });

  it("aux + marker rows: memory recall/writeback counts (detail.memories), reflect verdict, compaction/retry/error texts; end is dropped", () => {
    const rows = compactRowsOf([
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }, { id: "m2", kind: "fact", content: "y", importance: 0.5, confidence: 0.5 }] }),
      upd("reflect", { reflections: [{ verdict: "revise", critique: "漏了夜间" }] }),
      upd("memory_writeback", { written_memories: [{ id: "w1" }] }),
      ev("compaction", { passes: 1, tokens_before: 12300, tokens_after: 4100, summary_chars: 800 }),
      ev("retry", { attempt: 1, error_class: "TimeoutError", backoff_s: 2 }),
      ev("error", { message: "上游 502" }),
      ev("end", { status: "error" }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(["memory", "reflect", "memory", "compaction", "retry", "error"]);
    expect(rows[0]).toMatchObject({ direction: "recall", count: 2 });
    expect(rows[1]).toMatchObject({ verdict: "revise", status: "warn" });
    expect(rows[2]).toMatchObject({ direction: "writeback", count: 1 });
    expect(rows[3]).toMatchObject({ status: "warn", eventIndexes: [3] });
    expect(rows[5]).toMatchObject({ kind: "error", text: "上游 502", status: "error" });
  });

  it("a tool with worker sub-timelines gets one subagent row per worker right after it, carrying the worker frames' indexes", () => {
    // 复用 worker_timeline.test.ts 里 spawn_worker 的最小 fixture(worker 帧 + 父 tool_call);
    // 断言:tool 行之后紧跟 kind==="subagent",parentEntryId === 该 tool 的 entry.id,
    // eventIndexes 恰为那些 event==="worker" 且 worker_id 相同的帧下标。
  });

  it("row ids are unique within a turn", () => {
    const rows = compactRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "r" }, tool_calls: [{ id: "a", name: "t", args: {} }, { id: "b", name: "t", args: {} }] }] }),
      upd("agent", { step_count: 2, messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "r2" } }] }),
    ]);
    expect(new Set(rows.map((r) => r.id)).size).toBe(rows.length);
  });
});

describe("trajectoryRowsOf", () => {
  const EVENTS = [
    upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "a", name: "t1", args: {} }] }] }),
    upd("tools", { messages: [{ type: "tool", tool_call_id: "a", name: "t1", content: "r", status: "success" }] }),
    upd("agent", { step_count: 2, _duration_ms: 400, messages: [{ type: "ai", content: "最终答案", response_metadata: { model_name: "gpt-x" } }] }),
    ev("end", { status: "success" }),
  ];
  it("user first, one think per agent step even without reasoning, assistant last; ids shared with the compact projection", () => {
    const rows = trajectoryRowsOf(EVENTS, INPUT, "最终答案", "done");
    expect(rows.map((r) => r.kind)).toEqual(["user", "think", "tool", "think", "assistant"]);
    expect(rows[0]).toMatchObject({ id: "user", text: "帮我看看这个客户", seq: -1 });
    expect(rows[1]).toMatchObject({ kind: "think", text: "", step: 1 });
    expect(rows[3]).toMatchObject({ kind: "think", text: "", step: 2, model: "gpt-x", durationMs: 400 });
    expect(rows.at(-1)).toMatchObject({ id: "assistant", kind: "assistant", text: "最终答案", status: "ok", eventIndexes: [2] });
    const compactIds = new Set(compactRowsOf(EVENTS).map((r) => r.id));
    for (const id of compactIds) expect(rows.some((r) => r.id === id)).toBe(true);
  });
  it("assistant row omitted while answer is null; status follows turnStatus", () => {
    expect(trajectoryRowsOf(EVENTS, INPUT, null, "running").some((r) => r.kind === "assistant")).toBe(false);
    expect(trajectoryRowsOf(EVENTS, INPUT, "x", "error").at(-1)).toMatchObject({ kind: "assistant", status: "error" });
  });
});

describe("resolveGanttKey", () => {
  it("maps item-/tool-/worker- keys to row ids (planner merged into update_plan resolves through plannerSeq)", () => {
    const rows = trajectoryRowsOf([
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [{ id: "p1", name: "update_plan", args: { goal: "g", steps: PLAN.steps } }] }] }),
      upd("tools", { messages: [{ type: "tool", tool_call_id: "p1", name: "update_plan", content: "ok", status: "success" }], plan: PLAN }),
      upd("memory_recall", { recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }] }),
    ], INPUT, null, "running");
    const think = rows.find((r) => r.kind === "think");
    const plan = rows.find((r) => r.kind === "plan");
    const memory = rows.find((r) => r.kind === "memory");
    expect(resolveGanttKey(rows, `item-${think!.seq}`)).toBe(think!.id);
    expect(resolveGanttKey(rows, "tool-p1")).toBe(plan!.id);
    expect(resolveGanttKey(rows, `item-${(plan as { plannerSeq: number }).plannerSeq}`)).toBe(plan!.id);
    expect(resolveGanttKey(rows, `item-${memory!.seq}`)).toBe(memory!.id);
    expect(resolveGanttKey(rows, "tool-nope")).toBeNull();
    // worker: 沿用 subagent 那条测试的 fixture,断言 `worker-<workerId>-0` → 那条 subagent 行 id
  });
});
```

`timeline.test.ts` 追加:

```ts
it("items carry the index of the frame they came from (two items from one updates frame share it)", () => {
  const items = parseTimeline([
    ev("compaction", { passes: 1, tokens_before: 1, tokens_after: 1, summary_chars: 0 }),
    upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", additional_kwargs: { reasoning_content: "r" } }], recalled_memories: [{ id: "m" }] }),
  ]);
  expect(items.map((it) => it.eventIndex)).toEqual([0, 1, 1]);
});
```

(`subagent` / `worker-` 两处 fixture 从 `src/api/__tests__/worker_timeline.test.ts` 抄一份最小的 worker 帧;实施时把注释换成真代码。)

- [ ] **Step 2: 跑红**

Run: `pnpm exec vitest run src/api/__tests__/trajectory_rows.test.ts src/api/__tests__/timeline.test.ts` → 前者模块不存在;后者新增那条红(`eventIndex` 全 undefined)。

- [ ] **Step 3: 实现**

`timeline.ts`:接口各加 `/** 来源帧在 events 里的下标(Raw tab / 轨迹行用);老调用方不依赖。 */ eventIndex?: number;`;`parseTimeline` 里 `let evtIndex = 0;` + `push` 改为 `items.push({ ...it, seq: seq++, serverMs: evtServerMs, eventIndex: evtIndex } as TimelineItem)`,循环改 `for (const [i, evt] of events.entries()) { evtIndex = i; … }`(其余一字不动)。

`trajectory_rows.ts`:

```ts
function rowsOf(events: readonly SseEvent[], opts: { everyStepThinks: boolean }): CompactRow[] {
  const rows: CompactRow[] = [];
  const idx = (item: { eventIndex?: number }): number[] => (item.eventIndex === undefined ? [] : [item.eventIndex]);
  for (const item of parseTimeline(events)) {
    if (item.kind === "agent") {
      if (item.reasoning !== null || opts.everyStepThinks) {
        rows.push({ id: `think:${item.seq}`, kind: "think", seq: item.seq, step: item.stepCount, text: item.reasoning ?? "", content: item.content,
          model: item.model, inputTokens: item.inputTokens, outputTokens: item.outputTokens, finishReason: item.finishReason,
          status: item.hasError ? "error" : "ok", durationMs: item.durationMs, eventIndexes: idx(item), serverMs: item.serverMs ?? null });
      }
      item.tools.forEach((entry, ti) => {
        const status = toolStatus(entry.status);
        const eventIndexes = [...idx(item), ...resultIdx(events, entry.id)];
        if (entry.toolName === "update_plan") {
          const a = entry.args;
          rows.push({ id: `plan:${item.seq}:${ti}`, kind: "plan", seq: item.seq, step: item.stepCount, status, durationMs: entry.durationMs, eventIndexes,
            source: "update_plan", callId: entry.id, plannerSeq: null,
            stepsTotal: Array.isArray(a.steps) ? a.steps.length : 0,
            goal: typeof a.goal === "string" ? a.goal : null, reason: typeof a.reason === "string" ? a.reason : null, plan: null });
        } else {
          rows.push({ id: `tool:${item.seq}:${ti}`, kind: "tool", seq: item.seq, step: item.stepCount, status, durationMs: entry.durationMs, eventIndexes, entry });
        }
        entry.workers?.forEach((worker, wi) => {
          rows.push({ id: `subagent:${item.seq}:${ti}:${wi}`, kind: "subagent", seq: item.seq, step: item.stepCount,
            status: worker.status === "running" ? "running" : worker.status === "success" ? "ok" : "warn",
            durationMs: worker.summary?.wallClockMs ?? null, eventIndexes: workerIdx(events, worker.workerId), worker, parentEntryId: entry.id });
        });
      });
      continue;
    }
    if (item.kind === "memory_recall" || item.kind === "memory_writeback" || item.kind === "reflect" || item.kind === "planner") {
      // …按规则 2;planner 合并:for (let i = rows.length - 1; i >= 0; i--) 找 kind==="plan" && source==="update_plan" && plan===null,
      // 找到就 rows[i] = { ...rows[i], plan, stepsTotal, goal, plannerSeq: item.seq, eventIndexes: uniq([...rows[i].eventIndexes, ...idx(item)]) }(不可变替换;`uniq` = `Array.from(new Set(xs))`)
      continue;
    }
    if (item.kind === "workspace_ingest" || item.kind === "end") continue;
    rows.push({ id: `${item.kind}:${item.seq}`, kind: item.kind, seq: item.seq, step: null, text: item.text,
      status: item.tone === "bad" ? "error" : item.tone === "warn" ? "warn" : item.tone === "pause" ? "pause" : "ok",
      durationMs: null, eventIndexes: idx(item) });
  }
  return rows;
}
export function compactRowsOf(events: readonly SseEvent[]): CompactRow[] { return rowsOf(events, { everyStepThinks: false }); }
```

`resultIdx(events, callId)`:遍历 `events`,`event === "updates"` 且任一节点 `messages[]` 里有 `type === "tool" && tool_call_id === callId` → `[i]`,否则 `[]`(用 `messagesOf`(`api/tool_timeline.ts:126`)取消息)。`workerIdx(events, workerId)`:所有 `event === "worker"` 且 `obj(data).worker_id === workerId` 的下标。`toolStatus`:`pending`→`running`、`success`→`ok`、`error`→`error`、`pending_approval`→`pause`。`isPlan` 判定复用 `plan_reducer.ts` 的(导出它)。`trajectoryRowsOf` / `resolveGanttKey` 按规则 5 / 6。

- [ ] **Step 4: 跑绿**:`pnpm exec vitest run src/api/__tests__/trajectory_rows.test.ts src/api/__tests__/timeline.test.ts src/api/__tests__/gantt_timeline.test.ts src/pages/agent_detail/playground/__tests__/StepTimeline.test.tsx` + `pnpm typecheck` + `pnpm exec eslint src/api/trajectory_rows.ts src/api/timeline.ts`

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/timeline.ts apps/admin-ui/src/api/trajectory_rows.ts apps/admin-ui/src/api/__tests__/trajectory_rows.test.ts apps/admin-ui/src/api/__tests__/timeline.test.ts apps/admin-ui/src/api/plan_reducer.ts
git commit -m "feat(console): trajectory_rows —— parseTimeline 产物投影成紧凑行 / 轨迹行(同一 id 体系,含 update_plan 合并、Raw 帧下标、Gantt key 解析)"
```

---

### Task 5: 纯函数 `api/session_stats.ts`(状态栏公式)+ `components/console/types.ts` / `console_turns.ts`(视图模型)+ `live_rows.ts`(live 合成行)

**Files:**
- Create: `apps/admin-ui/src/api/session_stats.ts`;Test: `apps/admin-ui/src/api/__tests__/session_stats.test.ts`
- Create: `apps/admin-ui/src/components/console/types.ts`、`apps/admin-ui/src/components/console/console_turns.ts`;Test: `apps/admin-ui/src/components/console/__tests__/console_turns.test.ts`
- Create: `apps/admin-ui/src/components/console/live_rows.ts`;Test: `apps/admin-ui/src/components/console/__tests__/live_rows.test.ts`(live 轮合成行,Task 11 的 TurnBlock 与 Task 18 的 TrajectoryPanel 共用,所以放在这一层而不是各写一份)

**Interfaces:**
- Consumes:`summarizeTurn`(`api/turn_summary.ts`)、`RunTokens`(`api/runs.ts`,Task 1 后 `HistoryTurn.tokens` 有了)、`RateCardRecord`(`api/rate_card.ts`)、`Turn / HistoryLoad / HistoryTurn`(`components/turn/types.ts`)、`FallbackLine`(`pages/agent_detail/playground/history_turns.ts`)、`runIdOf`(`components/turn/TurnCard.tsx`——**复制**这个 12 行函数到 `console_turns.ts` 里导出,不 import TurnCard,PR-B 删它时不牵连)、`CompactRow / ToolRow`(Task 4)、`parseTimeline`、`LiveStep`(`pages/agent_detail/playground/useTokenStream.ts`)。
- Produces:

```ts
// components/console/types.ts
export type LoadState = "pending" | "loading" | "done" | "error";
export interface TurnTiming { ttftMs: number | null; firstTokenAt: number | null; lastTokenAt: number | null; }
export interface ConsoleTurn {
  key: string;                       // history: h.key;live: turn.id
  seq: number;                       // 0-based,历史在前
  source: "history" | "live";
  turn: Turn;                        // 历史轮由 HistoryTurn + HistoryLoad 合成(status/error 映射同 PlaygroundTab.tsx:1337-1360)
  runId: string | null;
  loadState: LoadState;              // live 恒 "done"
  fallbackLines: FallbackLine[];     // live 恒 []
  tokens: RunTokens | null;          // 历史轮持久 rollup;live null
  timing: TurnTiming | null;         // 只有本会话内 live 跑过的轮有
}

// components/console/console_turns.ts
export function buildConsoleTurns(args: {
  historyTurns: HistoryTurn[] | null;
  historyLoads: Record<string, HistoryLoad>;
  liveTurns: readonly Turn[];
  timings: Readonly<Record<string, TurnTiming>>;   // by live turn id
}): ConsoleTurn[];

// api/session_stats.ts
export interface StatsTurnInput {
  events: readonly SseEvent[];
  loaded: boolean;                   // events 完整(live 轮恒 true;历史轮 loadState==="done")
  status: "running" | "done" | "error";
  tokens: RunTokens | null;          // 仅 !loaded 时使用
  timing: TurnTiming | null;
}
export interface SessionStats {
  turns: number; steps: number;
  llmMs: number; toolMs: number;
  ttftAvgMs: number | null;
  tokPerSec: number | null;          // ≈,客户端时钟
  cacheHitPct: number | null;        // 0-100,整数四舍五入
  inputTokens: number; outputTokens: number;
  costCny: number | null;            // rate 为 null → null
  partial: boolean;                  // 有轮既未加载事件也没有 rollup
}
export function computeSessionStats(turns: readonly StatsTurnInput[], rate: RateCardRecord | null): SessionStats;
export function statsInputOf(t: ConsoleTurn): StatsTurnInput;   // 放在 console_turns.ts,ConsoleTurn → StatsTurnInput

// components/console/live_rows.ts
/** 已落地的 agent 步号集合(TurnCard.tsx:466-472 同算法:parseTimeline 里 kind==="agent" 且 stepCount 非 null 的 stepCount)。 */
export function settledStepsOf(events: readonly SseEvent[]): Set<number>;
/** live 轮未落地步的合成行:liveByStep 里每个不在 settled 的 step(升序)→ reasoning 非空时一条 think(id `live-think:<step>`,seq -1,status "running",text=reasoning,content=live.content,model null,tokens 0,durationMs null,eventIndexes [],serverMs null)
 *  + toolNames 每项一条 tool(id `live-tool:<step>:<i>`,status "running",entry 最小形状 { id: `live-<step>-<i>`, rawName: name, isMcp: false, server: null, toolName: name, args: {}, status: "pending", resultPreview: null, durationMs: null });liveByStep undefined → []。 */
export function liveSyntheticRows(events: readonly SseEvent[], liveByStep: ReadonlyMap<number, LiveStep> | undefined): CompactRow[];
```

公式(spec §二.1 状态栏表,R12 修正缓存项):
- `turns` = 满足「`loaded && (summary.stepCount ?? 0) >= 1`」或「`status === "running"`」或「`!loaded`」(历史未加载轮按 run 计)的轮数。
- `steps` = Σ loaded 轮 `summarizeTurn(events).stepCount ?? 0`。
- `llmMs` / `toolMs` = Σ loaded 轮所有 `updates` 帧里 `data.agent._duration_ms` / `data.tools._duration_ms`(数值型才计;键名就是 `agent` / `tools`)。
- `ttftAvgMs` = `timing.ttftMs` 非 null 者的平均;没有 → null。
- `tokPerSec` = Σ(有 `firstTokenAt < lastTokenAt` 的轮的 `usage.outputTokens`)÷ Σ(`lastTokenAt - firstTokenAt`)/1000;分母 0 → null;保留 1 位小数。
- 用量:loaded 轮取 `summarizeTurn(events).usage`(null 当 0);unloaded 轮取 `tokens`(null → 计入 `partial`)。`inputTokens` = Σ input;`outputTokens` = Σ output;`cacheHitPct` = round(Σ cacheRead ÷ Σ input × 100),Σ input 为 0 → null。
- `costCny` = `rate ? (max(0, Σ input − Σ cacheRead) × rate.input_per_mtok_micros + Σ cacheRead × rate.cache_read_per_mtok_micros + Σ output × rate.output_per_mtok_micros) / 1e12 : null`(与 `TurnCard.tsx:640-651` 同式,汇总后算)。

- [ ] **Step 1: 测试(红)**

`session_stats.test.ts`(核心几条写全;其余按同法补):

```ts
import { describe, expect, it } from "vitest";

import { computeSessionStats, type StatsTurnInput } from "../session_stats";
import type { SseEvent } from "../sessions";

function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return { id: null, event: "updates", data: { [node]: channels }, rawData: "", receivedAt: "" };
}
function aiStep(step: number, durationMs: number, usage: { in: number; out: number; cacheRead?: number }): SseEvent {
  return upd("agent", {
    step_count: step, _duration_ms: durationMs,
    messages: [{ type: "ai", content: "a", usage_metadata: {
      input_tokens: usage.in, output_tokens: usage.out, total_tokens: usage.in + usage.out,
      input_token_details: { cache_read: usage.cacheRead ?? 0 },
    } }],
  });
}
const toolsStep = (durationMs: number): SseEvent => upd("tools", { _duration_ms: durationMs, messages: [] });
const live = (events: SseEvent[], extra: Partial<StatsTurnInput> = {}): StatsTurnInput =>
  ({ events, loaded: true, status: "done", tokens: null, timing: null, ...extra });

describe("computeSessionStats", () => {
  it("sums turns/steps/LLM ms/tool ms/tokens across loaded turns; cache hit = cache_read ÷ input", () => {
    const s = computeSessionStats([
      live([aiStep(1, 800, { in: 1000, out: 100, cacheRead: 900 }), toolsStep(300), aiStep(2, 700, { in: 1200, out: 50, cacheRead: 1100 })]),
      live([aiStep(1, 500, { in: 500, out: 20 })]),
    ], null);
    expect(s).toMatchObject({ turns: 2, steps: 3, llmMs: 2000, toolMs: 300, inputTokens: 2700, outputTokens: 170, partial: false, costCny: null });
    expect(s.cacheHitPct).toBe(74); // 2000/2700
  });
  it("averages ttft and computes ≈tok/s from first/last token wall-clock", () => {
    const s = computeSessionStats([
      live([aiStep(1, 100, { in: 10, out: 300 })], { timing: { ttftMs: 800, firstTokenAt: 10_000, lastTokenAt: 12_000 } }),
      live([aiStep(1, 100, { in: 10, out: 100 })], { timing: { ttftMs: 400, firstTokenAt: 20_000, lastTokenAt: 21_000 } }),
      live([aiStep(1, 100, { in: 10, out: 999 })], { timing: null }), // no timing → excluded from tok/s
    ], null);
    expect(s.ttftAvgMs).toBe(600);
    expect(s.tokPerSec).toBe(133.3); // (300+100)/(2+1)s
  });
  it("uses the persisted rollup for unloaded history turns and flags partial when a turn has neither", () => {
    const s = computeSessionStats([
      { events: [], loaded: false, status: "done", timing: null, tokens: { input_tokens: 400, output_tokens: 40, cache_creation_tokens: 0, cache_read_tokens: 200, total_tokens: 440, llm_calls: 1, models: [] } },
      { events: [], loaded: false, status: "done", timing: null, tokens: null },
    ], null);
    expect(s).toMatchObject({ turns: 2, steps: 0, inputTokens: 400, outputTokens: 40, cacheHitPct: 50, partial: true });
  });
  it("prices with the rate card exactly like TurnCard.costCny (non-cached input + cache read + output)", () => {
    const rate = { input_per_mtok_micros: 3_000_000, cache_read_per_mtok_micros: 300_000, output_per_mtok_micros: 15_000_000 } as never;
    const s = computeSessionStats([live([aiStep(1, 1, { in: 1_000_000, out: 100_000, cacheRead: 400_000 })])], rate);
    // (600k*3e6 + 400k*3e5 + 100k*1.5e7)/1e12 = 1.8 + 0.12 + 1.5
    expect(s.costCny).toBeCloseTo(3.42, 6);
  });
  it("counts a running turn with no step yet; empty input → zeros/nulls", () => {
    expect(computeSessionStats([live([], { status: "running" })], null).turns).toBe(1);
    expect(computeSessionStats([], null)).toEqual({ turns: 0, steps: 0, llmMs: 0, toolMs: 0, ttftAvgMs: null, tokPerSec: null, cacheHitPct: null, inputTokens: 0, outputTokens: 0, costCny: null, partial: false });
  });
});
```

`console_turns.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { buildConsoleTurns } from "../console_turns";

const meta = (runId: string) => ({ id: "1", event: "metadata", data: { run_id: runId }, rawData: "", receivedAt: "" });

describe("buildConsoleTurns", () => {
  it("orders history before live, numbers seq from 0, maps history status/error like the old TurnCard call site", () => {
    const out = buildConsoleTurns({
      historyTurns: [
        { key: "h1", input: "q1", fallbackLines: [], runId: "r1", status: "success", tokens: null },
        { key: "h2", input: "q2", fallbackLines: [{ text: "partial", channel: "final" }], runId: "r2", status: "timeout", tokens: null },
      ],
      historyLoads: { r1: { state: "done", events: [meta("r1")] } },
      liveTurns: [{ id: "L1", input: "q3", attachments: [], events: [meta("r3")], status: "running", error: null, approval: null }],
      timings: { L1: { ttftMs: 500, firstTokenAt: 1, lastTokenAt: 2 } },
    });
    expect(out.map((t) => [t.key, t.seq, t.source])).toEqual([["h1", 0, "history"], ["h2", 1, "history"], ["L1", 2, "live"]]);
    expect(out[0]).toMatchObject({ runId: "r1", loadState: "done", turn: { status: "done", error: null } });
    expect(out[1]).toMatchObject({ runId: "r2", loadState: "pending", turn: { status: "error", error: "timeout", events: [] }, fallbackLines: [{ text: "partial", channel: "final" }] });
    expect(out[2]).toMatchObject({ runId: "r3", loadState: "done", timing: { ttftMs: 500 }, tokens: null });
  });
  it("passes the persisted rollup through and returns [] for null history + no live turns", () => {
    const tokens = { input_tokens: 1, output_tokens: 1, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 2, llm_calls: 1, models: [] };
    expect(buildConsoleTurns({ historyTurns: [{ key: "h", input: "", fallbackLines: [], runId: "r", status: "success", tokens }], historyLoads: {}, liveTurns: [], timings: {} })[0].tokens).toEqual(tokens);
    expect(buildConsoleTurns({ historyTurns: null, historyLoads: {}, liveTurns: [], timings: {} })).toEqual([]);
  });
});
```

`live_rows.test.ts`:①`liveByStep = Map{1 → {…}, 2 → {content:"partial", reasoning:"thinking…", toolNames: Map{0:"query_crm"}, reasoningMs:null}}` 而 events 只落地到 step 1 → 返回恰两条:`live-think:2`(text "thinking…",status running)与 `live-tool:2:0`(toolName query_crm,status running);②step 1 已落地 → 其残留 buffer 不出行;③`liveByStep` undefined → `[]`。(`LiveStep` 形状照 `useTokenStream.ts` 的导出。)

- [ ] **Step 2: 跑红** → 三个模块不存在。

- [ ] **Step 3: 实现**(纯函数;`nodeDurations(events)` 私有 helper 读 `updates` 帧;不可变累加)

- [ ] **Step 4: 跑绿 + `pnpm typecheck`**

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/session_stats.ts apps/admin-ui/src/api/__tests__/session_stats.test.ts apps/admin-ui/src/components/console/types.ts apps/admin-ui/src/components/console/console_turns.ts apps/admin-ui/src/components/console/__tests__/console_turns.test.ts apps/admin-ui/src/components/console/live_rows.ts apps/admin-ui/src/components/console/__tests__/live_rows.test.ts
git commit -m "feat(console): 状态栏公式 session_stats + ConsoleTurn 视图模型 buildConsoleTurns + live 合成行 liveSyntheticRows"
```

---

### Task 6: `SessionSidebar` —— 左栏会话列表(`SessionHistoryDrawer` 逻辑迁入)

**Files:**
- Create: `apps/admin-ui/src/components/console/SessionSidebar.tsx`(≤ 400 行;列表项拆 `SessionSidebarItem.tsx` 若超)、`apps/admin-ui/src/components/console/relative_time.ts`
- Test: `apps/admin-ui/src/components/console/__tests__/SessionSidebar.test.tsx`、`__tests__/relative_time.test.ts`
- 参考(只读,Task 19 才删):`apps/admin-ui/src/components/SessionHistoryDrawer.tsx:88-423`、`components/__tests__/SessionHistoryDrawer.test.tsx`

**Interfaces:**
- Consumes:`listSessions / renameSession / archiveSession / purgeSession / ThreadMeta`(`api/sessions.ts`,Task 1 后 `listSessions` 有 `orderBy`)、`useTenantScope`、`ReadonlyTooltip`、i18n `session_history.*` + `console.sidebar_*`。
- Produces:

```ts
export interface SessionSidebarProps {
  agentName: string;
  currentThreadId: string | null;
  /** 当前会话是否有 run 在跑(R3:活动点 + 禁用切换)。 */
  running: boolean;
  onNew: () => void;
  onResume: (session: ThreadMeta) => void;
  /** 切入态只读:新建 / 改名 / 归档 / 删除置灰,列表仍可看可切。 */
  readOnly?: boolean;
  /** 会话被改名 / 归档 / 删除后回调(父级刷新标题 / 若删的是当前会话则回到草稿)。 */
  onChanged?: (change: { kind: "rename" | "archive" | "purge"; threadId: string; title?: string }) => void;
  /** 列表刷新触发器:父级 `thread` 从 null 变成新建的会话时 +1,让新会话出现在列表顶部。 */
  reloadTick?: number;
}
export function SessionSidebar(props: SessionSidebarProps): JSX.Element;
```

行为:
- 拉取:`listSessions({ agentName, q, status, limit: 50, offset, orderBy: "last_activity", tenantScope })`;搜索框 300 ms 防抖(与 Drawer 同);状态筛选改成两档 `Segmented`:「活跃」(`status` 不传)/「已归档」(`status: "archived"`)——Drawer 那个 6 值 Select 在 jsdom 测不了,spec 也只要活跃 / 归档;「加载更多」同 Drawer(`page.length === 50` 则有更多)。
- 列表项:标题(`title ?? thread_id.slice(0,8)+"…"`)、相对时间(`updated_at`,复用 Drawer 的 `relativeTime`——搬进 `relative_time.ts` 导出)、当前项高亮(`--ew-surface-selected`)、当前项 + `running` → 标题前一个活动点(`data-testid="console-session-running-dot"`,`aria-label={t("console.sidebar_running_dot")}`,CSS 呼吸动画,`prefers-reduced-motion` 关);hover 出三个图标按钮(改名 / 归档 / 彻底删除,Popconfirm 文案沿用 `session_history.*`;`readOnly` 时 `ReadonlyTooltip` + disabled)。
- 点击项:`running` 时禁用(tooltip 沿用 `playground.running`);否则 `onResume(session)`。
- 顶部:`console.sidebar_title` + 「新建」按钮(`data-testid="playground-new-session"` **保留**;`disabled={running || readOnly}`)。
- testid:`console-session-sidebar`、`console-session-search`、`console-session-filter`、`console-session-list`、`console-session-item-<id>`、`console-session-rename-<id>` / `-archive-<id>` / `-purge-<id>`、`console-session-load-more`、`console-session-rename-input`、`console-session-empty`。

- [ ] **Step 1: 测试(红)** —— 把 `SessionHistoryDrawer.test.tsx` 9 条改写到新 testid + 新 props(mock 方式照抄:`vi.spyOn(sessionsSdk, …)` + `mockTenantScopeModule`),再加:

```ts
it("asks the server for last_activity order and the agent's own sessions", async () => {
  renderSidebar();
  await waitFor(() => expect(listMock).toHaveBeenCalled());
  expect(listMock.mock.calls[0][0]).toMatchObject({ agentName: "demo-agent", orderBy: "last_activity", limit: 50 });
});
it("highlights the current thread and shows the running dot only when running", () => {
  const { rerender } = renderSidebar({ currentThreadId: A.thread_id, running: false });
  expect(screen.queryByTestId("console-session-running-dot")).toBeNull();
  rerender(tree({ currentThreadId: A.thread_id, running: true }));
  expect(within(screen.getByTestId(`console-session-item-${A.thread_id}`)).getByTestId("console-session-running-dot")).toBeInTheDocument();
});
it("disables switching sessions while a run is in flight", async () => {
  const onResume = vi.fn();
  renderSidebar({ running: true, onResume });
  await userEvent.click(await screen.findByTestId(`console-session-item-${B.thread_id}`));
  expect(onResume).not.toHaveBeenCalled();
});
it("loads more with the next offset", async () => {
  listMock.mockResolvedValueOnce(Array.from({ length: 50 }, (_, i) => ({ ...A, thread_id: `t-${i}` })));
  renderSidebar();
  await userEvent.click(await screen.findByTestId("console-session-load-more"));
  expect(listMock.mock.calls[1][0]).toMatchObject({ offset: 50 });
});
it("archived filter passes status=archived", async () => {
  renderSidebar();
  await userEvent.click(await screen.findByText("已归档"));
  await waitFor(() => expect(listMock.mock.calls.at(-1)?.[0]).toMatchObject({ status: "archived" }));
});
it("read-only: new/rename/archive/purge disabled, list still clickable", async () => {
  const onResume = vi.fn();
  renderSidebar({ readOnly: true, onResume });
  expect(screen.getByTestId("playground-new-session")).toBeDisabled();
  await userEvent.click(await screen.findByTestId(`console-session-item-${A.thread_id}`));
  expect(onResume).toHaveBeenCalledWith(A);
});
```

(`renderSidebar(overrides)` / `tree(overrides)` 是本文件的 helper:`MemoryRouter > App > <SessionSidebar agentName="demo-agent" currentThreadId={null} running={false} onNew={vi.fn()} onResume={vi.fn()} {...overrides} />`;`A` / `B` fixture 从 Drawer 测试抄。)

- [ ] **Step 2: 跑红** → 模块不存在。
- [ ] **Step 3: 实现**(从 Drawer 搬 `load / debounce / rename Modal / Popconfirm` 逻辑;不改语义;`relativeTime` 抽到 `relative_time.ts` 并给它 3 条单测:刚刚 / N 分钟 / N 天)。
- [ ] **Step 4: 跑绿;`pnpm typecheck`;`pnpm exec eslint src/components/console`**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/SessionSidebar.tsx apps/admin-ui/src/components/console/relative_time.ts apps/admin-ui/src/components/console/__tests__/SessionSidebar.test.tsx apps/admin-ui/src/components/console/__tests__/relative_time.test.ts
git commit -m "feat(console): SessionSidebar 左栏会话列表(最近活动倒序 / 搜索 / 活跃-归档 / 新建 / 改名 / 归档 / 删除 / 加载更多)"
```

---

### Task 7: 输入区 —— `VariablesForm` / `Composer` / `AttachmentChips`

**Files:**
- Create: `apps/admin-ui/src/components/console/VariablesForm.tsx`、`Composer.tsx`、`AttachmentChips.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/VariablesForm.test.tsx`、`Composer.test.tsx`、`AttachmentChips.test.tsx`
- 参考:`PlaygroundTab.tsx:840-1013`(现 JSX)

**Interfaces:**
- Consumes:`readPromptVariables` 的元素类型(`components/manifest-editor/form_model.ts`——导出名照文件里的,形如 `{ name: string; description?: string; required?: boolean }`)、`Attachment`(`components/turn/types.ts`)、`ReadonlyTooltip`;`useIsTenantSwitched` **不在组件内读**(父级传 `readOnly`)。
- Produces:

```ts
export interface PromptVariable { name: string; description?: string; required?: boolean }
/** 纯函数:必填且值为空/全空白的变量名,按声明顺序。导出给 Composer / PlaygroundTab。 */
export function missingRequired(vars: readonly PromptVariable[], values: Readonly<Record<string, string>>): string[];

export interface VariablesFormProps {
  variables: readonly PromptVariable[];
  values: Readonly<Record<string, string>>;
  onChange: (name: string, value: string) => void;
  disabled: boolean;
}
export function VariablesForm(props: VariablesFormProps): JSX.Element | null;   // variables 为空 → null

export interface ComposerProps {
  value: string; onChange: (v: string) => void;
  onSend: () => void; onStop: () => void;
  running: boolean; uploading: boolean; readOnly: boolean;
  /** 非空 → 发送禁用 + tooltip `console.vars_required_missing`。 */
  missingVariables: readonly string[];
  onAttachImage: () => void; onAttachDocument: () => void;   // 父级持有 <input type=file> ref
  maxLength?: number;   // 默认 65536
}
export function Composer(props: ComposerProps): JSX.Element;

export interface AttachmentChipsProps { attachments: readonly Attachment[]; onRemove: (id: string) => void; }
export function AttachmentChips(props: AttachmentChipsProps): JSX.Element | null;
```

行为:
- `VariablesForm`:每变量一行 —— 名(`mono`)+ 必填标(`console.vars_required_mark`)+ 描述(secondary)+ `Input`(`data-testid="playground-var-<name>"`、`aria-label` 同现状);容器 `data-testid="playground-vars"`。
- `Composer`:`TextArea`(`data-testid="playground-input"`,`autoSize {minRows:3,maxRows:12}`,`maxLength`,`showCount`,`disabled={running || readOnly}`);**`onKeyDown`:`Enter` 且非 `shiftKey` 且 `!e.nativeEvent.isComposing` → `e.preventDefault(); if (canSend) onSend()`**;下面一行提示 `console.composer_hint`(secondary,12px);按钮:发送(`playground-run`,`disabled = readOnly || (!running && (value.trim() === "" || missingVariables.length > 0))`;missing 非空时外包 `Tooltip title={t("console.vars_required_missing", { names: missingVariables.join(", ") })}`;`readOnly` 用 `ReadonlyTooltip`)、图片(`playground-attach`)、文档(`playground-attach-doc`)、停止(`playground-stop`,仅 running)。文案沿用 `playground.run / running / attach_image / attach_document / uploading / stop`。
- `AttachmentChips`:沿用 `playground-attachments` / `playground-attachment` testid 与 `Tag closable` 结构。

- [ ] **Step 1: 测试(红)**

```ts
// Composer.test.tsx(base = 全部 props 的默认值,onSend 等 vi.fn())
it("Enter sends, Shift+Enter inserts a newline, Enter during IME composition does nothing", () => {
  const onSend = vi.fn();
  render(<Composer {...base} value="hi" onSend={onSend} />);
  const ta = screen.getByTestId("playground-input");
  fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.keyDown(ta, { key: "Enter", isComposing: true });   // fireEvent 把 isComposing 放进 nativeEvent
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.keyDown(ta, { key: "Enter" });
  expect(onSend).toHaveBeenCalledTimes(1);
});
it("send is disabled with a required variable missing and the tooltip names it", async () => {
  render(<Composer {...base} value="hi" missingVariables={["customer_code"]} />);
  const btn = screen.getByTestId("playground-run");
  expect(btn).toBeDisabled();
  await userEvent.hover(btn.parentElement ?? btn);
  expect(await screen.findByText(/customer_code/)).toBeInTheDocument();
});
it("send disabled on empty input; enabled with text; readOnly disables send/attach", () => {
  const { rerender } = render(<Composer {...base} value="" />);
  expect(screen.getByTestId("playground-run")).toBeDisabled();
  rerender(<Composer {...base} value="x" />);
  expect(screen.getByTestId("playground-run")).toBeEnabled();
  rerender(<Composer {...base} value="x" readOnly />);
  expect(screen.getByTestId("playground-run")).toBeDisabled();
  expect(screen.getByTestId("playground-attach")).toBeDisabled();
});
// VariablesForm.test.tsx
it("renders one input per variable with the required mark and forwards edits", async () => {
  const onChange = vi.fn();
  render(<VariablesForm variables={[{ name: "customer_code", description: "客户编码", required: true }, { name: "tone", required: false }]} values={{}} onChange={onChange} disabled={false} />);
  expect(screen.getAllByText("必填")).toHaveLength(1);
  await userEvent.type(screen.getByTestId("playground-var-customer_code"), "C-1");
  expect(onChange).toHaveBeenLastCalledWith("customer_code", "C-1");
});
it("missingRequired lists required vars whose value is empty/whitespace, in declaration order", () => {
  expect(missingRequired([{ name: "a" }, { name: "b", required: false }, { name: "c", required: true }], { a: " ", c: "x" })).toEqual(["a"]);
});
// AttachmentChips.test.tsx
it("renders a closable tag per attachment and calls onRemove with its id", async () => {
  const onRemove = vi.fn();
  render(<AttachmentChips attachments={[{ id: "image:a", name: "a.png", kind: "image", value: "a" }]} onRemove={onRemove} />);
  await userEvent.click(screen.getByLabelText("移除附件"));   // 沿用 playground.remove_attachment
  expect(onRemove).toHaveBeenCalledWith("image:a");
});
```

- [ ] **Step 2: 跑红** → 模块不存在。
- [ ] **Step 3: 实现**(JSX 从 PlaygroundTab 搬,加 keyDown 与 missing 逻辑;不读全局 hook)。
- [ ] **Step 4: 跑绿;typecheck;eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/VariablesForm.tsx apps/admin-ui/src/components/console/Composer.tsx apps/admin-ui/src/components/console/AttachmentChips.tsx apps/admin-ui/src/components/console/__tests__/VariablesForm.test.tsx apps/admin-ui/src/components/console/__tests__/Composer.test.tsx apps/admin-ui/src/components/console/__tests__/AttachmentChips.test.tsx
git commit -m "feat(console): 输入区 VariablesForm(必填拦发送)/ Composer(Enter 发送 + IME 守卫)/ AttachmentChips"
```

---

### Task 8: 任务卡 —— `PlanEditForm`(从 PlanPanel 抽)+ `usePlanCard` + `PlanCard`

**Files:**
- Create: `apps/admin-ui/src/components/console/PlanEditForm.tsx`(从 `pages/run_detail/PlanPanel.tsx:198-258` 抽出的表单 JSX + `patchStep`)
- Modify: `apps/admin-ui/src/pages/run_detail/PlanPanel.tsx`(改用 `PlanEditForm`;testid / 行为不变;`pages/__tests__/PlanPanel.test.tsx` 6 条必须原样绿)
- Create: `apps/admin-ui/src/components/console/usePlanCard.ts`、`PlanCard.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/PlanEditForm.test.tsx`、`usePlanCard.test.ts`、`PlanCard.test.tsx`

**Interfaces:**
- Consumes:`getThreadPlan / updateThreadPlan / ThreadPlan / PlanStep / PlanStepStatus`(`api/plan.ts`)、`reducePlan`(Task 3)、i18n `plan_panel.*` + `console.plan_*`、`localStorage` 键 `expert_work.console.planCollapsed`(`"1"` / `"0"`,照 `EventStreamPanel.tsx:33-60` 的读写法)。
- Produces:

```ts
// PlanEditForm.tsx —— 受控表单,不含按钮
export interface PlanEditFormProps { draft: ThreadPlan; onChange: (next: ThreadPlan) => void; }
export function PlanEditForm(props: PlanEditFormProps): JSX.Element;   // 内含 goal Input / 每步 Input+Select+删 / 添加步骤;testid 与 PlanPanel 现有一致(plan-edit-form / plan-goal-input / plan-step-input-N / plan-step-status-N / plan-step-remove-N / plan-add-step)
export function planDraftValid(d: ThreadPlan | null): boolean;   // 从 PlanPanel 的 draftValid 抽出

// usePlanCard.ts(R6)
export interface UsePlanCardArgs {
  threadId: string | null;
  /** 只有 live 轮的事件(不含历史懒重建);调用方传 `liveTurns.flatMap(t => t.events)` 的 memo。 */
  liveEvents: readonly SseEvent[];
  fetchPlan?: typeof getThreadPlan; savePlan?: typeof updateThreadPlan;   // DI
}
export interface UsePlanCard {
  plan: ThreadPlan | null;
  loaded: boolean;
  save: (next: ThreadPlan) => Promise<void>;   // PUT;成功 setPlan(回显);失败抛 ApiError 给调用方 toast
  saving: boolean;
}
export function usePlanCard(args: UsePlanCardArgs): UsePlanCard;

// PlanCard.tsx
export interface PlanCardProps {
  plan: ThreadPlan | null; loaded: boolean;
  /** run 进行中 → 编辑置灰 + tooltip plan_panel.locked_while_running。 */
  running: boolean;
  readOnly?: boolean;            // 对话记录页 / 切入态:不出编辑按钮
  onSave?: (next: ThreadPlan) => Promise<void>;
}
export function PlanCard(props: PlanCardProps): JSX.Element | null;   // plan === null → null(spec:没有计划时不渲染)
```

`usePlanCard` 逻辑:
- `threadId` 变化 → `setPlan(null); setLoaded(false); appliedRef.current = null;` 然后 `fetchPlan(threadId)`(null 线程不拉);结果落 `plan`(**若期间 `threadId` 又变了丢弃**),`loaded = true`。
- `useEffect([liveEvents])`:`const s = reducePlan(liveEvents); if (s && s.sourceKey !== appliedRef.current) { appliedRef.current = s.sourceKey; setPlan(s.plan); }`。
- `save`:`setSaving(true)` → `savePlan(threadId, next)` → `setPlan(stored)`;finally `setSaving(false)`;错误上抛。

`PlanCard` UI:标题行 = 折叠箭头(`data-testid="console-plan-toggle"`,持久化)+ `console.plan_title` + `console.plan_progress`(done/doing/todo 三计数)+ 编辑按钮(`plan-edit`,`disabled={running}`,running 时 Tooltip);展开体:读视图(与 PlanPanel 读视图同款 ○/◐/✓ + 文字,搬过来即可)或编辑态(`PlanEditForm` + `plan-cancel-edit` / `plan-save`,`disabled={!planDraftValid(draft)}`);保存失败 `message.error`(`App.useApp()`——**测试要包 `<App>`**)。容器 `data-testid="console-plan-card"`。

- [ ] **Step 1: 测试(红)**

`usePlanCard.test.ts`(`renderHook`,`fetchPlan` / `savePlan` 传 `vi.fn()`):

```ts
it("fetches the baseline once per thread and resets on thread change", async () => { /* threadId t1 → fetch 1 次 → rerender t2 → plan 先 null 再是 t2 的 */ });
it("applies the newest live plan snapshot once, and does not re-apply it after a PUT", async () => {
  const fetchPlan = vi.fn().mockResolvedValue(PLAN_A);
  const savePlan = vi.fn().mockResolvedValue(PLAN_EDITED);
  const events = [planFrame(PLAN_B, "id-1")];
  const { result, rerender } = renderHook(({ ev }) => usePlanCard({ threadId: "t", liveEvents: ev, fetchPlan, savePlan }), { initialProps: { ev: events } });
  await waitFor(() => expect(result.current.plan).toEqual(PLAN_B));   // 流快照盖过基线
  await act(() => result.current.save(PLAN_EDITED));
  expect(result.current.plan).toEqual(PLAN_EDITED);
  rerender({ ev: [...events, updatesFrame({ agent: { messages: [] } }, "id-2")] });   // 新帧但不是计划 → 不回退
  expect(result.current.plan).toEqual(PLAN_EDITED);
  rerender({ ev: [...events, planFrame(PLAN_C, "id-3")] });                          // 新计划帧 → 覆盖
  await waitFor(() => expect(result.current.plan).toEqual(PLAN_C));
});
it("ignores a plan snapshot that is not newer (same sourceKey) on unrelated re-renders", ...);
it("null threadId → no fetch, plan null, loaded false", ...);
```

`PlanCard.test.tsx`(包 `<App>`):渲染三计数文案;`plan === null` 不渲染;折叠态写 localStorage 并在重挂载时恢复;`running` 时编辑按钮 disabled;编辑 → 改一步 → 保存 → `onSave` 收到完整 plan;`readOnly` 无编辑按钮。`PlanEditForm.test.tsx`:改 goal / 改状态 / 增删步 → `onChange` 收到新对象(不可变);`planDraftValid` 三例。

- [ ] **Step 2: 跑红**;`pnpm exec vitest run src/pages/__tests__/PlanPanel.test.tsx` 此时仍绿(还没动)。
- [ ] **Step 3: 实现**:先抽 `PlanEditForm` 并让 PlanPanel 用它 → 跑 `PlanPanel.test.tsx` 绿 → 再写 hook 与 PlanCard。
- [ ] **Step 4: 跑绿(新三件 + PlanPanel 6 条);typecheck;eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/PlanEditForm.tsx apps/admin-ui/src/components/console/usePlanCard.ts apps/admin-ui/src/components/console/PlanCard.tsx apps/admin-ui/src/pages/run_detail/PlanPanel.tsx apps/admin-ui/src/components/console/__tests__/PlanEditForm.test.tsx apps/admin-ui/src/components/console/__tests__/usePlanCard.test.ts apps/admin-ui/src/components/console/__tests__/PlanCard.test.tsx
git commit -m "feat(console): 任务卡 PlanCard(GET 基线 + 流内 plan 快照 + PUT 编辑,PlanEditForm 从 PlanPanel 抽出共用)"
```

---

### Task 9: `WorkspacePanel`(自带 `useUserWorkspace`)

**Files:**
- Create: `apps/admin-ui/src/components/console/useUserWorkspace.ts`、`WorkspacePanel.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/WorkspacePanel.test.tsx`
- 参考:`PlaygroundTab.tsx:167-178`(state)、`:606-700`(handlers)、`:1015-1246`(JSX)、`:1470-1476`(`isHiddenWorkspacePath`)、`:105-116`(`formatBytes`)

**Interfaces:**
- Consumes:`getUserWorkspace / getUserWorkspaceFiles / downloadUserWorkspaceFile / deleteUserWorkspaceFile`(`api/workspace.ts`)、`downloadArtifact / deleteArtifact`(`api/artifacts.ts`)、`concreteTenantScope / useTenantScope`、`ReadonlyTooltip`、i18n `playground.workspace_*` / `artifact_*` / `file_*` / `delete_*`。
- Produces:

```ts
export interface UseUserWorkspace {
  workspace: SessionWorkspace | null; files: WorkspaceFile[]; loading: boolean;
  reload: () => Promise<void>;
  downloadFile: (path: string) => Promise<void>; deleteFile: (path: string) => Promise<void>;
  downloadArtifact: (name: string) => Promise<void>; deleteArtifact: (name: string) => Promise<void>;
  busyKey: string | null;   // 正在下载/删除的 path 或 `artifact:<name>`
}
/** 用户维度工作区;`refreshWhenIdle` 从 true 变 false(run 结束)时自动 reload;挂载时 reload 一次。 */
export function useUserWorkspace(args: { running: boolean }): UseUserWorkspace;

export interface WorkspacePanelProps { running: boolean; readOnly: boolean; }
export function WorkspacePanel(props: WorkspacePanelProps): JSX.Element;   // 内部调 useUserWorkspace
```

testid 全部沿用 `playground-workspace*`(见 PlaygroundTab.tsx:1017-1240)。`formatBytes` / `isHiddenWorkspacePath` 搬到 `components/console/workspace_format.ts` 导出(各 2 条单测)。**注意**:原代码 `useEffect(() => { if (!running) void loadWorkspace(); }, [running, loadWorkspace])` 语义 = 挂载 + 每次 running→false 都刷;保持一致。

- [ ] **Step 1: 测试(红)** —— 从 `PlaygroundTab.test.tsx` 迁 4 条(432 / 965 / 999 / 1009 行那四条,mock 用 `vi.spyOn(workspaceSdk, …)` / `vi.spyOn(artifactsSdk, …)`,渲染 `<TenantScopeProvider><WorkspacePanel running={false} readOnly={false} /></TenantScopeProvider>`),外加:`running` 从 true→false 触发第二次 `getUserWorkspace`;`readOnly` 时删除按钮 disabled 且下载可用。
- [ ] **Step 2: 跑红**;**Step 3: 实现**(JSX 原样搬);**Step 4: 跑绿 + typecheck**;
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/useUserWorkspace.ts apps/admin-ui/src/components/console/WorkspacePanel.tsx apps/admin-ui/src/components/console/workspace_format.ts apps/admin-ui/src/components/console/__tests__/WorkspacePanel.test.tsx apps/admin-ui/src/components/console/__tests__/workspace_format.test.ts
git commit -m "feat(console): WorkspacePanel + useUserWorkspace(从 PlaygroundTab 抽出,功能不变)"
```

---

### Task 10: 对话流叶子 —— `UserBubble` / `CompactRow` / `AnswerBubble` / `TurnFooter`

**Files:**
- Create: `apps/admin-ui/src/components/console/UserBubble.tsx`、`CompactRow.tsx`、`AnswerBubble.tsx`、`TurnFooter.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/CompactRow.test.tsx`、`AnswerBubble.test.tsx`、`TurnFooter.test.tsx`(UserBubble 太薄,并进 TurnBlock 测)
- 参考:`TurnCard.tsx:735-905`(用户消息 / 答案 / 产物 / 审批 / 元数据 / 反馈)、`:1040-1085`(导出 / 重试 / Langfuse 按钮)、`components/ToolTimeline.tsx:71`(`ToolCallCard`)

**Interfaces:**
- Consumes:`CompactRow`(Task 4)、`ConsoleTurn`(Task 5)、`TurnSummary / AnswerSegment`(`api/turn_summary.ts`)、`TurnArtifact / artifactsFromTools`(`api/tool_timeline.ts`)、`CommentarySegmentLine`(`components/turn/TurnCard.tsx`)、`FullTextTrigger / FullTextModal / FullTextState`(`components/turn/FullTextModal.tsx`)、`MarkdownView`、`TurnMeta`(`pages/agent_detail/playground/TurnMeta.tsx`)、`FeedbackBar`、`ToolCallCard`、`ReadonlyTooltip`、`FireNowResult`。
- Produces:

```ts
export function UserBubble({ input, attachments, inputs }: { input: string; attachments: readonly Attachment[]; inputs?: Record<string, string> }): JSX.Element;
// 右对齐气泡(样式沿用 TurnCard.tsx:673-685);附件 Tag;inputs 非空 → 气泡下一行小字 `key=value · key=value`(mono 11px,data-testid="console-turn-inputs")

export interface CompactRowProps {
  row: CompactRow;
  expanded: boolean; onToggle: () => void;
  /** 流式:think 行的实时文本(仅 live 轮当前步)。 */
  liveText?: string;
  onInspect?: () => void;                    // 行尾「检查」→ 右栏选中本轮并定位到同 id 的轨迹行(TurnBlock 包成 () => onInspectRow(turn.key, row.id))
  onFireResult?: (r: FireNowResult) => void; // 透传给 ToolCallCard
}
export function CompactRow(props: CompactRowProps): JSX.Element;
export function rowIsExpandable(row: CompactRow): boolean;   // think / tool / plan / memory / reflect / subagent → true;其它 false

export interface AnswerBubbleProps {
  turn: ConsoleTurn;                 // 用 turn.turn.status / error / events、loadState、fallbackLines
  summary: TurnSummary;              // 父级 memo
  /** 流式:当前未落地步的 content(打字机);settled 或历史轮 undefined。 */
  liveText?: string;
  onDownloadArtifact: (name: string) => Promise<void>;
}
export function AnswerBubble(props: AnswerBubbleProps): JSX.Element;

export interface TurnFooterProps {
  turn: ConsoleTurn; threadId: string | null; summary: TurnSummary; costCny: number | null;
  readOnly: boolean; isTenantSwitched: boolean;
  onRetry?: (turn: Turn) => void; onExport: (turn: Turn) => void; exporting: boolean;
  onInspect: () => void; selected: boolean;
}
export function TurnFooter(props: TurnFooterProps): JSX.Element;
```

行为:
- `CompactRow` 一行文案(i18n 在组件里拼):think `思考 · <首行>`(live 时标签 `console.row_think_live`、显示最新一行);tool `<toolName> · <args JSON 截 80 字> → <resultPreview 首行 | console.row_tool_error: <首行> | console.row_tool_pending>`;plan `console.row_plan_update{n} · <reason 首句>` / `console.row_plan_create{n}`;memory / reflect / subagent 用对应键;marker 类直接 `text`。行尾:`durationMs`(`fmtDuration`)、状态色点(`data-status`)、「检查」小按钮(`console-row-inspect`,`onInspect` 有才渲染)。可展开行整行是 `button`(`aria-expanded`),展开体 `console-row-detail`:think → `<pre>` 全文;tool → `<ToolCallCard entry={row.entry} onFireResult={onFireResult} />`;plan → `plan` 非 null 时步骤 `<ol>`(○/◐/✓),否则 goal + reason;memory / reflect → `<pre>{JSON.stringify(detail, null, 2)}</pre>`;subagent → `worker.taskExcerpt` + steps 数。testid:`console-row-<kind>`。
- `AnswerBubble`:①`turn.turn.status === "error"` → `Alert`(`playground-turn-error`,同 TurnCard);②有 segments → 按 TurnCard.tsx:792-830 逻辑渲染(running 时非末段 commentary、末段 plain;settled 时按 channel;`maxHeight 420` 滚动容器 `playground-turn-answer-scroll` + `FullTextTrigger`);running 且 `liveText` 非空 → 在 segments 后追加一段 plain 打字机文本(`data-testid="console-answer-live"`);③无 segments:running → `playground.turn_running`;历史未加载(`loadState !== "done"` 且 events 空)→ `fallbackLines`(弱化样式,TurnCard.tsx:660-728 那段)+ `history_loading` spinner(`loadState !== "error"` 时);否则 `playground.turn_no_text`;④产物行 `playground-turn-artifacts` / `playground-turn-artifact-download`(同 TurnCard.tsx:842-865)。容器 `data-testid="playground-turn-answer"`。
- `TurnFooter`:状态 Tag(`console-turn-status`,running / done / error 三键)+ `<TurnMeta …/>` + `!readOnly && status==="done" && threadId` → `<ReadonlyTooltip on={isTenantSwitched} block><FeedbackBar threadId turnSeq={turn.seq} disabled={isTenantSwitched} /></ReadonlyTooltip>` + 按钮:重试(`playground-turn-retry`,`onRetry && status !== "running"`,`danger={failed}`,`failed = status==="error" || events.some(e=>e.event==="error")`)、导出(`playground-export-json`,`loading={exporting}`)、检查(`console-turn-inspect`,`type={selected ? "primary" : "default"}`)。

- [ ] **Step 1: 测试(红)**

```ts
// CompactRow.test.tsx
it("tool row: name · args → result first line; click expands the ToolCallCard", async () => {
  const row = { id: "tool:0:0", kind: "tool", seq: 0, step: 1, status: "ok", durationMs: 420, eventIndexes: [0, 1], serverMs: null, entry: { id: "c1", rawName: "query_crm", isMcp: false, server: null, toolName: "query_crm", args: { id: "C-1" }, status: "success", resultPreview: "3 条记录\n第二行", durationMs: 420 } } as const;
  const onToggle = vi.fn();
  const { rerender } = render(<App><CompactRow row={row} expanded={false} onToggle={onToggle} /></App>);
  expect(screen.getByTestId("console-row-tool")).toHaveTextContent(/query_crm.*"id":"C-1".*3 条记录/);
  expect(screen.getByTestId("console-row-tool")).not.toHaveTextContent("第二行");
  await userEvent.click(screen.getByRole("button", { expanded: false }));
  expect(onToggle).toHaveBeenCalled();
  rerender(<App><CompactRow row={row} expanded onToggle={onToggle} /></App>);
  expect(within(screen.getByTestId("console-row-detail")).getByTestId("tool-call-card")).toBeInTheDocument(); // ToolCallCard 的 testid 按其文件实际值改
});
it("think row shows the first line settled and the latest line while live", () => { /* liveText="a\nb\nc" → 文本含 c 且标签为 思考中 */ });
it("plan row: update_plan with reason vs planner; expanded lists steps with status glyphs", () => { /* 两个 row 断言文案:计划 · 更新为 3 步 · 档案查完了 / 制定计划 · 3 步 */ });
it("marker rows (error/compaction) are not expandable and carry the status colour", () => { /* rowIsExpandable false;data-status="error" */ });
it("inspect button renders only when onInspect is given and calls it", ...);
// AnswerBubble.test.tsx
it("renders commentary lines de-emphasised and the final segment as markdown", ...);   // 抄 TurnCard.test 370 行那条的 fixture
it("while running: settled segments as commentary, liveText appended as plain typewriter text", ...);
it("history turn not yet loaded: fallback lines + loading spinner; error load: fallback without spinner", ...);
it("failed turn keeps the answer body under the error banner", ...);                 // 抄 TurnCard.test 428 行那条
it("artifact download row calls onDownloadArtifact(name)", ...);
// TurnFooter.test.tsx
it("shows retry (danger when failed) only with onRetry and a settled turn; export + inspect always", ...);
it("feedback bar only for a settled live turn that is not read-only; disabled when tenant-switched", ...);
```

- [ ] **Step 2: 跑红**;**Step 3: 实现**(JSX 从 TurnCard 对应段搬,不改文案键);**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/UserBubble.tsx apps/admin-ui/src/components/console/CompactRow.tsx apps/admin-ui/src/components/console/AnswerBubble.tsx apps/admin-ui/src/components/console/TurnFooter.tsx apps/admin-ui/src/components/console/__tests__/CompactRow.test.tsx apps/admin-ui/src/components/console/__tests__/AnswerBubble.test.tsx apps/admin-ui/src/components/console/__tests__/TurnFooter.test.tsx
git commit -m "feat(console): 对话流叶子组件 —— 用户气泡 / 紧凑行 / 答案气泡 / 轮脚注"
```

---

### Task 11: `TurnBlock` + `Transcript`

**Files:**
- Create: `apps/admin-ui/src/components/console/TurnBlock.tsx`、`Transcript.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/TurnBlock.test.tsx`、`Transcript.test.tsx`
- 参考:`PlaygroundTab.tsx:1275-1466`(transcript 容器 / 历史 / 降级块 / 任务结果卡)、`TurnCard.tsx:459-478`(`settledSteps` / `unsettledLiveByStep`)、`:867-880`(审批门)

**Interfaces:**
- Consumes:Task 4/5/10 全部;`compactRowsOf`、`summarizeTurn`、`ApprovalGate / approvalItemFromEvent`(`components/turn/TurnCard.tsx`)、`LiveStep`(`useTokenStream.ts`)、`TaskResultCard`、`HistoryDivider`、`FullTextModal`、`HistoryMessage`(`api/sessions.ts`)、`RateCardRecord`。
- Produces:

```ts
export interface TurnBlockProps {
  turn: ConsoleTurn; threadId: string | null;
  selected: boolean; onSelect: (key: string) => void;
  /** 紧凑行「检查」→ 父级切到该轮并让右栏选中该行(Task 19 接 TrajectoryPanel.focusRowId)。 */
  onInspectRow: (turnKey: string, rowId: string) => void;
  /** 仅当前流式 live 轮传;其它 undefined。 */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  rate: RateCardRecord | null; isSystemAdmin: boolean;
  readOnly: boolean; isTenantSwitched: boolean;
  onDecide: (turnId: string, approval: ApprovalItem, decision: "approve" | "reject") => void; deciding: boolean;
  onExport: (turn: Turn) => void; exporting: boolean;
  onRetry?: (turn: Turn) => void;
  onDownloadArtifact: (name: string) => Promise<void>;
  onFireResult?: (r: FireNowResult) => void;
  /** 历史轮懒加载 ref(`useHistoryTurns.registerRow(runId, threadId)` 的返回);live 轮不传。 */
  rowRef?: (el: HTMLElement | null) => void;
}
export function TurnBlock(props: TurnBlockProps): JSX.Element;

export interface TranscriptProps {
  turns: readonly ConsoleTurn[];
  /** 历史降级块(计数对不上时的扁平文本);非空且 turns 里没有历史轮时渲染,沿用 PlaygroundTab.tsx:1379-1428 的样式与 `playground-history` testid。 */
  flatHistory: readonly HistoryMessage[];
  taskResults: readonly FireNowResult[];
  threadId: string | null;
  selectedKey: string | null;                       // null = 跟随最新
  onSelectTurn: (key: string) => void;
  onInspectRow: (turnKey: string, rowId: string) => void;   // 透传 TurnBlock
  streamTurnKey: string | null; liveByStep: ReadonlyMap<number, LiveStep>;
  registerHistoryRow: (runId: string, threadId: string) => (el: HTMLElement | null) => void;
  // 透传给 TurnBlock 的一组回调 / 标志(同上)
  rate; isSystemAdmin; readOnly; isTenantSwitched; onDecide; deciding; onExport; exportingKey: string | null; onRetryLive?; onRetryHistory?; onDownloadArtifact; onFireResult;
}
export function Transcript(props: TranscriptProps): JSX.Element;
```

行为:
- `TurnBlock`:容器 `data-testid="console-turn"`(+ `data-selected`),点击容器空白区 `onSelect(key)`。内部:`UserBubble` → 紧凑行列表(`rows = useMemo(compactRowsOf(events))`;live 轮再追加合成行:`liveSyntheticRows(events, liveByStep)`(Task 5,不在这里重写);展开态 `Set<string>` 本地 state;每条 `CompactRow` 的 `onInspect = () => onInspectRow(turn.key, row.id)`)→ `ApprovalGate`(`!readOnly && turn.turn.approval && threadId`,包 `ReadonlyTooltip`,同 TurnCard.tsx:867-880)→ `AnswerBubble`(`liveText` = 最高未落地步的 `content`)→ `TurnFooter`(`costCny` 同 TurnCard.tsx:640-651 公式,从 `summary.usage` + `rate` 算)。`rowRef` 挂在容器上。`FullTextModal` 一处(think 全文 / 答案全文共用)。
- `Transcript`:`flex column; overflow:auto`(`playground-transcript` testid 保留);空态 `Empty`(`playground-empty-log` 保留,文案 `console.no_turns`);历史轮之后一条 `HistoryDivider`(有历史轮或降级块时);`turns.map(TurnBlock)`;末尾 `taskResults.map(TaskResultCard)`;自动滚底:`turns` 变化或 live 帧数变化时 `scrollTop = scrollHeight`(沿用现状 effect;用户手动上滚时不强制——判定 `scrollHeight - scrollTop - clientHeight > 80` 则不动)。`selected` = `selectedKey ?? turns.at(-1)?.key`。

- [ ] **Step 1: 测试(红)**

`TurnBlock.test.tsx`(fixture 用 `timeline.test.ts` 的 `upd()` 造真实帧):①settled 轮:用户气泡 + `console-row-think` + `console-row-tool` + 答案 + 脚注;②live 轮 `liveByStep = Map{2 → {content:"partial", reasoning:"thinking…", toolNames: Map{0:"query_crm"}, reasoningMs:null}}` 而 events 只落地到 step 1 → 出现合成 `console-row-think`(含 thinking…)与合成 `console-row-tool`(query_crm,running)、`console-answer-live` 含 partial;③settled 步的 live buffer 不再合成(step 1 落地后 `liveByStep` 里 step 1 的残留不出行——TurnCard.test 696 行那条的语义);④审批门:`approval` 非空且非 readOnly → `playground-approval`;点批准 → `onDecide(turn.id, approval, "approve")`;⑤`inputs` 小字;⑥点容器 → `onSelect(key)`;⑦点某行「检查」→ `onInspectRow(turn.key, row.id)`,点脚注「检查」→ `onSelect(key)`。

`Transcript.test.tsx`:①空 → `playground-empty-log`;②历史 2 + live 1 → 3 个 `console-turn`,中间 `HistoryDivider`,最后一个 `data-selected="true"`(selectedKey null);③`selectedKey` 指向第一个 → 第一个 selected;④`flatHistory` 非空且无历史轮 → `playground-history` 块渲染 CommentarySegmentLine / MarkdownView;⑤`taskResults` 渲染 `TaskResultCard`;⑥历史轮容器拿到 `registerHistoryRow(runId, threadId)` 的 ref(用 `vi.fn` 返回记录 el 的函数断言被调)。

- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/TurnBlock.tsx apps/admin-ui/src/components/console/Transcript.tsx apps/admin-ui/src/components/console/__tests__/TurnBlock.test.tsx apps/admin-ui/src/components/console/__tests__/Transcript.test.tsx
git commit -m "feat(console): TurnBlock(气泡 + 紧凑行 + 合成 live 行 + 审批门 + 脚注)与 Transcript(历史 / 降级块 / 任务结果 / 选中)"
```

---

### Task 12: `StatsBar`

**Files:**
- Create: `apps/admin-ui/src/components/console/StatsBar.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/StatsBar.test.tsx`

**Interfaces:**
- Consumes:`SessionStats`(Task 5)、`fmtDuration`(`pages/agent_detail/playground/duration_format.ts`)、`formatCompact`(`utils/runFormat.ts`,`641K` 这类)、i18n `console.stats_*`。
- Produces:`export function StatsBar({ stats, isSystemAdmin }: { stats: SessionStats; isSystemAdmin: boolean }): JSX.Element | null;` —— `stats.turns === 0` → null。

行为:一行,`display:flex; gap; white-space:nowrap; overflow:hidden; text-overflow:ellipsis`,`title` = 全文(tooltip);项之间 `|`;项按 spec 顺序:轮·步 / LLM·工具 / 首 token / tok/s / 缓存 / 入·出 / ≈¥(仅 `isSystemAdmin && costCny !== null`);null 项跳过;`partial` → 末尾追加 `console.stats_partial`。testid `console-stats-bar`,每项 `console-stat-<name>`(turns / durations / ttft / tps / cache / tokens / cost)。

- [ ] **Step 1: 测试(红)**:①全量 stats → 每项文案(如 `2 轮 · 17 步`、`LLM 1m25s · 工具 4.6s`、`首 token 0.8s`、`≈ 144 tok/s`、`缓存 94%`、`入 641K · 出 3K`、`≈ ¥0.12`);②`ttftAvgMs / tokPerSec / cacheHitPct / costCny` 为 null 时对应项不渲染;③非 admin 不渲染 cost;④`turns === 0` → 不渲染;⑤`partial` 出「(仅已加载轮)」。
- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/StatsBar.tsx apps/admin-ui/src/components/console/__tests__/StatsBar.test.tsx
git commit -m "feat(console): StatsBar 会话级状态栏"
```

---

### Task 13: 壳与右栏容器 —— `ConsoleShell` + `console.css` + `InspectPanel`

**Files:**
- Create: `apps/admin-ui/src/components/console/ConsoleShell.tsx`、`console.css`、`InspectPanel.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/ConsoleShell.test.tsx`、`InspectPanel.test.tsx`
- 参考:`components/turn/GanttTimeline.css` 的 import 方式;现状右栏高度 `PlaygroundTab.tsx` 里 `calc(100vh - 360px)` 那处

**Interfaces:**
- Consumes:antd `Drawer / Segmented`、i18n `console.sidebar_open` / `console.inspect_trajectory` / `console.inspect_workspace`。
- Produces:

```ts
// ConsoleShell.tsx
export const CONSOLE_HEIGHT_OFFSET_PX = 360;
export interface ConsoleShellProps { sidebar: ReactNode; main: ReactNode; inspect: ReactNode; sidebarLabel: string; }
export function ConsoleShell(props: ConsoleShellProps): JSX.Element;
// 三栏 grid(class ew-console / ew-console__sidebar / ew-console__main / ew-console__inspect;列宽 264px / 1fr / minmax(400px, 38%);根 height calc(100vh - 360px));
// <1200px 时 sidebar 列变 48px 图标条(class ew-console__rail,按钮 data-testid="console-sidebar-rail-open",aria-label=console.sidebar_open),
// 点开 antd Drawer(placement left, width 320, destroyOnHidden)再渲染一次 `sidebar` 节点;根 data-testid="playground-tab" 保留

// InspectPanel.tsx
export type InspectTab = "trajectory" | "workspace";
export interface InspectPanelProps { tab: InspectTab; onTabChange: (t: InspectTab) => void; trajectory: ReactNode; workspace: ReactNode; }
export function InspectPanel(props: InspectPanelProps): JSX.Element;
// 顶部 Segmented(console-inspect-tab-trajectory / -workspace),下方 flex:1 min-height:0 overflow:hidden(轨迹面板自己滚)只渲染当前 tab 的节点
```

`console.css` 要点:`.ew-console { display: grid; grid-template-columns: 264px 1fr minmax(400px, 38%); height: calc(100vh - 360px); min-height: 480px; gap: 0; }`;三个列 `min-height: 0; min-width: 0; display: flex; flex-direction: column;`;`.ew-console__sidebar { border-right: 1px solid var(--ew-border-subtle) }`、`.ew-console__inspect { border-left: 1px solid var(--ew-border-subtle) }`;`.ew-console__rail { display: none }`;`@media (max-width: 1199px) { .ew-console { grid-template-columns: 48px 1fr minmax(360px, 40%) } .ew-console__sidebar > .ew-console__sidebar-body { display: none } .ew-console__rail { display: flex } }`。颜色只用 `--ew-*` 令牌(双主题)。

- [ ] **Step 1: 测试(红)**

`ConsoleShell.test.tsx`:①三个槽位都渲染(各传一个带 testid 的节点);②`console-sidebar-rail-open` 存在(CSS 隐藏与否 jsdom 不管)且点它打开 Drawer 里再次出现 sidebar 节点内容(传一个 `data-testid="probe"` 的节点,期望出现 2 个);③根节点 `playground-tab` 存在。`InspectPanel.test.tsx`:①默认渲染 trajectory 节点、不渲染 workspace 节点;②点 tab 触发 `onTabChange("workspace")`;③`tab="workspace"` 时反过来。

- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/ConsoleShell.tsx apps/admin-ui/src/components/console/console.css apps/admin-ui/src/components/console/InspectPanel.tsx apps/admin-ui/src/components/console/__tests__/ConsoleShell.test.tsx apps/admin-ui/src/components/console/__tests__/InspectPanel.test.tsx
git commit -m "feat(console): 三栏壳 ConsoleShell(CSS 断点折叠 + 抽屉)+ 右栏 tab 容器 InspectPanel"
```

---

### Task 14: 纯函数 `api/trace_match.ts`(轨迹行 ↔ Langfuse span 配对)+ hook `useRunTrace`(trace 拉取 / 轮询 / 重新武装 / Langfuse trace_id)

**Files:**
- Create: `apps/admin-ui/src/api/trace_match.ts`;Test: `apps/admin-ui/src/api/__tests__/trace_match.test.ts`
- Create: `apps/admin-ui/src/components/console/useRunTrace.ts`;Test: `apps/admin-ui/src/components/console/__tests__/useRunTrace.test.ts`
- 参考:`components/turn/TurnCard.tsx:121-126`(轮询常量)、`:555-640`(拉取 / 轮询 / 重新武装 / trace_id 逻辑,**逐段搬**,PR-B 删 TurnCard 时不丢);`pages/agent_detail/playground/trace_purpose.ts:21-42`(主对话 span 判据);hook 测试的 provider mock 写法照 `components/turn/__tests__/useHistoryTurns.test.ts:15-22`

**Interfaces:**
- Consumes:`TrajectoryRow`(Task 4)、`RunTrace / TraceSpan / getRunTrace`(`api/trace_facade.ts`)、`getRun`(`api/runs.ts`)、`concreteTenantScope / useTenantScope`(`tenant/TenantScopeContext.tsx`)。
- Produces:

```ts
// api/trace_match.ts
export type SpanMatchReason = "matched" | "count_mismatch" | "no_trace" | "unsupported";
export interface SpanMatch { span: TraceSpan | null; reason: SpanMatchReason }
/** 每条轨迹行配一个 Langfuse span(Timing 双列的右列)。规则见 R15;返回 Map<row.id, SpanMatch>,rows 里每个 id 都有键。 */
export function matchTraceSpans(rows: readonly TrajectoryRow[], trace: RunTrace | null): ReadonlyMap<string, SpanMatch>;

// components/console/useRunTrace.ts
export const TRACE_NOT_READY_MAX_RETRIES = 6;
export const TRACE_NOT_READY_RETRY_MS = 1500;
export interface RunTraceState {
  trace: RunTrace | null;          // null = 未拉 / 拉取中
  loading: boolean;                // enabled && trace === null
  refresh: () => void;             // 手动刷新:重置重试计数并清 trace(触发重拉)
  traceId: string | null;          // wantTraceId 时经 getRun 拿到的 Langfuse trace_id;否则 null
}
export function useRunTrace(args: {
  threadId: string | null; runId: string | null;
  enabled: boolean;                                   // 面板正显示这一轮
  turnStatus: "running" | "done" | "error";
  wantTraceId: boolean;                               // isSystemAdmin
}): RunTraceState;
```

`matchTraceSpans` 规则(R15):
1. `trace === null || trace.status !== "ok" || !trace.spans` → 全部 `{ span: null, reason: "no_trace" }`。
2. `think` 行 ↔ `kind === "llm" && (purpose === "" || purpose === "main")` 的 span,按出现顺序一一对应;**仅当两者数量相等**(照 `labelPurpose` 的判据),否则所有 think 行 `count_mismatch`。
3. `tool` 行与 `source === "update_plan"` 的 `plan` 行 ↔ `kind === "tool" && label === <toolName | "update_plan">` 的 span,同名按顺序第 n 条对第 n 条;某名字的 span 少于行数 → 多出的行 `count_mismatch`。
4. aux 行 ↔ `kind === "llm"` 且 `purpose` 对上的 span,同 purpose 按顺序:`memory` recall→`"rerank"`、`memory` writeback→`"memory"`、`plan` planner→`"planner"`、`reflect`→`"reflect"`、`compaction`→`"compress"`;不够 → `count_mismatch`。
5. `user / assistant / subagent / retry / error / approval / guard / gap` → `unsupported`。

`useRunTrace` 行为(与 TurnCard 一致):①`enabled && threadId && runId && trace === null` → `getRunTrace(threadId, runId, concreteTenantScope(apiTenantScope))`,成功 `setTrace(data)`,失败 `setTrace({ status: "unavailable" })`,cleanup 置 cancelled;②`runId` / `enabled` 变化 → 重试计数归零;`runId` 变化 → `trace` / `traceId` 置 null;③`trace.status === "not_ready"` 且计数 < 6 → 1.5 s 后计数 +1 并 `setTrace(null)`;④`turnStatus` 变成 `"done"` → `setTrace(null)`(重新武装一次);⑤`wantTraceId && threadId && runId` → `getRun` 取 `trace_id`,失败静默;⑥`refresh` = 计数归零 + `setTrace(null)`。

- [ ] **Step 1: 测试(红)**

```ts
// trace_match.test.ts
import { describe, expect, it } from "vitest";
import type { RunTrace, TraceSpan } from "../trace_facade";
import { matchTraceSpans } from "../trace_match";
import type { TrajectoryRow } from "../trajectory_rows";

const base = { seq: 0, step: null, status: "ok", durationMs: null, eventIndexes: [], serverMs: null } as const;
function span(p: Partial<TraceSpan> & Pick<TraceSpan, "id" | "kind" | "label">): TraceSpan {
  return { parentId: null, detail: null, startMs: 0, latencyMs: 10, model: null, inputTokens: null, outputTokens: null, costUsd: null,
    input: null, output: null, level: "default", statusMessage: null, purpose: "", group: null, ...p };
}
const okTrace = (spans: TraceSpan[]): RunTrace => ({ status: "ok", trace: { name: "run", latencyMs: 100, totalCostUsd: 0.01, spanCount: spans.length }, spans });
const think = (id: string, seq: number): TrajectoryRow => ({ ...base, id, seq, kind: "think", text: "", content: null, model: null, inputTokens: 0, outputTokens: 0, finishReason: null });
const tool = (id: string, name: string): TrajectoryRow => ({ ...base, id, kind: "tool", entry: { id, rawName: name, isMcp: false, server: null, toolName: name, args: {}, status: "success", resultPreview: null, durationMs: 1 } });

describe("matchTraceSpans", () => {
  it("no trace / not ok → every row no_trace", () => {
    const rows = [think("think:0", 0)];
    expect(matchTraceSpans(rows, null).get("think:0")).toEqual({ span: null, reason: "no_trace" });
    expect(matchTraceSpans(rows, { status: "not_ready" }).get("think:0")).toEqual({ span: null, reason: "no_trace" });
  });
  it("think rows pair with main llm spans in order only when counts match", () => {
    const rows = [think("think:0", 0), think("think:2", 2)];
    const l1 = span({ id: "l1", kind: "llm", label: "llm" }), l2 = span({ id: "l2", kind: "llm", label: "llm", purpose: "main" });
    const aux = span({ id: "a", kind: "llm", label: "llm", purpose: "memory" });
    const m = matchTraceSpans(rows, okTrace([aux, l1, l2]));
    expect(m.get("think:0")?.span?.id).toBe("l1");
    expect(m.get("think:2")?.span?.id).toBe("l2");
    const bad = matchTraceSpans(rows, okTrace([l1]));
    expect(bad.get("think:0")).toEqual({ span: null, reason: "count_mismatch" });
  });
  it("tool rows pair by label, nth-of-name; extra rows are count_mismatch", () => {
    const rows = [tool("tool:0:0", "query_crm"), tool("tool:0:1", "query_crm"), tool("tool:1:0", "send_mail")];
    const m = matchTraceSpans(rows, okTrace([span({ id: "t1", kind: "tool", label: "query_crm" }), span({ id: "t2", kind: "tool", label: "query_crm" })]));
    expect(m.get("tool:0:0")?.span?.id).toBe("t1");
    expect(m.get("tool:0:1")?.span?.id).toBe("t2");
    expect(m.get("tool:1:0")).toEqual({ span: null, reason: "count_mismatch" });
  });
  it("aux rows pair by purpose; user/assistant/markers are unsupported", () => {
    const rows: TrajectoryRow[] = [
      { ...base, id: "user", seq: -1, kind: "user", text: "q", attachmentNames: [], inputs: {} },
      { ...base, id: "memory:1", seq: 1, kind: "memory", direction: "writeback", count: 1, detail: {} },
      { ...base, id: "plan:2", seq: 2, kind: "plan", source: "planner", callId: null, plannerSeq: null, stepsTotal: 1, goal: null, reason: null, plan: null },
      { ...base, id: "error:3", seq: 3, kind: "error", text: "x" },
    ];
    const m = matchTraceSpans(rows, okTrace([span({ id: "p", kind: "llm", label: "llm", purpose: "planner" }), span({ id: "mm", kind: "llm", label: "llm", purpose: "memory" })]));
    expect(m.get("user")).toEqual({ span: null, reason: "unsupported" });
    expect(m.get("memory:1")?.span?.id).toBe("mm");
    expect(m.get("plan:2")?.span?.id).toBe("p");
    expect(m.get("error:3")).toEqual({ span: null, reason: "unsupported" });
  });
});
```

```ts
// useRunTrace.test.ts(fake timers;mock TenantScopeContext 照 useHistoryTurns.test.ts)
it("fetches lazily once enabled and stops when disabled", …);        // enabled=false → getRunTrace 0 次;rerender enabled=true → 1 次,trace 落地
it("auto-polls not_ready up to 6 times at 1.5 s, then settles", …);   // getRunTrace 恒回 {status:"not_ready"} → 共 7 次调用后不再调
it("re-arms one fetch when the turn turns done", …);                  // running 时拉到 not_ready 用光预算 → turnStatus→done → 再调 1 次
it("maps a rejected fetch to unavailable; refresh() re-fetches", …);
it("fetches trace_id via getRun only when wantTraceId; resets on runId change", …);
```

- [ ] **Step 2: 跑红**;**Step 3: 实现**(hook 逐段搬 TurnCard.tsx:555-640,常量搬 `:121-126`);**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/trace_match.ts apps/admin-ui/src/api/__tests__/trace_match.test.ts apps/admin-ui/src/components/console/useRunTrace.ts apps/admin-ui/src/components/console/__tests__/useRunTrace.test.ts
git commit -m "feat(console): 轨迹行 ↔ Langfuse span 配对(trace_match)+ useRunTrace(拉取 / not_ready 轮询 / 结束重新武装 / trace_id)"
```

---

### Task 15: `LaneStrip` —— 三泳道时间条(输入 / 模型 / 工具)+ 纯模型 `lane_strip_model.ts`

**Files:**
- Create: `apps/admin-ui/src/components/console/lane_strip_model.ts`、`LaneStrip.tsx`、`lane_strip.css`
- Test: `apps/admin-ui/src/components/console/__tests__/lane_strip_model.test.ts`、`LaneStrip.test.tsx`
- 参考:`components/turn/TurnCard.tsx:151-162`(`lastKnownFrame`,**复制**到 `lane_strip_model.ts`)、`:387`(`buildGanttRows(turn.events, { settled: turn.status !== "running" })`)、`:413-421`(运行中轴生长算法);`components/turn/GanttTimeline.tsx:74-116`(刻度 / 百分比 / 最小宽度,**只抄算法**,组件不复用);`components/turn/GanttTimeline.css` 的令牌用法

**Interfaces:**
- Consumes:`buildGanttRows / GanttModel / GanttRow / GanttMarker`(`api/gantt_timeline.ts`)、`serverMsOf`(`api/sse_id.ts`)、`TrajectoryRow / resolveGanttKey`(Task 4)、i18n `console.lane_*` + `playground.gantt_degraded`。
- Produces:

```ts
// lane_strip_model.ts
export type Lane = "input" | "model" | "tools";
export interface LaneBlock { key: string; lane: Lane; rowId: string | null; label: string; startMs: number; durationMs: number | null; hasError: boolean }
export interface LaneMarker { key: string; atMs: number; kind: GanttMarker["kind"]; text: string }
export interface LaneModel { blocks: LaneBlock[]; markers: LaneMarker[]; totalMs: number; degraded: boolean }
export const LANE_OF: Record<GanttRow["kind"], Lane> = { aux: "input", agent: "model", final: "model", tool: "tools", worker: "tools" };
/** `buildGanttRows(events, { settled: !running })` → 泳道模型;`running` 时用 `nowMs` 把 totalMs 长到「现在」(TurnCard.tsx:413-421 同一算法)。 */
export function laneModelOf(events: readonly SseEvent[], rows: readonly TrajectoryRow[], opts: { running: boolean; nowMs: number }): LaneModel;

// LaneStrip.tsx
export interface LaneStripProps {
  events: readonly SseEvent[]; rows: readonly TrajectoryRow[]; running: boolean;
  selectedRowId: string | null; onSelectRow: (rowId: string) => void;
}
export function LaneStrip(props: LaneStripProps): JSX.Element | null;   // blocks 空 → null
```

行为:
- 模型:每个 `GanttRow` → 一个 `LaneBlock`(`lane = LANE_OF[kind]`,`rowId = resolveGanttKey(rows, key)`,`label = row.label`,`startMs / durationMs / hasError` 抄);`markers` 抄 `GanttMarker`(`key = \`${kind}-${atMs}-${i}\``);`totalMs`:running 时 `last = lastKnownFrame(events)`,`nowServerMs = last.serverMs + (nowMs - last.receivedAtMs)`,`grown = model.totalMs + max(0, nowServerMs - last.serverMs)`;`lastKnownFrame(events)` 为 null 或非 running → 原 totalMs;`degraded` 抄。
- 组件:三行,行头 `console.lane_input / lane_model / lane_tools`(宽 40px);每行相对定位容器,块 `button.ew-lane__block`(`data-testid="console-lane-block"`,`data-lane`,`data-row-id`,`data-error`,`aria-pressed={rowId === selectedRowId}`,`title={label}`,`left/width` 用 `pct(startMs, totalMs)` / `max(MIN_BAR_WIDTH_PCT, pct(durationMs))`,`durationMs === null` 时 `running` → 宽到右边缘并加 `ew-lane__block--live` 类,否则最小宽 + `--interrupted`);`rowId === null` 的块渲染成不可点的 `span`;标记 `span.ew-lane__marker`(`data-testid="console-lane-marker"`,`data-kind`,`title=text`,竖线跨三行);running 时每 1 s `setNowTick` 重算(effect 只在 running 时挂,cleanup 清 interval);`degraded` → 底部一行灰字 `playground.gantt_degraded`;点块 → `onSelectRow(rowId)`。样式全部 `--ew-*` 令牌;三泳道颜色:输入 `--ew-trace-entry`、模型 `--ew-text-info`、工具 `--ew-accent-violet`(与 TraceView 的语义色一致),错误块 `--ew-text-danger`。

- [ ] **Step 1: 测试(红)**

```ts
// lane_strip_model.test.ts(fixture 照 gantt_timeline.test.ts:带 id 时戳的 ev)
it("maps gantt kinds to lanes and resolves each block to a trajectory row id", () => {
  // events: agent 步(reasoning + tool query_crm)+ tools 结果 + memory_recall
  // rows = trajectoryRowsOf(events, INPUT, null, "done")
  // 期望:blocks 里 lane 分别 model / tools / input;rowId 分别 think:<seq> / tool:<seq>:0 / memory:<seq>
});
it("running: totalMs grows to now using the last frame's server ms + receivedAt delta", () => {
  // 最后一帧 id 时戳 T、receivedAt R;nowMs = R + 5000 → totalMs 比 settled 版本多 5000
});
it("markers are carried over with kind/text; degraded flag passes through", ...);
// LaneStrip.test.tsx
it("renders three lanes with blocks positioned by percentage; clicking a block selects its row", async () => {
  // onSelectRow 被调且参数 === 该块 data-row-id;aria-pressed 随 selectedRowId 变
});
it("a block with no resolvable row is a non-interactive span", ...);
it("shows the degraded hint when the model is degraded", ...);
```

- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/lane_strip_model.ts apps/admin-ui/src/components/console/LaneStrip.tsx apps/admin-ui/src/components/console/lane_strip.css apps/admin-ui/src/components/console/__tests__/lane_strip_model.test.ts apps/admin-ui/src/components/console/__tests__/LaneStrip.test.tsx
git commit -m "feat(console): LaneStrip 三泳道时间条(buildGanttRows 数据 → 输入 / 模型 / 工具泳道,块点击选中轨迹行,运行中轴生长)"
```

---

### Task 16: `TrajectoryRows` —— 扁平轨迹行列表

**Files:**
- Create: `apps/admin-ui/src/components/console/TrajectoryRows.tsx`、`trajectory_rows.css`
- Test: `apps/admin-ui/src/components/console/__tests__/TrajectoryRows.test.tsx`

**Interfaces:**
- Consumes:`TrajectoryRow`(Task 4)、`fmtDuration`(`pages/agent_detail/playground/duration_format.ts`)、i18n `console.traj_kind_*` / `console.row_*` / `console.traj_llm_call`。
- Produces:

```ts
export interface TrajectoryRowsProps {
  rows: readonly TrajectoryRow[];             // 已含 live 合成行(父级拼)
  selectedRowId: string | null;
  onSelectRow: (rowId: string) => void;
  running: boolean;                           // 该轮进行中 → 行列表自动滚到底(用户没上滚时)
}
export function TrajectoryRows(props: TrajectoryRowsProps): JSX.Element;
export function rowSummary(row: TrajectoryRow, t: TFunction): string;   // 导出给 RowDetail 的 Summary tab 复用
```

行为:
- 容器 `ul`(`data-testid="console-traj-rows"`,`role="listbox"`,`overflow:auto`);每行 `li > button`(`data-testid="console-traj-row"`,`data-kind={row.kind}`,`data-row-id`,`data-status={row.status}`,`aria-selected={row.id === selectedRowId}`,选中行 `ew-traj-row--selected`);行内三段:kind 标签(等宽大写 `USER / THINK / PLAN / MEMORY / TOOL / SUBAGENT / REFLECT / COMPACTION / ASSISTANT / ERROR / RETRY / APPROVAL / GUARD / GAP`,文案 `console.traj_kind_<kind>`,think 行 `text === ""` 时标签仍是 THINK 但摘要用 `console.traj_llm_call{model}`)+ 一行摘要(`rowSummary`,`text-overflow: ellipsis`;规则同 CompactRow 的一行文案:think 首行 / tool `<name> · <args 截 80> → <result 首行>` / plan / memory / reflect / subagent / marker `text` / user `text` 首行 / assistant `text` 首行)+ 右侧 `fmtDuration(durationMs)`(null 不显示)。
- 状态:`data-status="running"` 行标签旁一个脉冲点(`ew-traj-row__pulse`,`prefers-reduced-motion` 下静止);`error` 行整行文字 `--ew-text-danger`;`pause` 行 `--ew-accent-violet`;`warn` 行 `--ew-text-warning`。
- 选中行变化 → `scrollIntoView({ block: "nearest" })`(jsdom 里 `Element.prototype.scrollIntoView` 不存在 —— 组件里 `el.scrollIntoView?.(…)` 可选调用);`running` 且 rows 数增长且用户没上滚(`scrollHeight - scrollTop - clientHeight <= 80`)→ `scrollTop = scrollHeight`。
- 键盘:容器 `onKeyDown` ↑/↓ 移动选中(调用 `onSelectRow` 相邻行 id)。

- [ ] **Step 1: 测试(红)**

```ts
it("renders one row per trajectory row with kind label, summary and duration; llm-call summary for empty think", () => {
  // rows = trajectoryRowsOf(EVENTS, INPUT, "答", "done")(EVENTS 同 Task 4 trajectoryRowsOf 测试)
  // 断言 console-traj-row 数 === rows.length;第 2 行 data-kind="think" 且文本含 "模型调用";tool 行含 "t1" 与 "r";最后一行 data-kind="assistant"
});
it("clicking a row calls onSelectRow(id); the selected row is aria-selected", async () => { … });
it("ArrowDown/ArrowUp move the selection to the neighbouring row", async () => { … });
it("running rows show the pulse; error rows carry data-status=error", () => { … });
```

- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/TrajectoryRows.tsx apps/admin-ui/src/components/console/trajectory_rows.css apps/admin-ui/src/components/console/__tests__/TrajectoryRows.test.tsx
git commit -m "feat(console): TrajectoryRows 扁平轨迹行列表(kind 标签 / 摘要 / 时长 / 选中 / 键盘 / 自动滚底)"
```

---

### Task 17: `RowDetail` —— 行详情五 tab(Summary / Payload / Result / Timing / Raw)

**Files:**
- Create: `apps/admin-ui/src/components/console/RowDetail.tsx`(≤ 400 行:tab 壳 + Summary + Raw)、`RowDetailPayloadResult.tsx`(Payload / Result 两 tab 的按 kind 渲染)、`RowDetailTiming.tsx`(Timing 双列)
- Test: `apps/admin-ui/src/components/console/__tests__/RowDetail.test.tsx`、`RowDetailTiming.test.tsx`
- 参考:`pages/agent_detail/playground/StepTimeline.tsx:467-666`(aux 详情:memories 列表 / plan 步骤 / reflect critique 的取值 helper `asMemories / planGoal / planSteps / reflectCritique` —— **复制**这四个小函数进 `RowDetailPayloadResult.tsx`,PR-B 删 StepTimeline 时不丢)、`TraceView.tsx:534-740`(`MessageBlock / IoSection / IoText`,只抄「messages → role + content」的最小渲染,不抄截断 / 原文弹层)、`components/EventCard.tsx`(Raw)、`components/ToolTimeline.tsx:71`(`ToolCallCard`)

**Interfaces:**
- Consumes:`TrajectoryRow` 全部变体(Task 4)、`SpanMatch`(Task 14)、`RunTrace / TraceSpan / RunTraceIo`(`api/trace_facade.ts`)、`ToolCallCard`、`EventCard`、`MarkdownView`、`CopyButton`、`FullTextTrigger / FullTextModal / FullTextState`、`fmtDuration`、i18n `console.detail_*` / `console.timing_*` / `console.traj_kind_*` / `common.refresh`。
- Produces:

```ts
export type RowDetailTab = "summary" | "payload" | "result" | "timing" | "raw";
export interface RowDetailProps {
  row: TrajectoryRow;
  turnSeq: number;                        // 0-based;显示 +1
  events: readonly SseEvent[];            // Raw tab 按 row.eventIndexes 取帧
  match: SpanMatch;                       // Task 14 的配对结果
  trace: RunTrace | null; traceLoading: boolean; onRefreshTrace: () => void;
  onFireResult?: (r: FireNowResult) => void;
  onClose: () => void;
}
export function RowDetail(props: RowDetailProps): JSX.Element;
```

行为:
- 头:`console-detail-header`:`第 {turnSeq+1} 轮` · kind 标签 · 摘要首行(用 Task 16 的 `rowSummary`);右侧关闭按钮 `console-detail-close`。antd `Tabs size="small"`,五项 `key = RowDetailTab`,label 里包 `<span data-testid="console-detail-tab-<key>">`;默认 `summary`;`row.id` 变化时 tab **不**重置(用户看 Timing 换行还在 Timing)。
- **Summary**(`console-detail-summary`):`dl` 表:层级(`console.detail_level{turn, step}`,`step === null` → `console.detail_level_turn_only{turn}`)/ 状态(`console.traj_status_<status>`)/ 时长(`fmtDuration`,null → `—`)/ 按 kind 追加:think → 模型 / tokens(`console.detail_tokens{in,out}`)/ finishReason;tool → 工具名 / `entry.server`(MCP)/ `entry.action`;plan → `stepsTotal` / `goal`;memory → `count`;subagent → `worker.label` / `worker.status` / `steps.length`;user → 附件数 / 变量数;assistant → 字数。
- **Payload**(`console-detail-payload`):think → `match.span?.input`(`RunTraceIo`:messages → 每条 `role` 小标 + `content` `<pre>`;text → `<pre>`),没有 span → `console.detail_need_langfuse`(灰字);tool / plan(update_plan)→ `<pre>{JSON.stringify(entry.args ?? {goal, steps, reason}, null, 2)}` + `CopyButton`;plan(planner)/ memory → `plan` / `detail.memories` JSON;reflect → `—`;user → 文本 + 附件名列表 + `inputs` 键值;assistant → `—`;marker 类 → 第一帧 `data` JSON。
- **Result**(`console-detail-result`):tool → `<ToolCallCard entry={row.entry} onFireResult={onFireResult} />`;think → `text`(reasoning,`<pre>` + 超 2000 字 `FullTextTrigger`)与 `content`(有则 `MarkdownView`);plan → 步骤 `<ol>`(○ pending / ◐ in_progress / ✓ completed,`plan` 为 null 时只显示 `reason` 或 `—`);memory → memories 列表(`asMemories`:kind / content / importance);reflect → verdict + critique(`reflectCritique`);subagent → `taskExcerpt` + `summary`(llmCallCount / wallClockMs)+ steps 数;assistant → `MarkdownView`;user → `—`;marker 类 → `text`。
- **Timing**(`RowDetailTiming`,`console-detail-timing`):两列表格 `<table>`(`thead`:`console.timing_col_sse` / `console.timing_col_langfuse`),行:结束 / 开始时刻(SSE 列 `serverMs` → `HH:mm:ss.SSS` 本地时;Langfuse 列 `span.startMs` 相对 trace 起点 `fmtDuration`)、时长(SSE `durationMs`;Langfuse `latencyMs`)、模型(think 行 SSE `model`;Langfuse `span.model`)、tokens(SSE think 行 in/out;Langfuse `inputTokens/outputTokens`)、成本(SSE `—`;Langfuse `costUsd` → `$x.xxxx`,null → `—`)。Langfuse 列整列按状态:`match.reason === "matched"` → 数值(`span.level === "error"` → 该列红字 + 底下一行 `statusMessage`);`"no_trace"` → `traceLoading` ? `console.timing_loading` : `trace?.status === "not_ready"` ? `console.timing_not_ready` + 刷新按钮(`console-timing-refresh` → `onRefreshTrace`) : `trace?.status === "unavailable"` ? `console.timing_unavailable` : `console.timing_no_trace`;`"count_mismatch"` → `console.timing_mismatch`;`"unsupported"` → `console.timing_unsupported`。
- **Raw**(`console-detail-raw`):`row.eventIndexes.map(i => events[i]).filter(Boolean).map(evt => <EventCard evt />)`;空 → `console.detail_no_frames`。
- 全部颜色 / 字号走 `--ew-*` 令牌;`FullTextModal` 一处放在 RowDetail 根。

- [ ] **Step 1: 测试(红)**

```ts
// RowDetail.test.tsx(rows 用 trajectoryRowsOf(EVENTS, INPUT, "答", "done") 造;match 手写)
it("summary lists level/status/duration and think-specific model + tokens", () => { /* 第 1 轮 · 第 1 步;模型 gpt-x;入 120 · 出 30 */ });
it("payload: tool args JSON with copy; think without span shows the need-langfuse hint", ...);
it("result: tool renders ToolCallCard (fire-now reachable), plan renders steps with glyphs, assistant renders markdown", ...);
it("raw: one EventCard per eventIndexes entry; empty → no-frames text", ...);
it("tab stays put when the row changes; close button calls onClose", ...);
// RowDetailTiming.test.tsx
it("matched span: two columns with SSE duration and Langfuse latency/tokens/cost", () => { /* costUsd 0.0123 → $0.0123 */ });
it("no trace: loading → 加载中; not_ready → 入库中 + refresh calls onRefreshTrace; unavailable → 不可用", ...);   // ← 1661 / 1585(UI 侧)/ 1493 迁入
it("matched span with level=error renders the Langfuse column in danger colour with the status message", ...);   // ← 1493
it("count_mismatch / unsupported show their explanatory text", ...);
```

- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck + eslint**
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/RowDetail.tsx apps/admin-ui/src/components/console/RowDetailPayloadResult.tsx apps/admin-ui/src/components/console/RowDetailTiming.tsx apps/admin-ui/src/components/console/__tests__/RowDetail.test.tsx apps/admin-ui/src/components/console/__tests__/RowDetailTiming.test.tsx
git commit -m "feat(console): RowDetail 行详情五 tab(Summary / Payload / Result / Timing 双列 / Raw)"
```

---

### Task 18: `TrajectoryPanel` —— 轨迹面板容器(头 + banner + 泳道条 + 行列表 ‖ 行详情)

**Files:**
- Create: `apps/admin-ui/src/components/console/TrajectoryPanel.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/TrajectoryPanel.test.tsx`
- 参考:`components/turn/TurnCard.tsx:1069-1080`(Langfuse 链)、`:1088-1110` 附近的 `RunStatusBanner` 接线与工具计数 Tag(`playground-tool-count` / `playground-tool-failed`,`toolStatusSummary`)

**Interfaces:**
- Consumes:`ConsoleTurn`(Task 5)、`liveSyntheticRows`(Task 5)、`trajectoryRowsOf / TrajectoryRow`(Task 4)、`matchTraceSpans`(Task 14)、`useRunTrace`(Task 14)、`LaneStrip`(Task 15)、`TrajectoryRows`(Task 16)、`RowDetail`(Task 17)、`summarizeTurn`、`parseTimeline`、`timelineBannerModel`、`RunStatusBanner`、`toolStatusSummary`、`buildLangfuseTraceUrl`(`config/env`)、`LiveStep`(`useTokenStream.ts`)、antd `Splitter / Tag / Empty`、i18n `console.inspect_*` / `trace_toolbar.open_in_langfuse` / `playground.tool_count`(count 复数键)/ `playground.tool_failed_count`。
- Produces:

```ts
export interface TrajectoryPanelProps {
  turn: ConsoleTurn | null; threadId: string | null;
  isSystemAdmin: boolean;
  /** 仅当前流式 live 轮传;其它 undefined。 */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  /** 中栏「检查」传来的行 id(`{ turnKey, rowId }` 由父级换算成本轮的 rowId 或 null);变化且非 null → 选中并滚到该行。 */
  focusRowId: string | null;
  onFireResult?: (r: FireNowResult) => void;
}
export function TrajectoryPanel(props: TrajectoryPanelProps): JSX.Element;
```

行为:
- `turn === null` → `Empty`(`console.inspect_no_turn`,`data-testid="console-traj-empty"`)。
- 派生(全部 `useMemo`,依赖 `turn.turn.events / status / input / attachments / inputs`、`liveByStep`):`events`;`summary = summarizeTurn(events)`;`answer = summary.segments.length ? segments.map(s => s.text).join("\n\n") : null`;`baseRows = trajectoryRowsOf(events, { text: turn.turn.input, attachmentNames: attachments.map(a => a.name), inputs: turn.turn.inputs ?? {} }, answer, turn.turn.status)`;`rows = [...baseRows, ...liveSyntheticRows(events, liveByStep)]`(合成行排在末尾;`assistant` 只在 answer 非空时存在,running 时不会跟合成行打架);`banner = timelineBannerModel(parseTimeline(events))`;`toolSummary = toolStatusSummary(events)`(→ `{ total, failed }`)。
- `const { trace, loading, refresh, traceId } = useRunTrace({ threadId, runId: turn.runId, enabled: true, turnStatus: turn.turn.status, wantTraceId: isSystemAdmin })`;`matches = useMemo(() => matchTraceSpans(rows, trace), [rows, trace])`;`langfuseUrl = isSystemAdmin ? buildLangfuseTraceUrl(traceId) : null`。
- 选中:`const [selectedRowId, setSelectedRowId] = useState<string | null>(null)`;`useEffect(() => setSelectedRowId(null), [turn?.key])`;`useEffect(() => { if (focusRowId) setSelectedRowId(focusRowId) }, [focusRowId])`;`selectedRow = rows.find(r => r.id === selectedRowId) ?? null`(行被合成行替换掉了 → null → 详情关掉)。
- 头(`console-inspect-turn-header`):`console.inspect_turn_header{ n: turn.seq + 1, status: t(console.footer_status_<status>) }` + Tag `playground-tool-count`(`toolSummary.total > 0`)/ `playground-tool-failed`(`failed > 0`)+ `langfuseUrl` 非 null → `<a data-testid="playground-turn-langfuse" href target=_blank rel="noreferrer">`。
- banner:`banner !== null` → `<RunStatusBanner status summary metrics? errorLabel errorMessage onJump />`(键名照 TurnCard.tsx:1092-1110:`summary = t("playground.rb_ok")`,error 时 `errorLabel = errorStepCount != null ? t("playground.tl_step", { n }) : (errorText ?? t("playground.rb_ok"))`,其余 prop 逐字抄;`onJump` = `setSelectedRowId(rows.find(r => r.status === "error")?.id ?? null)`)。
- 主体:`<LaneStrip events rows running={status === "running"} selectedRowId onSelectRow={setSelectedRowId} />`;下面 `selectedRow === null` ? `<TrajectoryRows rows selectedRowId onSelectRow running />` : `<Splitter layout="vertical" style={{ flex: 1, minHeight: 0 }}><Splitter.Panel defaultSize="55%" min="25%"><TrajectoryRows … /></Splitter.Panel><Splitter.Panel min="20%"><RowDetail row={selectedRow} turnSeq={turn.seq} events match={matches.get(selectedRow.id) ?? { span: null, reason: "no_trace" }} trace traceLoading={loading} onRefreshTrace={refresh} onFireResult onClose={() => setSelectedRowId(null)} /></Splitter.Panel></Splitter>`。根 `data-testid="console-trajectory-panel"`,`display:flex; flex-direction:column; height:100%; min-height:0`。

- [ ] **Step 1: 测试(红)**(渲染包 `MemoryRouter > AuthProvider > TenantScopeProvider > App`;`getRunTrace` / `getRun` `vi.spyOn`;admin 用 `setStoredToken(jwt(["system_admin"]))`;`consoleTurnFrom(events, status)` helper = `buildConsoleTurns({ historyTurns: null, historyLoads: {}, liveTurns: [{ id: "t1", input: "q", attachments: [], events, status, error: null, approval: null }], timings: {} })[0]`)

```ts
it("null turn → empty state; a turn → header 第 2 轮 · 已完成 + lane strip + rows", ...);
it("ok run: banner ok (RunStatusBanner) — migrated from PlaygroundTab.test 1280", ...);
it("error run: banner error + jump selects the first error row and opens its detail — 1307", ...);
it("a top-level error frame yields exactly one error row and one banner — 1379", ...);
it("clicking a row opens RowDetail (Summary tab) below; close hides it; switching turn clears the selection", ...);
it("focusRowId selects that row when it changes", ...);
it("Langfuse link hidden for non-admin — 1705; shown for system_admin via getRun trace_id — 1734", ...);
it("live turn: unsettled step's reasoning/tool names appear as running rows appended at the end", ...);
it("trace is fetched when the panel shows a turn (no view switch needed) — 1416 (fetch part)", ...);
```

- [ ] **Step 2: 跑红**;**Step 3: 实现**;**Step 4: 跑绿 + typecheck + eslint**(遇 Splitter/jsdom 问题按全局约束处理并记台账)
- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console/TrajectoryPanel.tsx apps/admin-ui/src/components/console/__tests__/TrajectoryPanel.test.tsx
git commit -m "feat(console): TrajectoryPanel 轨迹面板(头 / banner / 泳道条 / 行列表 ‖ 行详情 Splitter / trace 配对 / 焦点行联动)"
```

---

### Task 19: `PlaygroundTab` 组装 + 54 条测试迁移 + 退役 `SessionHistoryDrawer`

**Files:**
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx`(1476 → 目标 ≤ 700 行:状态与请求逻辑 + 组装 JSX)
- Modify: `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx`(按「行为清单迁移表」逐条改写 / 迁出)
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.stories.tsx`(axios 桩加 `/v1/sessions` 列表分支返回两条 ThreadMeta,`Ready` story 仍是唯一 story)
- Delete: `apps/admin-ui/src/components/SessionHistoryDrawer.tsx`、`SessionHistoryDrawer.stories.tsx`、`components/__tests__/SessionHistoryDrawer.test.tsx`(Task 6 已迁 9 条)
- 全仓 grep:`SessionHistoryDrawer`、`playground-history-open`、`playground-resumed-notice`、`LegacyTrajectoryTab` → 0;`EVENT_VIEW_STORAGE_KEY` / `expert_work.playground.eventView` → 只在 `TurnCard` / `ConversationDetail` 侧残留(PR-B 清),`PlaygroundTab.tsx` 里为 0

**Interfaces:** 消费 Task 3–18 全部导出。

`PlaygroundTab` 组装要点(每条对应现有代码位置,便于评审对照):
- **保留**:`thread / threadError / creatingThread / input / turns / running / exportingId / attachments / uploading / uploadError / varValues / rate / tokenStream / streamTurnId / taskResults / abortRef / fileInputRef / docInputRef`,`useHistoryTurns`,`ensureThread / handleResume / handleAttach / handleRemoveAttachment / patchTurn / detectApproval / startRun / handleRun / handleRetry / handleHistoryRetry / handleDecide / handleExport / handleStop / handleFireResult`。
- **删除**:workspace 五组 state + `loadWorkspace / handleDownloadFile / handleDownloadArtifact / handleDeleteFile / handleDeleteArtifact` + `useEffect([running])`(→ `WorkspacePanel`);`historyOpen`、`resumed`(R7);`eventView` 状态与持久化(`:150-165`,右栏不再有三档切换);`transcriptRef` 与滚底 effect(→ `Transcript`);`formatBytes / isHiddenWorkspacePath`(→ `workspace_format.ts`);全部内联 JSX。
- **新增 state**:`selectedTurnKey: string | null`(R9)、`inspectTab: InspectTab`(默认 `"trajectory"`)、`inspectRow: { turnKey: string; rowId: string } | null`(R18)、`timings: Record<string, TurnTiming>`、`sidebarTick: number`。
- **新增派生**:`consoleTurns = useMemo(() => buildConsoleTurns({ historyTurns, historyLoads, liveTurns: turns, timings }), […])`;`selectedTurn = consoleTurns.find(t => t.key === selectedTurnKey) ?? consoleTurns.at(-1) ?? null`;`liveEvents = useMemo(() => turns.flatMap(t => t.events), [turns])`;`{ plan, loaded: planLoaded, save: savePlan } = usePlanCard({ threadId: thread?.thread_id ?? null, liveEvents })`;`stats = useMemo(() => computeSessionStats(consoleTurns.map(statsInputOf), rate), [consoleTurns, rate])`;`missing = missingRequired(promptVariables, varValues)`。
- **新增 effect**:`tokenStream.finalized && streamTurnId` → `setTimings(prev => prev[streamTurnId] ? prev : { ...prev, [streamTurnId]: { ttftMs, firstTokenAt, lastTokenAt } })`(依赖 `tokenStream.finalized / ttftMs / firstTokenAt / lastTokenAt / streamTurnId`)。
- **改动**:`resetDraft` 与 `handleResume` 加 `setVarValues({})`、`setSelectedTurnKey(null)`、`setInspectRow(null)`、`setInspectTab("trajectory")`(R11 / R9 / R18);`startRun` 开头 `setSelectedTurnKey(null)`,`finally` 里 `setSidebarTick(n => n + 1)`;`ensureThread` 创建成功后 `setSidebarTick(n => n + 1)`;`handleRun` 若 `missing.length > 0` 直接 return;`onDownloadArtifact`(答案气泡产物下载)改为组件内 `downloadArtifact(name, undefined, concreteTenantScope(apiTenantScope))` 的薄包装(原 `handleDownloadArtifact` 只剩这一处用途,保留同名函数即可)。
- **JSX**:

```tsx
<ConsoleShell
  sidebarLabel={t("console.sidebar_title")}
  sidebar={<SessionSidebar agentName={r.name} currentThreadId={thread?.thread_id ?? null} running={running}
            onNew={resetDraft} onResume={handleResume} readOnly={isTenantSwitched} reloadTick={sidebarTick}
            onChanged={handleSidebarChanged} />}
  main={<>
    <div className="ew-console__main-head">
      {threadError !== null ? <Alert type="error" showIcon message={t("playground.session_failed")} description={threadError} data-testid="playground-session-error" />
        : thread ? <Text type="secondary" className="mono" data-testid="console-thread-id">{`${t("console.thread_id_label")}: ${thread.thread_id}`}</Text> : null}
      {consoleTurns.length > 0 && <Text type="secondary">{t("console.turn_count", { n: consoleTurns.length })}</Text>}
    </div>
    <Transcript turns={consoleTurns} flatHistory={historyTurns === null ? history : []} taskResults={taskResults} threadId={thread?.thread_id ?? null}
      selectedKey={selectedTurnKey} onSelectTurn={handleSelectTurn} onInspectRow={handleInspectRow} streamTurnKey={streamTurnId} liveByStep={tokenStream.liveByStep}
      registerHistoryRow={registerHistoryRow} rate={rate} isSystemAdmin={isSystemAdmin} readOnly={false} isTenantSwitched={isTenantSwitched}
      onDecide={handleDecide} deciding={running} onExport={handleExport} exportingKey={exportingId}
      onRetryLive={isTenantSwitched ? undefined : handleRetry} onRetryHistory={isTenantSwitched ? undefined : handleHistoryRetry}
      onDownloadArtifact={handleDownloadArtifact} onFireResult={handleFireResult} />
    <div className="ew-console__composer">
      <PlanCard plan={plan} loaded={planLoaded} running={running} readOnly={isTenantSwitched} onSave={savePlan} />
      <VariablesForm variables={promptVariables} values={varValues} onChange={(k, v) => setVarValues(prev => ({ ...prev, [k]: v }))} disabled={running} />
      {uploadError !== null && <Alert … data-testid="playground-upload-error" />}
      <AttachmentChips attachments={attachments} onRemove={handleRemoveAttachment} />
      <Composer value={input} onChange={setInput} onSend={() => void handleRun()} onStop={handleStop} running={running} uploading={uploading}
        readOnly={isTenantSwitched} missingVariables={missing} onAttachImage={() => fileInputRef.current?.click()} onAttachDocument={() => docInputRef.current?.click()} />
      <StatsBar stats={stats} isSystemAdmin={isSystemAdmin} />
    </div>
    <input ref={fileInputRef} … data-testid="playground-file-input" />
    <input ref={docInputRef} … data-testid="playground-doc-input" />
  </>}
  inspect={<InspectPanel tab={inspectTab} onTabChange={setInspectTab}
    trajectory={<TrajectoryPanel turn={selectedTurn} threadId={thread?.thread_id ?? null} isSystemAdmin={isSystemAdmin}
      liveByStep={selectedTurn?.key === streamTurnId ? tokenStream.liveByStep : undefined}
      focusRowId={inspectRow !== null && inspectRow.turnKey === selectedTurn?.key ? inspectRow.rowId : null}
      onFireResult={handleFireResult} />}
    workspace={<WorkspacePanel running={running} readOnly={isTenantSwitched} />} />}
/>
```

`handleSelectTurn = (key) => { setSelectedTurnKey(key); setInspectRow(null); setInspectTab("trajectory"); }`;`handleInspectRow = (turnKey, rowId) => { setSelectedTurnKey(turnKey); setInspectRow({ turnKey, rowId }); setInspectTab("trajectory"); }`;`handleSidebarChanged = ({kind, threadId, title}) => { if (thread?.thread_id !== threadId) return; if (kind === "rename") setThread({ ...thread, title: title ?? thread.title }); else resetDraft(); }`。

- [ ] **Step 1: 先改测试文件(红)** —— 按迁移表逐条改写;迁出的条目从本文件删除时,在同一 commit 里对应组件测试已存在(Task 6 / 9 / 14 / 17 / 18 已落);`renderPg` 不变;`establishThread` 里 `findByText(/33333333-3333/)` 改查 `console-thread-id`;新增 4 条:①「必填变量未填时发送按钮禁用,填了才可发」(用 609 行那条的 jinja fixture);②「切换会话清空变量值」;③「点脚注『检查』右栏头变成该轮」;④「点紧凑行『检查』→ 右栏对应 `console-traj-row` `aria-selected=true` 且详情打开」。
- [ ] **Step 2: 跑红**:`pnpm exec vitest run src/pages/__tests__/PlaygroundTab.test.tsx` → 大面积红(新 testid 不存在)。
- [ ] **Step 3: 重写 PlaygroundTab.tsx**(按上面要点);删 Drawer 三件;stories 桩补 `/v1/sessions`。
- [ ] **Step 4: 跑绿**:`pnpm exec vitest run src/pages/__tests__/PlaygroundTab.test.tsx src/components/console` → 全绿;然后**全套**:`pnpm typecheck && pnpm exec vitest run && pnpm build && pnpm exec eslint src` → 全绿(改共用组件跑全套 vitest,教训见记忆);`pnpm build-storybook`(CI 有这一档)→ 绿。
- [ ] **Step 5: 行数与残留核对**:`wc -l src/pages/agent_detail/PlaygroundTab.tsx` ≤ 700;`grep -rn "SessionHistoryDrawer\|playground-history-open\|playground-resumed-notice\|LegacyTrajectoryTab" src` → 0;`grep -n "EVENT_VIEW_STORAGE_KEY\|eventView" src/pages/agent_detail/PlaygroundTab.tsx` → 0;`grep -c "^\s*it(" src/pages/__tests__/PlaygroundTab.test.tsx` + 迁出条数 ≥ 54 + 新增。台账写一张「54 条去向」核对表。
- [ ] **Step 6: Commit**

```bash
git add -A apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx apps/admin-ui/src/pages/agent_detail/PlaygroundTab.stories.tsx apps/admin-ui/src/components/SessionHistoryDrawer.tsx apps/admin-ui/src/components/SessionHistoryDrawer.stories.tsx apps/admin-ui/src/components/__tests__/SessionHistoryDrawer.test.tsx
git commit -m "feat(playground): 调试台切到三栏壳 —— PlaygroundTab 只留状态与请求逻辑;54 条行为测试逐条迁移;退役 SessionHistoryDrawer"
```

---

### Task 20: 上线与真栈冒烟(合并后)

**Files:** 无代码;`infra/k8s/overlays/test/kustomization.yaml`(release.sh 改 newTag → 记录 PR)。

- [ ] **Step 1: 发布**:`export KUBECONFIG=~/.kube/expert-work-test.yaml && tools/deploy/release.sh test`(后台 ~12 min,期望 `SMOKE PASS`);把 kustomize 挪走的 `# Sandbox migration W1 Task 2 …` 注释挪回 control-plane newTag 下方;分支 `chore/deploy-test-<sha>`,开记录 PR(照 #1206 写法)。
- [ ] **Step 2: API 探针(pod 内,key 不出集群)**:临时 key → `GET /v1/sessions?order_by=last_activity` 200 且列表非空、`GET /v1/sessions/{thread}/runs` 每行有 `tokens` 键;`?order_by=bogus` 422;finally 撤 key。脚本放 scratchpad(照 `probe_plan_event.py`)。
- [ ] **Step 3: 浏览器冒烟(用户或 Chrome 扩展)**:一张清单贴进记录 PR:①左栏列出会话、最近活动在前、搜索 / 归档筛选、新建、改名、归档;②中栏发一条 jinja agent 消息:必填未填发送禁用 → 填了发送 → 思考行 / 工具行 / 答案打字机 / 脚注;③任务卡出现并随 `update_plan` 更新、空闲时可编辑、运行中置灰;④状态栏数字随轮增长;⑤右栏轨迹:泳道条块点击选行、行列表、行详情五 tab(Timing 双列在 Langfuse 入库后有数、入库中显示「入库中」)、Langfuse 链(admin);中栏『检查』跳到右栏对应行;工作区 tab 列产物 / 文件;⑥切会话 → 历史懒重建成紧凑行;⑦窗口缩到 <1200 → 左栏折成图标条,点开抽屉;⑧切入他租户 → 全部写控件置灰。
- [ ] **Step 4: 记录 PR 说明**写清探针结果 + 清单勾选;绿了等用户一句合并。

---

## Self-Review(写完对着 spec 过一遍)

- **Spec 覆盖**:§二.1 左栏(T6)/ 中栏紧凑行(T4+T5+T10+T11)/ 任务卡(T3+T8)/ 变量表单(T7)/ 输入区(T7)/ 状态栏(T5+T12)/ 右栏轨迹:泳道条(T15)、扁平行(T4+T16)、行详情 Summary / Payload / Result / Timing 双列 / Raw(T14+T17)、默认跟随 + 面板头(T18)/ 工作区(T9);§二.4 前端 `plan_reducer`(T3);§三 文件表:`ConsoleShell / SessionSidebar / Transcript / TurnBlock / CompactRow / AnswerBubble / TurnFooter / PlanCard / VariablesForm / Composer / AttachmentChips / StatsBar / InspectPanel / TrajectoryPanel / LaneStrip / TrajectoryRows / RowDetail / WorkspacePanel / api/trajectory_rows / api/plan_reducer` 全部有归属;`useSessionStats.ts` 在本计划里叫 `api/session_stats.ts`(纯函数比 hook 好测,spec 表里的名字是建议);spec 里没有但本计划加的纯函数:`api/trace_match.ts`(R15)、`lane_strip_model.ts`(R20)、`live_rows.ts`(两处共用);§四 后端 `order_by`(T1)+ 裁定 R2 的 `runs[].tokens`(T1);§五 PR2 验收:54 条迁移(T19 表)+ 状态栏公式单测(T5)+ 真栈冒烟(T20);§五 PR3 验收:行投影单测与 `parseTimeline` 一一对应含 `update_plan` 合并(T4)、Timing 两来源都有的样例(T17 `matched span` 那条)、Langfuse `not_ready` 轮询保留(T14)、真栈冒烟(T20);§三「退役」条目全部**推迟到 PR-B**(本 PR 不删旧文件,只删 SessionHistoryDrawer)。
- **占位扫描**:T5 / T6 / T8 / T9 / T10 / T11 / T12 / T13 / T15 / T16 / T17 / T18 的部分测试用 `it("…", ...)` 一句话描述而不是完整代码——这些条目的断言对象(testid / 文案 / 调用)都已在同一 Task 的「行为」段写死,实施者按段落写;完整代码的条目覆盖了每个 Task 最难的一条(T4 / T14 的纯函数测试全码)。
- **类型一致**:`ConsoleTurn`(T5)在 T10 / T11 / T18 / T19 同名同形;`CompactRow / TrajectoryRow / RowBase.serverMs / ThinkRow.content`(T4)在 T5 `liveSyntheticRows`、T10 / T11 / T14 / T15 / T16 / T17 / T18 同形;`SpanMatch`(T14)在 T17 / T18 用;`useRunTrace` 返回名 `trace / loading / refresh / traceId`(T14)在 T18 解构;`LaneStripProps / TrajectoryRowsProps / RowDetailProps`(T15 / T16 / T17)与 T18 的 JSX 对得上;`onInspectRow`(T11)在 T19 接 `handleInspectRow`,`focusRowId`(T18)在 T19 由 `inspectRow` 派生;`TurnTiming`(T5)与 `useTokenStream` 新字段(T3)对得上;`missingRequired`(T7)在 T19 用;`InspectTab`(T13)在 T19 用;`usePlanCard` 返回名 `plan / loaded / save / saving`(T8)在 T19 用 `plan / planLoaded / savePlan` 解构。
- **spec 之外**:R2(runs tokens)、R5(IME 守卫)、R12(缓存命中分母)三处是我替 spec 补的事实修正;R13–R20 是把 spec §二.1 右栏一段话落成可测规则时补的裁定(行集合 / Raw 帧下标 / span 配对 / trace 时机 / Splitter / 选中源 / 测试去处 / 泳道映射)。用户过目时可否决。
