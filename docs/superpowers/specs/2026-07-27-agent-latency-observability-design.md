# Agent 延迟可观测性 + 连接复用（一期）设计

> 二期（P1 入口链并行化 / P3 缓冲快路径 / P4 全局并发闸 / `verify_reads` 默认值）在文末「二期 backlog」列出，不在本 spec 范围内。

## 背景

从 Agent 性能角度做的一次全后端代码走查，得出六项优化空间。走查结论按影响排序：

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| P0 | 每次 LLM/embed/rerank/工具调用重建 httpx 客户端 | 8 处 | 每次调用一次完整 TLS 握手 |
| P1 | 入口链全串行，首 token 前最多 3 次 LLM 往返 | `graph_builder/builder.py:1423-1442` | TTFT 大头 |
| P2 | TTFT 指标测的不是 TTFT | `sse.py:437-475` | 优化了看不见 |
| P3 | 流式首字被 64 字符缓冲卡住（guards 关了也卡） | `streaming_redact.py:119-135` | 感知延迟 |
| P4 | subagent 并行无全局并发闸 | `builder.py:1305` + `tools/subagent.py` | 稳定性，非速度 |

一期取 **P2 + P0**（可观测性 + 连接复用），因为 P2 是其余五项的前提：没有正确的测量口径，任何优化都无法验证。

### 走查中发现的额外事实

`runtime.py:820` 的 `ResolvingEmbedder.embed()` 每次调用都走一遍：

```python
secret_ref = await self.resolver.resolve_provider(...)   # DB 读
api_key = await self.secret_store.get(...)               # vault 读
delegate = OpenAICompatibleEmbedder(client=HTTPEmbeddingClient(api_key=api_key), ...)
```

`ResolvingReranker`（`:865`）同款。所以召回链上那两次调用，除 TLS 握手外每次还额外吃一次 DB 读 + 一次 vault 读。

一期**不修**这个（secret 缓存有轮换失效的安全取舍，需单独拍板），但 span 布点要能量到它 —— 见下文 `memory.embed` 的 attribute 设计。

## 目标

让首字延迟从「看不见」变成「分段可见」，同时消掉每次调用的 TLS 握手，并把省下的毫秒当场量出来。

一期的合同是**让它可测量**，不是承诺提速百分之多少。

## 非目标

- 不承诺具体的提速幅度。bench 测出多少就是多少；若测出握手只省了几十毫秒，那本身就是有价值的结论（说明瓶颈在别处，二期该把力气压在 P1）。
- 不改 `verify_reads` 等任何默认值。
- 不做 secret / provider-resolve 缓存。
- 不给连接池参数做 UI（运维 env 即可，不是租户旋钮）。

---

## 一、范围与交付顺序

五个 task。中间那次基线是整期的支点：

| # | 内容 | 层 |
|---|---|---|
| 1 | 入口链 span 布点（8 个新）+ 标签契约 + facade `group` 字段 | orchestrator + common + control-plane |
| 2 | `first_output_seconds{source}` 指标 + 老指标改名 | orchestrator |
| 3 | bench 脚本，**跑第一次存基线** | tools |
| 4 | 连接池 8 处改造 | orchestrator + control-plane |
| 5 | TraceView 分解条 + 配色分组 | admin-ui |

**顺序理由**：1、2 落完才有数据可抽；4 落完才有对比对象。Task 3 跑出的基线直接进 Task 4 的 PR 描述，这一期自带「优化前后」的演示样本。

**并行性**：Task 1 与 Task 2 文件不重叠（1 动 `memory.py` / `builder.py` / `tracing.py` / `trace_facade.py`，2 动 `sse.py`），可并行两个 worktree。Task 3 依赖 1+2。Task 4 独立于 1/2/3，但须等 3 跑完基线才合。Task 5 依赖 1。

---

## 二、后端：span 布点

### 2.1 布点表（新增 8 个）

