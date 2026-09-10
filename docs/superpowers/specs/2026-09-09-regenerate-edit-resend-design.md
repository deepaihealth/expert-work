# 对外「重新生成」与「编辑重发」—— 设计(P-1)

> 2026-09-09 用户拍板逐条:B 方案(同会话原地标「已被取代」)/ 副作用不撤销 / 计划回退 / 会话有 run 在跑 → 409 / 被取代轮等审批 → 409 / 同一轮旧版本最多留 5 份 / 两轮都计费 / 对外文档随 PR 同步。
> spike(`scratchpad/spike_p1_supersede.py`,真 Postgres checkpointer + 真 graph,5/5 过,含 2 反证)已证三个存亡判据成立,结论并入 §3。
> 读者:实施工程师 + 用户审阅。术语:**轮** = 一次 run(一条用户输入 + agent 的全部回应);**被取代** = 该轮仍在历史里,但 agent 后续看不见、界面划掉。

## 1. 目标 / 非目标

**目标**
- 对接方界面上,终端用户可对最近一轮点「重新生成」(同一输入再跑一次)或「编辑重发」(改输入后再跑一次)。
- 被取代的轮在所有读面可见且标 `superseded_by`;agent 新一轮的上下文里**不含**被取代轮的任何消息与计划。
- 审计可回溯最近 5 个被取代版本。

**非目标**
- 分支/版本切换器(ChatGPT 式「< 1/2 >」)—— A 方案,以后需要再加锚点列升级。
- 撤销被取代轮的副作用(工作区文件、产物登记、长期记忆、技能提升记录、委派记录)。
- 退还被取代轮的 token 费用。
- 对非最后一轮做重新生成(编辑重发只允许对**最后一轮**的用户消息;更早的轮 = 分支语义,不做)。

## 2. 现状(行号以 main `723c5a40` 为准)

- 会话真相 = LangGraph 检查点 `messages` 通道(`add_messages` 追加型 reducer,`services/orchestrator/src/orchestrator/state.py:183`);SQL `thread_message` 只是搜索镜像,`seq` = 通道下标,`ON CONFLICT (thread_id, seq) DO NOTHING`(`packages/expert-work-persistence/.../models/thread_message.py:22-44`)。
- run 行 `agent_run` 只有 `thread_id`、`is_resume`;没有 parent / superseded 字段(`.../models/agent_run.py:27-108`)。
- run ↔ 消息靠消息戳 `expert_work_run_id`(`packages/expert-work-common/src/expert_work/common/message_stamp.py:21-34`);**ToolMessage 与每轮的 SystemMessage 没有戳**(spike 实测)。
- 读面全部读最新 checkpoint:`transcript.py:93-111`(`read_messages`)→ `/messages`(`api/external_sessions.py:211-280`)、`/items`(`api/external_session_items.py:427-436`)、控制台 `/v1/sessions/{id}/messages`(`api/runs.py:1659-1690`)、`thread_meta.message_count`(`thread_stats.py:32-45`)、镜像 sweep。
- run 创建:`spawn_run`(`api/runs.py:953-1206`)每次建 run 行 → `build_run_graph_input`(`:455-504`,System+Human)→ `run_agent`;config 从不带 checkpoint_id。审批续跑 = `aupdate_state` 写裁定 + `graph_input=None` 起新 run(`:857-926`)—— 已验证的「先改检查点再正常跑」写入缝。
- **没有 per-thread run 互斥**:全仓无端点因「该会话已有 run 在跑」拒绝新 run;`api/plan.py` 有 `_WRITE_BLOCKED_STATUSES` → 409 的先例。
- token 记账无 run_id 列(只有 trace_id),不可能按轮回滚。
- 检查点 metadata 带 `run_id`(langchain `ensure_config` 复制 configurable 标量;spike 实测 keys = `parents, run_id, source, step, tenant_id`),`aget_state_history(filter={"run_id": r})` 可服务端过滤(**但慢得不能用在热路径上,见 §8-2**)。

## 3. 机制(B 方案,spike 已验)

