# 调试台 / 对话记录 / Run 详情 交互重设计 —— 设计说明(2026-08-17)

**背景**:用户对照 deepseek-harness 的客户端界面,指出我方调试台与对话记录页四个问题:

1. 调试台左边是「输入面板」、右边是一张张调试卡,**没有连续对话视图**;理想是「像一个正常客户端,
   左边连续对话,点某一轮右边才是这一轮的轨迹」,调试参数(token / 缓存 / 时长)作为附加信息。
2. 一轮里有「执行轨迹 / 工具调用 / 原始事件」三个视图,应该只有**一条运行轨迹**,点轨迹上的点才看详情。
3. 调试台**没法填 `inputs`**(提示词模板变量)。
4. 对话记录详情页要做同样的优化。

随后追加:**计划(任务列表)**要作为一等公民 —— 调试台怎么显示、轨迹里怎么显示、对外 SSE 怎么推给第三方前端。

查代码后,第 3 点不是缺功能,是两条 bug(见「PR0」);其余是信息架构问题。

**前提**:平台尚未上生产,对外 API 也未上生产 —— 调试台整体替换、对外新增事件都**不留旧路**。

**决策(用户 2026-08-17 逐条拍板)**:

| # | 问题 | 决定 |
|---|---|---|
| D1 | `{{ }}` 保存报错的修法 | **拆掉「保存时填空」整层**(`template_vars` 字段 + loader 渲染步骤) |
| D2 | 会话列表 | **左栏常驻**,窄屏可折;抽屉退役 |
| D3 | 工作区 | **右栏第二个 tab**(轨迹 \| 工作区) |
| D4 | 右栏轨迹粒度 | **按轮**,默认跟随最新 / 进行中的一轮 |
| D5 | Langfuse「执行轨迹」视图 | **降成 Timing 的一个来源 + 成本列**;瀑布图退役;system_admin 保留「在 Langfuse 打开」 |
| D6 | 对外计划事件 | **新增顶层 `plan` 事件**(全量快照);run 开始时会话已有计划先补发一条;`updates` 里的 `plan` 键保留 |
| D7 | 对话记录页 | **摘要卡留,Runs 表去**;轮标题带状态与「查看运行」 |
| D8 | Run 详情页 | **纳入**,同一套轨迹面板,保持一致 |

---

## 一、现状(已查实,写进来是为了让评审能核对)

### 调试台 `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx`(1471 行,单组件)

- 两栏 grid(`:753-762`)。左栏 = 「会话历史」按钮(开抽屉)+ 新建 + Jinja 变量输入(`:840-879`)+ 输入框 + 附件 + 按钮 + **底部工作区面板**(`:1015-1246`,内联 JSX,按**用户**取 `GET /v1/workspace`)。右栏 = 每轮一张 `TurnCard`。
- **没有连续对话视图**;纯气泡视图只在历史重建失败时才退化出现(`:1389-1426`)。
- 会话:懒创建(`ensureThread`,`:243-267`),`SessionHistoryDrawer`(423 行)调 `listSessions({agentName, q, status, limit:50, offset})`;后端 `GET /v1/sessions` **只列调用者自己的会话**(`_user_scope.py:139-143`),支持 `agent_name / status / q / include_archived / limit / offset`,默认按 `created_at DESC`(持久层有 `last_activity` 排序,端点没暴露)。

### `TurnCard`(`components/turn/TurnCard.tsx`,1257 行)

用户消息 + 答案 + 折叠面板三个:「推理」/「运行状态」(`PlanPanel` + `AgentStatePanels` + 压缩摘要)/「事件」。「事件」里 `Segmented` 三档:

| UI 标签 | 键 | 组件 | 数据源 |
|---|---|---|---|
| 执行轨迹 | `exact` | `TraceView`(1055 行) | **Langfuse** trace(run 完 ~1 s 入库,轮询;树 + 瀑布 + 详情;per-span token / 成本) |
| 工具调用 | `timeline` | `GanttTimeline`(244 行)→ 行点开 `StepTimeline`(769 行) | SSE 帧 `parseTimeline`(`api/timeline.ts`) |
| 原始事件 | `raw` | `EventCard` × N | 原始 SSE 帧 |

三档是同一次 run 的三种画法;`PlanPanel` 挂在**每张**卡的「运行状态」里,**轮询** `GET /v1/sessions/{thread}/plan`(不吃流,展开时取一次,进行中不刷新,每张历史卡显示的都是当前这一份)。

### 数据

- 前端 `Turn { id, input, attachments, inputs?, events: SseEvent[], status, error, approval }`(`components/turn/types.ts:27-40`)。
- 后端事件:`metadata / updates / token(临时,不落库不续传)/ compaction / worker / guard / retry / approval / error / end`,续传端点另有 `gap / truncated`。**对外流没有任何过滤**:`sse_consumer`(`sse.py:1442`)与 `_run_event_stream.py` 原样转发每一条落库帧。
- `_publish_frame(event_name, data)`(`sse.py:399-407`)= bridge 分配 seq + 入持久化队列,是全 orchestrator 唯一发布点;`compaction / worker / guard` 都经它。
- `updates` 帧每个节点值带 `_duration_ms`(距上一帧毫秒,`sse.py:564-571`,agent 与 tools 节点都有);每条 AIMessage 带 `usage_metadata`(in / out / cache_read / cache_creation / reasoning,`turn_summary.ts` 已聚合成 `TurnUsage`);`token` 帧**没有时间戳**,TTFT 由 `useTokenStream` 客户端 `Date.now()` 测,**没有记最后一个 token 的时间**。
- 计划:`Plan { goal, steps[{id, description, status: pending|in_progress|completed}] }`(protocol `plan.py`),**会话级、checkpoint 持久**;`update_plan` 整份替换;`tools` 节点把 `plan` 快照展进返回 dict(`builder.py:1400-1405`),`planner` 节点同样 → **`updates` 帧里已有 `plan` 键**;`parseTimeline`(`timeline.ts:216-222`)已识别成一行 `制定计划 · 目标 + N 步`。对外文档 3.x 明说「值里除 `messages / step_count / _duration_ms` 之外的键请忽略」→ 第三方按契约看不到。
- `GET/PUT /v1/sessions/{thread}/plan`(`api/plan.py`)读写 LangGraph checkpoint(`graph.aget_state / aupdate_state`),PUT 在 run 进行中 409;只对**自己的**会话(他人会话 404)。

### 对话记录页 `pages/ConversationDetail.tsx`(574 行)

