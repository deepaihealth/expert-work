# B-168 PR B 设计:长期记忆写入挪到结束帧之后、后台执行

> 专项:B-168(长期记忆写入挡住结束帧 + 记忆独立模型)。PR A(#1779)是记忆独立模型 + 默认关思考 + 轨迹泳道,本稿只管 PR B。
> 排期(2026-10-09 晚用户拍板):整波目标随 10-12 班车,**周日晚测试环境真栈没全过就从这班车撤下、只上 PR A**,不为赶班车砍测试。
> 状态:设计稿(2026-10-09),待拍板 §8 四个问题后开工。

## 1. 现在的问题

### 1.1 现象

测试环境对话 `6e60e0c4`(test-headphones,deepseek-v4-flash 开思考,开长期记忆 + 去重合并):每轮模型已经答完,对接方还要再等 1.9 / 5.1 / 12.4 / 12.7 / 36.1 / 9.4 秒才收到 `end` 帧。

PR A 把记忆调用的思考关掉,预计每轮还剩几秒(以测试环境实测为准)。但只要记忆写入还挡在结束帧前面,这几秒就一直在,而且模型一抖就会变长。

### 1.2 为什么挡住(摸底结果)

| 事实 | 位置 |
|---|---|
| 写回节点挂在图的出口,`END` 之前 | `graph_builder/builder.py:2126-2131` |
| `success` 状态在整张图跑完后才写,包括写回 | `orchestrator/sse.py:934` |
| `end` 帧在 `finally` 里由 `bridge.publish_end` 发出 | `orchestrator/sse.py:1145`、`:1844-1870` |
| 同一会话上一轮还在 `running` 时,新一轮会**排队**,等上一轮结束才开始(B-139) | `thread_queue.py:82-89`、`runs/schemas.py:120-124` |

### 1.3 已经排除的两个省事做法

**只把 `end` 帧提前发、run 继续跑完写回**:不行。
- run 还是 `running`,用户收到 `end` 就发下一句,会被 B-139 排进队列,照样等写回结束。等待只是从「收 `end` 前」挪到了「下一轮开始前」。
- 控制台「断开即取消」:`end` 发完,`sse_consumer` 收尾时看到 run 还在跑,会把它取消掉,写回被杀(`sse.py:1897-1903`)。

**先把 run 标成 `success`,再接着在同一个 run 里写回,崩了靠孤儿复活**:也不行。
- 孤儿复活只接管 `running` 且租约过期的 run(`orphan_sweep.py:197-231`、`runs/store.py:1065-1072`)。已经标成 `success` 的 run,复活机制看不见。
- 标成结束后,run 仍在往同一个会话写检查点,会和下一轮的检查点分叉,正是 B-139 / B-75 修掉的问题。

结论:**本轮正常结束,记忆写入作为独立的后台任务落表,由后台执行**。写入不碰会话检查点,只读、不写,所以和下一轮并行没有冲突。

## 2. 用户看到的行为

| 场景 | 改之后 |
|---|---|
| 一轮答完 | `end` 帧紧跟最后一个回答帧(只多一次写库,毫秒级),不再等记忆 |
| 紧接着发下一句(同一会话) | 立即开始,不排队。上一轮的内容本来就在对话上下文里,不受影响 |
| 新开一个对话,而且就在上一轮结束后几秒内 | **可能还读不到**上一轮刚写的记忆(后台通常 1–2 秒领到,再加模型抽取几秒)。业界做法都接受这个代价 |
| 控制台轨迹 | 「记忆写回」这一行仍在,但变成**后台**事件:带排队用时、执行用时、写了几条、成功或失败。数据来自任务表,不进对外事件流;实时看的时候要刷新页面才出现 |
| 第三方的流与重放 | 写回节点那一帧 `updates` 仍是 `end` 前最后一帧,值变成「已排队」;`/events` 重放与续传的帧序列和实时流完全一致,不多不少 |
| 对外 `end` 帧里的 `usage_by_model` | **不再包含**记忆调用的 token(那时还没发生);按拍板 (b),这部分记为平台开销,run 用量接口也不含,两处一致。对接方计费零改动 |
| pod 重启 / 发版 | 已经落表、还没写完的任务,由其它副本或重启后的 pod 接着写,不丢 |
| 用户被清除 / 会话被删除 | 没写完的任务一起删掉,不会在清除后又写进记忆 |

## 3. 做法

### 3.1 本轮结束时:只落一行任务

写回节点的位置不变(仍是 `END` 前最后一步),但在后台模式下它只做一件事:往新表 `memory_writeback_job` 插一行,然后返回。返回值沿用 PR A 的字段,另加 `memory_writeback_queued: true`,泳道据此画成「已排队」。

任务行**只存指针,不复制对话内容**,健康数据不多一份拷贝:

| 字段 | 说明 |
|---|---|
| `id` / `tenant_id` / `user_id` | 归属。所有查询都按租户过滤 |
| `agent_name` / `agent_version` | 执行时按这个取 Agent 配置,得到记忆模型、是否去重合并、最低重要度 |
| `thread_id` / `checkpoint_id` | 读这一轮结束时那一刻的对话。用 `checkpoint_id` 而不是「会话最新」,因为下一轮可能已经跑起来 |
| `run_id` / `trace_id` | 来源 run,用于记忆行的出处、用量归属、轨迹追加 |
| `status` | `pending` / `running` / `done` / `failed` |
| `attempts` / `lease_until` / `last_error` / `created_at` / `started_at` / `finished_at` | 领取、重试、观测 |

**待实现时核实**:节点里拿 `checkpoint_id` 的取法。LangGraph 在节点的 `config` 里带的是父检查点 id,要确认读出来的 `messages` 恰好是本轮结束时的完整对话。退路是记录 `message_count`,读会话检查点后截取前 N 条;但 B-126 跨轮清理会改写早期消息,截取可能对不上,所以优先用 `checkpoint_id`。读检查点的函数现成:`transcript.read_messages`(`transcript.py:125`),质量监控和对话镜像两个后台任务已经在用。检查点没有定期清理,只在删会话和清除用户时删(`api/sessions.py:1037`、`purge/user_purge.py:253`)。

### 3.2 谁来执行:控制面每个副本跑一个后台 worker

新增 `MemoryWritebackWorker`,照 `MemoryDLQWorker` 的样子写:`start` / `stop`、`asyncio.Event` 停止标志、按间隔轮询。在 lifespan 里随应用启停(`app.py:2021`、`:2364-2420`)。

- **快路径**:本 pod 刚落了任务就唤醒本 pod 的 worker,不等轮询,一般 1 秒内开始。
- **慢路径**:每 2 秒轮询一次,接住别的 pod 落的任务、重启前没做完的任务、租约过期的任务。
- **领取**:`SELECT … FOR UPDATE SKIP LOCKED` 加 CAS 写 `running` + `lease_until`,照 `memory_writeback_dlq` 的 `take_ready`(`persistence/memory/dlq.py:289-333`)。
- **同一用户串行**:只领每个 (租户, 用户) 最早的那条 `pending`,而且这个用户当前没有未过期租约的 `running` 任务。条件写在领取的 SQL 里,同 B-139 `claim_queued` 的「前面没有更早的忙 run」(`runs/store.py:1260-1280`)。为什么按用户不按会话:事实类记忆跨该用户所有智能体共享,两个会话的去重合并同时跑,会对同一批旧记忆各做一次增删改。
- **租约**:5 分钟,写回中途每分钟续一次。pod 被杀,租约到期后别的副本接手。重试最多 3 次,之后 `failed` 并留下 `last_error`。抽取成功、存库失败的部分仍交给现有重试队列(DLQ),不变。
- **关机**:先停领新任务,再等手上这条做完(上限复用 `run_drain_timeout_s`)。做不完就放着,租约过期后别的副本接手。

### 3.3 worker 怎么调模型

1. 按 `tenant_id + agent_name + agent_version` 取 Agent 配置。配置已删或已没有 `memory.long_term`,就把任务标成 `done`,原因写「已不需要」。
2. 记忆模型用 PR A 的 `_memory_model(spec)`(改成公开函数),路由用现成的 `build_llm_router(model_spec, secret_store=, provider_key_resolver=, http_client=, rate_limiter_factory=)`(`agent_factory.py:2440`),做法同 `aux_model_adapter.py:143`。依赖都在 `app.state`:`secret_store`、`credentials_resolver`、`shared_http`、`llm_rate_limiter_factory`。
3. 记忆库、向量、重试队列现在是 lifespan 里的局部变量 `memory_env`(`app.py:1801`),要挂到 `app.state` 上给 worker 用。
4. 调 PR A 的 `flush_messages_with_outcome(...)`,参数取自配置,和今天在 run 里调用时一致。

### 3.4 用量与 Langfuse

> **按 §8 问题 3 的拍板 (b)**:后台记忆调用**不归 run**,按平台开销记(`usage_kind` 与重排序同类)。`end` 帧、run 用量接口、对接方计费三处口径一致,都不含后台记忆 token;控制台按租户 / 智能体看平台开销时能看到这部分。下面关于 trace 的做法只为 Langfuse 能挂到原 run 下;实现时核实 run 用量接口按 `usage_kind` 过滤,不会把平台开销算回 run。

- 今天记账靠 run 作用域的上下文变量(`usage_metering.py:53`、`:268-292`)。在 run 外调用时**静默不记**,必须改。
- 做法:worker 打开一个 span,父 span 设为任务行里的 `trace_id`(远程 `SpanContext`)。然后直接调 `UsageMeter.__call__(response, tenant_id=, user_id=)` 记账。`token_usage` 按 trace 关联 run(表里没有 `run_id` 列,`token_usage_store.py:71`),所以 run 用量接口会把记忆 token 算回这个 run。
- 路由带上 `around_llm_chain`,Langfuse 仍能看到 extract / reconcile 两个 span,挂在原 run 的 trace 下。

### 3.5 轨迹

**不往 `run_event` 追加。** 虽然 `run_event` 在 run 结束后仍允许追加(`runtime/runs/event_store.py:70`、`:149` 的 `next_seq`,没有状态闸),但它也是对外 `/events` 重放与 `since_seq` 续传的数据源(`api/_run_event_stream.py:270`)。追加会让第三方重连或重放时,多出一个实时流里从没出现过的 `updates` 帧,还会让它的 `seq` 排在原来的最后一帧之后。这违反对外文档「每一步一次 `updates`、`end` 在最后」的约定(`sse-events.md` §3.1)。

改为:写回结果记在任务行上,包括写入条数、是否失败、排队用时、执行用时。控制台的 run 详情接口(只对控制台,不对 API Key)按 `run_id` 读任务行,拼成「后台记忆写回」这一行。前端认这个来源、改文案。

指标(Prometheus):排队用时、执行用时、失败数、积压数。

### 3.6 删除与清除

- `purge_user`(`purge/user_purge.py:328`):在清记忆、清 DLQ 的同一段,加一步删该用户的任务(`PurgeDeps` 加字段)。
- 删除会话:删该会话的任务。
- 如果 worker 正在执行,清除后它会在存库时写进已清除用户的记忆。所以 worker 在存库前再确认一次任务行还在,不在就丢弃。剩下一个很小的竞态窗口:确认之后、存库之前恰好发生清除。这个窗口和今天「run 正在跑时被清除」是同一类,不额外处理。

### 3.7 开关

`Settings.memory_writeback_mode: Literal["inline", "background"]`,环境变量 `EXPERT_WORK_MEMORY_WRITEBACK_MODE`。
- `inline` 就是今天的行为,作为逃生口。
- 默认值见 §8 问题 1。

开关要传到 orchestrator 建图那一层。

### 3.8 不变的部分

- **压缩前抢存**(`pre_compaction_flush`)仍在本轮内同步执行。它只在对话快被压缩时触发,而且必须在压缩丢掉中间消息之前做完。
- 读取端:召回、读时校验、改写检索词都不变。
- PR A 的记忆模型和默认关思考,两种模式都生效。

## 4. 不做的

- 不降低抽取频率(每 N 轮 / 会话结束时)。这是 B-168 的后续项,要先用 LoCoMo 验证召回不掉。
- 不做「下一轮开始前等上一轮记忆写完」的读写一致保证。如果实测这个窗口在真实使用里频繁命中,再加。
- 不把任务表并进现有 DLQ 表。DLQ 存的是已经抽好的内容,跳过模型直接重写;任务存的是 run 指针,要调模型。两套状态机分开,指标也干净。

## 5. 风险

| 风险 | 处理 |
|---|---|
| 改的是每一轮的收尾时序,所有对接方都走这条路 | 开关可以一键退回 `inline`;测试环境真栈先跑满场景再上 |
| 后台积压(模型慢、某个用户任务很多) | 同一用户串行、不同用户并行;指标看积压;重试有上限 |
| 回滚到旧镜像 | 新表是纯新增,旧镜像不读它。回滚那一刻还没写完的任务不会被执行,这几轮的记忆丢失,范围是秒级窗口。执行单写明 |
| `end` 帧用量和 run 用量接口对不上 | 见 §8 问题 3,对外文档同步改 |
| `checkpoint_id` 取法不对,读到的不是本轮结束时的对话 | 单测用真图驱动断言读出来的消息;取不准就走 `message_count` 退路并写明限制 |

## 6. 验证

### 6.1 单测与集成测(本地,每条变异自证)

1. **先写的红测**:写回很慢(假模型挂 30 秒)时,`end` 帧和 `success` 状态不被推迟。改之前这条必须是红的。
2. 落表的任务字段完整,能读回本轮结束时的消息,而不是下一轮之后的。
3. 同一用户两条任务严格先后执行;不同用户并行。用真 PG(testcontainers)测领取 SQL。
4. 租约过期后另一个 worker 接手;重试 3 次后 `failed`。
5. 清除用户 / 删会话后,任务被删;执行中的任务存库前发现任务行没了就丢弃。
6. 用量按任务记账,trace 关联到原 run;模型调用在 run 外也被记账(防「静默不记」回归)。
7. 控制台从任务表读到后台写回行;`/events` 重放的帧序列与实时流逐帧一致,不含后台写回(防回归)。
8. `inline` 模式下行为与 PR A 完全一致(现有测试全绿)。

### 6.2 测试环境真栈

- 用 PR A 的探针,两个智能体(默认 / 刻意开思考)各跑 5 轮:
  - 「最后回答帧 → `end`」都 < 1 秒;
  - 随后 30 秒内记忆写入完成,条数与 `inline` 模式同量级。
- 写回进行中删掉执行它的 pod:任务被另一个副本接手,记忆最终写入。
- 写回进行中清除该用户:清除后记忆为 0。
- run 用量接口包含记忆 token;Langfuse 里 extract / reconcile span 挂在原 run 下。
- 控制台轨迹刷新后看到后台写回行。
- smoke + 金丝雀。

## 7. 拆 PR

| PR | 内容 | 是否改行为 |
|---|---|---|
| B1 | 迁移(新表 + 索引)、store + 领取 SQL、worker、开关(先默认 `inline`)、清除接入、用量 / trace | 否,默认 `inline` |
| B2 | 写回节点在 `background` 模式下改为落表;`run_event` 追加;前端后台行;对外文档(`sse-events.md` `end` 帧用量、记忆可见性);默认值按 §8 拍板 | 是 |

B1 可以先合、先上测试环境泡着。B2 开关一拨就切换。

## 8. 待拍板

1. **10-12 上生产时默认用哪种模式?** 建议 `background`。平台默认就该是对的,而且测试环境真栈已经过了才上。`inline` 只留作逃生口。另一种选择是生产先 `inline`,观察一周再切。
2. **接受「新开对话几秒内可能读不到上一轮刚写的记忆」?** 建议接受,业界都这样。
3. **✅ 已拍板(2026-10-09):选 (b),记忆 token 不归 run,按平台开销记。** 依据:project-service 只从 `end` 帧的 `usage_by_model` 扣费,一次性扣完,从不调 run 用量接口,也没有对账(其会话代码核实:`ai-health-plan-run.service.ts:669-733` 是唯一的 `chargeRunIfNeeded` 调用点)。(a)/(c) 都要对方新增「`end` 后补扣」并改它的只扣一次逻辑。以后要改成 (a),再和对方一起排。以下为拍板前的原文。
   **对外 `end` 帧的 `usage_by_model` 不再含记忆 token,run 用量接口几秒后才包含,两处会不一致。** 今天 `best-practices.md:113` 让客户端核对两处一致。对接方 project-service 按 `end` 帧扣费(执行单 §1),改后**开了长期记忆的智能体,记忆那部分 token 不会再被扣到终端用户头上**。三个选项:
   - (a) 记忆 token 仍归 run,对外文档改成「以 run 用量接口为准」,并通知 project-service 改为按用量接口对账或补扣;
   - (b) 记忆 token 不归 run,按平台开销记。两处一致、第三方零改动,但按 run 看不到记忆成本,这部分成本由平台承担;
   - (c) 对外 `end` 帧加一个字段,说明「本轮还有后台记忆写入,用量稍后可查」,沿用 (a) 的归属。
   需要先和对接方确认他们怎么用这两处用量,再定。
4. **赶 10-12 的判据**:周日晚 §6.2 全过才上,任何一条没过就撤 PR B、只上 PR A。