```
expert_work.memory.recall                    ← 新，父 span
  ├ expert_work.memory.resolve_mode          ← 新（tenant_config 读）
  ├ expert_work.memory.query_rewrite         ← 已有（memory.py:471）
  ├ expert_work.memory.embed                 ← 新（attr: resolve_ms / secret_ms）
  ├ expert_work.memory.retrieve              ← 新
  ├ expert_work.memory.rerank                ← 新（attr: resolve_ms / secret_ms）
  ├ expert_work.memory.verify                ← 已有（memory.py:431）
  └ expert_work.memory.bump_access           ← 新
expert_work.orchestrator.workspace_ingest    ← 新
expert_work.orchestrator.context_gates       ← 新（prune + window + compress 合一）
```

粒度取「中档」：每一段都对应二期的一个具体优化靶子（`resolve_mode` → P1.2、`bump_access` → P1.1、`workspace_ingest` → P1.3），所以二期每一项都能量出前后差。

更细的粒度（mmr 选择、abstain 判定、redact、每个 context gate 单列）**明确不做**：那些是纯 CPU 毫秒级，span 自身的 OTel 上下文切换 + 导出开销跟被测对象同量级，噪音盖过信号。

### 2.2 命名不撞车

`memory.rerank` 与已有的 `orchestrator.rerank`（知识库文档重排，`_LLM_LABELS` 里标「文档重排」）是不同的东西，不撞名。

### 2.3 隐藏 I/O 用 attribute 露出，不占瀑布行

`memory.embed` 与 `memory.rerank` 两个 span 上挂 `resolve_ms` / `secret_ms` 两个 attribute，把「背景」一节发现的那两次隐藏 I/O 暴露在详情面板里，但不新增瀑布图行数。

### 2.4 标签契约必须做单源

现有 `_LLM_LABELS`（`trace_facade.py:319`）被 parity 测（`test_trace_facade_normalize.py:154`：`set(_LLM_LABELS) == set(LLM_SPAN_PURPOSES)`）锁死在 `LLM_SPAN_PURPOSES` 上。

新增的是**非 LLM** span，塞进 `LLM_SPAN_PURPOSES` 会污染语义（那是「LLM 用途」契约）。所以照它的形状再来一套：

- common：`TRACED_SPANS: frozenset[str]` —— span 真名的单源
- facade：`_SPAN_LABELS: dict[str, str]` —— 中文文案
- 一条 parity 测锁住两者

**不做单源会 drift**：改了 span 名忘改标签，`_classify`（`trace_facade.py:404`）的 fallback 会让瀑布图静默退回裸英文名 `memory.embed`，跟旁边的「规划」「记忆校验」中英混排，且不炸 CI。

### 2.5 facade 新增 `group` 字段

`_classify` 现在只产四种 kind（llm / tool / session / span），新增的 8 个全落 `span` → 前端一片灰。

facade 为入口链 span 多返一个 `group: "entry" | null`，前端按它上色。

**不复用 `purpose` 字段**的理由：`purpose` 被 parity 测锁着且语义是「LLM 用途」。虽然 `isAuxLlm()`（`TraceView.tsx:216`）带 `kind === "llm"` 前缀不会误伤，但 `purpose="recall"` 挂在非 LLM span 上会让下一个读代码的人困惑。

---

## 三、后端：指标

```
expert_work_first_output_seconds{source="token"}   ← _publish_token 第一帧
expert_work_first_output_seconds{source="node"}    ← 第一个 agent 节点的 updates 帧
expert_work_session_first_node_seconds             ← 老 _session_ttft_seconds 改名
```

### 3.1 为什么要两个 source

有三类 run 一个 token 帧都不会发：

- 开了 output judge 的（`make_token_sink:216` 返回 `None` —— judge 要看全文才能判）
- LLM cache 命中的（不进 router）
- provider 不支持流式 / 流式关掉的

只在 `_publish_token` 打点的话这些 run 全部不进直方图，而**开了 judge 的 agent 恰恰是最慢的那批** —— 幸存者偏差会让优化数据失真。

