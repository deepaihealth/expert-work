# Agent 延迟优化二期 — 设计(提速 / 缓存 / 稳健,3 PR)

> 一期(#1059,main `f892b6dc`)交付了可观测性 + 连接池:入口链 8 span、`first_output_seconds{source}`、bench 尺子、13 处 httpx 共享池。真栈 embed 中位 -30%、first_llm_start -17%。
> 本 spec 是其文末「二期 backlog」的全量落地设计。基线:首字前入口链 median 476.5ms,其中记忆召回 238.5ms(向量化 202.5 + 检索 22 + 读配置 15)。

## 决策纪要(已与用户确认,2026-07-27)

1. **范围 = 全量**(backlog 所有项),按主题切 **3 个 PR**:PR1 提速 / PR2 缓存 / PR3 稳健。
2. **缓存失效策略 = 主动失效 + TTL 兜底**:凭据写入口 fan-out 失效所有相关缓存(顺带修「换 key 要重启容器」的生产 bug);TTL 防漏接的写入口。符合本仓既定立场「进程内失效 + TTL 兜底跨副本」(每个 platform_*_config service 的 docstring 都声明这一条)。
3. **P4 并发闸 = 平台配置节**,照 #1029 dynamic_worker 全栈模板(admin-ui 可调、热生效)。
4. **verify_reads 默认值 = 量完拿数字拍板**:本期产出 verify on/off 对比数据,拿数字问用户,若改默认就本期顺带改;无论改不改,tooltip 补代价说明。
5. 每项优化用 `tools/bench/entry_latency.py` 量 before/after,数字进 `tools/bench/baselines/`。

---

## PR1 — 提速(bench 扩展 + P1.1/P1.2/P1.3/P3 + verify 数据)

### Task 1:bench 场景扩展(尺子先行)

现状缺口(after 基线 meta.note 自陈):固定 prompt 零召回结果 → rerank/bump_access/verify 不触发;无持久工作区 → workspace_ingest 不存在。基线只亮 8 段中 5 段。`bench-entry@1.0.0` manifest 未入仓,基线不可复现。

- **manifest 入仓**:`tools/bench/manifests/bench-entry.yaml`(bench 专属资产跟 bench 走,不进顶层 `manifests/`)。要点:
  - `sandbox.filesystem.persistent_workspace: true`(可抄 `manifests/canonical-agent/v1.0.0.yaml:90-97`)——注意该字段只控 CM-0 计划投影,不控文件持久性;
  - `memory.long_term`:`write_back: true` + `retrieve_top_k` + `verify_reads` 显式声明(便于 on/off 两组对照);rerank 开。
- **预埋记忆**:无 `POST /v1/memory` 创建接口(只有 GET/PATCH/DELETE/correct)。照 e2e runbook(`docs/runbooks/canonical-agent-e2e-test.md:238-287`)做法:种子轮对话让 writeback(`memory.py:875-1010` `flush_messages_to_memory`)落库,之后 bench 轮召回非空。落地为 bench 脚本 `--seed` 子命令 + README 步骤。
- **workspace_ingest 非零耗时**还需 `/workspace/PLAN.md` 有内容(`workspace_ingest.py:113-114`:candidate None → `{}`);种子轮顺带写入,或接受该段近 0(能量到出现即可)。
- **verify 耗时抓法**:verify 有自己的 span(`memory.py:431`)但**不在** `TRACED_SPANS`;facade 把它折成 `kind="llm"`、label「记忆校验」、`purpose="memory"`、`group=None` 的节点。bench 脚本按 label 抓,输出独立 metric(不进 `segments`)。**不动 TRACED_SPANS/facade**(免撞 parity 测,免毁基线可比性)。
- 已知口径坑:verify 开着时 `first_llm_start` = verify 的开始时间(它是最早的 llm span,README 已写明)。verify 收益口径用「on/off 两组的端到端总时长差 + verify span 自身耗时」,不用 first_llm_start。

### Task 2:P1.1 bump_access 改 fire-and-forget

现状:`memory.py:644-659`,recall 管线**最后一步**,后续仅 `_redact_memory`(纯 CPU)+ return,零数据依赖。`bump_access` 是单条 UPDATE + 独立 session(`sql.py:483-497`),best-effort 语义基类 docstring 已声明。

- `asyncio.create_task` + **模块级强引用集合**(照 `sse.py:797-799` `_BACKGROUND_CLEANUP_TASKS` 范式:`add` + `add_done_callback(discard)`,防 RUF006/GC 提前回收)。
- ids 在调度时刻取快照(`[m.id for m in memories]`,之后 memories 不再变)。
- 异常处理进后台任务体内(照旧 warning,`RunCancelledError` 不再特判——后台任务不在 run 取消范围内,plan 阶段确认 create_task 的 context 拷贝对 cancellation token 的影响)。
- span 保留(`TRACED_SPANS` 不动):fire-and-forget 后该 span 时间上仍在 trace 里,但不再阻塞入口链——bench 里该段耗时仍可见,分解条该段不再计入首字等待,收益体现为 recall 段变短。文档注明。
- **已知会破的测试**:`services/orchestrator/tests/test_memory_nodes.py:337-357` 在 node 返回后立刻断言 `access_count == 1` → 改为 drain 后台任务再断言(暴露 task 集合或提供 flush 测试钩子)。

### Task 3:P1.2 tenant_config 读走已有缓存(修正版)

现状核实(2026-07-27 复核,推翻侦察初判):`TenantConfigService`(`tenancy/tenant_config.py:59`)**已带 per-tenant TTL 缓存**(`tenant_config_cache_ttl_s`,默认 60s)+ `upsert` 主动失效(prime);resolver 注入的 `tenant_config_getter` 就是它(`app.py:1191`)——resolve 路径早有缓存。**唯一裸奔的是 MemoryEnv 注入**(`app.py` 建 `MemoryEnv(tenant_config_store=resolved_tenant_config_repo)` 给的是无缓存 repo),即 bench「读取召回配置」段的 15ms/recall。

- 修法:**薄适配器委托 TenantConfigService**——实现 orchestrator 期望的 store 接口(`get -> record | None`),内部调 `service.get(tenant_id=..., actor_id=None)` 并把 `TenantConfigNotConfiguredError` 转 `None`。缓存、TTL、upsert 失效全复用 service 现有实现,**零新缓存逻辑**。
- `actor_id=None` 不触发 audit(service 只在带 actor 时 emit),热路径无审计噪音。
- **不包共享 repo**(spec 早稿方案作废):`TenantStatusService`(kill switch)用秒级短 TTL 消费同一 repo,repo 层加长缓存会架空 kill switch 传播,是安全回归。

### Task 4:P1.3 workspace_ingest 与 memory_recall 并行

现状:LangGraph 线性边链 `START → memory_recall → planner → workspace_ingest → agent`(`builder.py:1420-1443`,`itertools.pairwise`)。ingest 排最后的语义理由**只针对 planner**(人改的 PLAN.md 覆盖 planner 生成的 plan),与 memory_recall 无关。写集不相交:recall 写 `recalled_memories`、ingest 写 `plan`,两 key 均无 reducer 但无并发写者。

- 改边拓扑为两条并发分支:`START → memory_recall → agent` ‖ `START → planner → workspace_ingest → agent`(planner/ingest 各自可选,分支内保序,汇合在 agent 的 superstep 屏障)。
- bench agent 无 planner 时即 `START → {memory_recall ‖ workspace_ingest} → agent` 纯并行。
- **前提已核实**:planner 只读 `state["messages"]`(`planner.py:148` `_extract_task`),不消费 `recalled_memories`——并行不改变 planner 输入语义。
- approval RESUME 重入路径(`builder.py:1160-1167` 在 agent_node 内直接 await ingest)不受影响,不动。

### Task 5:P3 guards 全关跳 64 字符缓冲

现状:`streaming_redact.py:131` 的 `- HOLD_CHARS` 无视 dlp/screen 开关,guards 全关仍扣住尾部 64 字符。`make_token_sink`(`:202-218`)只在 judge 开或无 publish 时短路;dlp=False ∧ screen=False 时仍构造 TokenSink(内含两个 StreamingRedactor)。

- `make_token_sink` 加分支:`not dlp and not screen and not judge_enabled` → 返回直通 sink(不构造 redactor,delta 直接 publish;保 flush/接口签名兼容)。实现形态(TokenSink 快路径 vs 独立 PassthroughTokenSink 类)plan 定。
- **默认配置不受益**(`output_screen` 默认 `"block"`)——这是安全默认的正确代价,不动默认值。收益人群 = 显式全关守卫的 agent。
- reasoning 频道同样直通(TokenSink 现构造 content + reasoning 两个 redactor)。

### Task 6:verify_reads 数据 → 拍板

- bench 跑两组:同 agent 同 prompt 同预埋记忆,仅 verify_reads on/off。产出:端到端总时长差(median)+ verify span 自身耗时 + verify 淘汰率(kept/candidates,可从日志/metrics 取)。
- 数字摆给用户当场拍板:改默认(protocol `agent_spec.py:398` `default=True` → False)或不改。
- 无论改不改:admin-ui verify_reads 开关 tooltip 补代价说明(一期 spec 遗留的「可选」项)。

---

## PR2 — 缓存三合一(失效连动 + secret 缓存 + agent 缓存治理)

### 背景事实(侦察定案)

- **轮换 = 同 ref 原地覆写**:`_canonical_secret_name`(`platform_config.py:174-203`)对同 `(provider, key_id)` 永远同 slot 名,`_resolve_write_ref` 原地 `secret_store.put`,**secret_ref 不变** → 换 key 后 catalog 视图刷新也发现不了,已构建 agent 里烤住的明文旧 key 继续用。这就是「换 key 要重启」的根因。
- **built-agent 烤 key 证据链**:`build_llm_router`(`agent_factory.py:1956`)`secret_store.get` → `HTTPAnthropicClient(api_key=...)` 等;多 key(Y-MK)+ fallback 链 + 双 judge router,每个都各烤一份。
- **失效缺口全名单**:`platform_config.py` 10 个写入口(平台 provider/key/tool 的 PUT×3 + DELETE×3;租户覆盖 PUT×2 + DELETE×2)只调 `service.invalidate()`(仅刷 30s catalog 视图);`mcp_oauth_refresh.py:184,188` 后台刷新 token 完全不失效任何缓存。
- **已有失效基建**:`AgentRuntime.invalidate_all/invalidate_tenant/invalidate_user` + hook 注册(`runtime.py:285-340`);subagent 缓存经 hook fan-out,但 **`invalidate_user` 不 fan-out**(现有缺口,OAuth 断连后子 agent 缓存残留)。
- **缓存实现参照**:`services/credential-proxy/src/credential_proxy/cache.py` `SecretCache`(OrderedDict LRU + TTL + 注入 clock,key 含 tenant_id 防跨租户)。全仓无 cachetools,全自研。

### Task 1:失效连动(修「换 key 要重启」bug)——故意排第一

先补失效再加缓存,否则新缓存恶化现有 bug。

- platform_config.py 平台级 6 入口 → `runtime.invalidate_all()`;租户级 4 入口 → `runtime.invalidate_tenant(tenant_id)`(tenant_id 是现成路径参数)。handler 加 `request: Request` + `getattr(request.app.state, "agent_runtime", None)` 兜底,照 `agents.py:253` 现成写法(部分测试 app 不装 runtime)。
- `mcp_oauth_refresh.py` 2 处 → `invalidate_user(tenant_id, user_id)`(装配时注入 runtime 引用)。
- 补 `invalidate_user` 的 hook fan-out:runtime 加 user 级 hook 注册,subagent 缓存注册对应 invalidator(清 5-tuple 里 `oauth_user_id` 匹配的条目)。
- 同一失效链挂上 Task 2 的 secret 值缓存(fan-out 清它)。
- **回归测试**:换 key(PUT 同 provider 新值)→ 下一个 `get_agent` 构建的 client 拿新 key,不重启。

### Task 2:secret/resolve 值缓存

现状:4 个 Resolving 类(`runtime.py:825-1021`,生产走 Dynamic 两个)每次 embed/rerank 调用都做 config(DB)+ resolve(DB)+ secret(vault)——一期已在 span 挂 `config_ms/resolve_ms/secret_ms` attribute。provider→ref 映射已有 `PlatformSecretsService` 30s TTL 挡;**没挡的是 tenant_config 读(PR1 Task 3 吃掉)和 `secret_store.get` vault 读(本 task 吃掉)**。

- 新 `CredentialValueCache`,照 credential-proxy `SecretCache` 抄:key `(tenant_id, secret_ref)`,LRU 上限 256,**TTL 5 分钟**(拍板值),注入 clock。放 control-plane,挂 `app.state`。
- 接入点:4 个 Resolving 类 + `aux_model_adapter.py:100` + `quality_judge.py:115`(同 resolver 同缺口,接入一行);`build_llm_router` 的 rerank-LLM 分支(`agent_factory.py:1956` 的 vault 读)plan 阶段评估是否同缓存穿透。
- 失效:Task 1 的 fan-out 清它(平台级 clear-all,租户级按 tenant 清)+ TTL 兜底。
- 不缓存 embedder delegate/client 对象(纯构造成本低,主成本是 I/O;YAGNI)。
- 收益量法:before/after 读 embed span 的 `resolve_ms/secret_ms`(一期已埋,bench trace 里直接可见)。

### Task 3:built-agent 缓存治理

现状:`runtime.py:217` `_cache` 纯 dict,无上限/无 TTL/无 size 指标;subagent 侧独立缓存(`subagent_runtime.py:186`)同样,且多一个 depth 维度。单条 BuiltAgent 持有:CompiledStateGraph、N 个烤 key 的 LLM client、双 judge router、3 个 MCPServerPool(**活连接/子进程句柄**)、数十 KB 渲染后 prompt。条目上界 ≈ 租户×agent×版本×(1+OAuth用户),无界。

- 两处缓存都加:**LRU 上限 256 + TTL 30 分钟**。TTL 故意比 secret 缓存长——agent 构建贵(秒级),TTL 太短反伤 TTFT;这里 TTL 只兜「漏接的写入口」的底,主动失效(Task 1 补全后)是主力。
- 驱逐 = 遗弃靠 GC,与现状 `invalidate_tenant` 行为一致(`del` 不调 `MCPServerPool.close_all`——活连接可能正被进行中的 run 使用,不能主动 close;不恶化现状,注释写明)。
- 加缓存条数 gauge 指标(两处),现在连 size 埋点都没有。

---

## PR3 — 稳健(subagent 全局闸 + sse 后台写)

### Task 1:subagent 全局并发闸(P4)

现状:唯一并发限制是 per-tools_node 的 `Semaphore(MAX_TOOL_WORKERS=8)`(`builder.py:1306`,每次节点执行新建);子 agent 递归各层自带 8 并发,depth 上限 3,理论展开 8³;`spawn_worker` 有 per-run `WorkerSpawnBudget`,**静态 `SubAgentTool` 什么闸都没有**。部署事实:orchestrator 是被 control-plane import 的库,单容器单 uvicorn 进程单事件循环(蓝绿部署,正常只有一色活)。

- **闸体**:进程级 `asyncio.Semaphore` 挂 `AgentRuntime`(进程内单例 `app.state.agent_runtime`)上的长寿对象。单进程部署下真闸得住;HA 双色同时活时每实例一个闸,与本仓「多副本陈旧度 TTL 兜底」同一立场,文档写明。
- **覆盖**:`SubAgentTool.call` + `SpawnWorkerTool.call` 都过闸(spawn_worker 的 per-run 预算保留叠加)。注入照 `worker_spawn_budget` 现成范式:runtime → `configurable` → `ToolContext` 字段 → 工具读 ctx。
- **防嵌套死锁**:acquire 带 30s 超时。死锁场景 = depth1 委托占满闸、其内 depth2 等闸;超时后返回软失败 ToolResult(「委托并发已满,请稍后重试或简化任务」),LLM 自行降级,不挂死。排队期间 run deadline 照常兜底(`deadline_at` 检查在 call 入口已有)。
- **配置节**:新 `platform_delegation_config`,单字段 `max_concurrent_delegations`(默认 16,界 1-64)。全套照 #1029 模板(计划文档 `docs/superpowers/plans/2026-07-20-dynamic-worker-config-pr2.md` 有逐文件映射:13 新建 + 8 修改,含 persistence 四件套 + migration + service TTL 30s + API + 审计枚举 + app.py 7 处 + admin-ui 配置卡 + i18n 三处 + e2e)。热生效语义同先例:对下一次委托生效,不影响进行中的。

### Task 2:sse updates 帧移出流路径

现状:`sse.py:516-524`(spec 旧行号 484-491 已漂移)每帧 `bridge.publish` 后同步 `_persist_event`,`SqlRunEventStore.append` = 1 session + 1 INSERT + 1 commit。已是 best-effort(H-7:吞异常只记 warning + counter)。`run_event` 唯一读者 = 调试台/历史轮重建(replay 端点),checkpoint 才是会话权威;前端已有 `sawEnd && hasContent` 降级。live 路径 StreamBridge 本来就是 drop-oldest 256。

- **per-run 有界队列(512)+ 后台 writer task**:主循环把 `(seq, event_name, data)` 入队即返回;writer 攒批(≤32 条或 100ms flush)写新 `RunEventStore.append_batch`(N+1 套路:一 session 多行 add + 单 commit;ABC + SQL + in-memory 三处同步加)。
- **seq 在入队前同步分配**(任何 await 之前),照 `_publish_worker`(`sse.py:405-417`)先例及其注释——并发 worker/guard 帧与主循环共用 `event_seq` 计数器,交错 await 会撞 `(run_id, seq)` 主键。所有 6 类帧(updates/metadata/compaction/worker/guard/approval/error)统一走队列。
- **队满**:drop-oldest + counter 指标(沿用 H-7 立场)。
- **run 结束**:正常/错误路径发 sentinel + 带超时等 writer drain(保证终态 status 落库前尾帧尽力写完,缩小 replay 不完整窗口);**`asyncio.CancelledError` 路径不 await**(该分支现状连 trajectory/audit 都跳,注释明言 teardown 中 await 不可靠)——writer 进模块级强引用集合 fire-and-forget 收尾。
- **PAUSED 路径**(`sse.py:620-635`)的 approval 帧同队列,顺序天然保序(单队列 FIFO)。
- **HA resume**:接管实例 `next_seq = MAX(seq)+1` 续号;旧实例进程已死不会再写(不撞 seq),其未落库尾帧永久丢——可容忍(唯一用途调试台,前端已降级),文档写明。
- **token 帧照旧不入库**(`sse.py:369-373` 设计不变)。

---

## 验证

- **PR1**:每项 bench before/after(同容器同 agent 同 prompt sha),数字进 `tools/bench/baselines/`;新增段(rerank/bump_access/verify/workspace_ingest)首次亮相成为二期基线。P1.3 需并行前后 trace 对照(两分支时间重叠证据)。
- **PR2**:换 key 回归测试(不重启生效);`resolve_ms/secret_ms` before/after;缓存 gauge 指标可见;LRU 驱逐/TTL 过期单测(注入 clock)。
- **PR3**:并发闸单测(闸满排队/超时软失败/嵌套不死锁/配置热生效);sse 队列测(drop 计数/drain 超时/seq 唯一性/取消路径不挂);e2e 照 platform-tool-budget.spec 模板。
- **CI 门(全 PR)**:`ruff check` 全库 + `ruff format --check`;CI-scope mypy(`uv run mypy packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src`);control-plane + orchestrator pytest(orchestrator 测试需 `DOCKER_HOST= uv run`);`pnpm exec vitest run src && pnpm typecheck && pnpm build`。
- 改 SQL/约束本地跑 integration(`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`,in-memory 不校验 CHECK)。

## 风险与对策

| 风险 | 对策 |
|---|---|
| P1.3 planner 消费 recalled_memories | 已核实不消费(`planner.py:148` 只读 `state["messages"]`),前提成立 |
| bump_access 后台化后测试时序 | 暴露 drain 钩子;bench 里该段仍可见(span 保留) |
| secret 缓存把旧 key 延长到 TTL 窗口 | 主动失效为主力,TTL 只兜漏;失效链回归测试盖住 10+2 入口 |
| agent 缓存 LRU 驱逐活 MCP 连接 | 遗弃靠 GC,与现状 invalidate 行为一致,不恶化 |
| 全局闸嵌套死锁 | acquire 30s 超时 → 软失败 ToolResult |
| sse 后台写让 replay 窗口不完整 | 终态前带超时 drain;前端已有降级;丢帧仅影响调试台 |
| 三 PR 互相踩(runtime.py 都动) | 按 PR1→PR2→PR3 串行合入,各自基于最新 main |

## 不做的事

- 不缓存 embedder delegate/client 对象(主成本是 I/O 非构造)。
- 不动 `HOLD_CHARS=64` 的值、不动 `output_screen` 默认值(安全默认)。
- 不做跨副本失效广播(无 Redis pub/sub;TTL 兜底,与本仓立场一致)。
- 不给 run 级并发加闸(只闸委托;run 并发是另一话题)。
- 不动 token 帧持久化设计(live-only 不变)。
- `resolve_embedder/resolve_reranker` 死代码清理不在本期(follow-up 单开)。