### 3.1 supersede 一轮(内核函数 `supersede_run`)
输入:`thread_id`、目标 `run_id`(必须是该会话**最后一轮**)、新 `run_id`。
1. **并发闸**:per-thread advisory lock(照 `trigger_delivery.py:59-75`);会话有 run 处于 `_WRITE_BLOCKED_STATUSES`(running/pending/paused…)→ 409。
2. **定轮边界(取法 A)**:~~`aget_state_history(cfg, filter={"run_id": target})`~~ **一条两列轻 SQL**(§8-2 实测:`aget_state_history` 在最长会话上要 810–950 ms / 52 MB,不能进热路径)→ 该 run 最早 checkpoint 的 `parent_checkpoint_id` → `aget_state(parent)`:`len(messages)` = 该轮起始下标 `s`,`plan` = 回退值 `plan_before`;目标 run 最新 checkpoint(`aget_state(cfg)`)的 `len(messages)` = 结束下标 `e`。**不靠消息戳划轮**(Tool/System 无戳)。
3. **一次 `aupdate_state(values={"messages": copies, "plan": plan_before}, as_node="agent")`**:`copies` = 下标 `[s, e)` 的每条消息按**原 id** 复制,`additional_kwargs["expert_work_superseded_by"] = new_run_id`、`["expert_work_superseded_at"] = now`。reducer 同 id 原地替换:条数、下标、内容不变(spike 反证:副本无 id → 被当新消息追加)。**`as_node` 必须是 `"agent"`**(`"__start__"` 会让 `next=("agent",)`,orphan sweep / 审批续跑会把被取代轮再跑一次;`None` 靠运气)。
4. 同一事务:`agent_run.superseded_by_run_id = new_run_id`(迁移 0153);`thread_message` 镜像**显式更新**该 `seq` 范围的 `superseded_by`(镜像 `DO NOTHING` 永远学不到标记,spike 实测);`thread_meta.message_count` 重算。
5. **留 5 份**:同一起始下标 `s` 的被取代版本数 > 5 时,最老版本的消息**内容置为墓碑**(`content=""`、`additional_kwargs["expert_work_tombstone"]=true`,保留 id 与下标)—— **不用 `RemoveMessage` 真删**,真删会让后续下标左移、镜像 `seq` 错位。
6. 若目标轮结束在 PAUSED:直接 409(拍板);不做「清 `pending_approval` 再 supersede」。

### 3.2 构图侧整轮过滤
- `builder.py:656`(`messages = list(state["messages"])` 之后)加 supersede 过滤,**放在 working_window 之前**(否则被取代轮占窗口预算)。
- **按轮整段剔除**,不按条:一轮的 AI(tool_calls) 与其 ToolMessage 必须同进同出,否则孤儿 tool_call 厂商 400。分组规则与 `/items` 的 `_group_messages_by_run`(`external_session_items.py:104-130`)下沉为同一个共享函数(`expert-work-common`),两处只认它。
- 墓碑消息同样剔除。

### 3.3 新一轮
- `spawn_run` 加 `supersedes_run_id` 参数:先 `supersede_run`,再照常建 run 行、`build_run_graph_input`、`run_agent`。`:regenerate` 用目标轮的原输入(从被取代轮的 HumanMessage 取,含附件引用);`:edit` 用新 `input`。
- 新 run 行加 `regenerated_from_run_id`(迁移 0153 同批)。

## 4. 对外 API

