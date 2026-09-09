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
- 检查点 metadata 带 `run_id`(langchain `ensure_config` 复制 configurable 标量;spike 实测 keys = `parents, run_id, source, step, tenant_id`),`aget_state_history(filter={"run_id": r})` 可服务端过滤。

## 3. 机制(B 方案,spike 已验)

### 3.1 supersede 一轮(内核函数 `supersede_run`)
输入:`thread_id`、目标 `run_id`(必须是该会话**最后一轮**)、新 `run_id`。
1. **并发闸**:per-thread advisory lock(照 `trigger_delivery.py:59-75`);会话有 run 处于 `_WRITE_BLOCKED_STATUSES`(running/pending/paused…)→ 409。
2. **定轮边界(取法 A)**:`aget_state_history(cfg, filter={"run_id": target})` → 最早的 checkpoint(`source=="input"`)的 `parent_config` → `aget_state(parent)`:`len(messages)` = 该轮起始下标 `s`,`plan` = 回退值 `plan_before`;目标 run 最新 checkpoint 的 `len(messages)` = 结束下标 `e`。**不靠消息戳划轮**(Tool/System 无戳)。
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
  - 错误:`RUN_NOT_LAST`(422,目标不是最后一轮)/ `THREAD_BUSY`(409,有 run 在跑)/ `RUN_AWAITING_APPROVAL`(409,目标轮等审批)/ `RUN_ALREADY_SUPERSEDED`(409)。进 `docs-site/guide/errors.md`。
- 读面:`/messages` 每条、`/items` 每个条目与 `runs[]` 加 `superseded_by: run_id | null`;`runs[]` 加 `regenerated_from: run_id | null`;墓碑条目 `tombstone: true` 且无 content。缺省不出现(对接方按既有约定忽略未知字段)。
- SSE **无新帧**;新一轮的帧序与普通 run 相同。
- 三张手工路由表登记(`test_external_only_gate.py:66` / `test_console_lockdown.py:251` / `test_external_path_param_nul_guard.py:502`)。
- 文档:`chat.md`(新节「重新生成与编辑重发」,明写不撤销副作用、两轮都计费、只对最后一轮)、`sse-events.md`(无新帧但说明 `superseded_by`)、`query.md`(字段表)、`errors.md`、`examples.md`、`best-practices.md`。

## 5. 控制台
- 对话详情页把被取代轮画成折叠态(标「已被取代 → 新轮链接」),墓碑轮显示「内容已清理」。
- 调试台不提供重新生成入口(对外功能;调试台有草稿试跑)。

## 6. 已知不回退、已接受(文档明写)
- 工作区文件、产物登记、长期记忆写回、`promoted_tools`(union 通道)、`subagent_invocations` / `reflections`(add 通道)。
- 压缩摘要:若压缩器已把被取代轮摘要进 `<context-summary>` 消息,标记撤不回摘要内容 —— **实施前核实摘要是否落检查点**(§8-1);若落,supersede 时对摘要消息同样打标并剔除,让下一轮重新压缩。

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
1. 压缩摘要是否落检查点、是否带 run 戳(§6)。
2. `filter={"run_id"}` 历史过滤在生产版本 langgraph-checkpoint-postgres 上的性能(长会话 history 条数);必要时给 `agent_run` 加 `base_checkpoint_id` 列走 A 方案的锚点(与 B 不冲突)。
3. 对接方在 `stream_format=legacy` 实时流里取 `run_id` 的帧位置(端点按 run 的前提;历史侧已确定有)。
4. 队列模式(`mode=queue`)下 supersede 与 queue worker 认领的先后顺序:supersede 必须在建 run 行**之前**完成并持锁,防止 worker 抢跑。
5. 对接方对新增字段是否 strict 解析 —— 问一句。

## 9. 关联
- 探索报告结论(A/B/C 对比)与 spike 文件:`scratchpad/spike_p1_supersede.py`、`spike_p1_run.log`(留档,不入仓)。
- 附件统一约定:`docs/superpowers/specs/2026-08-17-external-upload-unification-design.md`(`:regenerate` 复用原轮附件引用)。
- 对外文档规范:`docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`。