摘要卡(用户 / 轮次+错误 / token / 模型 / 最后活跃)+ 消息卡(只读 `TurnCard`)+ Runs 表。`readOnly` 没盖住 `ToolCallCard` 的「立即触发」(#1057 已记)。

### Run 详情页 `pages/RunDetail.tsx`(212 行)+ `pages/run_detail/*`(1183 行)

PageHeader → `ApprovalCard` → 元数据卡 → `RunSummaryPanel` → `PlanPanel`(带编辑,3 s 轮询)→ `TraceToolbar` → `EventStreamPanel`(`EventCard` + `ToolTimeline` + 压缩摘要,timeline / raw 切换)。不用 `TurnCard`。

### 两条 bug(第 3 点的真相)

- **Bug A**:`PlaygroundTab.tsx:134` `manifestLike = { spec: r.spec }`,而 `record.spec` **本身就是完整 manifest**(后端 `agents.py:124` 直接取 `record.spec.metadata.labels`);多包一层后 `readPromptJinja` 读到 undefined → 变量框永不渲染。`PlaygroundTab.test.tsx:609` 的 fixture 把 `record.spec` 造成内层 spec,所以测试绿。#824 引入即坏。
- **Bug B**:保存 agent 走 `agents.py:642 → loader.load_from_string → loader._render`(`loader.py:106-110`):**整份 YAML 先当 Jinja 渲染**(`SandboxedEnvironment` + `StrictUndefined`),这是「保存时填空」老功能(请求字段 `template_vars`);控制台从不发它,全仓无一份 agent YAML 用它,非测试代码里唯一引用是 `api/agents.ts:97` 的 TS 类型。`{{customer_code}}` 被当保存时变量求值 → 未定义 → 保存失败。设计文档 `docs/design/jinja-dynamic-prompt.md` §2 只说两层「独立」,没处理碰撞。**单花括号 `{customer_code}` 能存但 run 期不替换**(`prompt_render.py:70` 是真 Jinja)—— 用户当前的绕法等于没有变量。

### deepseek-harness 参照(只抄交互,不抄实现)

三栏壳(会话侧栏 | 对话 | 详情);对话里 Think / Bash 是一行紧凑摘要;「对话 / 轨迹」按会话切 tab;轨迹 = 三泳道时间条 + 扁平行 + 右侧详情(Summary / Payload / Result / Schema / Timing,含 `Timing source`);composer 下一条状态栏(`轮·步 | LLM·工具 | 首 token·tok/s | 缓存命中 | 输入·输出`,全部由事件日志前端折叠 + 一个 token 投影算出);任务卡钉在输入框上方、会话级、全量快照事件驱动。它是 cordis 插件槽位 + WebSocket 架构,`TrajectoryTable.tsx` 一个文件 3074 行 —— 我们不需要那个规模。

---

## 二、目标形态

### 1. 调试台

```
┌────────────┬─────────────────────────────────────┬──────────────────────────┐
│ 会话        │ 对话流                                │ 检查面板                   │
│ 🔍 搜索     │  [你] 帮我看看这个客户                  │ [轨迹] [工作区]            │
│ + 新建      │   ◇ 思考 · 先查客户档案…                │ 输入 ▪   模型 ▪ ▪ ▪   工具 ▪▪ │
│ ● 会话 A    │   ◇ 记忆召回 · 3 条                    │ USER   帮我看看这个客户     │
│   会话 B    │   ◇ 计划 · 4 步(1 完成)               │ THINK  先查客户档案…        │
│   会话 C    │   ◇ query_crm · {"id":"…"} → 3 条记录   │ MEMORY 记忆召回 · 3 条      │
│   …         │   [答案 markdown]                     │ PLAN   4 步                │
│             │   👍 👎 · 12.3k tok · 8.2s · 4 步      │ TOOL   query_crm → 3 条 ▶  │
│             │  [你] 那下一步…                        │ ASSISTANT 答案             │
│             │   ◇ 思考 · …(打字机)                  │ ────────────────────────  │
│             │ ┌ 任务  1 已完成 · 1 进行中 · 2 待处理 ┐│ TOOL · 第 2 轮 · 第 3 步    │
│             │ │ ✓ 查档案  ◐ 分析  ○ 出建议  ○ 复核  ││ [Summary][Payload][Result] │
│             │ └────────────────────────────────┘│ [Timing][Raw]             │
│             │ 客户 code [        ]  必填            │  Payload {"id": "…"}       │
│             │ [输入框…………………] [图][文][发送]        │  Result  3 条记录…         │
│             │ 2 轮·17 步 | LLM 1m25s·工具 4.6s | 首 token 0.8s·144 tok/s | 缓存 94% | 入 641K·出 3K│
└────────────┴─────────────────────────────────────┴──────────────────────────┘
```

**左栏 · 会话列表**(`SessionSidebar`):本 Agent 下当前操作者的会话(`GET /v1/sessions?agent_name=`,后端已按调用者过滤),按最近活动倒序(端点新增 `order_by=last_activity`,持久层已支持),搜索、状态筛选(活跃 / 归档)、新建、改名、归档、删除,「加载更多」。选中态高亮;进行中的会话带活动点。窄于 1200 px 折成图标条(展开为浮层)。抽屉 `SessionHistoryDrawer` 退役。

**中栏 · 对话流**(`Transcript`):每轮 = 用户气泡(带附件缩略、变量键值小字)+ 若干**紧凑行** + 答案气泡 + 轮脚注(反馈 👍👎、token、耗时、步数、状态、「查看运行」)。紧凑行种类与来源全部来自现有 `parseTimeline` 的产物,一种解析、两处投影(中栏紧凑行 / 右栏轨迹行):

| 紧凑行 | 一行文案 | 来源 |
|---|---|---|
| 思考 | `思考 · <首行/流式时最新一行>` | agent 步 `reasoning`(`token` 帧 reasoning 频道打字机) |
| 工具 | `<工具名> · <args 摘要> → <结果首行 / 错误首行>` | agent 步 `tools[]` |
| 计划 | `计划 · 更新为 N 步 · <reason 首句>` / `制定计划 · N 步` | `update_plan` 调用 / planner 节点 |
| 记忆召回 / 写回 | `记忆召回 · N 条` | aux 节点 |
| 反思 | `反思 · 通过 / 修订` | aux 节点 |
| 压缩 | `上下文压缩 · 12.3k → 4.1k` | `compaction` 帧 |
| 子代理 | `子代理 · <名>` | `worker` 帧 |
| 审批 | 内联 `ApprovalGate`(可操作;只读页只显示结论) | `approval` 帧 |
| 错误 / 重试 | 红色一行 | `error` / `retry` 帧 |

紧凑行点击 → 就地展开一层摘要(工具的完整 args / 结果、思考全文);行尾「检查」→ 右栏跳到对应轨迹行并选中。答案 markdown 打字机(`token` 帧 content 频道,复用 `useTokenStream`)。历史轮沿用 `useHistoryTurns` 懒重建(进入视口才拉 run 事件),重建失败仍退化为纯气泡。`TaskResultCard`(队列结果)、`HistoryDivider`、`FullTextModal` 原样搬入。

**任务卡**(`PlanCard`):钉在输入框上方,会话级、可折叠(记住折叠态);标题行 `任务  N 已完成 · N 进行中 · N 待处理`,展开为步骤表(○ / ◐ / ✓ 图标 + 颜色 + 文字,黑白可读);数据 = 打开会话时 `GET /v1/sessions/{thread}/plan` 基线 + 流里的 `plan` 事件整段覆盖;空闲(无 run 进行中)时可编辑,复用现有 `PlanPanel` 的表单与 `PUT`,进行中置灰并提示。没有计划时不渲染。

**变量表单**(`VariablesForm`):agent 声明了变量(`system_prompt.jinja && variables[]`)时出现在输入框上方,每个变量一行(名称 + 描述 + 必填标记),必填未填「发送」置灰并提示;值随会话保留(切会话清空);已发的轮在用户气泡下用小字显示当次键值。

**输入区**(`Composer`):输入框(`maxLength 65536`,`Enter` 发送 / `Shift+Enter` 换行,与现状一致)、图片 / 文档附件、发送 / 停止;切入他租户只读态照旧置灰(`useIsTenantSwitched`)。

**状态栏**(`StatsBar`):会话级、一行、溢出省略并 tooltip 全文。字段与算法(全部前端聚合,不加后端帧):

| 项 | 算法 | 来源 |
|---|---|---|
| `N 轮 · N 步` | 轮 = 至少完成一步的 run 数(进行中的算);步 = 各轮 `step_count` 最大值之和 | `updates` |
| `LLM x · 工具 y` | agent 节点 `_duration_ms` 之和 / tools 节点 `_duration_ms` 之和(近似,含节点开销) | `updates` |
| `首 token 平均 z` | 各轮 `ttftMs` 平均 | `useTokenStream` |
| `N tok/s` | Σ 输出 token ÷ Σ(最后一个 token 时刻 − 第一个 token 时刻);`useTokenStream` 新增 `firstTokenAt / lastTokenAt` | `usage_metadata` + 客户端时钟 |
| `缓存命中 %` | cache_read ÷(input + cache_read + cache_creation) | `usage_metadata` |
| `输入 K · 输出 K` | live 轮 Σ `usage_metadata`;未加载的历史轮用 `listThreadRuns` 的持久 rollup(`RunTokens`) | 同上 |
| `≈ ¥x` | 仅 system_admin 且有费率(现状) | `RateCardRecord` |

**右栏 · 检查面板**(`InspectPanel`,tab:轨迹 | 工作区):

- **轨迹**(`TrajectoryPanel`,按轮):
  - 顶部三泳道时间条(输入 / 模型 / 工具),用 `buildGanttRows` 的数据(时间轴按该轮墙钟);块点击 = 选中对应行;不做拖选。
  - 扁平行列表:`USER / THINK / PLAN / MEMORY / TOOL / SUBAGENT / REFLECT / COMPACTION / ASSISTANT / ERROR`,每行 = kind 标签 + 一行摘要 + 时长;进行中的行有活动态;错误行标红。行数据 = 中栏同一份 `parseTimeline` 结果的另一个投影(`api/trajectory_rows.ts`)。
  - 行详情(点行,在轨迹面板**下半区**打开,中间可拖分割;不做左右分栏):`Summary`(层级:第 N 轮 · 第 N 步;状态;时长;模型;token)/ `Payload`(工具 args / LLM 输入摘要 / 计划快照)/ `Result`(工具结果 / 答案 / 反思结论)/ `Timing`(**两列**:SSE 时戳、Langfuse 精确;后者含 per-span 成本;Langfuse 未入库显示「入库中」并按现有 `not_ready` 轮询;system_admin 显示「在 Langfuse 打开」)/ `Raw`(该行对应的原始帧 JSON,复用 `EventCard`)。
  - 默认跟随最新 / 进行中的一轮;中栏点其他轮切过去;面板头显示「第 N 轮 · <状态>」。
- **工作区**(`WorkspacePanel`):把现在 `PlaygroundTab.tsx:1015-1246` 那块内联 JSX 抽成组件,功能不变(卷信息 / 产物 / 文件、下载、删除,run 结束刷新)。

### 2. 对话记录详情页

同一套组件,只读模式:顶部保留**摘要卡**;下面是「对话流 + 右栏轨迹」(没有左栏、没有输入区、没有变量表单;`PlanCard` 只读、由事件折叠得出;`StatsBar` 用持久 rollup);Runs 表**撤掉**,每轮脚注带状态与「查看运行」。`readOnly` 补盖「立即触发」按钮。

### 3. Run 详情页

保留 PageHeader / `ApprovalCard` / 元数据卡 / `RunSummaryPanel`;`PlanPanel` 换成 `PlanCard`(编辑能力保留:GET/PUT 成功即可编辑,他人会话 404 则只读);`TraceToolbar + EventStreamPanel` 换成 **同一个 `TrajectoryPanel`**(单 run = 单轮,时间条 + 行 + 详情)。`EventStreamPanel` 退役。

### 4. 计划一等公民

**后端(orchestrator `sse.py`)**:

- 新增 `_publish_plan(plan)` → `_publish_frame("plan", payload)`(与 `compaction / worker / guard` 同款,落库、带 id、可续传、对外流自动可见)。
- 派生点一:流循环里 `await _publish_frame(stream_mode, jsonable_chunk)` 之后,遍历节点值,任一含非空 `plan` → 发一条 `plan`(同一 chunk 多个节点都有取最后一个)。
- 派生点二:`metadata` 帧之后、`graph.astream` 之前,`snapshot = await graph.aget_state(effective_config)`,`snapshot.values.get("plan")` 非空 → 先发一条 `plan`(冷启动补发)。一次 checkpoint 读,可接受;读失败只记日志不影响 run。
- payload(与 protocol `Plan` 同形,`model_dump(mode="json")`):

```json
{ "goal": "给客户 C-1024 出一份续约建议",
  "steps": [ { "id": "1", "description": "查客户档案", "status": "completed" },
             { "id": "2", "description": "分析近半年工单", "status": "in_progress" },
             { "id": "3", "description": "出建议", "status": "pending" } ] }
```

- `updates` 里的 `plan` 键**保留**(我方 `parseTimeline` 不动)。

**前端**:`api/plan_reducer.ts` 把一段事件流折叠成当前计划 —— `plan` 事件与 `updates.*.plan` 都算(后者兜 PR1 之前的历史 run),按 seq 取最后一份、整段覆盖。`PlanCard` = 基线 + reducer 输出:调试台基线是 `GET /v1/sessions/{thread}/plan`;只读页(对话记录 / 他人 run)`GET` 会 404,基线为空,只靠已加载 run 的事件折叠。轨迹 / 中栏的「计划」行来自 `updates`(`update_plan` 工具行的详情 = 同一 tools 节点值里的快照;planner 节点单独一行);`plan` 事件本身**不生成行**。

**对外文档**(`apps/admin-ui/docs-site/guide/`,按 `2026-08-17-external-docs-style-guide.md`):

- 第 3 章新增 `### plan` 一节,四段:什么时候发(计划创建 / 更新时;run 开始时会话已有计划先发一条)/ `data` 字段(`goal` string、`steps` 数组:`id` string、`description` string、`status` 取值三个及含义)/ 示例 / 客户端怎么处理(**收到即整段覆盖本地那份,以最新一条为准;不要按 `id` 做增量合并;断线续传会重放**)。3.x「哪些事件有 id」表加一行;`updates` 节的「请忽略其他键」措辞不变。
- 第 10 章:10.1 导语的事件列表加 `plan`(四种语言的示例对未列举事件都是原样打印,代码不用改);渲染示例(`onPlan` reducer)放在第 3 章 `plan` 节的「客户端怎么处理」里,与其它事件同款。
- 第 8 章不涉及。侧栏同步。

### 5. PR0 —— 两条 bug

- **Bug A**:`PlaygroundTab.tsx:134-140` 改为 `readPromptJinja(r.spec)` / `readPromptVariables(r.spec)`;测试 fixture 改成真实形状(`record.spec = { apiVersion, kind, metadata, spec: { system_prompt: … } }`);新增回归测试(先按现行代码跑红,再修绿)。
- **Bug B(D1 = 拆干净)**:
  - control-plane:`ManifestPayload` 删 `template_vars` 字段(带该字段的请求 → 422,零使用者;仓库无 CHANGELOG,变更记 PR 说明与设计文档勘误);`ManifestLoader.load_from_string / load_from_path / load_manifest` 删 `template_vars` 参数与 `_render` 步骤,YAML 直接 `_parse_yaml → _validate`;`ManifestTemplateError`(`manifest/errors.py:15`,随之不可达)连同 `api/agents.py:685` 的 `MANIFEST_TEMPLATE` 400 映射、`manifest/__init__.py` 导出一并删(前端与文档零引用,已 grep);**`build_sandboxed_environment` 保留**(`prompt_render.py` 在用)。
  - admin-ui:`api/agents.ts:97` 删字段。
  - `docs/design/jinja-dynamic-prompt.md` §2 补一段勘误(保存时填空已下线,原因)。
  - 测试:loader 测试里的渲染用例改为「`{{ }}` 原样入库」;API 层一条:POST 保存 `jinja:true` + `{{ persona }}` 的 agent → GET 回读 prompt 原样;它与既有 `test_external_run_inputs.py::test_inputs_reaches_prompt_render`、`test_prompt_render.py` 一起构成「保存原样 → run 期渲染」的证据链(中间的 `runtime.get_agent` 构建路径 PR0 不碰)。
- 修完的用户侧后果:双花括号能存;单花括号不再需要(它本来也不替换)—— 用户现有配置要改回 `{{ customer_code }}`。

---

## 三、前端结构

新目录 `apps/admin-ui/src/components/console/`(三页共用):

| 文件 | 职责 | 来源 |
|---|---|---|
| `ConsoleShell.tsx` | 三栏 grid + 响应式折叠 + 右栏 tab 容器 | 新 |
| `SessionSidebar.tsx` | 会话列表(搜索 / 筛选 / 新建 / 改名 / 归档 / 删除 / 加载更多) | 由 `SessionHistoryDrawer` 逻辑迁入 |
| `Transcript.tsx` + `TurnBlock.tsx` + `CompactRow.tsx` + `AnswerBubble.tsx` + `TurnFooter.tsx` | 对话流 | 新;吸收 `TurnCard` 的用户消息 / 答案 / 反馈 / 审批 / 分割线 |
| `PlanCard.tsx` | 任务卡(读 + 编辑) | 由 `run_detail/PlanPanel` 改造 |
| `VariablesForm.tsx` / `Composer.tsx` / `AttachmentChips.tsx` | 输入区 | 由 `PlaygroundTab` 抽出 |
| `StatsBar.tsx` + `useSessionStats.ts` | 状态栏 | 新 |
| `InspectPanel.tsx` | 右栏 tab 壳 | 新 |
| `TrajectoryPanel.tsx` + `LaneStrip.tsx` + `TrajectoryRows.tsx` + `RowDetail.tsx`(五个 tab 子组件) | 轨迹 | `LaneStrip` 由 `GanttTimeline` 改;`RowDetail` 吸收 `StepTimeline` 的详情渲染与 `TraceView` 的 span 详情;`Raw` 复用 `EventCard` |
| `WorkspacePanel.tsx` | 工作区 | 由 `PlaygroundTab.tsx:1015-1246` 抽出 |
| `api/trajectory_rows.ts` | `parseTimeline` → 轨迹行 / 紧凑行 投影(纯函数) | 新 |
| `api/plan_reducer.ts` | `plan` 事件 + `updates.*.plan` 折叠(纯函数) | 新 |

页面:`PlaygroundTab.tsx` 变成组装壳(状态与请求逻辑保留:`ensureThread / startRun / streamRun / 审批续跑 / 上传`);`ConversationDetail.tsx` = 摘要卡 + `Transcript`(只读)+ `InspectPanel`(仅轨迹);`RunDetail.tsx` = 现有头部 + `PlanCard` + `TrajectoryPanel`。

退役(PR3 / PR5 完成后删除):`TurnCard.tsx`、`TraceView.tsx`、`GanttTimeline.tsx`(改造成 `LaneStrip` 后原文件删)、`StepTimeline.tsx`、`SessionHistoryDrawer.tsx`、`run_detail/EventStreamPanel.tsx`、`run_detail/TraceToolbar.tsx`(链接并入 Timing tab)、`playground/TurnMeta.tsx`(脚注吸收)、`playground/EntryBreakdown.tsx`(并入 Timing)。保留:`api/timeline.ts`、`api/turn_summary.ts`、`api/gantt_timeline.ts`、`api/trace_facade.ts`(Timing 来源)、`api/tool_timeline.ts`、`useHistoryTurns`、`useTokenStream`(加 `firstTokenAt / lastTokenAt`)、`ApprovalGate`、`FeedbackBar`、`FullTextModal`、`TaskResultCard`、`HistoryDivider`、`ToolCallCard`(紧凑行展开用)、`AgentStatePanels` 的子面板(进 `RowDetail`)。

i18n:新增 `console.*` 命名空间(zh-CN + en 两份),旧 `playground.*` 键随组件退役逐步删除;新键先查是否撞既有键(同 object 重复键 esbuild 静默覆盖)。

---

## 四、后端与文档改动清单

| 处 | 改动 | PR |
|---|---|---|
| control-plane `api/agents.py` `ManifestPayload` | 删 `template_vars` | PR0 |
| control-plane `manifest/loader.py` | 删 `_render` / `template_vars` 参数 / `ManifestTemplateError`(若无引用);保留 `build_sandboxed_environment` | PR0 |
| control-plane `api/sessions.py` `GET /v1/sessions` | 新增 `order_by=created|last_activity`(默认不变) | PR2 |
| orchestrator `sse.py` | `_publish_plan`;流循环派生;开跑前 `aget_state` 补发 | PR1 |
| protocol `event.py` `EventType` | 加 `PLAN = "plan"`(照 `COMPACTION` 先例:枚举成员与 wire 值对齐) | PR1 |
| docs-site 第 3 章 / 第 10 章 / 侧栏 | `plan` 事件 | PR1 |
| `docs/design/jinja-dynamic-prompt.md` | 勘误段 | PR0 |
| PR 说明 + `docs/design/jinja-dynamic-prompt.md` 勘误 | `template_vars` 下线;`plan` 事件新增(仓库无 CHANGELOG) | PR0 / PR1 |

---

## 五、PR 切分与验收

每个 PR 独立可上线,按 SDD(实施 → 任务评审 → 全分支终审 → 上测试环境 → 记录 PR)。

| PR | 内容 | 验收(全部要有测试或真栈证据) | 量级 |
|---|---|---|---|
| **PR0** `fix(playground+manifest)` | Bug A + Bug B(拆层)+ 端到端测试 + 设计文档勘误 | 真实形状 fixture 下变量框渲染并随 `inputs` 发出;API 保存 `{{ }}` 成功且回读原样;run 渲染替换;带 `template_vars` 的请求 422 | S |
| **PR1** `feat(sse+docs)` | `plan` 事件(派生 + 开跑补发)+ 对外文档 + 死链脚本入仓 | 单测:`update_plan` 后流里出现 `plan` 帧且与 `updates` 快照相等;开跑前有计划 → 第一条业务帧前有 `plan`;帧落库(replay 可重放);文档站 build + 死链 0;真栈探针(pod 内 key,不出集群)三项 PASS | M |
| **PR2** `feat(console)` | 三栏壳 + 左栏会话 + 对话流紧凑行 + 任务卡 + 变量表单 + 状态栏 + 工作区 tab;右栏「轨迹」tab **先原样装现有三档视图**(`TurnCard` 的事件面板抽出来放进去) | 现有 `PlaygroundTab.test.tsx` 覆盖的行为(创建会话 / 发送 / 附件 / 变量 / 审批 / 重试 / 导出 / 只读态)全部迁移到新组件测试并绿;状态栏公式单测;真栈冒烟一轮 | L |
| **PR3** `feat(trajectory)` | 轨迹合一:`LaneStrip` + 行 + `RowDetail` 五 tab(Timing 双来源);退役 `TraceView / GanttTimeline / StepTimeline / EventCard-as-view` | 行投影单测(与 `parseTimeline` 一一对应,含 `update_plan` 合并成一行);Timing 两来源都有的样例;Langfuse `not_ready` 轮询保留;真栈冒烟 | L |
| **PR4** `feat(conversation-detail)` | 对话记录页切到共用组件(只读)+ Runs 表去 + 「立即触发」gate | 现有 `ConversationDetail.test.tsx` 行为迁移;只读态下无任何写按钮可点(含立即触发);跨租户读透传照旧 | M |
| **PR5** `feat(run-detail)` | Run 详情页换 `PlanCard` + `TrajectoryPanel`;退役 `EventStreamPanel / TraceToolbar` | 现有行为迁移;单 run 轨迹与调试台同 run 的轨迹行一致(同 fixture 断言) | M |

顺序:PR0 → PR1 ‖ PR2 → PR3 → PR4 → PR5(PR1 与 PR2 可并行;PR4 / PR5 依赖 PR3)。每个 PR 出实施计划时再用 `writing-plans` 拆任务;本文档是总纲。

---

## 六、刻意不做(理由已核对)

- **不加 `plan` 以外的新 SSE 事件**(如 usage 汇总帧):状态栏全部能从现有 `updates` / `usage_metadata` 前端算出(见状态栏表),精确值走 Langfuse。
- **不给 `token` 帧加服务端时间戳**:tok/s 用客户端时钟近似即可,标注「≈」;`token` 帧是临时帧不落库,加字段收益小。
- **不做轨迹拖选 / 搜索索引**:deepseek 有,但我们按轮的行数少(几十行量级),不值一个 3 s 节流索引。
- **不做「按会话」长轨迹**:D4 拍了按轮;会话累计由状态栏兜。
- **不给对话记录页加编辑计划**:它是运维视角、他人会话,`PUT /plan` 本就 404。
- **不做旧版切换开关**:平台未上生产;PR2 整体替换,测试环境验收。
- **不动 `updates` 契约**:第三方文档「忽略其他键」措辞不变,`plan` 走独立事件。

---

## 七、风险

- **PR2 是一次性替换 1471 行页面 + 2438 行测试**:靠「行为清单迁移」兜底 —— 出计划时先从现有测试文件抽出行为清单(每个 `it` 一条),新测试逐条对号。
- **`aget_state` 开跑前多一次 checkpoint 读**:失败必须只记日志;真栈量 TTFT 前后对比(`_session_first_node_seconds`)。
- **同一 `update_plan` 在轨迹里两条→一条的合并**依赖「tools 节点值里的 `plan` 与前一 agent 步的 `update_plan` 调用」配对;并行工具批次里有多个 `update_plan` 时按原顺序最后一个赢(后端 `accumulated_state` 同语义)。测试要覆盖。
- **Langfuse 双来源 Timing**:行 ↔ span 的配对靠工具名 + 顺序(现有 `trace_facade` 归一);配不上就只显示 SSE 列,不报错。

## 八、修订 2026-08-18(PR-A 上测试环境后的反馈)

PR-A(#1207)上线后用户在测试环境过了一遍,六条反馈全部采纳,形态修订如下(设计稿已确认;实施 = PR-A.1,计划 `docs/superpowers/plans/2026-08-18-debug-console-pr-a1-feedback.md`)。**与 §二.1 冲突处以本节为准。**

1. **壳与左栏**:三栏壳高度不再写死 `100vh - 360px`,按壳自身在视口里的实际位置铺满到底(`useLayoutEffect` 量 top,写成 CSS 变量)。左栏会话条目标题单行省略占满整行;改名 / 归档 / 删除三个图标 hover 时浮在条目右上角(绝对定位),不再常驻占位;搜索框独占一行,「活跃 / 已归档」下移一行。
2. **会话级状态栏**:从中栏底部搬到中栏**头部下方**一条细行,每个数字一枚小芯片(轮数 · 步数 · LLM · 工具 · 首 token · 速度 · 缓存命中 · 入 · 出 · 费用),`flex-wrap` 自动换行,永不截断;底部整行取消。
3. **过程条**(替代 §二.1「若干紧凑行」直接堆在用户气泡下的形态):每轮的思考 / 工具 / 计划 / 记忆 / 反思 / 标记等紧凑行收进一个「过程」条。**运行中**自动展开,只显示最近 3 步 + 正在进行的一步(带转圈),更早的折进「还有 N 步…」;**完成后**默认折叠成一句摘要「思考 3 次 · 工具 5 次(web_search ×4 · http ×1)· 1m23s」,有失败时末尾加红色「1 次失败」;点开可看全部行,每行右侧「轨迹」跳右栏对应行;展开 / 折叠状态每轮各自记住。中栏不再背两份过程——深看去右栏。
4. **脚注一行式**:左 = 状态 tag + 紧凑摘要(合计 token · 步数 · 耗时 · 模型;hover 看输入 / 输出 / 缓存 / 思考 / 费用 / finish_reason 拆分);右 = 👍👎 · 重试 · 导出 · **查看轨迹**(替换原「检查」+ 插头图标)。「查看运行」从脚注移走(见 6);「停止」不进脚注(见 5)。
5. **输入区**:文本框 2 行起自动长高(≤ 8 行);运行 / 图片 / 文档按钮与字数提示同一行;**运行中「运行」按钮原位变红色「停止」**(不再并排两个按钮);去掉「完整的 SSE 事件流会显示在右侧」占位文案。
6. **右栏头部**:「第 N 轮 · 状态 · N 个工具 · 总耗时」+ 「Run 详情 ↗」链接(原脚注「查看运行」的新家)+ Langfuse 链接(admin)+ 投影切换 **顺序 / 时长**(记在 localStorage)。
7. **泳道重做**(参照 deepseek-harness `ui-trajectory` 的 `timeline.ts` 投影模型,只抄交互):三条 8px 细泳道(用户 / 模型 / 工具)带标签;**每条轨迹行都有一个块**(user / think / assistant / tool / subagent / plan / memory / reflect / marker),泳道按行种类分配;**顺序模式**(默认)每条记录等宽按发生顺序排,并行工具自然分开;**时长模式**按真实起止时间画(沿用现有 gantt 时序);失败块红色;hover 提示「类型 · #序号 · 摘要 · 起止 · 耗时」并高亮表中对应行(反向:悬停表行块描边);点击块 = 选中行 + 打开详情;在泳道上横向拖选一段 → 段外块变淡、下表只剩段内行、上方出现「已筛选 #a–#b(n 条)」芯片,点 ✕ 或双击泳道复位;下方一条刻度(顺序:#k;时长:秒);运行中当前块呼吸闪烁。模型块的「首 token 前 / 解码」双色留待后端给出每步 TTFT 后再做(本次数据没有)。
8. **行表加列 + 详情修边**:行列表改成表格列 `# / 类型 / 摘要 / 入 / 出 / 思考 / 耗时`(think 行填三列 token,其它行留空;`思考` = `usage_metadata.output_token_details.reasoning`,`parseTimeline` 加可选字段透传);选中行高亮并与泳道块联动;`↑ ↓` 移动、`Esc` 关详情。行详情面板加内边距(修掉左侧被裁一截的 bug),头部改成「#序号 · 类型 · 摘要 · 耗时」。

**明确不做**:滚轮缩放 / 平移;对话记录页 / Run 详情页(PR-B);`playground.*` → `console.*` 改名。

## 九、修订 2026-08-19(轨迹视图对齐 deepseek-harness `ui-trajectory`)

PR-A.1(#1214)上测试环境后,用户看了泳道后的反馈是「没有实现这种划定指定区域的效果」,继而拍板:「其实我就想把我们的轨迹跟 deepseek-harness 做成一样」。设计稿(整屏 + 六个交互样例 + 逐项对照 + 六条拍板)已确认。**与 §二.1「右栏 · 检查面板」及 §八.6–8 冲突处以本节为准。** 实施 = PR-A.2,计划 `docs/superpowers/plans/2026-08-19-debug-console-pr-a2-trajectory.md`;PR-B(对话记录页 / Run 详情页)顺延到它之后。

参照物:`/Users/mac/src/github/deepseek-harness/packages/client/ui-trajectory/src/client/`(`TrajectoryView.tsx` / `TrajectoryToolbar.tsx` / `TrajectoryTimeline.tsx` + `timeline.ts` / `TrajectoryTable.tsx` + `*.module.css`)。**只抄交互与观感,不抄实现**;它 7900 行、虚拟化 + 分页 + 插件槽位,我们按自己的数据层重写。

### 1. 拍板六条(用户 2026-08-19 逐条同意)

- **D1 放哪**:轨迹从右栏搬到中栏,中栏头部三个视图 tab「对话 | 轨迹 | 工作区」;右栏「检查面板」(`InspectPanel`,轨迹 | 工作区 Segmented)**整个退役**,壳变两栏(会话侧栏 | 主区)。左栏照旧。
- **D2 一步一条 ASSISTANT**:轨迹里每个 agent 步一条 `ASSISTANT` 记录(文字 + 思考 + 它发起的工具调用),原「每步一条 THINK 行 + 最后一条 ASSISTANT 行」退役;思考进详情「预览」的折叠段。中栏过程条的 THINK 紧凑行**不变**(那是另一个投影)。
- **D3 账本去四列**:账本只有「事件槽 + 内容」两列(与 deepseek 一致);`入 / 出 / 思考 / 耗时` 四列删除,数字进「请求 #N」详情与悬停提示。
- **D4 滚轮缩放 + 右键平移**:做(撤销 §八「明确不做」那条)。
- **D5 三处缺数据本期不做,另开 PR-A.3 补齐**(用户 2026-08-19 追加拍板「要做的」):`Schema` tab(控制面没有工具 JSON schema 接口 → 加 `GET /v1/agents/{name}/{version}/tools`)、`SYSTEM` 行(系统提示词只在 Langfuse span 输入里 → orchestrator 每 run 开头发一帧 `system_prompt`)、模型块 TTFT / Decoding 双色(后端没有每步首 token 时刻 → `updates` 帧带 `_first_token_ms`)。PR-A.3 = 后端三帧 / 接口 + 前端三块 UI,排在 PR-A.2 之后、PR-B 之前。
- **D6 顺序**:PR-A.2 先于 PR-B。

### 2. 形态

**壳**:`ConsoleShell` 两栏 `264px | 1fr`(<1200px 左栏折图标条照旧),`inspect` 列取消。主区头部一行:会话 id · 视图 tab(`console-view-tab-chat / -trajectory / -workspace`,antd `Segmented`)· 轮数;下方 §八.2 芯片行不动;再下方按 tab 渲染「对话」(现 `Transcript`)/「轨迹」(新 `TrajectoryView`)/「工作区」(现 `WorkspacePanel`)之一;输入区(计划卡 / 变量 / 附件 / `Composer`)**三个 tab 下都钉在底部**——轨迹 tab 里照样能发送 / 停止,账本与时间轴实时长。每会话记住上次 tab(内存态,切会话回「对话」)。

**轨迹视图 = 工具条 + 概览时间轴 + 账本(左)+ 详情(右,可拖宽)**,整个会话、按轮分段、账本行序即事件序。

**工具条**(sticky,28px):左「◷ 时长」(`aria-pressed`,记 `localStorage["expert_work.console.lane_mode"]`,沿用)· 「⊟ 轮次」(全部折叠 / 展开轮;全折时图标 ⊞)· 「⊟ 调用」(折叠 / 展开每条 ASSISTANT 下的工具调用);右搜索框(实时,大小写不敏感子串,匹配「类型标签 + 内容 + 结果 + 工具名」;**有查询时账本只剩匹配行**,时间轴不匹配块 0.14 透明)。

**概览时间轴**(50px;44px 标签列「输入 / 模型 / 工具」+ 轨道):
- 记录 → 块:8px 高、泳道纵向 14px 步进、`min-width 2px`、块间 `min(8%, 1px)` 缝;泳道分配:输入 = USER / MEMORY(召回);模型 = ASSISTANT / REFLECT / COMPACTED;工具 = TOOL / SUBTOOL / PLAN / MEMORY(写回)/ 标记行。颜色按类型(下表),失败一律 danger;运行中的尾块呼吸(`prefers-reduced-motion` 关)。
- 投影:**顺序**(默认)每条记录等宽 `[i, i+1)`;**时长**按绝对起止毫秒(每轮 `buildGanttRows` 的绝对时序:新增 `GanttModel.originMs`,`absStart = originMs + startMs`;USER 记录钉本轮首条有时序记录之前;记录之间的空档压掉——deepseek `compressIdle`),运行中的尾块长到「现在」;任何一轮拿不到时序 → 整条时间轴退化成顺序排布并在工具条「时长」旁标「时长不可用」。
- 轮边界:每轮首条记录处一条 1px 竖线。
- 悬停:块 → 500ms 后提示「类型 / 起止钟点(时长模式)/ 总时长」+ 账本对应行同步高亮(反向:悬停账本行 → 块描边);轨道空白处 → 一条 2px 竖线跟随鼠标。
- 选区:左键按下拖动 → 草稿选区(18% 底 + 两端 2px 竖条);松手位移 ≥ 3px → 定格(12% 底 + 3px 竖条 + **选区外整片压暗 58%**);选区含义 = 与区间有交集的全部记录,段外块 0.2 透明、账本段外行 0.24 透明(不隐藏)并把段内首行滚进视口;点空白处 = 以点击位置为中心开「一条记录宽」的最小选区并把最近记录滚进视口;点块 = 选中记录 + 打开详情 + 清选区;`Esc` / 双击 / 右键单击 = 清选区;缩放态下拖到轨道边缘 8% 内自动平移。
- 缩放 / 平移:滚轮 = 以鼠标位置为锚缩放(顺序模式最小 4 条记录宽,时长模式最小 20ms;缩到 ≥ 99.9% 全景自动回全景);右键按住拖 = 平移(仅缩放态);选中账本里视口外的记录时视口 180ms 平滑挪过去。
- 历史未加载完:轨道左端「…」按钮(渐变底)= 加载上一页。

**账本**(表格,两列 `122px | 1fr`,行高 27px 固定,虚拟化只挂视口 + overscan 12 行):
- 事件槽(左列,`padding-left 36px`):轮起点一枚「第 N 轮」标签(左上角,mono 8px;当前轮高亮);当前轮 2px 竖轨、选中行 3px 竖轨(失败红);每次 LLM 请求一枚圆点(`top:-8px` 跨在请求首行上方;失败红、当前蓝、悬停放大 + 提示「请求 #N · 第 N 轮 · 第 M 步」;点开请求详情);类型标签(76px 槽右对齐,10px 粗体,颜色见下表)。
- 内容(右列,单行省略):USER = 输入首行;ASSISTANT = 该步文字首行,没文字写「(仅工具调用)」;TOOL / SUBTOOL / PLAN = `名字 参数JSON` mono + ` → ` + 结果首行(失败红,「(无输出)」灰);MEMORY = 「召回 N 条 → 首条摘要」;REFLECT = 「pass / revise → 评语首行」;COMPACTED = 压缩摘要;标记行 = 文案。
- 轮起点 2px 粗分隔线;折叠轮 = 一行「… 思考 N 次 · 工具 N 次(…)· 时长 · N 次失败」(复用 `process_summary`);折叠调用 = ASSISTANT 行下一行「… bash ×3 · read_file ×2」;双击行 = 折叠所在轮 / 折叠该 ASSISTANT 的调用;`Enter/Space` 同单击,`↑ ↓` 在可见行间移动,`Esc` 关详情;选中行底色 + 竖轨,悬停行底色。
- 尾随:初始与运行中跟到最新;上滚离底 > 80px 暂停跟随。首行「加载更早的历史(还有 N 轮)」按钮,一页 20 轮(`useHistoryTurns.loadRuns`),加载中禁用带转圈。
- 未回放的轮(`loadState !== "done"`)只出 USER + 一条占位 ASSISTANT(内容取 fallbackLines 首行,状态「加载中 / 回放失败」)。

**类型 → 标签 / 颜色 / 泳道**

| 记录 | 标签 | 颜色 token | 泳道 |
|---|---|---|---|
| user | USER | `--ew-color-brand-300` | 输入 |
| memory(recall) | MEMORY | `--ew-color-success-500` | 输入 |
| assistant | ASSISTANT | `--ew-accent-violet` | 模型 |
| reflect | REFLECT | `--ew-color-accent-400` | 模型 |
| compaction | COMPACTED | `--ew-text-secondary` | 模型 |
| tool | TOOL | `--ew-color-warning-500` | 工具 |
| subagent | SUBTOOL | `--ew-color-warning-700` | 工具 |
| plan | PLAN | `--ew-color-teal-500` | 工具 |
| memory(writeback) | MEMORY | `--ew-color-success-500` | 工具 |
| retry / error / approval / guard / gap | RETRY / ERROR / APPROVAL / GUARD / GAP | `--ew-text-secondary` | 工具 |
| 任一失败 | — | `--ew-color-danger-500` | — |

**详情**(右侧 `aside`,默认 420px,可拖 320–720,双击手柄复位,`← →` 步进 16px;头部 = 类型标签 + 「第 N 轮 · 第 M 步」+ ✕):
- TOOL / SUBTOOL / PLAN / MEMORY / REFLECT / 标记:`概要 / 载荷 / 结果 / 计时 / 原始`。
- ASSISTANT:`概要 / 预览 / 原文 / 计时 / 原始`(预览 = Markdown 正文,上方「▸ 思考(N tokens)」折叠段;原文 = 思考 + 正文的 `<pre>`)。
- USER:`概要 / 预览 / 原文 / 原始`。
- 请求 #N(点圆点):`概要 / 输入 / 用量 / 计时`(输入 = 该步 Langfuse span 的渲染消息,没配到 span 显示现有「只在 Langfuse 精确轨迹里有」文案;用量 = 本次 输入 / 输出 / 思考 / 缓存读 + 累计至此 输入 / 输出;计时 = 现有 `RowDetailTiming` 两列)。
- 「概要」= 顶部 `dl`(层级链接:ASSISTANT →「请求 #N ›」;TOOL / PLAN / MEMORY 写回 →「Assistant Message ›」;SUBTOOL →「Tool Call ›」;+ 状态 + 耗时 + ASSISTANT 的 模型 / 输入 / 输出 / 思考 / 缓存读 + 「Run <id> ↗」链接(`console-inspect-run-link`,原右栏头部的新家)+ Langfuse ↗(admin))+ 分节预览(载荷 / 结果 / 计时 各一段,标题带「›」跳对应 tab,预览区 120px 封顶渐隐)。
- 计时 tab 沿用现有 `RowDetailTiming`(会话时间戳 vs Langfuse 精确两列、入库中轮询、成本);原始 tab 沿用 `EventCard`。
- 换记录不重置当前 tab(该记录没有这个 tab 时回「概要」)。

**联动**:中栏脚注「查看轨迹」→ 切轨迹 tab + 选中该轮最后一条 ASSISTANT 记录 + 滚到位;过程条每行「轨迹」→ 切轨迹 tab + 选中对应记录(`think:<seq>` 映射到 `assistant:<seq>`,其它 id 同名);目标轮在加载窗口之外时先扩窗口再选。

**退役**:`InspectPanel` / `TrajectoryPanel` / `LaneStrip` + `lane_strip_model` + `lane_strip.css` / `TrajectoryRows` + `trajectory_rows.css` / `RowDetail`(拆成新的 `RecordDetails` / `RequestDetails`,`RowDetailPayloadResult` / `RowDetailTiming` 保留复用)/ `RunStatusBanner` 在轨迹里的用法(错误已经是红行红块;该组件随后由 PR-B 连 TurnCard 一起清)。`api/trajectory_rows.ts` 的旧 `trajectoryRowsOf(events, input, answer, status)` 与 `resolveGanttKey` 的 think 分支随之删除。

**明确不做(本节)**:Composer 浮层(钉底部即可);对话记录页 / Run 详情页迁移(PR-B);`playground.*` → `console.*` 改名。Schema tab / SYSTEM 行 / TTFT 双色 → PR-A.3(见 D5)。

## 十、PR-A.3 轨迹补数据(2026-08-19 追加;D5 的兑现 + 两条真栈发现)

PR-A.2(#1216)上测试环境后,§九 D5 留下的三处「缺后端数据」用户拍板「要做的」;同一次真栈冒烟又逮到两条既有后端 bug(Langfuse 计时 ×1000、span 乱序配对)。五件事合成 PR-A.3,排在 PR-B 之前。**§九 的形态不变,这一节只加数据与三块 UI。**

### 10.1 SYSTEM 行(系统提示词帧)

- **数据**:orchestrator `run_agent` 在 `metadata` 帧之后、TTFT 计时起点之前,发一帧 `event: system_prompt`,`data = {"text": <最终喂给模型的 system prompt 全文>}`。只在 `graph_input` 带 `SystemMessage` 首条时发(新 run);resume / 审批续跑(`graph_input=None` / `Command`)不发。帧照常落库(回放可见)。
- **平面**:**只给控制台平面**。对外 API(第三方 API key)的实时流与回放都把 `system_prompt` 帧滤掉 —— 系统提示词属于管理面产物,对外平面只给「跑 agent」的能力(见 `external-api-third-party-scope`)。对外文档站不改(它们看不到这帧)。
- **账本**:每轮若有该帧,轮首多一条 `SYSTEM` 记录(在 USER 之前,`id = system`,泳道「输入」,标签 `SYSTEM`,颜色 `--ew-text-secondary`);内容列 = 提示词首行。**相邻轮系统提示词相同就折掉**:同一加载窗口内只在第一轮、以及提示词**变化**的轮出现 —— 跟 deepseek-harness「一条轨迹开头一个 SYSTEM」的观感一致,又不把 50 轮同样的提示词刷 50 遍。
- **折叠 / 计数**:SYSTEM 与 USER 同属「上下文行」:轮折叠时仍显示、不计入「其它 N 步」、不计入「值得折叠」的非 USER 记录数、双击落点同 USER。这个谓词只写一处(`ledger_collapse.CONTEXT_KINDS`),账本 / 折叠 / 双击都引它。
- **详情**:`概要(字数 + Run 链接)/ 原文(全文 <pre> + 复制)/ 原始`。
- 中栏对话视图、过程条、Gantt **不变**(`parseTimeline` 不认这帧)。

### 10.2 Schema tab(工具 JSON Schema)

- **数据**:新端点 `GET /v1/agents/{name}/{version}/tools`(控制台平面,`manifest:read`,与 `GET /{name}/{version}` 同闸同审计)。用与跑 run 完全相同的 `runtime.get_agent(...)`(有 LRU 缓存)拿到 `BuiltAgent`,返回它**整个工具注册表**(含延迟挂载的):`{"items": [{name, description, parameters, source, from_skill, deferred}], "total": N}`;`parameters` 就是喂给模型的 JSON Schema(`ToolSpec.parameters`)。构建失败 → 422(与 `run_agent` 同码)。**不起 run**。
- 为此 `BuiltAgent` 新增 `tool_catalog: tuple[ToolCatalogEntry, ...]`(零行为改动)。顺手把租户侧 `GET /v1/mcp-servers/{name}/tools` 补回丢掉的 `input_schema`,与平台侧 `mcp_catalog` 对齐。
- **UI**:TOOL 记录与 PLAN(`update_plan`)记录的详情多一个 `Schema` tab(在「结果」与「计时」之间):描述 + 来源(builtin / mcp:<server> / skill:<name>)+ 「延迟挂载」标记 + JSON Schema `<pre>`(可复制)。**懒加载**:第一次打开需要它的记录才请求一次,之后整个会话复用;加载中 / 失败(可重试)/ 当前工具集里没有这个名字,三态各一句。
- 不做:ASSISTANT 记录展示「本步喂给模型的工具清单」(那是另一件事)。

### 10.3 模型块 TTFT / Decoding 双色

- **数据**:`agent_node` 每次 LLM 调用记起点,`TokenSink` 记第一个**非空** delta(content / reasoning / tool_calls 任一)的时刻,调用返回后把差值写进 `AIMessage.additional_kwargs["first_token_ms"]`(与工具那一路 `ToolMessage.additional_kwargs["duration_ms"]` 同款信道)→ 随 `updates` 帧落库、回放可见。没 sink(judge 开着 / 无 publish)或厂商不流式 → 不写。
- **UI**:时长 / 顺序两种模式下,ASSISTANT 块都按 `first_token_ms / 步时长` 的比例分两段:前段(首 token 前)同色 40% 透明,后段(解码)实色;失败块仍整块红。悬停提示多一行「首 token 1.2s」;「请求 #N」概要多一行「首 token」。
- 不做:会话级状态栏的「首 token」芯片仍走客户端计时(它量的是「点发送到看见第一个字」,与每步 LLM 首 token 不是一个量)。

### 10.4 Langfuse 计时两处修正(既有 bug)

- `trace_facade.py` 把 Langfuse `observation.latency` 当秒 ×1000,测试集群这版 Langfuse 返回毫秒 → 计时 tab「121m5s」。改成有 `start_time`/`end_time` 就用差值,`latency` 只作兜底。
- 返回的 spans 按 Langfuse 原序(非时间序),前端 Rule 2 按数组顺序配对 → 第 1 步 ASSISTANT 配到第 2 步 LLM span。后端按 `startMs` 稳定排序再返回;前端 Rule 2 配对前也排一遍(防御)。

### 10.5 类型表增补

| 记录 | 标签 | 颜色 token | 泳道 | 详情 tab |
|---|---|---|---|---|
| system | SYSTEM | `--ew-text-secondary` | 输入 | 概要 / 原文 / 原始 |
| tool / plan(update_plan) | —(不变) | —(不变) | —(不变) | 概要 / 载荷 / 结果 / **Schema** / 计时 / 原始 |