- `POST /v1/agents/{code}/runs/{run_id}:regenerate`、`POST /v1/agents/{code}/runs/{run_id}:edit`(与 `:cancel` 同族,`api/external_runs.py:169`)。
  - 权限 `external_only()` + `require("session","write")`;`load_owned_run` 404 不泄露存在性。
  - body:`{ user_id, mode: "stream"|"queue", stream_format?, input? (仅 :edit 必填), files?: [{upload_id}] }`;支持 `Idempotency-Key`(重发不产生第二条 run);`on_disconnect=CONTINUE` 与普通 run 一致。
  - 响应与 `POST …/runs` 完全一致(stream 模式直接是 SSE;queue 模式 202 + run_id)。
  - 错误:`RUN_NOT_LAST`(422,目标不是最后一轮)/ `THREAD_BUSY`(409,有 run 在跑)/ `RUN_AWAITING_APPROVAL`(409,目标轮等审批)/ `RUN_ALREADY_SUPERSEDED`(409)/ `RUN_INPUT_UNAVAILABLE`(422,仅 `:regenerate`,目标轮没留下可重放的输入 —— 计划新增,PR3 落地)/ `RUN_BOUNDARY_UNRESOLVED`(422,这一轮的下标区间划不出来:链首 checkpoint 不是 `source="input"`,历史损坏或审批链没串全 —— **2026-09-10 拍板新增**,PR3 落地)。进 `docs-site/guide/errors.md`。
  - `RUN_BOUNDARY_UNRESOLVED` 为什么不能并进 `RUN_NOT_LAST`:那一支里目标**就是**最后一轮,而 `RUN_NOT_LAST` 给对接方的处置是「去 run 列表取最新的 `run_id` 再试」,照做会拿回同一个 id、同一个 422,反复查列表反复重试也绕不出去;真实原因与「哪一轮」无关,处置是换一段新会话或联系排查。
- 读面:`/messages` 每条、`/items` 每个条目与 `runs[]` 加 `superseded_by: run_id | null`;`runs[]` 加 `regenerated_from: run_id | null`;墓碑条目 `tombstone: true` 且无 content。**字段始终出现,缺省 `null` / `false`**(与 P-2 的 `feedback: null` 同一约定;对接方已确认无 strict 解析 —— 09-09 计划评审改口,原写「缺省不出现」)。
- SSE **无新帧**;新一轮的帧序与普通 run 相同。
- 三张手工路由表登记(`test_external_only_gate.py:66` / `test_console_lockdown.py:251` / `test_external_path_param_nul_guard.py:502`)。
- 文档:`chat.md`(新节「重新生成与编辑重发」,明写不撤销副作用、两轮都计费、只对最后一轮)、`sse-events.md`(无新帧但说明 `superseded_by`)、`query.md`(字段表)、`errors.md`、`examples.md`、`best-practices.md`。

## 5. 控制台
- 对话详情页把被取代轮画成折叠态(标「已被取代 → 新轮链接」),墓碑轮显示「内容已清理」。
- 调试台不提供重新生成入口(对外功能;调试台有草稿试跑)。

## 6. 已知不回退、已接受(文档明写)
- 工作区文件、产物登记、长期记忆写回、`promoted_tools`(union 通道)、`subagent_invocations` / `reflections`(add 通道)。
- ~~压缩摘要:若压缩器已把被取代轮摘要进 `<context-summary>` 消息,标记撤不回摘要内容 —— **实施前核实摘要是否落检查点**(§8-1);若落,supersede 时对摘要消息同样打标并剔除,让下一轮重新压缩。~~ **✅ 09-09 核实后取消(见 §8-1)**:摘要只活在单次 prompt 视图里,从不落检查点,所以没有「撤不回的摘要」这回事 —— supersede 不需要对摘要做任何事,下一轮从原始历史重压时被取代轮已被剔除。

## 7. PR 切分与验收

**PR1 内核 + 读面**(共享分组函数下沉 common;`supersede_run`;迁移 0153 两列(0152 归 P-2 反馈表,P-2 先合);`extract_turns`/`visible_turns` 的 `include_superseded` 开关;镜像与 message_count 更新;墓碑上限 5)
- 验收:supersede 后 `/messages`、`/items`、控制台消息接口三者对被取代轮一致标记且内容仍在;`message_count` 与 `/messages` 同口径;第 6 次 supersede 最老版本变墓碑、下标不变、镜像 seq 不变;拿掉写入 → 红。

**PR2 执行侧**(builder 整轮过滤 + 变异自证;`spawn_run(supersedes_run_id=)`;per-thread 409 闸;PAUSED 409)
- 验收:**新 run 的模型上下文不含被取代轮文本与计划**(用会复述上下文的探针 agent 验,不看日志;拿掉过滤 → 红);必调工具的 agent 编辑重发后厂商不 400;在飞 run 时 409;两副本并发 supersede 同一轮只成一条;PAUSED → 409。