两个 source 让聚合时既能只看 `source="token"` 比纯流式路径（P3 的靶子只影响这条），也能合起来看真实的「用户多久见到第一个字」。

### 3.2 `source="node"` 的实现陷阱

现有的 `first_chunk_seen`（`sse.py:470`）认的是**任意**第一个 chunk。有 `memory_recall` 的 run，那第一个 chunk 是 recall 不是 agent。

所以 node 路径必须挑 `"agent" in jsonable_chunk` 的那一帧，**不能沿用现有 flag**。两条路径互斥、先到先得（有 token 流时 token 必定先于 agent 的 updates 帧，因为 token 在节点内部发、updates 帧在节点结束时发）。

### 3.3 老指标改名

`_session_ttft_seconds` → `_session_first_node_seconds`。它名字说 TTFT 但测的是首节点完成，留着会持续误导。单开发者项目，Grafana 面板改名成本可忽略。

---

## 四、后端：连接池

### 4.1 形状

一个进程级 `httpx.AsyncClient`，挂 control-plane lifespan。httpx 内部本来就按 `(scheme, host, port)` 分连接池，不需要我们再按 provider 分。

timeout 走 per-request 覆盖 —— 流式那条 `httpx.Timeout(self.timeout_s, read=None)`（`openai.py:341`）照样传得进去。`transport` 字段（测试注入点）原样保留。

### 4.2 八处改造点

| 类 | 位置 | 构造点 | 注入方式 |
|---|---|---|---|
| LLM | `openai.py:290/343`、`anthropic.py:328/389` | `agent_factory.py:2163/2206` + `openai_compatible.py` 的 7 个 `make_*_client` 工厂（agent_factory 的 dict 映射其中 5 个，self_hosted / azure 走别的分支） | `build_agent` 加 kwarg 传下去 |
| 召回 | `embedder.py:84` | `runtime.py:820/897` | 直接给 `ResolvingEmbedder` |
| 召回 | `rerank.py:63` | `runtime.py:865/934` | 直接给 `ResolvingReranker` |
| 工具 | `web_search.py:120` | ToolEnv 单例字段 `web_search_client`（`assembly.py:115`） | `TavilyClient` 加 `http` 字段 |
| 工具 | `sandbox.py:259` | ToolEnv 单例字段 `supervisor_client`（`assembly.py:45`） | `SupervisorClient` 加 `http` 字段 |

后两处的收益不在 TTFT 而在工具调用延迟。纳入一期的理由：改造套路一模一样，留尾巴的真实成本是下次还得把这套重新理解一遍。

需要额外留意：`sandbox.py:214` 那个工厂形式跟另外几处形状不同，且带自己的 timeout 语义；`web_search` 也带自己的 transport。task 实施时要逐个确认语义没漂。

### 4.3 兼容契约

每个 client 类加：

```python
http: httpx.AsyncClient | None = None   # 有则复用，无则退回 per-call 建
```

**没接线的路径（测试、eval CLI、单测）字段是 `None`，行为逐字节不变。** 这是这个改造能安全落地的关键 —— 不需要一次性改完所有调用点。

---

## 五、前端

### 5.1 TraceView 顶部分解条

```
首字 4.2s ─────────────────────────────────────────────
[ 召回 2.0s ][ 规划 1.6s ][ 摄取 .05 ][ 门 .01 ][ 首调 0.6s ]
   ↑ 点一段 → 下面瀑布对应行高亮
```

数据全从已有的 `RunTrace.spans`（`api/trace_facade.ts`）算，不加接口。右端边界是首个 `llm_call` 的结束。

放 TraceView 顶部而非 TurnCard，是因为它跟下面的瀑布同源同一份 trace 数据，点一段能高亮对应 span。

### 5.2 配色分组

`kindDotColor` / `kindBarColor`（`TraceView.tsx:224-235`）加一档：`group === "entry"` 时用独立颜色。现有四档（llm 蓝 / aux-llm 淡蓝 / tool 紫 / 其余灰）不动。

