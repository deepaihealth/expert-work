# 按 run 的用量对外可见 —— 设计

**日期**:2026-09-11
**ROADMAP**:B-52
**状态**:设计定稿,待写实施计划

---

## 1. 问题

对接方(deep-ai-health)要按 run 给他们的客户扣费,今天拿不到可靠的用量数据。

**他们唯一的来源是 SSE 流**:主线用量在 `updates` 帧的 `usage_metadata`,子任务用量在
`worker` 事件 `kind=end` 的 `usage` / `usage_by_model`,客户端自己累加。

**而他们两个 agent 都跑 `stream_format=items`** —— 这个模式下 `updates` 帧不出现,
八个条目类型(`conversation_items.py`)逐个核过**零 usage 字段**。

后果他们描述得准确:

> 派了 worker 的 run 反而比纯主线 run 收得多,纯对话轮收 0。

**比整体低估更糟** —— 是结构性的反向偏差:用得越多的形态反而收得越少。

控制台侧的 `/v1/usage/cost` 与 `/v1/usage/tokens` 挂 `console_only()`,API key 拿不到,
且按**月**聚合,没有 run 维度。

---

## 2. 已经现成的(先说这个,避免重复造)

立项时我判断这是"大"工程,**实测大部分能力已经存在**:

| 能力 | 状态 | 位置 |
|---|---|---|
| 按 trace 聚合用量 | ✅ 现成 | `TokenUsageStore.totals_by_trace_ids()` |
| 按 `(provider, model)` 分桶 | ✅ 现成(B-42) | `TokenTotals.by_model` → `ModelTokenTotals` |
| 四档 token 计数 | ✅ 现成 | `ModelTokenTotals` 的四个字段 + `llm_calls` |
| `agent_run.trace_id` 已绑 | ✅ 现成 | `bind_exec_trace`,三个执行入口全覆盖 |
| `end` 帧单一构造口 | ✅ 现成 | `orchestrator.sse.end_frame_data()` |
| run 归属校验 | ✅ 现成 | `load_owned_run(tenant, agent_code, user_id, run_id)` |

`ModelTokenTotals` 的 docstring 写的正是我们要的语义:

> A run whose worker ran on a different model (`dynamic_workers.model`) shares this
> trace with its main line; only the split lets a consumer price each part at its
> own rate instead of the main model's.

**所以缺的只有三件**:`usage_kind` 过滤、一个对外端点、`end` 帧里塞这个字段。

### 2.1 不需要加列、不需要迁移

`token_usage` 没有 `run_id` 列,但有 `trace_id`;`agent_run.trace_id` 已经绑好。
两表按 trace 连接即可。

**子 run 与父共用同一个 trace**,实证在 `_child_run.py:519` 的 docstring:

> 线上实例(run f562fa69)对话页显示 175,137 tok,**同一 trace 下** worker 另有 69 次
> 调用共 3,317,974……(计费不受影响:`token_usage` 按 `{parent}-worker` 记全了)

所以按 trace 聚合天然包含整棵调用树。

### 2.2 不会双计 —— 与墙钟时长的教训相反

`token_usage` 由 `after_llm_call` 中间件写入,**每次 LLM 调用落一行**,不是按 span 嵌套记。
同一段注释把这点写死了:

> 每个 worker 只发一个 end 帧,不会重复(**与 duration 的双计教训相反:那次是同一段
> 时间既进工具行又进 subagent 行**)

墙钟会双计是因为父 span 的时长天然包含子 span;token 是按调用累加的,结构上不重复。

---

## 3. 对外契约

### 3.1 `end` 帧带 `usage_by_model`(对接方主路径)

`POST /v1/agents/{agent_code}/runs` 的 `mode:"stream"`,run 结束的 `end` 帧:

```json
{
  "status": "success",
  "run_id": "...",
  "artifacts": [ ... ],
  "usage_by_model": [
    { "provider": "glm",  "model": "glm-5.3",
      "input_tokens": 146024, "output_tokens": 2819,
      "cache_read_tokens": 129315, "cache_creation_tokens": 0 },
    { "provider": "kimi", "model": "kimi-k3",
      "input_tokens": 23548, "output_tokens": 230,
      "cache_read_tokens": 17916, "cache_creation_tokens": 0 }
  ]
}
```

### 3.2 `GET .../runs/{run_id}/usage`(对账兜底)

```
GET /v1/agents/{agent_code}/runs/{run_id}/usage?user_id=<必填>
```

- `external_only()` + `require("session", "read")` —— **与 `GET /{agent_code}/runs` 同档,
  不新造 resource**
