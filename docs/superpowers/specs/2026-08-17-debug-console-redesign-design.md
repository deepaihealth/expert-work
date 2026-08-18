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