**PR3 对外端点**(`:regenerate` / `:edit`、Idempotency-Key、错误码、三张表、`/messages` `/items` 字段)
- 验收:同租户他人 run → 404 external 信封;非最后一轮 → 422;Idempotency-Key 重发不产生第二条 run;计费两条 run 的 `token_usage` 都在(明确不回滚)。

**PR4 文档 + 控制台**(六页文档、对话页折叠态、runbook 一句)。

真栈验收用探针 user(`pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation`)+ 金丝雀 agent,不碰对接方 agent。每条新断言按仓库规矩「实现改坏 → 红 → 改回 → 绿」。

## 8. 实施前要核实(第一天做,回填本文)
1. ~~压缩摘要是否落检查点、是否带 run 戳(§6)。~~ **✅ 已核实(09-09)**:**不落检查点,也没有 run 戳**。压缩只重绑 `agent_node` 里的本地 prompt 变量(`builder.py:829`);两个返回字典 `update_mw`(`:1206`)/ `update_plain`(`:1246`)的 `"messages"` 只装 LLM 回复(+ advisory / dispatch),`_extract_post_llm_messages`(`:2397`)返回的是「超出原 prompt 前缀的尾巴」。全仓只有四处写 `ctx.payload["messages"]`,挂 `after_llm_call` 的只有 `loop_detection`(`middleware/loop_detection.py:182`),它写的是 `[cleaned, reminder]` 两条 —— 所以 `_extract_post_llm_messages` 那条「原样返回整份 updated」的兜底分支也永远拿不到压缩后的列表。动态验证(真 `build_react_graph` + 真 `ContextCompressor` + checkpointer,同一 thread 两轮):两轮 prompt 都被压到 5 条且都含 `<context-summary>`,检查点里仍是 19 条原始消息、**0 条摘要**,run 戳只落在两条 AIMessage 上;配套反证(人为把一条摘要 `aupdate_state` 进 messages 通道)同一探测器报红,判据不是重言式。**→ §6 里「摘要撤不回」的顾虑不成立,supersede 时无需对摘要打标;下一轮从原始历史重新压缩,被取代轮已被构图侧整轮过滤剔除,自然不进摘要。**
2. ~~`filter={"run_id"}` 历史过滤在生产版本 langgraph-checkpoint-postgres 上的性能(长会话 history 条数);必要时给 `agent_run` 加 `base_checkpoint_id` 列走 A 方案的锚点(与 B 不冲突)。~~ **✅ 已核实(09-09,测试环境 pod 内只读 EXPLAIN)—— 判据不过,`aget_state_history` 不能进 supersede 热路径,但也不需要加锚点列**。
   - **实际 SQL 形状**(`langgraph-checkpoint-postgres 3.1.2`,测试环境与本地同版本):`aget_state_history` → `AsyncPostgresSaver.alist`(`aio.py:119`)= `SELECT_SQL`(`base.py:93`)+ `_search_where`(`:624`)+ `ORDER BY checkpoint_id DESC`,一次 `fetchall()` 全量拉回。`SELECT_SQL` 除 checkpoints 六列外**还带两个相关子查询**:一个按 `channel_versions` 把 `checkpoint_blobs` 聚成 `channel_values`(= 每个 checkpoint 一整份 messages 通道 blob),一个聚 `checkpoint_writes`。谓词是 `thread_id = %s AND checkpoint_ns = %s AND metadata @> %s::jsonb`。
   - **是否走索引**:走。`Index Scan Backward using checkpoints_pkey`(`(thread_id, checkpoint_ns, checkpoint_id)`),无 Seq Scan;`metadata @>` 没有 GIN,只作 Filter(`Rows Removed by Filter: 136`)—— 但 `thread_id` 已把扫描面限死,过滤不走索引不是瓶颈。
   - **实测数字**(测试库 `checkpoints` 9309 行 / 419 会话;`checkpoint_blobs` 731 MB):最长会话 185 条 checkpoint,其中最大一个 run 占 49 条 → 服务端 `Execution Time` 三次稳定 **~173 ms**(减去探针自己的 InitPlan ~31 ms ≈ **141 ms**),**客户端整轮 810–950 ms、拉回 52 MB `channel_values`**;同一会话不带 filter 的全历史 ~375 ms / 185 行。全表最大的单个 run 有 **100 条** checkpoint,还要再翻一倍。blob 量随轮数二次增长(每个 checkpoint 都带完整 messages 通道),这是 `alist` 的固有形状,不是本库数据的偶然。
   - **替代取法(推荐,PR1 Task 4 照此实现,无需迁移)**:两步走,总计实测 **40–97 ms**、只拉 0.89 MB ——
     ① 一条两列轻查询定位该 run 最早 checkpoint 的父:`SELECT checkpoint_id, parent_checkpoint_id FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' AND metadata @> %s::jsonb ORDER BY checkpoint_id ASC LIMIT 1`(实测 **4–8 ms**);
     ② `aget_state(config 带 checkpoint_id=parent)` 取那**一条** checkpoint 拿 `len(messages)` = 起始下标 `s` 与 `plan_before`(实测 **36–89 ms**);结束下标 `e` 照旧从 `aget_state(cfg)`(最新)拿,同样是一条。
   - **给 `agent_run` 加 `base_checkpoint_id` 锚点列**:实测**不必要**。它只能省掉上面 ① 那 4–8 ms,却要一列迁移 + 存量 run 无法回填(老 run 没有锚点仍得走 ①)。A 方案(分支切换器)将来若要做,再单独议。