- `user_id` **必填无默认**,漏传是 422 不是"列出整个租户"(对外平面既有规矩)
- 归属校验走 `load_owned_run()`,不属于该 `(user, agent)` 返 **404 不是空结果** ——
  响应不能携带存在性信息
- 返回体的 `usage_by_model` 与 `end` 帧**逐字段同形**,外加 `run_status`

**用途分工**:日常扣账走 `end` 帧;GET 用于对账、补数、或没接住 end 帧时。

### 3.3 三条口径

**① 字段缺席 ≠ 零**

`usage_by_model` **缺席**(历史 run 无记录)与**空数组**(确实零用量)是两回事。
这是 `artifacts` 字段的既有规矩,`end_frame_data` 的 docstring 写着:

> `None`(迁移前的历史 run 无记录)时**字段缺席**而不是放 null,文档口径:
> 缺席 = 老 run 无记录,别当零交付。

`usage_by_model` 逐字照抄,**不新造口径**。

**② 只取 `usage_kind='conversation'`**

`token_usage` 还有 `quality_sampling`(质量抽检)与 `skill_evolution`(技能进化),
那是平台自身开销,不该算到对接方头上。今天测试环境全是 `conversation` 所以看不出来,
**quality monitor 一开就有**。

**③ 消费者不要再累加 `worker` 事件的 `usage`**

`usage_by_model` 已含整棵树。再叠加 worker 帧就是双计。已书面告知对接方。

---

## 4. 数据传递:照抄 `artifacts` 的结构