### 5.3 TurnCard 保持现状

现有的「首字 {{d}}」（`useTokenStream.ts:36` 算，`zh-CN.ts:1257` 文案）继续只在正在流式的那一轮显示。历史轮要看首字，去 TraceView 顶部的分解条。

理由：两处都做是重复，TraceView 那条信息更全。

### 5.4 前端不做的

- 不改 `verify_reads` 的 tooltip（那属于二期 P1.4 的配套）
- 不给连接池 / 并发闸做平台配置节

---

## 六、bench 与验收

### 6.1 脚本

`tools/bench/entry_latency.py`，几十行：

```
输入:  --agent <name@version> --prompt-file <f> --runs 10
过程:  真栈发 N 轮 → 每轮从 trace facade 拉 span 树
输出:  各段 median / p95 + 首字 median / p95，写 JSON
```

不是完整 benchmark 框架，就是个取数脚本。二期量 P1.1 / P1.2 / P1.3 / P3 时直接复用同一个脚本，那四项每一项都能给出「省了多少毫秒」。

基线 JSON 进仓 `tools/bench/baselines/`，文件名带日期，内容带机器标识 + 环境 + commit sha（换机器数字不可比，标记让人一眼看出而不是默默误比）。

### 6.2 验收线

| 时点 | 动作 | 产出 |
|---|---|---|
| Task 1+2 合完 | 跑 bench 第一次 | `baselines/<date>-before.json` |
| Task 4 合完 | 跑 bench 第二次 | PR 描述里 before/after 对照表 |
| Task 5 合完 | 手动看 TraceView | 分解条数字跟 bench 对得上 |

### 6.3 成功判据

1. TraceView 上能看到入口链每段耗时，标签是中文，入口链 span 与 LLM / tool 视觉可区分
2. `first_output_seconds` 两个 source 都有数据，judge-on 的 run 不再落在盲区
3. bench 前后对照表存在且数字可解释 —— **不承诺具体幅度**

---

## 七、风险

| 风险 | 缓解 |
|---|---|
| span 太多拖慢 run | 中档粒度已排除毫秒级细分；bench 第一次跑同时观察 span 开销本身 |
| 共享 client 的 event-loop 绑定 | orchestrator / control-plane 单 loop 长驻；`None` 回退路径保证测试不受影响 |
| 共享 client 让某个 provider 的 timeout 语义漂 | per-request timeout 覆盖；task 4 逐处确认，尤其 sandbox / web_search |
| 标签契约 drift | common 单源 + parity 测（§2.4） |
| bench 数字跨机器误比 | 基线 JSON 内嵌机器 / 环境 / commit 标识 |

---

## 二期 backlog（不在本 spec 范围）

| 项 | 内容 | 卡点 |
|---|---|---|
| P1.1 | `bump_access`（`memory.py:637`）改 fire-and-forget | 无，零风险 |
| P1.2 | tenant_config 加 TTL 缓存 | 无 |
| P1.3 | `workspace_ingest` 与 `memory_recall` 并行 | 无 |
| P1.4 | `verify_reads` 默认值重估（现 `agent_spec.py:398` 默认 `True`） | 要拍板：安全取舍 |
| P3 | guards 全关时跳过 64 字符缓冲（`streaming_redact.py:119`） | 无 |
| P4 | subagent 全局并发闸 | 要拍板：是否给 UI |
| — | secret / provider-resolve 缓存（§背景发现） | 要拍板：轮换失效策略 |
| — | `runtime.py:214` agent 缓存无界、无 TTL | 内存问题非性能 |
| — | `sse.py:484-491` updates 帧 publish → persist 串行，DB 写在流路径上 | 改有界队列后台写 |
| 可选 | `verify_reads` tooltip 补代价说明；judge 开启不流式的提示 | 跟 P1.4 / P4 绑 |

二期每一项都用 §6.1 的脚本量收益。`verify_reads` 那个取舍到时候是数据题（「verify 占首字的百分之多少」摆在面前）而非空谈。