3. ~~对接方在 `stream_format=legacy` 实时流里取 `run_id` 的帧位置~~ **✅ 已核实(09-09)**:legacy 流首帧 `metadata` 就带 `run_id` + `thread_id`,`docs-site/guide/sse-events.md` 明写「保存 run_id」,对接方现有代码已在存。
4. ~~队列模式(`mode=queue`)下 supersede 与 queue worker 认领的先后顺序:supersede 必须在建 run 行**之前**完成并持锁,防止 worker 抢跑。~~ **✅ 已核实(09-09)**:
   - **worker 认领链**:`RunQueueWorker.run_once`(`services/control-plane/src/control_plane/run_queue_worker.py:172`)→ `RunStore.list_queued`(`packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:1601`,`WHERE status='queued' ORDER BY created_at`)→ `_claim_and_start`(`:213`)→ `RunStore.claim_queued` CAS(`store.py:1613`,`UPDATE … WHERE id=%s AND status='queued' … RETURNING`)→ `_execute`(`:262`)。worker **只可能看到已经存在的 QUEUED 行**。
   - **保证来自顺序,不是锁**:advisory lock 照 `trigger_delivery.delivery_thread_lock`(`trigger_delivery.py:57-88`)开在**独立的 lock session** 上(`pg_advisory_xact_lock`),queue worker 走另一条连接、**不取这把锁** —— 锁只负责把「同一 thread 上两个并发 supersede / spawn」串行化(防两副本同时取代同一轮)。挡住 worker 抢跑的是「QUEUED 行在 supersede 全部提交之后才 INSERT」这个先后。
   - **锁的放置点**:`api/runs.py` `spawn_run`(`:953`)内,从 `run_id = uuid4()`(**`:1077`**)起,一直持到两个建行调用返回为止 —— queue 分支 `runtime.run_manager.enqueue(...)`(**`:1082`**)、stream 分支 `runtime.run_manager.create(...)`(**`:1109`**);`supersede_run` 必须在锁内、在这两个调用**之前**跑完并提交。stream 分支的 `asyncio.create_task(run_agent…)` 在 `:1156`,晚于建行,锁在建行后释放即可。
5. ~~对接方对新增字段是否 strict 解析~~ **✅ 已确认(09-09,用户)**:对接方无 strict,自动忽略多出的字段。

## 9. 关联
- 探索报告结论(A/B/C 对比)与 spike 文件:`scratchpad/spike_p1_supersede.py`、`spike_p1_run.log`(留档,不入仓)。
- 附件统一约定:`docs/superpowers/specs/2026-08-17-external-upload-unification-design.md`(`:regenerate` 复用原轮附件引用)。
- 对外文档规范:`docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`。