`end_frame_data()` 有**四个**调用点(不是两个 —— 这正是"执行入口三个、规矩只写一处
就漏两个"的同款陷阱),`artifacts` 用**三种来源**喂它:

| # | 位置 | 分支 | `artifacts` 来源 |
|---|---|---|---|
| 1 | `sse.py:1595` | `sse_consumer`(**第三方主路径**) | bridge end 帧 data 透传 |
| 2 | `_run_event_stream.py:288` | replay | `run_artifacts` 参数(调用方从 `run.artifacts` 传) |
| 3 | `_run_event_stream.py:467` | live-join probe | `run_probe()` 返回值 |
| 4 | `_run_event_stream.py:499` | live 实时 | bridge end 帧 data 透传 |

`usage_by_model` **走同一套传递结构**,四个点全覆盖。具体到每条路:

- **点 1 / 点 4(bridge 透传)** —— 发布侧 `orchestrator.sse` 的 `finally` 块里
  `bridge.publish_end(run_id, status=…, artifacts=…)` 加一个 `usage_by_model=…`
  参数,消费侧照 `arts = entry.data.get("artifacts")` 的样子取。
  **`publish_end` 那处是一处覆盖全部终局分支的**(它自己的注释列了:正常结束 /
  `RunCancelledError` / `CancelledError` / MaxSteps / 兜底 `Exception` / PAUSED),
  所以两个 live 点只需这一处改动。
- **点 2(replay)** —— 调用方(`external_events.py:122` / `runs.py:2034`)已经在传
  `run_artifacts=run.artifacts`,同处再查一次 usage 传 `run_usage=`。
- **点 3(live-join probe)** —— `run_probe()` 现在返回 `(status, artifacts)`,
  扩成三元组或返回一个小 dataclass。**扩元组要改全部实现与调用方**,计划阶段定形状。

**发布侧那次查询必须 try/except 兜住**:它跑在终局路径上,查询失败绝不能把
`publish_end` 带崩 —— 失败就让字段缺席(语义正好是"无记录"),run 的终局状态照发。

### 4.1 但数据源不同:现查,不固化

`artifacts` 固化在 `agent_run.artifacts` 列上,因为它是**内存快照**
(`_manifest_snapshot()`)—— 不固化就没了。

`usage` 不同:它本来就在 `token_usage` 表里,按 trace 聚合**幂等且永久**(行不会删)。
所以重放时现查与固化结果一致,**固化只会多一列**。

**结论:不加 `agent_run.usage_by_model` 列。**

### 4.2 单一聚合入口

新增一个共享函数,四个调用点 + GET 端点**全部经过它**,防止 live 与 replay 两条路分叉
(`end_frame_data` 的 docstring 记着这两条流的字段集合**已经分叉过一次**):

```
load_run_usage(trace_id) -> list[ModelTokenTotals] | None
```

- 内部调 `totals_by_trace_ids([trace_id])`,取 `by_model`
- **加 `usage_kind` 过滤**(见 §5.1)
- `trace_id` 为 `None`(run 尚未开始执行)→ 返回 `None` → 字段缺席

---

## 5. 要改的三处

### 5.1 `totals_by_trace_ids` 加 `usage_kind` 过滤

**现状:SQL 与 in-memory 两个实现都没有过滤**,把所有 `usage_kind` 的行一并计入。

改法:加一个 `usage_kinds: Sequence[str] | None = None` 参数,`None` 保持现有行为
(**不过滤**),对外路径显式传 `("conversation",)`。

`None` 默认值的理由是**不改现有消费者的行为**(控制台 Runs 列表/详情今天看到的是全部
开销)。控制台**应该**看哪些 kind 是另一个问题 —— `quality_sampling` 是对该 run 的抽检、
算它头上尚可争辩,`skill_evolution` 是异步流程、大概率不该算。**本次不动,留作 backlog。**

**两个实现必须同步改**,且谓词逐字同义 —— 这是本仓库反复踩过的坑
(SQL ↔ in-memory 谓词必须 byte-identical)。

### 5.2 `end_frame_data()` 加参数

```python
def end_frame_data(
    *, run_id: UUID, status: str | None,
    artifacts: list[dict[str, Any]] | None = None,
    usage_by_model: list[dict[str, Any]] | None = None,   # 新增
) -> dict[str, Any]:
```

`None` → 字段缺席(照抄 `artifacts` 那行的形状)。

### 5.3 新端点

`external_runs.py` 加 `GET /{agent_code}/runs/{run_id}/usage`。

---

## 6. 边界行为

| 情形 | 行为 |
|---|---|
| run 未结束 | GET 返回**到目前为止**的量 + `run_status`,由调用方判断是否落账 |
| `trace_id` 为 `NULL`(排队中,未开始执行) | 字段缺席 / GET 返回空 `usage_by_model` + `run_status` |
| 历史 run(有 trace 但无 usage 行) | 字段缺席 —— **不是零** |
| 厂商未报 usage | 该键缺席,**不填 0**(填 0 会让消费者把"未知"当成"免费") |
| run 不属于该 `(user, agent)` | **404**,不是空结果 |
| `cache_creation_tokens` | GLM / Kimi 不报,恒为 0(LangChain 侧 cache 细分是 Anthropic only) |

---

## 7. 明确不做

**① 不返回金额。** 用户 2026-09-11 拍板走「只给 token + 模型」,由对接方按自己的价位表
换算。代价已知:他们算出来的是自己口径的估算,与我方账单不必然一致。

**② 不拆缓存折扣。** 用户 2026-09-11 明确「先不考虑缓存的问题」。

**已知代价,记在这里以免日后当成遗漏**:`input_tokens` **已包含** `cache_read_tokens`
(LangChain `usage_metadata` 约定:`input_tokens` 是所有输入类型之和,`input_token_details`
是它的细分)。实测验死 —— 测试环境 4322 行中 `cache_read > input_tokens` **零行**,
`cache_read < input_tokens` 3905 行。

按 `input_tokens` 全量乘标准输入价,相对四档精算**高估 3~5 倍**(测试环境全库缓存占比
86.5%,生产 92.0%)。这是明知的取舍,不是缺陷。

**③ 不加 `token_usage.run_id` 列,不加 `agent_run.usage_by_model` 列。** 理由见 §2.1 / §4.1。

**④ 不改控制台现有 `/v1/usage/*`。** 那是月度聚合 + `console_only()`,与本条正交。

---

## 8. 一条已知风险,不在本次范围

`totals_by_trace_ids` 的 SQL 注释写着:

> Tenant scoping rides on RLS (the sessionmaker sets the tenant GUC), so `trace_id`
> collisions across tenants can't leak.

**但运行期 RLS 目前是空转的** —— app 以 superuser + BYPASSRLS 连库,租户隔离实际只靠
ORM 的 WHERE 子句(见 ROADMAP RLS 条目)。所以这句注释描述的保护**今天不成立**。

实际风险极低:`trace_id` 是 32 位 hex,跨租户碰撞概率可忽略;且对外路径先过
`load_owned_run()` 的归属校验才会走到聚合。**记在这里是因为注释在说谎**,
RLS 真 enforce 之后这句才成立。

---

## 9. 验收判据

1. items 模式下,`end` 帧带得出 `usage_by_model`,且**四个调用点全部覆盖**(每个点都有
   测试钉住,不是只测 replay)
2. 派了 worker 的 run,`usage_by_model` 含 worker 那一桶,且**总和不双计**
   (对照 `token_usage` 原始行)
3. `usage_kind='quality_sampling'` 的行**不出现**在对外结果里
4. 无 usage 记录的 run → 字段**缺席**,不是空数组也不是零
5. 他人的 run → **404**
6. GET 与 `end` 帧对同一个 run 返回**逐字段相同**的 `usage_by_model`
