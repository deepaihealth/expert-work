# 对外 API 文档站 · 第三轮可读性审计（不改稿，只出清单）

**审计对象**：`apps/admin-ui/docs-site/guide/*.md`
**审计基线**：`main @ 4a3d37c7`。审计开始时 `chat.md` / `errors.md` 还在 in-flight PR（worktree `upload-a1`）里；审计过程中该 PR **已合并**（#1193，commit `4a3d37c7`），我逐文件比对确认 **worktree 与 main 十个文件全部逐字节相同**，因此下文所有行号都可直接按 `main` 上的 `apps/admin-ui/docs-site/guide/*.md` 定位。

**审计镜头**（产品负责人原话转化）：
1. 概念句必须回答 **谁发起 → 发给谁 / 什么时候 / 读者要做什么**；
2. 一张表只回答一个问题，表头是自解释名词短语；**按类别分组、逼读者自己把类别映射回成员的表一律算坏**；
3. 每一个字段行必须给出 **名称 / 类型 / 含义 / 可能取值 + 每个取值的含义**。代码里是有限集时，「取值不在这里穷举」不可接受；「除 X / Y 外还有」这种表头不可接受。

---

## 目录

- [A. 文档留成开集、代码其实是闭集的字段取值](#a)
- [B. 没回答「谁→谁 / 何时 / 做什么」的概念段](#b)
- [C. 结构不清的表](#c)
- [D. 章节格式缺陷](#d)
- [E. 跨章一致性](#e)
- [F. 优先级排序（Top 15）](#f)

---

<a id="a"></a>
## A. 文档留成开集、代码其实是闭集的字段取值

> 每条格式：**章节 + 标题 + 字段** → 文档现状 → 代码真值（含每个取值的含义）+ `file:line`。
> 全部取值都在代码里核对过，没有推测；确实是开集的，明确写「开集」并给出**今天能出现的闭子集**与「客户端遇到没见过的值该怎么办」。

### A-1 ❗ `updates` → `messages[]` → `type:"tool"` → `status`　（负责人点名）

- 位置：`sse-events.md:374`
- 文档现状：> 「执行状态。实测 `"success"`;非 `"success"` 即为这一步工具失败，**具体取值不在这里穷举**」
- **代码真值：闭集，恰好 2 值**，来自 langchain-core 的 `Literal`，平台没有扩展：

| 取值 | 含义 | 平台在哪里写入 |
|---|---|---|
| `success` | 工具正常返回。**这是默认值**，成功路径从不显式赋值 | 默认值定义 `.venv/lib/python3.13/site-packages/langchain_core/messages/tool.py:82`；成功构造点 `services/orchestrator/src/orchestrator/graph_builder/builder.py:2871`（不传 `status=`） |
| `error` | 这次工具调用失败/被拦。7 种成因见下 | 见下表 |

`status="error"` 的**全部 8 个写入点**（这就是「工具失败」的完整成因清单，值得直接写进文档）：

| 成因 | `file:line` |
|---|---|
| 动作安全屏（action screen）拦截 | `services/orchestrator/src/orchestrator/graph_builder/builder.py:1243` |
| 工具自己抛异常 | `.../graph_builder/builder.py:2341` |
| `ToolBlockedError`（工具被策略禁用） | `.../graph_builder/builder.py:2379` |
| 模型给的参数没过工具 schema 校验 | `.../graph_builder/builder.py:2812` |
| 派发阶段异常 | `.../graph_builder/builder.py:2836` |
| 审批被 `reject` | `.../graph_builder/_approval.py:226` |
| 审批参数绑定摘要对不上（checkpoint 漂移） | `.../graph_builder/_approval.py:277` |
| 续跑时注入的占位结果 | `services/orchestrator/src/orchestrator/resume.py:59` |

- 建议改法：字段行写 `"success"（工具正常返回，默认值）/ "error"（这次调用失败或被平台拦下）`，并在下面补一句「`error` 的成因见 …」。当前 `sse-events.md:492` 的渲染示例 `msg.status !== "success"` 是对的，但文档没给读者判据。

---

### A-2 ❗ `worker` → `data.outcome`　（负责人点名 + 文档示例值在代码里不存在）

- 位置：`sse-events.md:546`（字段表）、`sse-events.md:582`（完整示例）
- 文档现状：> 「这个 worker 执行完的结果。**取值不在这里穷举**，遇到没见过的照常展示即可」；示例里写 `"outcome":"completed"`
- **代码真值：闭集，恰好 3 值**。`build_worker_end_frame(outcome=…)` 全仓只有 2 个调用点、3 条取值路径：

| 取值 | 含义 | `file:line` |
|---|---|---|
| `success` | 子任务正常跑完 | `services/orchestrator/src/orchestrator/tools/_child_run.py:223`（`"max_steps" if raised_max_steps else "success"`） |
| `max_steps` | 子任务把自己的步数预算用完了 —— **是部分结果，不是失败**（父 agent 会拿这份部分进展继续推理，见 `_child_run.py:93-96` 的 docstring） | `.../tools/_child_run.py:223` |
| `cancelled` | 子任务在执行中被取消（`RunCancelledError`） | `.../tools/_child_run.py:199` |

- **`"completed"` 在代码里根本不存在**（`sse-events.md:582` 的示例值是编的）。类型别名 `TrajectoryOutcome`（`packages/expert-work-protocol/src/expert_work/protocol/eval_dataset.py:54`）里还有第 4 个值 `"failed"`，但 **worker end 帧没有任何一条路径会发它**——未捕获异常会直接向上抛，那种情况下**根本不发 `end` 帧**。这一点必须写进文档，因为前端的 `store.workers` 会永远留一张「运行中…」的卡（`sse-events.md:612-615` 的示例正好会踩到）。
- 建议改法：三值全列 + 每值含义 + 补一句「异常终止时不会有 `end` 帧，前端需要给 worker 卡设兜底（比如父 run 的 `end` 到达时把所有未收尾的 worker 卡标成未知）」。

---

### A-3 ❗ `retry` → `error_class` / `attempt` / `backoff_s`　（文档示例值在代码里不可能出现）

- 位置：`sse-events.md:786-792`（字段表 + 尾句）、`sse-events.md:799`（示例）
- 文档现状：`error_class` = 「触发这次重试的错误类型名」（无取值）；示例写 `"error_class":"ReadTimeout"`；`backoff_s` = 「这次重试前会等多少秒」（无范围/默认值）
- **代码真值**：

| 字段 | 类型 | 真值 | `file:line` |
|---|---|---|---|
| `attempt` | int | **恒为 `1`**。`MAX_RUN_RETRIES = 1`，一次 run 最多一条 `retry` 帧 | `services/orchestrator/src/orchestrator/run_retry.py:50`；闸 `services/orchestrator/src/orchestrator/sse.py:574`；自增 `sse.py:581` |
| `error_class` | string | 名义上是 `type(exc).__name__`（开集），但**帧只在 `is_transient_run_error(exc)` 通过后才发**，而可重试类型注册表今天只有一个元素 → **今天的闭集 = `{"AllProvidersExhaustedError"}`**（含义：所有配置的 LLM 供应商 fallback 链都试完了还是失败） | 发帧 `sse.py:592`；闸 `sse.py:577`；注册表 `run_retry.py:45`（`TRANSIENT_RUN_ERRORS = (AllProvidersExhaustedError,)`） |
| `backoff_s` | number（float） | 默认 **`10.0`**；由环境变量 `EXPERT_WORK_RUN_RETRY_BACKOFF_S` 覆盖，**钳制在 `[1.0, 120.0]`**；解析失败回落默认值。**无抖动、无指数退避**（因为只有一次） | 默认 `run_retry.py:54`；上下界 `run_retry.py:55-56`；取值函数 `run_retry.py:78-88` |

- `"ReadTimeout"` 不可能出现（它不在 `TRANSIENT_RUN_ERRORS` 里）。示例必须换成 `AllProvidersExhaustedError`。
- 另需写清「客户端该怎么办」：重试是服务端做的，**不需要也不应该由客户端重连**；`sse-events.md:814` 的注释说对了，但字段表本身没说。

---

### A-4 ❗ `approval` → `node`

- 位置：`sse-events.md:734`
- 文档现状：> 「是哪个节点提出的审批请求」——暗示可变
- **代码真值：常量 `"tools"`**。全仓只有一个 `ApprovalRequest(...)` 构造点，`node` 是硬编码字面量：
  - 构造点：`services/orchestrator/src/orchestrator/graph_builder/_approval.py:160`，`node="tools"` 在 `_approval.py:162`
  - `request_id` 的哈希输入里也硬编码了同一个 `"tools"`：`_approval.py:161`
  - ⚠️ 协议层 docstring `packages/expert-work-protocol/src/expert_work/protocol/approval.py:98` 举例说可能是 `'ask_for_approval'` —— **那条 docstring 是过期的**，文档不要照抄它。
- 建议改法：`node` | string | 恒为 `"tools"`（审批一律由工具节点提出）。**客户端不需要按它分支**；将来若新增取值会是向后兼容的新值。

---

### A-5 ❗ `approval` → `reason_kind`（取值有，但缺「谁设的 / 客户端拿它干什么」）

- 位置：`sse-events.md:735`
- 文档现状：五个值 + 括号里的中文短语，够用；但没说**这五个值分成两类、来源完全不同**，也没说客户端该拿它做什么。
- **代码真值**：`ApprovalReasonKind`，闭集 5 值，`packages/expert-work-protocol/src/expert_work/protocol/approval.py:68-74`：

| 取值 | 谁产生 | 含义 | `file:line` |
|---|---|---|---|
| `policy_gate` | **平台**（声明式路径） | 这个工具被 Agent manifest 的 `PolicySpec.approval_required_tools` 列为强制审批，或属于 TE-4 不可逆工具。**硬编码，非 agent 可控** | 赋值 `services/orchestrator/src/orchestrator/graph_builder/_approval.py:156` |
| `missing_info` | **Agent 自己**（调 `ask_for_approval`） | 它缺信息，要人补 | 枚举 `approval.py:70`；白名单 `_approval.py:46-53` |
| `ambiguous_requirement` | Agent 自己 | 需求有歧义，要人澄清 | `approval.py:71` |
| `approach_choice` | Agent 自己 | 要人在几种做法里选一个 | `approval.py:72` |
| `risk_confirmation` | Agent 自己 | 高风险动作要人确认。**同时是兜底默认值**——agent 给了平台不认识的字符串时会被强制归到这个值 | `approval.py:73`；兜底 `_coerce_reason_kind` `_approval.py:112-121` |

- 关键遗漏：**`policy_gate` 与其余四个的 `reject` 后果不同**（run-control.md:127-130 讲了，但 `sse-events.md` 的 `approval` 一节没有交叉链接到那里）。而 `reason_kind` 正是客户端**唯一能提前区分这两条路径**的字段——这才是它的用途，文档没写。
- 另：`_AGENT_REASON_KINDS` 白名单（`_approval.py:46-53`）保证 wire 上**永远只会是这 5 个值**，即使 agent 乱传。这个「保证」值得写出来，否则客户端会写防御性分支。

---

### A-6 ❗ `updates` → `messages[]` → `type:"ai"` → `response_metadata.finish_reason`　（文档把开集写成了闭集，方向反了）

- 位置：`sse-events.md:363`
- 文档现状：> `"tool_calls"`(还要继续下一步)/ `"stop"`(这一步已经答完) —— 只给两个值，读起来像穷举
- **代码真值：这是唯一一个「文档收得太紧」的字段。它是厂商原样透传，是开集，而且在一整条 provider 路径上根本不存在。**

| 事实 | `file:line` |
|---|---|
| OpenAI 兼容路径：原样拷贝厂商的 `choice["finish_reason"]`，不做任何映射 | `services/orchestrator/src/orchestrator/llm/providers/openai.py:811-817`；消费点 `openai.py:764` |
| **Anthropic 路径：`_from_anthropic_response` 根本不写 `response_metadata`** → 这些模型上 **`finish_reason` 字段整个缺失** | `services/orchestrator/src/orchestrator/llm/providers/anthropic.py:1001-1041` |
| 平台自己合成的第 3 个值：`"stream_idle_timeout"`（流式读空闲超时，平台注入，非厂商值） | `services/orchestrator/src/orchestrator/llm/providers/_streaming.py:190` |
| 仓内实际观察到的 OpenAI 兼容取值 | `stop` / `tool_calls` / `length` / `content_filter` —— `openai.py:184`、`services/orchestrator/tests/test_llm_provider_openai_stream.py:18,92`、`services/orchestrator/tests/test_llm_streaming_wire.py:70,137` |
| 后端**没有任何 Python 代码读它**（produce-and-forward）；只有前端读 | 消费点 `apps/admin-ui/src/pages/agent_detail/playground/TurnMeta.tsx:32` |

- 建议改法：明确写「**开集，厂商原样透传，可能整个缺失**」+ 今天已知子集表（`stop` 答完 / `tool_calls` 还要继续 / `length` 被 max_tokens 截断 / `content_filter` 被厂商内容策略拦 / `stream_idle_timeout` 平台流式读空闲超时）+ 「**不要拿它判断这一轮是否结束**，判据是 `end` 帧的 `status`；判断这一步是否还要继续，看 `tool_calls` 数组是否为空」。

---

### A-7 `end` → `status`（文档正确，但可以给「为什么是四值」的闭合证明）

- 位置：`sse-events.md:876`、`sse-events.md:879-887`
- 文档现状：**正确**。四值、含义、客户端动作齐全，`interrupted`/`paused` 的坑也点了。
- 代码佐证（可加到文档做背书）：
  - 闭集定义：`EXTERNAL_END_STATUSES = frozenset({"success","paused","interrupted","error"})` — `services/orchestrator/src/orchestrator/sse.py:1456`
  - **结构性钳制**：`end_frame_data` 对不在集合里的值一律强制成 `"error"` — `sse.py:1472`。所以「四值」是**代码保证**，不是约定，值得写一句。
  - 内部 outcome → wire 映射（6 进 4 出）：`sse.py:267-274`。其中 `cancelled → interrupted`、`max_steps → error` 是两处**收敛**，文档现在只提了 `timeout → error` 一处。
  - 回放分支的 `RunStatus` → wire 映射：`services/control-plane/src/control_plane/api/_run_event_stream.py:39-45`。
- **需要补的两条收敛**：`max_steps`（步数用尽）在 `end` 里也是 `error`——这跟 `guard` 章反复强调的「`tripped` 不是错误」看起来矛盾，读者会困惑。必须点破：`guard.tripped` 说的是「平台主动收尾了这一轮」，而这一轮**最终**在 `end` 里仍然算 `error`（`sse.py:737-742` → `session_outcome = "max_steps"` → `sse.py:272` → `"error"`）。这是全站最容易误导人的一处。

---

### A-8 `token` → `channel` / `step`（文档正确，表头形式有问题，见 C-3）

- 位置：`sse-events.md:228-242`
- 代码真值：闭集 3 值，`services/orchestrator/src/orchestrator/graph_builder/streaming_redact.py:163-207`

| `channel` | 含义 | 除 `step`/`channel` 外的字段 | `file:line` |
|---|---|---|---|
| `content` | 答案正文的增量片段（已过脱敏） | `text`(string) | `streaming_redact.py:185`（增量）、`:204`（收尾 flush） |
| `reasoning` | 思维链的增量片段（只有推理类模型有；独立走第二条脱敏流） | `text`(string) | `streaming_redact.py:188`、`:207` |
| `tool_args` | 模型开始发起第 N 个工具调用（**只报名字，不报参数**） | `tool_index`(int)、`name`(string)，**没有 `text`** | `streaming_redact.py:192-199` |

- `tool_args` **每个 `tool_index` 只发一次**（首次看到名字时）：`streaming_redact.py:189-191`。文档 `sse-events.md:242` 说对了。
- `step` 起点：`make_token_sink(step=step_count + 1)` — `services/orchestrator/src/orchestrator/graph_builder/builder.py:902-903`；初始 `step_count = 0`（`services/control-plane/src/control_plane/api/runs.py:473`）→ 第一步的 token 帧 `step: 1`。有回归测试守着这个对齐：`services/orchestrator/tests/test_token_step_alignment.py`。文档 `sse-events.md:238` 正确。

---

### A-9 `guard` → `kind` / `guard` / `detail`（文档正确，可补两条硬事实）

- 位置：`sse-events.md:627-642`
- 代码真值：全仓**恰好 4 个发帧点**，都在 `services/orchestrator/src/orchestrator/graph_builder/builder.py`：

| `guard` | `kind` | `detail` | 触发条件 | `file:line` |
|---|---|---|---|---|
| `token_budget` | `tripped` | `{"spent","limit"}` | `spent >= limit` | `builder.py:840-847`（`guard=` 在 `:844`） |
| `max_steps` | `tripped` | `{"steps","max"}` | `step_count >= max_steps` | `builder.py:850-857`（`:854`） |
| `no_progress` | `tripped` | `{"streak","max"}` | `no_progress_streak >= max_no_progress` | `builder.py:859-866`（`:863`） |
| `token_budget` | `warning` | `{"spent","limit"}` | 首次跨过 80% | `builder.py:873-880`（`:877`） |

- 文档 `sse-events.md:641-642` 的 tip「只有 `token_budget` 会发 `warning`」**正确**；80% 常量核实：`WARN_PCT: ClassVar[float] = 0.8` — `services/orchestrator/src/orchestrator/tools/_guards.py:34`，判据 `:44-45`。
- 两条可补的硬事实：
  1. 三条 `tripped` 全在同一个 `budget_exhausted` 分支里（`builder.py:561`），**同一步可能同时来两三条 `tripped`**，前端别假设一轮只有一条。
  2. `tripped` 之后平台会追加一条收尾指令、并且**这一步不给模型任何工具**（`builder.py:832-838`），所以用户仍会拿到一段完整回答，只是被截断了推理。这句话正是「`tripped` 不是错误」的真正理由，比现在的 `sse-events.md:659-663` 更有说服力。

---

### A-10 `compaction` → 四个字段（缺「这是估算值」这一关键限定）

- 位置：`sse-events.md:691-696`
- 文档现状：`tokens_before` = 「压缩前的 token 数」、`tokens_after` = 「压缩后的 token 数」——读起来像精确计量。
- **代码真值**（`CompactionStats`，`services/orchestrator/src/orchestrator/context/compressor.py:121-138`；载荷构造 `services/orchestrator/src/orchestrator/graph_builder/builder.py:718-725`）：

| 字段 | 类型 | 真值/口径 | `file:line` |
|---|---|---|---|
| `passes` | int | 这次 `compress()` 里**真正跑成功**的「摘要中段」轮数；`<= 0` 时整个帧不发 | `compressor.py:678`、自增 `:645`、不发帧 `:671-672` |
| `tokens_before` | int | **估算值**：进入 `compress()` 时的估算 token 数。估法 = 字符数 // 4（`_CHARS_PER_TOKEN = 4`），或注入的 tiktoken 估算器。**不是计费口径** | `compressor.py:679`；估算器 `:527-528`；常量 `:153` |
| `tokens_after` | int | 同上口径，最终返回时的估算值 | `compressor.py:673`、`:680` |
| `summary_chars` | int | 结果里最后一条 `<context-summary>` 系统消息的字符数；没有则 `0` | `compressor.py:682`；函数 `_summary_chars` `:417-432` |

- ⚠️ **文档的渲染示例有 bug**：`sse-events.md:712` 写 `const saved = data.tokens_before - data.tokens_after;` 然后直接显示。代码里官方口径是 `max(0, tokens_before - tokens_after)`（`compressor.py:674`，且 docstring `:129` 明说），因为这是两次估算之差、**可能为负**。文档示例会给用户显示「省下约 -37 token」。
- 建议改法：字段表标注「估算值」，示例改成 `Math.max(0, …)`，并补一句「这四个数只用于提示，不要拿来做计费或用量核对」。

---

### A-11 `error` → `name` / `message`（开集，但要给出保证子集与判据）

- 位置：`sse-events.md:831-834`
- 文档现状：`name` = 「错误类型名。**取值不是固定枚举**，别按它写分支」——方向对，但没给读者任何可用信息。
- **代码真值**：两个发帧点，形状相同 `{"message": str(exc), "name": type(exc).__name__}`：
  - `MaxStepsExceededError` 分支：`services/orchestrator/src/orchestrator/sse.py:752-753`（此时 `name` **必然**是 `"MaxStepsExceededError"`）
  - 通用 `except Exception` 分支：`sse.py:781-782`（`name` 是任意 Python 异常类名）
- **明确不会发 `error` 帧的两条路径**（这条比枚举更有用）：`RunCancelledError`（`sse.py:702-725`）与 `asyncio.CancelledError`（`sse.py:726-736`）——取消不产生 `error` 帧，只有 `end{status:"interrupted"}`。文档现在没说，会让人写出「没收到 error 就是成功」的错代码。
- 建议改法：`name` | string | **开集**（是服务端 Python 异常类名，会随平台演进变化）。**平台今天唯一保证语义的值是 `MaxStepsExceededError`**（= 步数预算耗尽，同时会有一条 `guard{guard:"max_steps",kind:"tripped"}`）。**客户端处理原则**：不要按 `name` 写分支；只用它做日志/上报，最终判据一律看 `end.status`。**取消不会产生 `error` 帧。**

---

### A-12 `gap` → `from` / `to`（文档正确，可补一条）

- 位置：`sse-events.md:1183-1186`
- 代码真值：闭集 2 字段 `{"from": int, "to": int}`，闭区间；**只由 control-plane 产生，orchestrator 从不产生**。
  - 发帧点：`services/control-plane/src/control_plane/api/_run_event_stream.py:240`（`_gap_frames`）、`:279`（容量溢出快路径）、`:358`（`backfill_short`）
  - 四种内部 `reason`（`hole_passed` `:247` / `holes_overflow` `:285` / `hole_unfilled_at_end` `:308` / `backfill_short` `:358`）**故意只进日志、不上 wire**：`_run_event_stream.py:227-230`。文档 `sse-events.md:1188` 说对了。
  - 容量上限 `_MAX_TRACKED_HOLES = 4096`（`_run_event_stream.py:50`）；连续 seq 会被合并成一帧（`_merge_ranges` `:66-74`）。**「一帧 gap 可能覆盖成千上万个 seq」** 这点文档没说，前端 `sse-events.md:1206` 那句「有 ${data.to - data.from + 1} 条…」在极端情况下会显示一个吓人的数字。

---

### A-13 `truncated` → `next_seq` + 页大小（文档正确，但页大小有重复定义的漂移风险）

- 位置：`sse-events.md:1232`、`sse-events.md:1236-1238`
- 代码真值：单字段 `{"next_seq": int}` — `services/control-plane/src/control_plane/api/_run_event_stream.py:180`。发 `truncated` 时**不发 `end`**（`:181` 直接 `return`）。
- 页大小 = `MAX_LIST_LIMIT = 500` — `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:522`（`_run_event_stream.py:24` 从这里 import）。
- ⚠️ **同名常量在另一个模块里独立定义了第二份**：`packages/expert-work-runtime/src/expert_work/runtime/runs/event_store.py:42` 也是 `MAX_LIST_LIMIT = 500`，而**真正钳制查询的是后者**。今天两处一致，但这是漂移隐患。文档 `sse-events.md:1232` 已经写了「别把这个数字写死」，是对的——建议**不要在文档里出现「500」这个数字**，改成「一页有上限，具体值可能变；判据永远是有没有收到 `truncated`」。
- 截断判定用「探第 501 行」而不是「行数 == 页大小」，避免整除误报：`_run_event_stream.py:370-374`。这条可以不写进对外文档。

---

### A-14 `worker` 信封的四个字段：`label` / `agent_ref` / `role` / `depth`　（文档示例是一个**不可能存在**的组合）

- 位置：`sse-events.md:513-531`（表）、`sse-events.md:574`（示例）
- 文档现状：`label` = 「人类可读标签」、`agent_ref` = 「用的是哪个 Agent」、`role` = 「没指定角色时是 `null`」、`depth` = 「数值越大嵌套越深」（无范围）。示例写 `"label":"检索行业报告","agent_ref":"researcher","role":"researcher"`。
- **代码真值：只有两条产生路径，四个字段的取值是成套绑定的**：

| | 静态子 Agent（manifest 里声明的 `subagents:`） | 动态 worker（模型调内建 `spawn_worker`） |
|---|---|---|
| `label` | 子 Agent 的**工具名** = `subagent.name` | 字面量 **`"spawn_worker"`** |
| `agent_ref` | `subagent.agent_ref`，形如 **`name@version`** | 形如 **`dynamic:<role>`**，无 role 时 `dynamic:general` |
| `role` | **恒为 `null`**（这条路径不传 `extra_meta`） | 模型 `focus` 工具参数的自由文本（strip 后为空则 `null`）——**开集，LLM 供给，无枚举** |
| `file:line` | `services/orchestrator/src/orchestrator/tools/subagent.py:176-188` | `services/orchestrator/src/orchestrator/tools/spawn_worker.py:222-236`；`role` 来源 `spawn_worker.py:174-175`，工具 schema `:151-158`（纯 `"type":"string"`） |

约定说明在 `services/orchestrator/src/orchestrator/tools/_child_run.py:87-89`。

- ⚠️ **`sse-events.md:574` 的示例组合在代码里不可能出现**：`role` 非空 ⇒ 必然是动态路径 ⇒ `label` 必然是 `"spawn_worker"`、`agent_ref` 必然是 `"dynamic:researcher"`。示例把两条路径的字段混在了一起。
- `depth`：**范围 1–3**。`child_depth = subagent_depth + 1`（`services/orchestrator/src/orchestrator/tools/assembly.py:393` 静态 / `:430` 动态），顶层 agent 的 `subagent_depth = 0`；硬上限 `MAX_SUBAGENT_DEPTH: Final = 3`（`services/orchestrator/src/orchestrator/tools/subagent.py:52`），到达上限的 worker 不再注册委托类工具（`assembly.py:381-387`、`:426-428`）。文档应给出 `1–3`——前端 `sse-events.md:595` 拿 `depth * 16` 算缩进，知道上界才好排版。
- `parent_worker_id` 非 `null` 当且仅当 `depth > 1`：`services/orchestrator/src/orchestrator/tools/_child_run.py:123`。文档 `sse-events.md:516` 说法等价，可以直接给这个更硬的判据。
- `wseq`：**per-worker 局部计数器**，`0` = start，`1..N` = update，`N+1` = end。初始化 `_child_run.py:129`，自增 `:141`/`:172`，读取 `:198`/`:222`。文档 `sse-events.md:551-555` 的警告是对的，但可以直接给「0=start / 末位=end」这个结构。

---

### A-15 `worker` → `kind` + 三种 `data` 的截断上限（数值正确，来源可标）

- 位置：`sse-events.md:521`、`:558`
- `kind` 闭集 3 值：`start` / `update` / `end` — `services/orchestrator/src/orchestrator/tools/_worker_events.py:44-56`、`:59-67`、`:70-89`。
- 截断常量核实无误（`_worker_events.py:27-29`）：`WORKER_CONTENT_EXCERPT = 500`、`WORKER_ARGS_EXCERPT = 200`、`WORKER_RESULT_EXCERPT = 500`。截断函数 `_excerpt` 在 `:108-109`：`text[:limit] + "…"`，所以**输出最长是 `limit + 1` 个字符**（多出来的是 U+2026）。文档 `sse-events.md:558` 说「超过上限的部分被截掉、末尾补一个 `…`」——正确，但没说这会让长度变成 501 / 201，做长度校验的客户端会踩。
- `update.data.step_count` 是**条件字段**：只有当节点写入里 `step_count` 是 int 时才带（`_worker_events.py:63-65`）。文档 `sse-events.md:539` 说「可选，只在部分节点出现」——正确但含糊，可以直说「只有 `agent` 节点的 update 有」。

---

### A-16 `updates` → 节点名 + 节点写入的内部通道（文档说「不是穷举」，其实注册点是闭的）

- 位置：`sse-events.md:326-331`（内部通道 bullet）、`sse-events.md:347-351`（节点名 warning）
- 文档现状：节点名列了 5 个 + 「还有一些节点按 Agent 配置才会注册」；内部通道列了 3 组 + 「这几条也不是穷举」。
- **代码真值：`add_node()` 全仓只有 7 处，是闭集**（是否出现取决于 Agent 配置，但**名字集合是闭的**），`services/orchestrator/src/orchestrator/graph_builder/builder.py`：

| 节点名 | 是否总有 | 出现条件 | `file:line` |
|---|---|---|---|
| `agent` | 是 | 总是 | `builder.py:1430` |
| `tools` | 是 | 总是 | `builder.py:1431` |
| `memory_recall` | 否 | 开了长期记忆 | `builder.py:1444` |
| `planner` | 否 | `workflow.type == plan_execute` | `builder.py:1449` |
| `workspace_ingest` | 否 | 接了工作区 | `builder.py:1453` |
| `memory_writeback` | 否 | 开了记忆回写 | `builder.py:1472` |
| `reflect` | 否 | manifest 里有 `reflection:` 块 | `builder.py:1479` |

- 文档漏掉了 **`planner`** 和 **`reflect`** 两个节点名。既然是闭集，直接给这张表比「不是穷举」有用得多——「遇到不认识的就忽略」这条防御建议**保留**即可。
- **`sse-events.md:333-345` 那个 danger 框（「节点写入可能整个是 `null`」）与代码不符**：全部 7 个节点的实现都返回 `dict`（可能是 `{}`），没有一处返回 `None`（`builder.py:1150` `tools` 空返回 `{}`；`graph_builder/memory.py:568/571/627/664/730/734` `memory_recall`；`memory.py:1141/1161` `memory_writeback`；`graph_builder/workspace_ingest.py:96/112/114/122`）。生产者侧只是**防御性**地判了 `isinstance(node_val, dict)`（`sse.py:567-570`）。
  → 真栈里观察到的 `{"workspace_ingest":null}` 很可能是 **LangGraph 自身**在节点被跳过时产出的（不是这七个函数的返回值）。这条框子里的「客户端必须判 `null`」的建议**依然正确、必须保留**，但**归因写错了**（写成了「节点写入是 null」，实际是「节点这一步没有写入」）。建议改成事实描述 + 归因留白，不要写成节点实现的行为。
- `_duration_ms` 注入点：`sse.py:564-570`。口径 = **相邻两个 chunk 到达的墙钟毫秒差**，基线是 RUNNING 标记（`sse.py:518`），所以第一条约等于 TTFT。文档 `sse-events.md:323` 说法正确。

---

### A-17 run `status`（`query.md` 5.4）—— 8 值有，但**没有一个值给了含义**，且 `timeout` 是不可达的

- 位置：`query.md:263`（字段行）、`query.md:268-275`（「`status` 取值」表）、`query.md:227`（查询参数）
- 文档现状：一张两列表，把 8 个值挤成 2 行，第二列是「是否最终状态」。**没有任何一个值的含义**。这正是负责人第 3 条投诉的形态。
- **代码真值**：`RunStatus`，`packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py:21-42`；DB CHECK 约束同集 `packages/expert-work-persistence/src/expert_work/persistence/models/agent_run.py:21-23`（约束名 `agent_run_status_valid` 在 `:74`）：

| 取值 | 是否最终状态 | 含义 | `file:line` |
|---|---|---|---|
| `pending` | 否 | 同步 SSE 路径上「已创建、正在转 running」的瞬时态 | `schemas.py:24` |
| `queued` | 否 | 已入分布式队列：记录已持久化但还没有任何进程认领。某个实例的 `RunQueueWorker` 会 CAS 抢占 `queued → running` | `schemas.py:30` |
| `running` | 否 | 正在执行 | `schemas.py:31` |
| `success` | 是 | 正常跑完 | `schemas.py:32` |
| `error` | 是 | 执行失败（含步数耗尽） | `schemas.py:33` |
| `timeout` | 是 | **保留值：枚举里有、`end` 帧映射里有，但全仓没有任何代码写入它**（`grep RunStatus.TIMEOUT` 只命中枚举定义、终态集合、两处只读分类） | `schemas.py:34`；只读点 `services/control-plane/src/control_plane/scheduler.py:89`、`.../runs/store.py:43` |
| `interrupted` | 是 | 调用方主动取消 | `schemas.py:35` |
| `paused` | 是 | 停在审批门，**可续跑**；与 `interrupted` 的区别是「等人裁决」而不是「已中止」。**注意它算最终状态** | `schemas.py:42` |

  终态集合 `TERMINAL_RUN_STATUSES`（含 `PAUSED`）：`schemas.py:54-62`。
- **两条要改的事实**：
  1. `query.md:275` 说「这里的 `timeout` 在 `end` 里显示为 `error`」——映射本身是对的（`_run_event_stream.py:44`），但 `timeout` **今天永远不会出现在 run 列表里**。用一个不可达值当作「两处字样不同」的唯一例证，读者对着真实数据永远验证不了。真正会出现的差异是 **`error`（步数耗尽）** 这一路。
  2. `query.md:227` 的 `status` 查询参数只写「见下方」。真值：类型就是这个 8 值枚举（`services/control-plane/src/control_plane/api/external_runs.py:78`），传别的值 → **422 `INVALID_REQUEST`**（`services/control-plane/src/control_plane/app.py:2561-2580`）。这两条都该写进参数表。
- 另外：run 列表返回的是**原始 DB 值，8 值全都可能**（`external_runs.py:152` → `r.status.value`，无映射）；而 `queue` 模式创建 run 的 202 响应体里 `status` 是**硬编码字面量 `"queued"`**（`services/control-plane/src/control_plane/api/runs.py:1016`）——`chat.md:88-98` 的示例正确，但没说这是硬编码、幂等重放时返回的是**原 run 的真实状态**（`services/control-plane/src/control_plane/api/agents.py:265`），也就是 8 值中的任意一个。这是 `chat.md` 2.8 「重试时拿到什么」一节的隐藏坑。

---

### A-18 会话列表 / run 列表 / 消息列表 的筛选与分页（缺默认值、缺上限、缺无效值行为）

- 位置：`query.md:5`（全章通则）、`query.md:90-95`、`query.md:157-163`、`query.md:222-229`
- 文档现状：通则说「`limit`（1–200，默认 50）和 `offset`（≥0，默认 0）」——**数值全部正确**，逐条核对：

| 端点 | `limit` | `offset` | 其它 | `file:line` |
|---|---|---|---|---|
| `GET .../sessions` | 1–200，默认 50 | ≥0，默认 0 | `user_id` 必填 1–255 | `external_sessions.py:148-150` |
| `GET .../sessions/{id}/messages` | 同上 | 同上 | 同上 | `external_sessions.py:223-225` |
| `GET .../runs` | 同上 | 同上 | `session_id` UUID 可选、`status` 8 值枚举可选 | `external_runs.py:76-80` |
| `GET /v1/agent-catalog` | 同上 | 同上 | **无 `user_id`** | `external_agent_catalog.py:65-66` |
| `GET .../workspace/files` | **无** | **无** | 只有 `user_id` | `external_workspace.py:66` |
| `GET .../artifacts` | **无** | **无** | 只有 `user_id` | `external_artifacts.py:128` |

- **文档缺的三条**：
  1. **只有 Agent 目录返回 `total`**（`external_agent_catalog.py:122`、`:137-148`）；`sessions` / `messages` / `runs` **三个列表都不返回 `total`，也不返回 `has_more`**，只回显 `limit`/`offset`（`external_sessions.py:203-209`、`:274-280`；`external_runs.py:144-164`）。`query.md:76-80` 只在 5.1 讲了「用 `total` 判断翻页」，其余三个列表读者拿不到 `total`，文档从头到尾没告诉他们该怎么判最后一页。**这是一个功能性的文档缺口，不只是格式问题。**
  2. **归档会话被硬编码排除、不可开关**：`include_archived=False` 是写死的（`external_sessions.py:183`）。`query.md:147` 说「这个接口当前没有查询参数能拿回已归档的会话」——正确。
  3. **`messages` 的分页是在 Python 里对整份 transcript 切片的**（`external_sessions.py:263`：`turns[offset : offset + limit]`），不是数据库分页。会话很长时每次请求都要读全量。这条值得给对接方一句提示。
- **无效值统一行为**（文档里散在各处，值得在 `conventions.md` 集中说一次）：任何 pydantic/FastAPI 校验失败（漏 `user_id`、`limit=0`、`limit=201`、`offset=-1`、UUID 格式错、`status` 传了枚举外的值、`since_seq=-1`、请求体带未知字段）→ **422 + `{"success":false,"data":null,"error":{"code":"INVALID_REQUEST","message":<第一条 pydantic 消息>}}`**。拦截器：`services/control-plane/src/control_plane/app.py:2561-2580`，作用域是 `path.startswith("/v1/agents/")` 或 `"external" in route.tags`（`app.py:2566-2568`）。
- **`user_id` 是租户没见过的值时，各端点行为不一致**——`query.md` 在 5.2/5.4/5.6/5.7 分别写了「返回空列表」，但没有一处集中对照，而行为确实分三档：

| 端点 | 未知 `user_id` 的结果 | `file:line` |
|---|---|---|
| 会话列表 / run 列表（不带 `session_id`）/ 产物列表 / 工作区文件列表 | 200 + 空数组 | `external_sessions.py:171-178`、`external_runs.py:126-133`、`external_artifacts.py:150-151`、`external_workspace.py:85-86` |
| run 列表（**带** `session_id`） | 404 `SESSION_NOT_FOUND` | `external_runs.py:104-118` |
| 产物下载/删除 | 404 `ARTIFACT_NOT_FOUND` | `external_artifacts.py:201-202`、`:307-308` |
| 附件下载 | 404 `UPLOAD_NOT_FOUND` | `external_uploads.py:427-428` |
| 工作区单文件下载 | 404 `WORKSPACE_FILE_FAILED` | `_workspace_shared.py:127-128` |
| 消息 / 重命名 / 归档 / 取消 / 决策 / 事件回放 | 404 | `_external.py:337-345`、`:383-397` |

---

### A-19 审批决策请求体：`decision` / `modified_args` / `mode` / `request_id`

- 位置：`run-control.md:106-116`（参数表）、`run-control.md:121-125`（decision 三值表）
- 代码真值（`ExternalDecideRequest`，`services/control-plane/src/control_plane/api/external_approvals.py:100-156`，`frozen=True, extra="forbid"` 在 `:111`）：

| 字段 | 类型 | 必填 | 默认 | 约束 | 文档对不对 | `file:line` |
|---|---|---|---|---|---|---|
| `user_id` | str | 是 | — | 1–255 | ✅ | `:113` |
| `decision` | `Literal["approve","reject","modify"]` | 是 | — | 3 值 | ✅ | `:114` |
| `modified_args` | `dict \| None` | 仅 `modify` | `None` | 键与值**深度拒 NUL** | ✅（NUL 那条没写，可不写） | `:115`、`:148-151` |
| `reason` | `str \| None` | 否 | `None` | ≤2048；**故意不做 NUL 校验** | ✅ | `:116`、`:143-147` |
| `idempotency_key` | `str \| None` | 否 | `None` | ≤255，拒 NUL | ✅ | `:119`、`:153-156` |
| `mode` | `Literal["stream","queue"]` | 否 | `"stream"` | 2 值 | ✅ | `:124` |

- `request_id` **不被接受**：模型里没这个字段 + `extra="forbid"` ⇒ 传了就是 422 `INVALID_REQUEST`。`sse-events.md:733` 说「提交决策时不需要传」——**说轻了**，应该是「**传了会 422**」。
- `decision` 三值 → 落库 `ApprovalStatus` 的映射（`external_approvals.py:68-72`）：`approve → approved`、`reject → rejected`、`modify → modified`。`run-control.md:121-125` 的三值表讲了「之后 run 会怎样」，但没讲落库状态——影响不大，可不加。
- **`ApprovalStatus` 是一个 5 值闭枚举，但对外 API 从来不暴露它**（`packages/expert-work-protocol/src/expert_work/protocol/approval.py:163-176`；DB CHECK `packages/expert-work-persistence/src/expert_work/persistence/models/agent_approval.py:21,58`）：`pending`（唯一非终态）/ `approved` / `rejected` / `modified` / `timeout`（超时 job 自动拒）。**第 5 个值 `timeout` 对接方看不到、也查不到**——这就是为什么 `sse-events.md:739` 那句「过了这个点会被自动拒绝」必须给出「怎么发现它超时了」的答案（见 B-4）。
- **审批列表/详情端点对外不存在**：`GET /v1/approvals` 与 `POST /v1/approvals:decide` 都是 `console_only()`（`services/control-plane/src/control_plane/api/approvals.py:106`、`:151`）。第三方**只能通过 SSE 的 `approval` 帧**知道有待审批。文档从没明说这一点，读者会去找一个不存在的「待审批列表」接口。
- 超时预算：`policies.approval_timeout_s`，**默认 86400 秒（24h）**，范围 `[60, 604800]`（`packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:941-951`）。`timeout_at = requested_at + timeout_s`（`_approval.py:167`）。超时扫描把它判成 `reject` + `ApprovalStatus.TIMEOUT`，`reason="approval timed out"`，`decided_by="approval_timeout_sweep"`（`services/control-plane/src/control_plane/approval_timeout_sweep.py:194-197`）。`sse-events.md:739` 只说「过了这个点会被自动拒绝」，没说这个窗口**由 Agent 配置决定、默认 24h、最短 60 秒**——对接方无法据此设计自己的提醒/超时逻辑。

---

### A-20 取消端点：`stopped` + 请求体 + 「已结束」的定义

- 位置：`run-control.md:19-25`（参数）、`run-control.md:40-43`（`stopped` 表）、`run-control.md:45`
- 代码真值：
  - 请求体 `ExternalCancelRequest`：**恰好一个字段 `user_id`（1–255）**，`frozen=True, extra="forbid"` — `services/control-plane/src/control_plane/api/external_runs.py:37-46`。文档 `run-control.md:25` 正确。
  - 响应：`200 {"success":true,"data":{"run_id":"<uuid>","stopped":true|false},"error":null}` — `external_runs.py:213-219`。`stopped` 是纯 bool，**只有两值**，没有状态码/时间戳。文档正确。
  - `stopped: false` 的判据是 `run.status in TERMINAL_RUN_STATUSES`（`external_runs.py:206-207`），即 `success` / `error` / `timeout` / `interrupted` / **`paused`**。`run-control.md:45` 把这五个全列了，**完全正确**——这是全站少数几处把枚举摊开写的地方，可以作为改写其它表的范本。
  - 取消成功后 run 落到 **`interrupted`**：`external_runs.py:210-212` → `runs.request_cancel(...)`，CAS 只在当前状态是 `running`/`pending`/`queued` 时翻成 `INTERRUPTED` 并盖 `finished_at`（契约 `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:175-194`；参考实现 `:593-607`，`RunStatus.INTERRUPTED` 在 `:603`）。文档没写这个落点（读者只能从 `end.status` 反推）。
  - `user_id` 为纯空白**不会**报 `INVALID_USER_ID`，被 `load_owned_run` 折成 404 `RUN_NOT_FOUND`（`_external.py:386-397`）。文档 `run-control.md:63` 写了，正确。

---

### A-21 Agent 目录字段（4 个字段全部正确，但漏了「没有 status 字段」这条负空间）

- 位置：`query.md:51-57`
- 代码真值（`services/control-plane/src/control_plane/api/external_agent_catalog.py:126-135`）：

| 字段 | 类型 | 含义 | `file:line` |
|---|---|---|---|
| `agent_code` | str | `record.name` | `:129` |
| `display_name` | str | `spec.display_name or record.name`，**永不为空** | `:131`；源字段默认 `""` 在 `packages/.../agent_spec.py:1157` |
| `description` | str | 自由文本，可能是空串 | `:132`；默认 `""` 在 `agent_spec.py:1151` |
| `available` | bool | `record.name not in disabled` —— **只编码 kill-switch 状态** | `:133` |

- **唯一的枚举字段就是 `available` ∈ {true,false}**；没有 `status`、没有能力开关、没有 `mode`、没有版本号。`query.md:56` 写「现在能不能对它发起对话」——语义偏宽：它**只**反映「管理员有没有把这个 Agent 下线」，不反映配额、租户暂停、manifest 能不能构建。这三种情况都会让「发起对话」失败但 `available` 仍是 `true`。
- `ACTIVE` 是用「在不在列表里」编码的，不是字段：只有存在 ≥1 个 `AgentSpecStatus.ACTIVE` 版本的 code 才会出现（`external_agent_catalog.py:115-117`）；只剩 `deprecated`/`deleted` 的 code **整条不出现**（理由 `:77-88`）。`query.md:70-74` 讲对了。`AgentSpecStatus` 枚举 = `active` / `deprecated` / `deleted`（`packages/.../agent_spec.py:1389-1394`）。
- ✅ **缓存窗口数值：文档是对的，反倒是代码注释过期了。** `query.md:59/61` 写「最长约 5 秒 / 默认约 5 秒，平台可配」——核实无误：实际接线值 `kill_switch_cache_ttl_s: int = Field(default=5, gt=0)`（`services/control-plane/src/control_plane/settings.py:933`），在 `services/control-plane/src/control_plane/app.py:861-863` 传给 `AgentDisableService`，**覆盖**了类签名上的默认值 `ttl_seconds: float = 30.0`（`services/control-plane/src/control_plane/agent_disable_status.py:33`）。
  ⚠️ 但 `external_agent_catalog.py:94-106` 的 docstring 里**六次**写成「30s TTL 缓存」——**那段代码注释是过期的**。这不是文档缺陷，是**代码注释缺陷**，建议顺手提给实现方（照它去改文档会把对的改错）。
- 可补的一条：`query.md:61-68` 讲了两个方向的偏差会自愈，但没讲**为什么会有这个窗口**——`invalidate()` 只清「处理这次禁用请求的那个副本」自己的缓存，生产是多副本部署，接下一次 run 的副本可能不是那一个，只能等 TTL 自然过期（`external_agent_catalog.py:100-106`）。一句话就能让「为什么不能当前置校验」立住。

---

### A-22 产物 `kind`（4 值，文档已列，但漏了「不校验」这条重要负空间）

- 位置：`query.md:525`
- 文档现状：`document` / `code` / `data` / `other`，「由 Agent 保存时声明」——**取值和来源都对**。
- 代码真值：`ArtifactKind = Literal["document","code","data","other"]` — `packages/expert-work-protocol/src/expert_work/protocol/artifact.py:20`。每值含义（`artifact.py:19`）：`document` 文稿/报表类、`code` 源码、`data` 数据文件、`other` 其它。
- **要补的负空间**：DB 列是裸 `Text`、**没有 CHECK 约束**（`packages/expert-work-persistence/src/expert_work/persistence/models/artifact.py:39`，`__table_args__` 在 `:56-58` 只有唯一约束），读路径也不复校验。所以理论上可能读到这四个之外的值 → 客户端仍应按「不认识就当 `other`」处理。
- 另：**产物没有 status 字段**。生命周期靠 `deleted_at`（NULL = 存活）与 `archived_object_key` 表达，两者**都不对外序列化**（`packages/.../protocol/artifact.py:57-61`）。`query.md:520-527` 的字段表没有一句「没有状态字段」的说明，读者会去找。
- 对外的 `PATCH kind` **故意没镜像**（`external_artifacts.py:21-23`），第三方永远改不了 `kind`。可以写一句。

---

### A-23 上传响应 `type`（2 值）+ 允许的 MIME 清单 + inline/attachment 分支

- 位置：`chat.md:172-183`（文档上传响应）、`chat.md:197-208`（图片上传响应）、`chat.md:250-252`（Content-Disposition）、`errors.md:87`
- **`type` 是闭集恰好 2 值**，两个硬编码字面量，没有第三条分支：

| 取值 | 含义 | `file:line` |
|---|---|---|
| `document` | 走文档通路（落沙箱工作区，会被解析器读） | `services/control-plane/src/control_plane/api/external_uploads.py:325` |
| `image` | 走图片通路（落对象存储，EXIF 已剥离） | `.../external_uploads.py:382` |

  判定点只有一处：`external_uploads.py:294` —— `if content_type in settings.document_allowed_content_types:` 走 document，**否则一律进 image 分支**（不认识的类型在 image 分支被拒）。`content_type = (file.content_type or "").lower()`（`:289`）。

- **允许的 MIME —— 精确清单**（文档 `errors.md:87` 用扩展名口径写的「png/jpeg/webp/gif」「pdf/docx/xlsx/pptx/txt/md/csv」，**内容正确**，但真正的判据是 **MIME**，不是扩展名；对接方按扩展名理解会踩坑）：

| 通路 | 允许的 `Content-Type` | `file:line` |
|---|---|---|
| 文档 | `application/pdf`、`application/vnd.openxmlformats-officedocument.wordprocessingml.document`、`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`、`application/vnd.openxmlformats-officedocument.presentationml.presentation`、`text/plain`、`text/markdown`、`text/csv` | `services/control-plane/src/control_plane/settings.py:337-345` |
| 图片 | `image/png`、`image/jpeg`、`image/webp`、`image/gif` | `settings.py:325-330` |

  MIME→扩展名映射表（决定沙箱解析器派发，**取信 Content-Type 而不是文件名**）：`services/control-plane/src/control_plane/api/uploads.py:66-74`（文档）、`:56-61`（图片）。
  其它类型 → 400 `unsupported content type: ...`（`uploads.py:336-340`）→ 信封化成 `INVALID_UPLOAD`。

- **大小上限**（`errors.md:154` 写「文档 25 MiB，图片 10 MiB」——正确）：

| 限制 | 值 | `file:line` |
|---|---|---|
| 单图 | 10 MiB | `settings.py:322`（`multimodal_max_image_bytes`） |
| 单文档 | 25 MiB | `settings.py:334`（`document_max_bytes`） |
| 单次 run 处理图片数 | **8** | `settings.py:323`（`multimodal_max_images_per_run`），校验 `services/control-plane/src/control_plane/api/runs.py:347-350` |
| `files[]` 条数 | 64 | `services/control-plane/src/control_plane/api/agents.py:575` |
| ZIP 类文档声明的解压总量 | 200 MiB | `services/control-plane/src/control_plane/api/uploads.py:80`，校验 `:141-152` |

- **`Content-Disposition` 的 inline/attachment 判据：100% 按扩展名，`kind` 完全不参与**（`kind` 参数在 `_artifact_mime.py:234` 被 `del` 掉了）。决策函数 `services/control-plane/src/control_plane/api/_artifact_mime.py:190-239`，五条分支：

| 分支 | 结果 | 成员 | `file:line` |
|---|---|---|---|
| ① 可执行内容（**优先判**） | `attachment` | `.html .htm .xhtml .xht .svg .svgz .xml .xsl .xslt .mathml`（10 个） | `_artifact_mime.py:71-84`，判定 `:205-213` |
| ② 图片 | `inline` | `.png .jpg .jpeg .gif .webp .bmp .ico`（7 个；**`.svg` 明确不在**） | `:150-159` |
| ③ 结构化文本 | `inline` | `.json .jsonl .ndjson .yaml .yml .toml`（6 个） | `:140-147` |
| ④ 文本/代码（`text/plain`） | `inline` | 43 个扩展名：`.txt .log .md .markdown .rst .csv .tsv .ini .conf .py .js .mjs .cjs .jsx .ts .tsx .go .rs .java .kt .scala .rb .php .sh .bash .zsh .fish .sql .c .h .cc .cpp .hpp .cs .swift .dart .lua .r .jl .pl .vue` | `:90-136` |
| ⑤ 其它 / 无扩展名 | `attachment` + `application/octet-stream` | 兜底 | `:235-239` |

  → **对附件下载的实际后果**：`.txt`/`.md`/`.csv` 走 **inline**；`.pdf`/`.docx`/`.xlsx`/`.pptx` 落到 ⑤ 走 **attachment**；四种图片走 ② **inline**。`chat.md:250` 的括号说明「JSON 走不到这条 inline 分支」**正确**（因为上传根本不收 `application/json`）。
  `Content-Type` 用的是**上传时记录的 `row.mime_type`**，不是扩展名猜测（`external_uploads.py:522-531`），只借用 `infer_content_type` 的 disposition 那一半（`:517-519`）。`chat.md:250` 说「`Content-Type` 是上传时记录的 MIME」——正确。
- `query.md:442-446` 的工作区文件 `Content-Disposition` 表用「等」收尾（第一行末尾「`.md` 等」），把 43 + 6 + 7 个扩展名压成一句省略号。既然是闭集，应该给全（或至少给全「哪些走 attachment」这个安全相关的 10 个）。

---

### A-24 错误码总表：**代码里有、表里没有的 7 个码**

- 位置：`errors.md:9-53`
- 交叉核对结论：**表里的 39 个码在代码里全都存在，没有幽灵码**。反向缺 7 个：

| 缺失的 `code` | HTTP | 触发条件 | `file:line` | 第三方能不能撞上 |
|---|---|---|---|---|
| `TOO_MANY_IMAGE_REFS` | 422 | `files[]` 里图片超过 64 张（**信封化，能读到 `error.code`**） | `services/control-plane/src/control_plane/api/agents.py:1270-1275`；上限 `services/control-plane/src/control_plane/api/runs.py:120` | **能** |
| `INVALID_FILE_REF` | 422 | `files[]` 解析时抛出的、`detail` 里没带 `code` 的 `HTTPException` 的兜底码 | `agents.py:297`（raise）、`agents.py:1281`（兜底） | 能 |
| `APPROVAL_ERROR` | 任意未映射状态 | 审批决策端点遇到 `_DECISION_ERROR_CODES` 没覆盖的状态码时的兜底 | `services/control-plane/src/control_plane/api/external_approvals.py:215` | 能（低概率） |
| `UPLOAD_ERROR` | 任意未映射状态 | 上传端点同款兜底 | `services/control-plane/src/control_plane/api/external_uploads.py:103` | 能（低概率） |
| `AUTH_UNAUTHENTICATED` | 401 | 认证异常基类的默认码 | `services/control-plane/src/control_plane/auth/errors.py:17` | 能（低概率） |
| `AUTH_BACKEND_UNAVAILABLE` | 503 | 认证后端不可用 | `auth/errors.py:46` | 能 |
| `PLATFORM_SCOPE_FORBIDDEN` | 403 | 平台级 scope 检查 | `services/control-plane/src/control_plane/api/_authz.py:196` | 不能（对外路由到不了）——**可以不加，但值得在实现侧确认** |

- **`errors.md:55` 那段免责声明写得好**（明说「这张表不是穷尽清单」），但它把「表外的失败」全归给了「没有 `error.code` 的裸 detail」。上面前两个码 **是有 `error.code` 的信封**，属于「有码但表里没列」，不在那段免责范围内。
- 另一处需订正：**`errors.md:181` 关于图片数量的段落把两条上限讲对了**（`files[]` 64 项是 `INVALID_REQUEST`；单次 run 8 张是裸 `detail` 无 `error.code`），代码核实一致（`agents.py:575` / `runs.py:347-350`）。但它**漏了中间那一档**：`files[]` 里图片单独超过 64 张时是 `TOO_MANY_IMAGE_REFS`（信封，有码）。三档而不是两档。
- **另一个跨端点的漏项**：`conventions.md:44-49` 的响应头表少了 **`X-Expert-Work-Trace-Id`** —— 它由中间件在**每一个响应**上设置（`services/control-plane/src/control_plane/middleware/observability.py:37` 常量、`:106` 设置）。这是对接方报障时唯一能给的关联 id，**必须写进文档**，而且它是唯一一个「每种响应都有」的头，正好对冲 7.4 开头那句「别假设它们成套出现」。
- `X-Expert-Work-Stream-Mode` 闭集 2 值确认：`"live"` / `"replay"`，由 `run.status in TERMINAL_RUN_STATUSES` 决定（`services/control-plane/src/control_plane/api/external_events.py:102`、`:116`）。`conventions.md:48` 与 `chat.md:375-378` 都正确。

---

### A-25 会话列表 / 历史消息的字段（`role` / `channel` 已给值，`running` 缺判据）

- `query.md:201` `role` = `"user"` / `"assistant"` —— 两值，正确。
- `query.md:203` `channel` = `"final"` / `"commentary"` / `null` —— 三值 + 每值含义 + 「`user` 消息恒为 `null`」，**这是全站写得最好的一个字段行，可作为范本**。
- `query.md:130` `running` = 「这段会话里当前还有没有 run 在执行」。代码判据：`_ACTIVE_RUN_STATUSES = (PENDING, QUEUED, RUNNING)`（`services/control-plane/src/control_plane/api/external_sessions.py:97`）。**把这三个状态名写出来**，读者才能把它和 5.4 的 `status` 对上（尤其是要知道 `paused` **不算** running —— 等审批的会话这里是 `false`，这是个真会踩的坑）。

---

<a id="b"></a>
## B. 没回答「谁→谁 / 何时 / 做什么」的概念段

### B-1 ❗ 「心跳是注释行，不是事件」　（负责人第 1 条投诉，原样落地）

- 位置：`sse-events.md:69`（标题）、`sse-events.md:71` 起
- 引用首句：> 「连接活着、但**空闲约 15 秒没有任何事件**时，服务端发一行心跳:」
- **缺的答案 + 代码真值**：

| 缺什么 | 代码真值 | `file:line` |
|---|---|---|
| **谁发** | 服务端的 SSE 端点。**两条路径都发**：`POST .../runs`（`mode:"stream"`）的响应流，以及 `GET .../runs/{run_id}/events` 的 **live 分支** | `services/orchestrator/src/orchestrator/sse.py:1430`；`services/control-plane/src/control_plane/api/_run_event_stream.py:304` |
| **谁不发** | **回放（replay）分支从不发心跳**（它不会阻塞等待，一页读完就返回） | `_run_event_stream.py` 的 `_stream_replay`（`:163-185`）里没有心跳分支 |
| **发给谁** | 当前这条打开着的连接的客户端。**它不是 run 身上的事件**，不落库、不占 `seq`、重连拿不回来 | 心跳是裸注释字节，绕过 `format_sse`（`sse.py:1477-1488`） |
| **多久一次** | **15 秒的「空闲」计时器，不是固定节拍**——任何真实事件都会重置它 | 定时器 `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/memory.py:166-168`（`asyncio.wait_for(..., timeout=heartbeat_interval)`）；默认值 `15.0` 出现在 4 处：`sse.py:1402`、`_run_event_stream.py:302`（字面量）、`packages/.../stream_bridge/base.py:122`、`packages/.../stream_bridge/memory.py:136`。**没有任何调用方覆盖它**（`api/runs.py:1091-1096`、`:1802-1807`、`api/external_approvals.py:311-316` 都不传这个 kwarg） |
| **确切字节** | `: heartbeat\n\n` —— 一行以 `:` 开头的注释 + 一个空行 | `sse.py:1430`、`_run_event_stream.py:304` |
| **客户端要做什么** | ① 跳过所有以 `:` 开头的行；② **不要**把它当事件、不要动 `seq` 游标、不要当 `data` 解析；③ 拿它当「连接还活着」的判活信号 | 文档 `sse-events.md:78` 已写 ①，②③ 缺 |
| **多久没动静算断？** | **代码/配置里没有定义任何客户端侧判死阈值。** 我查了 `apps/admin-ui/src/api/sessions.ts`（`streamRun` `:231-267`、`parseSseStream` `:271-293` —— 纯 `reader.read()` 循环，只靠调用方的 `AbortSignal`，没有 watchdog）、`apps/admin-ui/src` 全量 grep（`reconnect|watchdog|lastEventAt|stale`）、`infra/` `manifests/` `configs/` `environments/` 全量 grep（`proxy_read_timeout|read_timeout|idle_timeout`，只有 k8s 探针的 `timeoutSeconds`，无关）。唯一的书面意向是一条设计笔记里建议 nginx `proxy_read_timeout 60s`（`docs/streams/STREAM-B-DESIGN.md:290`）| — |

- **建议（明确标为「建议值，非代码定义」）**：心跳间隔 15 秒 ⇒ 客户端读超时**建议设 45 秒（3 个心跳周期）**，超时即判连接已死并按 3.6 重连。
- ⚠️ **与 3.5 骨架自相矛盾**：`sse-events.md:80` 说「一段时间里连心跳都没收到，就该认为这条连接已经断了」但**不给数字**；而 `sse-events.md:988` 的骨架把 `readTimeoutMs` 硬编码成 `60_000`，正文从未解释这个 60 秒从哪来、和 15 秒心跳是什么关系。读者拿不到一个可执行的规则。
- ⚠️ **`best-practices.md:40` 开了一张空头支票**：「完整的重连策略——怎么找回 `run_id`、怎么用事件回放接口续上、**多久没收到数据就判定连接已死**——见 [3.6 断线重连与回放分页]」。3.6 全文（`sse-events.md:1059-1288`）**没有任何一处回答「多久」**。这是一条实打实的断链承诺。

---

### B-2 ❗ 「`worker` —— 什么时候发」

- 位置：`sse-events.md:503`（标题）、`sse-events.md:505` 起
- 引用首句：> 「Agent 把一部分工作委托给子任务(worker)时——比如子 agent、并行执行的子任务——子任务的**开始 / 每一步 / 结束**各发一次。」
- **缺的答案 + 代码真值**：

| 缺什么 | 代码真值 | `file:line` |
|---|---|---|
| **谁发起委托** | **模型自己**，通过调用一个工具。只有两种工具会产生 worker：manifest 里声明的静态子 Agent 工具，或内建的 `spawn_worker` | `services/orchestrator/src/orchestrator/tools/subagent.py:176-188`；`services/orchestrator/src/orchestrator/tools/spawn_worker.py:222-236` |
| **为什么 `parent_tool_call_id` 能配对** | 因为 worker 就是**那一次工具调用**的执行体 —— `ident.parent_tool_call_id = ctx.tool_call_id` | `services/orchestrator/src/orchestrator/tools/_child_run.py:126` |
| **「每一步」是哪一步** | 子 run 自己的 graph 每产出一个 `updates` chunk 就发一条 `update`（不是父 run 的步） | `_child_run.py:145-172` |
| **能嵌多深** | 1–3 层，硬上限 `MAX_SUBAGENT_DEPTH = 3` | `services/orchestrator/src/orchestrator/tools/subagent.py:52`；拒绝点 `services/orchestrator/src/orchestrator/tools/assembly.py:381-387`、`:426-428` |
| **客户端要做什么** | 文档 `sse-events.md:507` 说了「权威结果仍只认 `updates`」——**这一句很好**。缺的是：**收不到 `end` 帧怎么办**（异常终止时不会有 `end`，见 A-2），以及 `start` 丢了怎么办（`sse-events.md:603` 的示例是静默 `return`，但正文没解释这是有意的） | — |

---

### B-3 ❗ 「`updates` —— `data` 的最外层是一个对象:键是节点名」

- 位置：`sse-events.md:310`
- 引用首句：> 「`data` 的最外层是一个对象:键是**节点名**，值是这个节点这一步的**写入**。**一个事件通常只有一个节点键。**」
- **缺的答案**：
  - **谁决定有哪些节点** —— 租户管理员在管理控制台里给这个 Agent 开的功能（长期记忆 → `memory_recall`/`memory_writeback`；工作区 → `workspace_ingest`；`plan_execute` 工作流 → `planner`；`reflection:` 块 → `reflect`）。见 A-16 的注册表。文档在 `sse-events.md:40` 提了一句「取决于这个 Agent 怎么配」，但没说**是谁、在哪里配**，读者无法自己去确认自己对接的那个 Agent 会有哪些节点。
  - **「通常只有一个节点键」的「通常」是什么意思** —— 什么情况下会有多个？（并行分支）文档留了个悬念不收口。
  - **客户端要做什么** —— 已写（遍历所有键、判 `null`、不认识就忽略），✅。

---

### B-4 ❗ 「`approval` —— 什么时候发」

- 位置：`sse-events.md:723`、`sse-events.md:725`
- 引用首句：> 「run 走到人工审批节点、停下来等人决策时发一次。发完这个事件之后，流会以 `end` 收尾，`status` 是 `paused`。」
- **缺的答案 + 代码真值**：

| 缺什么 | 代码真值 | `file:line` |
|---|---|---|
| **谁决定要审批** | 两条路：① **平台**——这个工具在 Agent manifest 的 `PolicySpec.approval_required_tools` 里，或属于不可逆工具 → `reason_kind: policy_gate`；② **Agent 自己**——它调了内建的 `ask_for_approval` → 另外四个 `reason_kind` | `services/orchestrator/src/orchestrator/graph_builder/_approval.py:152-159` |
| **发给谁 / 谁来批** | **平台不通知任何人。** 对外 API **没有待审批列表端点**（`/v1/approvals` 是 console-only），第三方**只能**靠这个 SSE 帧知道有待审批 —— 也就是说，**漏掉这一帧就等于永久错过这次审批**（直到超时被自动拒） | `services/control-plane/src/control_plane/api/approvals.py:106`、`:151` |
| **多久内必须决策** | `timeout_at = requested_at + policies.approval_timeout_s`，默认 **86400 秒（24h）**，Agent 可配，范围 `[60, 604800]` | `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:941-951`；`_approval.py:167` |
| **超时之后发生什么** | 后台扫描把它按 `reject` 处理，落库状态 `timeout`，`reason="approval timed out"`，`decided_by="approval_timeout_sweep"`。**run 会以被拒的方式续跑或终止**（按审批类型，见 `run-control.md:127-130`） | `services/control-plane/src/control_plane/approval_timeout_sweep.py:194-197` |
| **超时之后客户端怎么发现** | **文档没答，而且这个答案不好找**：`ApprovalStatus.timeout` 对外不可见；对接方只能靠 `GET .../runs?status=...` 看那个 run 的最终状态，或者再打一次 `:decide` 拿 409 `APPROVAL_CONFLICT`。这条必须写出来 | — |
| **客户端要做什么** | ① 立刻持久化整个 `approval` 帧（因为没有第二个获取渠道）；② 在 `timeout_at` 之前调 4.2；③ 别当错误报（已写） | — |

---

### B-5 「`token` —— 什么时候一个 `token` 都没有」

- 位置：`sse-events.md:224`
- 引用首句：> 「什么时候一个 `token` 都没有:`mode: "queue"` 的 run、命中缓存的回答、不支持流式的模型，以及开启了输出结果二次判定的 Agent。」
- 缺的答案：**谁开的「输出结果二次判定」**（= 租户管理员在 Agent 配置里开的输出安全守卫；代码闸在 `services/orchestrator/src/orchestrator/graph_builder/streaming_redact.py:210-226`），以及**客户端要做什么**（退化成只靠 `updates` 渲染 —— 也就是说 **打字机效果是可选增强，不能作为唯一渲染路径**）。这句才是这一段的行动结论，现在没有。
- 这四种情况是**四个不同主体的决定**（调用方选 `mode` / 平台缓存 / 模型能力 / 租户管理员配置），挤在一句话里读者分不清哪个自己能控制。

---

### B-6 「`guard` —— 什么时候发」

- 位置：`sse-events.md:621`、`sse-events.md:623`
- 引用首句：> 「平台护栏预警或触发时。覆盖三类护栏:步数上限、token 预算、检测到没有实际进展。」
- 缺的答案：
  - **谁设的上限** —— Agent 配置（`max_steps` / `max_no_progress`）+ 平台 token 预算（`token_budget > 0` 才有这个闸：`services/orchestrator/src/orchestrator/sse.py:497-498`）。对接方不能改，只能找租户管理员。
  - **触发之后发生了什么** —— 平台给模型追加一条收尾指令、**这一步不再给它任何工具**，模型只能作答（`services/orchestrator/src/orchestrator/graph_builder/builder.py:832-838`）。所以用户仍然会拿到一段完整回答。这才是「`tripped` 不是错误」的真正理由，比 `sse-events.md:659-663` 现在的说法有力。
  - **但最终 `end.status` 仍是 `error`**（`max_steps` 那条）—— 见 A-7。**这是文档目前最大的自相矛盾点**，两处都必须点破。

---

### B-7 「`compaction` —— 什么时候发」

- 位置：`sse-events.md:685`、`sse-events.md:687`
- 引用首句：> 「上下文太长时，平台会自动把早于当前请求的历史对话压缩成摘要——这个动作发生时发一次。」
- 缺的答案：**谁触发**（平台自动，无人工介入、对接方不能开关）、**对这一轮回答有没有影响**（`sse-events.md:708` 说「不影响这次回答的正确性」——但这是个很强的断言，代码上它意味着更早的历史被摘要替换了，长期多轮里语义确实有损；建议改成「不影响这一轮能不能答完，但更早的细节会被摘要替代」）、**客户端要做什么**（已写：轻提示、别做模态弹窗 ✅）。

---

### B-8 「归档是软删除，不是彻底删除」

- 位置：`query.md:341-345`
- 引用首句：> 「归档只是把会话状态改成 `archived`。」
- 缺的答案：**谁能撤销**（文档 `query.md:344` 说「当前 API 没有对外的取消归档操作」✅），**归档之后终端用户还能不能看到自己的历史**（能——`query.md:342` 说了 ✅）。这一段其实答得比较全，只缺一条：**归档不影响正在跑的 run**。

---

### B-9 「`upload` —— 同名文档会覆盖同一份工作区文件」

- 位置：`chat.md:186`
- 引用首句：> 「同名文档会覆盖同一份工作区文件：同一个用户重复上传同一个文件名后，两个 `upload_id` 下载到的都是最后一次上传的内容。」
- 缺的答案：**读者要做什么** —— 需要同时保留两份就先改文件名再上传。一句话的事，现在读者读完只知道「有这么个现象」。**谁的范围** —— 是「同一个终端用户的工作区」范围内，不是全租户；这条对多租场景很关键，但句子里只有「同一个用户」，没说是 `user_id` 这个维度。

---

### B-10 「`error.code` 的形状不统一」

- 位置：`errors.md:57`、`errors.md:59`
- 引用首句：> 「大多数错误会用**标准格式**返回，能读到 `error.code`」
- 缺的答案：**为什么会有两种形状**（代码事实：`app.py` 只注册了 `RequestValidationError` 一个异常处理器 —— `services/control-plane/src/control_plane/app.py:2561`，全仓再无第二个 `add_exception_handler`。所以任何逃出端点函数的 `HTTPException` 都由 FastAPI 默认处理器渲染成裸 `{"detail": ...}`）。给出这条规律，读者就能**预测**哪些会是裸 `detail`，而不是背一张清单。
- 这条规律具体命中：`POST .../runs` 上 `spawn_run` 自己抛的 422/404（`services/control-plane/src/control_plane/api/runs.py:339-345`、`:347-350`、`:354`、`:964`），以及全部 `_authz` 的 403（`services/control-plane/src/control_plane/api/_authz.py:73/129/300/383`）。`errors.md:16` 只承认了 403 这一条是裸 `detail`。

---

<a id="c"></a>
## C. 结构不清的表

### C-1 ❗ `sse-events.md:84-89` —— 「哪些事件有 `id:`、能不能回放」　（负责人第 2 条投诉，原样落地）

现状表头：`这一类` / `有 id:` / `断线重连能拿回来吗` / `有哪些`

三重问题：
1. **按类别分组**，「有哪些」列把 8 个事件名塞进一个单元格，读者拿到一个 `event: guard` 要先反查它属于哪一类；
2. **一张表混了三个问题**（有没有 id / 能不能回放 / 属于哪一类）；
3. 第 4 行「这条连接的状况」的「断线重连能拿回来吗」是 `—`，语义空。

且**表下面紧跟的 3 条 bullet（`sse-events.md:91-95`）实际上是这张表的第 5 列（「为什么」）被挤出去了**，导致读者要在表和列表之间来回看。

**建议行集/列集** —— 每个事件一行，共 12 行：

| 事件 | 有 `id:` | 断线重连会重发吗 | 参与 `since_seq` 游标吗 | 为什么 |
|---|---|---|---|---|
| `metadata` | 有 | 会 | 是 | 落库的 run 事件 |
| `updates` | 有 | 会 | 是 | 同上 |
| `worker` | 有 | 会 | 是 | 同上 |
| `guard` | 有 | 会 | 是 | 同上 |
| `compaction` | 有 | 会 | 是 | 同上 |
| `approval` | 有 | 会 | 是 | 同上 |
| `retry` | 有 | 会 | 是 | 同上 |
| `error` | 有 | 会 | 是 | 同上 |
| `token` | 无 | **不会**——断连期间的永久丢失 | 否 | 一次性预览，不落库、不占序号 |
| `end` | 无 | 每条流各自新发一个 | 否 | 流的终止标记，不是 run 身上的事件 |
| `gap` | 无 | 不适用 | 否 | 描述**这条连接**的状况 |
| `truncated` | 无 | 不适用 | 否 | 同上 |

（代码依据：`sse.py:1484` 的 `if event_id:` 是唯一判据；`token` 走 `publish_ephemeral` 强制 `id=None`（`packages/.../stream_bridge/memory.py:113-116`、消费端 `_run_event_stream.py:320`）；`end` 每订阅新铸且 `id=None`（`memory.py:159-163`，消费端 `sse.py:1439`、`_run_event_stream.py:314`）；`gap`/`truncated` 直接 `format_sse(...)` 不传 `event_id`（`_run_event_stream.py:180/240/279/358`）。）

---

### C-2 ❗ `sse-events.md:133-144` + `sse-events.md:148-151` —— 3.3「事件一览」的**两张**表

问题：
1. **同一个问题被拆成两张表**（run 事件 10 行 + 连接事件 2 行），读者拿到一个陌生 `event:` 要查两张；两张表的表头完全相同，拆分的唯一理由写在中间那段散文里；
2. 列「`data` 里有什么」必然是模糊的（「run 的身份」「一小段文本 + 它属于第几步」），**和 3.4 重复且不可能一致**——它是维护负担，也是读者的第二信息源；
3. 列「有 `id:`」与 C-1 那张表**第三次重复**同一信息（3.2 一次、3.3 两次）。

**建议**：合并成一张 12 行表，删掉「`data` 里有什么」列（3.4 才是权威），删掉「有 `id:`」列（3.2 的表已经是权威），保留列：`event:` / 什么时候出现 / 前端该做什么 / 详见。用一列「分类」（run 事件 / 连接状况）代替拆表。

---

### C-3 ❗ `sse-events.md:230-234` —— token 的「`channel` 决定这个事件里还有哪些字段」表　（负责人点名表头）

现状表头：`channel` / `是什么` / **`除 step / channel 外还有`**

问题：
1. 第 3 列表头是一句话不是名词短语，且要求读者记住「`step` / `channel` 是公共字段」这个前提；
2. **和紧接着的 `sse-events.md:236-242` 那张字段表回答同一个问题**，两张表形状不同、口径不同（一张按 channel 组织，一张按字段组织），且第二张表用「只有 `content` / `reasoning` 有」这种散文补丁把第一张表的信息又说了一遍；
3. 第 2 列「是什么」的值（「答案正文的一小段」）和第二张表的「说明」列内容重叠。

**建议**：**只留一张表**，按字段一行，加一列「哪些 channel 有」：

| 字段 | 类型 | 含义 | 取值 | 哪些 `channel` 有 |
|---|---|---|---|---|
| `step` | int | 这一小段属于第几步，从 `1` 开始，与 `updates` 里 `agent` 节点的 `step_count` 同一编号 | 正整数 | 全部 |
| `channel` | string | 这一小段属于哪条内容通道 | `content`（答案正文）/ `reasoning`（思考过程，只有推理类模型有）/ `tool_args`（模型开始发起一次工具调用） | 全部 |
| `text` | string | 已脱敏的文本片段 | 任意字符串 | `content`、`reasoning` |
| `tool_index` | int | 这是本步里第几个并行工具调用，从 `0` 开始 | 非负整数 | 仅 `tool_args` |
| `name` | string | 工具名。同一个 `tool_index` 只发一次 | 任意工具名 | 仅 `tool_args` |

---

### C-4 ❗ `sse-events.md:635-639` —— guard 的「`detail` 的形状」表

现状表头：`guard` / `detail` / `含义`
现状值：`max_steps` | `{steps, max}` | `已执行步数 / 步数上限`

问题：**「含义」列靠位置对应把两个中文短语映射到两个字段名**，读者要自己数「第一个对第一个」。这是 A 类缺陷（字段没有独立的名称/类型/含义行）用表格伪装出来的形式。

**建议行集** —— 每个 `detail` 字段一行：

| `guard` | `detail` 字段 | 类型 | 含义 |
|---|---|---|---|
| `max_steps` | `steps` | int | 已经执行的步数 |
| `max_steps` | `max` | int | 这个 Agent 配置的步数上限 |
| `token_budget` | `spent` | int | 这棵委托树累计花掉的 token |
| `token_budget` | `limit` | int | 平台给这次 run 的 token 预算 |
| `no_progress` | `streak` | int | 连续多少步没有实质进展 |
| `no_progress` | `max` | int | 允许的连续无进展步数上限 |

---

### C-5 `sse-events.md:290-294` —— 「真栈实测三个场景的事件条数」表

现状表头：`场景` / `updates` / `token`；值 `简单问答` / `工具调用` / `分两步`

问题：① 表头第 2、3 列是裸事件名，没有说明是「条数」；② 「场景」三个值没有定义（「分两步」是什么？和「工具调用」互斥吗？）；③ 这张表放在一个 `::: danger` 框里，而框的主题是「别拿 token 重建状态」，表只是论据之一 —— 框里还夹了第二个主题（`updates` 才是过完整安全审查的结果，见 D-2）。

**建议**：表头改成 `场景` / `updates 条数` / `token 条数`，三个场景各加一句定义；或者干脆把表压成一句话（「实测 `token` 占全部事件的九成以上」），把版面还给结论。

---

### C-6 `sse-events.md:1128-1135` —— 「两条分支:live 与 replay」表

现状：**第一个表头单元格是空的**，列头是「run 还在跑(`live`)」「run 已经结束(`replay`)」，行头是 `since_seq` / 不带 `since_seq` / 遇到落库空洞 / 分页 / 收尾 / 会不会有 `token`。

问题：① 空表头 —— 读者不知道行是什么维度；② 行「`since_seq`」与「不带 `since_seq`」是同一个维度的两种情况，和其余四行不同质（其余四行是行为维度），混在一张表里。

**建议**：表头第一格填「对比维度」；把前两行合并成一行「`since_seq` 的作用」，把「不带 `since_seq`」的后果挪进单元格内的第二句（或提升成表下的一句警告，正文已经有 `sse-events.md:1120-1122` 的 danger 框在说同一件事，这里其实是第三次重复）。

---

### C-7 `chat.md:79-84` —— 「stream 还是 queue」表

同 C-6：**第一个表头单元格为空**，行头是 `响应` / `响应头` / `怎么拿 session_id` / `适合`。建议第一格填「对比维度」。

另：行「响应头」的 `queue` 单元格写「**不带**这两个头」，但 `queue` 模式的响应其实是带 `X-Expert-Work-Trace-Id` 的（见 A-24）。「不带这两个头」应改成「不带这两个头（`X-Expert-Work-Trace-Id` 仍然有）」。

---

### C-8 `chat.md:375-378` —— `X-Expert-Work-Stream-Mode` 表

现状表头：`X-Expert-Work-Stream-Mode` / `含义` —— 第一列表头是**响应头的名字**，行值是它的两个取值。读者第一眼会以为第一列是「响应头名称」这个维度。

**建议**：表头改成 `取值` / `含义`，并在表上一句说明「响应头 `X-Expert-Work-Stream-Mode` 有两个取值」。（`conventions.md:48` 已经用这种写法把两个值写在单元格里了，两处口径应统一。）

---

### C-9 `query.md:268-275` —— run `status` 取值表　（负责人第 3 条投诉的形态）

现状表头：`取值` / `是否最终状态`；两行分别塞 3 个值和 5 个值。

问题：① **按「是否最终状态」分组**，逼读者把类别映射回成员；② **没有任何一个值的含义**；③ 第二行的单元格里还塞了一句和取值无关的补充（「只有走到最终状态，`finished_at` 才会被写上」）。

**建议行集/列集**：8 行，每值一行，列 = `取值` / `含义` / `是否最终状态` / `客户端该怎么做`。取值与含义见 A-17。

---

### C-10 `query.md:442-446` —— 工作区文件 `Content-Disposition` 分类表

现状表头：`扩展名` / `Content-Disposition` / `原因`。

问题：① 第一行的「扩展名」单元格塞了**两个不同类别**（图片 + 结构化文本与代码）并用「等」收尾 —— 而代码里这是三个独立的闭集分支（图片 7 个 / 结构化文本 6 个 / 文本代码 43 个，见 A-23）；② 第一行的「原因」是 `—`，语义空；③ 安全相关的第二行（强制 attachment 的 10 个扩展名）反而是唯一写全的一行，说明作者知道该写全，只是没对第一行同等对待。

**建议**：拆成 4 行（① 可执行内容→attachment ② 图片→inline ③ 结构化文本→inline ④ 文本/代码→inline ⑤ 其它→attachment），每行给全成员，「原因」列每行都填。若嫌 43 个扩展名太长，把④折叠成 `<details>`，但**不要用「等」**。

---

### C-11 `sse-events.md:9-14` —— 章首「占位 / 本章示例值」表

现状：第一行 `{agent_code}` 的「本章示例值」是一句指令（「你的 Agent 编码，路径里原样填」），其余三行是真实值。**同一列混了「值」和「说明」两种东西。**

**建议**：列改成 `占位` / `本章统一用的值` / `说明`，第一行的「值」写 `{agent_code}`（原样保留），指令挪到说明列。

---

### C-12 `errors.md:9-53` —— 43 行错误码速查表

现状表头：`code` / `HTTP 状态` / `含义` / `建议处理`。表头本身**是好的**（自解释名词短语），但结构上有三个问题：

1. **同一个 `code` 出现多行**（`WORKSPACE_FILE_FAILED` ×3、`UPLOAD_CONTENT_UNAVAILABLE` ×2、`ARTIFACT_CONTENT_UNAVAILABLE` ×2），而读者的入口场景是「我拿到一个 code」——按 code 查会撞到多行，此时**唯一的区分维度是「哪个端点」，而表里没有这一列**；
2. `UPLOAD_FAILED` 那行的「HTTP 状态」是复合值 `500 / [502]`，破坏了列的原子性；
3. 43 行按 HTTP 状态码升序排，但读者不是按状态码查的。

**建议列集**：`code` / `HTTP 状态` / **`哪个端点`** / `含义` / `建议处理`。`UPLOAD_FAILED` 拆成两行（500 / 502）。若要保留「按状态码浏览」的用法，把状态码升序保留，但**必须加端点列**才能让重复码可区分。

---

### C-13 `conventions.md:74-79` 与 `errors.md`「限流 vs 配额」对比表

`conventions.md:74-79`：**第一个表头单元格为空**（同 C-6/C-7）。行头是 `限制什么` / `error.code` / `Retry-After 响应头` / `怎么办`。建议第一格填「对比维度」。

另：这张表和 `errors.md:312`（8.16）讲的是完全同一件事，`errors.md:203-236`（8.11）第三次讲。三处都带上「产物下载是例外」的补丁。见 E-4。

---

### C-14 `quickstart.md:82-89` 与 `sse-events.md:879-887` 的 `end.status` 表

`quickstart.md:82-89`：表头 `status` / `含义` —— 两列，**没有「客户端该怎么做」**，而 `sse-events.md:881-887` 的同一张表有三列。快速开始这一章恰恰是最需要「该怎么做」的地方（读者此刻正在写第一版代码）。建议两处列集统一为三列。

---

### C-15 `chat.md:294-296` 与 `chat.md:329-332` —— 「限制 / 超限时的 `error.code`」两张表

表头是自解释的 ✅。但两张表的「限制」列把**数值上限**和**边界语义**（「正好 16 项合法，第 17 项才拒」）挤在一格。建议拆成 `限制项` / `上限值` / `边界` / `超限时的 error.code` 四列，或至少把边界说明统一成一句表下注（现在每格都重复一遍「正好 N 合法」）。

---

<a id="d"></a>
## D. 章节格式缺陷

### D-1 ❗ `sse-events.md` 3.4 —— 十个小节的模板**实际上有五种不同形态**

`sse-events.md:159` 承诺：「每个小节都是同一个模板:**什么时候发** → **`data` 字段** → **完整示例** → **前端怎么渲染**」。实际：

| 事件 | `data` 字段一节的实际形态 |
|---|---|
| `metadata` (`:182`) | 一张字段表 ✅ + 模板外多一段响应头说明（`:216`） |
| `token` (`:226`) | **两张表**（`:230` channel 表 + `:236` 字段表）+ 一个 `::: warning` |
| `updates` (`:308`) | 散文 + 一个 `js` 骨架代码块（`:312`）+ 字段表（`:317`）+ bullet 列表（`:326`）+ 两个 admonition，**外加三个模板里没有的 `####` 子节**（`:353` `messages[]` 里的两种消息 / `:421` 配对 / `:427` 工具结果的文本要先还原） |
| `worker` (`:509`) | 信封表 + **三张用句子当标题引出的表**（`:526`「`kind: "start"` 的 `data`:」、`:533`、`:542`）+ 两个 warning |
| `guard` (`:625`) | 字段表 + `detail` 形状表 + 一个 tip |
| `compaction` (`:689`) | 一张字段表 ✅ |
| `approval` (`:727`) | 一张字段表 ✅ + 模板外多一段跳转（`:776`） |
| `retry` (`:784`) | 字段表 + 一句挂在表外的关键约束（`:792`「`attempt` 今天只可能是 `1`」，这条应该在表里） |
| `error` (`:829`) | 一张字段表 ✅ + 模板外多一段（`:862`） |
| `end` (`:872`) | **两张表**（`:874` 字段表 + `:879` status 表）+ 表外一段（`:888`） |

具体缺陷：
1. **句子当标题**（`worker` 的三张 `data` 表）—— 这三段是全章最需要被直接链接到的内容之一，但不是标题 ⇒ **不进页面目录、拿不到锚点**。
2. **只有 `updates` 有 `####` 子节**，其它九个没有 ⇒ 读者建立不起 4 步节奏。
3. **admonition 的位置不固定**：多数在「`data` 字段」和「完整示例」之间，但 `guard` 的 `::: warning`（`:659-663`）落在「前端怎么渲染」标题**之后**、代码块之前。
4. `retry`（`:792`）把「`attempt` 恒为 1」这条**字段级约束**放在表外的一句散文里。

**建议**：给 `worker` 的三张表各起一个 `##### kind: "start" 的 data` 之类的标题；把 `updates` 的三个 `####` 子节的模式推广或收敛；把散落在表外的字段级约束收回表内。

---

### D-2 `::: danger` / `::: warning` 框里装了不止一个主题

| 位置 | 框标题 | 装了几个主题 |
|---|---|---|
| `sse-events.md:288-300`（11 行） | 「别拿 `token` 重建状态」 | ① 条数悬殊（含一张表）② 断连丢失 ③ **`updates` 才是过完整输出安全审查的结果、被拦时是拒答文案** —— 第 ③ 条是独立且更重要的事实，被埋在框尾 |
| `sse-events.md:333-345`（11 行） | 「节点写入可能整个是 `null`」 | ① 现象 + 示例 ② 客户端判据 ③ 真栈观测（哪两个节点每次都是 null）—— 主题单一，但**归因写错了**（见 A-16） |
| `sse-events.md:557-567`（9 行） | 「`update` 里的 `messages` 是摘要,而且同样带着防注入包装」 | 标题自己就带「而且」= 两个主题：① 是摘要（含三种形状的 bullet 列表）② 同样要还原防注入包装 |
| `sse-events.md:1162-1171`（8 行） | 「传一个超出范围的值,服务端不报错」 | ① 两条分支的不同表现 ② 为什么这么设计（3 句） —— 第 ② 段可以下沉成正文 |
| `errors.md:234`（397 字符单段） | — | ① 一般配额规则 ② 产物下载例外 ③ 30 天窗口 ④ 该怎么处理 —— 四个主题一段 |
| `query.md:593-601` | 「这是软删除——工作区里的字节不会被这个 API 清除」 | ① 软删语义 ② 后台清理任务也不删字节 ③ 仍可从工作区下载 ④ 「删除我的数据」承诺不成立 ⑤ 同名保存会恢复 ⑥ 没有撤销 —— **六个主题**，是全站最长的一个框 |

### D-3 超长段落（应拆成 bullet）

阈值：单行 ≥ 300 字符的正文段（非表、非代码、非 admonition 标题）：

| 位置 | 长度 | 内容 |
|---|---|---|
| `errors.md:55` | 312 | 「这张表只覆盖有 `error.code` 的失败…」——三种例外挤在一段 |
| `errors.md:181` | 313 | 图片数量的三档限制 |
| `errors.md:234` | 397 | 见 D-2 |
| `errors.md:270` | 396 | 附件下载 500 的两种根因 + 图片分支的三种结果 |
| `errors.md:294` | 355 | 附件下载 503 |
| `errors.md:296` | 321 | 产物下载 503 |
| `errors.md:312` | 386 | 8.16 限流与配额（整节就是这一段） |
| `query.md:559` | 410 | 产物下载配额 + 429 + `Retry-After` 无效 |
| `examples.md:1958` | 335 | 断线重连说明 |

`errors.md` 一章占 7 条 —— 这一章的信息密度最高、句子最长，是全站最难读的一章。

### D-4 代码块标题

- 全站**只有 `errors.md` 和 `examples.md` 做到了 100% 有标题**。其余 8 个文件共 **29 处**没有标题的开块：

| 文件 | 无标题开块 |
|---|---|
| `query.md` | 11 处（`:9 :84 :151 :214 :294 :322 :371 :413 :487 :538 :573`）—— 全是「端点签名」块 |
| `chat.md` | 6 处（`:11 :40 :117 :136 :157 :342`） |
| `run-control.md` | 3 处（`:9 :71 :79`） |
| `auth.md` | 3 处（`:9 :22 :97`） |
| `conventions.md` | 2 处（`:11 :17`） |
| `quickstart.md` | 2 处（`:9 :39`） |
| `best-practices.md` | 1 处（`:11`） |
| `sse-events.md` | 1 处（`:1088`，mermaid） |

  其中 **mermaid 块 9 处**（`auth.md:9,:97`、`best-practices.md:11`、`chat.md:11,:117,:136`、`quickstart.md:9`、`run-control.md:79`、`sse-events.md:1088`）可以豁免；剩下 **20 处是裸 ``` 块**（`auth.md:22`、`chat.md:40,:157,:342`、`conventions.md:11,:17`、`query.md` 全部 11 处、`quickstart.md:39`、`run-control.md:9,:71`），绝大多数是「端点签名」。全站风格不统一（`sse-events.md` 给每一个块都打了 `[事件流片段]` / `[渲染示例]` 标签，`errors.md` / `examples.md` 做到了 100% 有标题）。建议统一加 `[端点]` 之类的标题。

- **连续两个无标题代码块**：`conventions.md:11-13` + `:17-19`（环境地址 + 端点示例）；`auth.md:22-24`（key 格式）。

### D-5 粗体伪标题

| 位置 | 内容 | 说明 |
|---|---|---|
| `best-practices.md:44 / :50 / :54 / :69` | 「**`agent_code` 从哪拿？**」等 4 条 | 9.5 常见问题整节用粗体当标题 ⇒ **不进目录、拿不到锚点**，而 FAQ 恰恰是最需要被单条链接的内容 |
| `sse-events.md:97` | 「**这三类都不参与断线重连的游标计算。**」 | 独占一行的粗体句 —— 它其实是 C-1 那张表的一列被挤出来了 |
| `sse-events.md:1160` | 「**别自己算、别自己加一、别用你本地的消息条数去凑。**」 | 同上形态 |
| `sse-events.md:357 / :367` | 「**`type: "ai"`**——来自 `agent` 节点…」/「**`type: "tool"`**——…」 | 两个粗体伪标题引出两张表；这两段是 `messages[]` 的核心，同样进不了目录 |
| `errors.md:160 / :187` | 「**第一类，请求体字段本身没通过校验**」/「**第二类，…**」 | 8.10 的两大分类用粗体标记 |

### D-6 内联代码里的中文占位

全站扫描（正确解析行内代码跨度，排除代码块）**只有 2 处**：

- `chat.md:102` —— `` `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>` ``
- `chat.md:106` —— `` `since_seq=<已见过的最大 seq>` ``

两处都把中文说明塞进了可复制的 URL 里，读者复制粘贴会直接失败。建议改成 `user_id={user_id}` / `since_seq={max_seq}` + 表外说明。

### D-7 残留的「帧」

**全站已清零** ✅ —— 合并前 `main` 的 `chat.md:107` 还有一处「从第 0 帧起」，#1193 已改成「从 `seq` 为 `0` 的那个事件起」。（注：`examples.md` 里的 `iter_sse_frames` 是代码标识符，不算。）

### D-8 `examples.md` 的 7 个巨型 `::: code-group`

`:20-481`(460 行) / `:487-946`(458) / `:952-1460`(507) / `:1466-1954`(487) / `:1960-2601`(640) / `:2607-2800`(192) / `:2806-3048`(241)。

单个 code-group 最长 640 行、四种语言。这是示例章的固有形态，**不算缺陷**，但 10.1 的 Python 示例里有一段 12 行的 docstring（`examples.md:44-56`）在解释 `readline()` vs `read(1024)` 的取舍 —— 这是**正文级的知识点被埋在代码注释里**，SSE 章（3.5）讲解析器时完全没提。建议提升到 3.5 或 10 章开头的通用约定。

### D-9 锚点与站内链接

跑了一遍锚点校验（复刻 VitePress 的 `slugify`：`apps/admin-ui/docs-site/node_modules/vitepress/dist/node/chunk-D3CUZ4fa.js:17690`，字符类在 `:17687-17689`）：**102 条带锚点的站内链接，0 条失效** ✅。上一轮 `ad6ea926` 修的 31 条死锚点没有回归。

---

<a id="e"></a>
## E. 跨章一致性

### E-1 ❗ `errors.md` 单方面把第 4 章改了名

`errors.md` 里**13 处**把第 4 章称作「**取消 run 与审批决策**」（`:7 :23 :24 :26 :27 :28 :41 :136 :140 :144 :177` 等），而该章的真实标题是「**4 对话过程中的控制**」（`run-control.md:1`）。其余每一章（`quickstart.md:119`、`examples.md`、`chat.md:36`、`auth.md:41`）都用「4 对话过程中的控制」。

同时 `errors.md` 还用「[认证](./auth)」（`:105`、`:115`）、「[通用约定](./conventions)」（`:302`）这种去掉编号的短名，而全站其它章一律用带编号的全名（「6 认证与 Key」「7 通用约定」）。

→ 读者从错误码表点进去，落地页的标题和链接文字对不上。

### E-2 ❗ `conventions.md:81` 的链接文字承诺了一个小节，但没有锚点

> 「完整的 `dimension` 字段含义和响应样例见 **[8.11 429](./errors)**。」

链接目标是 `./errors`（**没有 `#` 锚点**），实际会落在 errors.md 顶部，读者要自己往下翻到 8.11。同一文件 `conventions.md:84` 的产物下载例外框里链接的是 `[5.7 产物](./query#_5-7-产物)`（**有锚点**），可见作者知道该怎么写。

### E-3 ❗ 一个对外端点在正文里从未被文档化

`POST /v1/agents/{agent_code}/sessions`（会话绑定）——
- 代码：`services/control-plane/src/control_plane/api/agents.py:1040`，`status_code=201`，`_EXTERNAL_ONLY`，要 `session:write`；
- 文档：**全站唯一一次出现是 `best-practices.md:64` 表格里的一行**「会话绑定 `POST /v1/agents/{agent_code}/sessions` | 响应体 `data.session_id`」。

`chat.md`（对话章）和 `query.md`（查询与管理章）都没有它的小节，读者从那一行拿不到请求参数、响应形状、错误码。要么补一节，要么把那一行删掉。

### E-4 同一件事在三处讲，三处口径略有不同

「限流 vs 配额」讲了三遍：`conventions.md:70-85`（7.6）、`errors.md:203-236`（8.11）、`errors.md:310-312`（8.16）。三处都要额外挂一个「产物下载是例外」的补丁（`conventions.md:83-85`、`errors.md:234`、`query.md:559`），**同一个例外被写了四遍**。

三处的差异：只有 `errors.md:220` 提到「**网关这一层的 429 不带 `dimension` 字段**」这条实用判据，`conventions.md` 的表和 `errors.md` 8.16 都没有。

### E-5 「任务」一词同时指两个东西

- 指 **run**：`quickstart.md:119`（「取消正在执行的任务」）、`chat.md:84`（「长任务」）、`chat.md:109`（「还有没有在跑的任务」）、`query.md:130`（「任务跑完没有」）、`query.md:218`（「我的任务列表」）
- 指 **后台清理作业**：`query.md:594`（「保留期后台清理任务」，一句里出现两次）、`query.md:596`（「被后台任务清掉」）
- 指 **worker 的委托任务**：`sse-events.md:529`（`task_excerpt`「委托给这个 worker 的任务描述」）、`sse-events.md:505`（「委托给子任务(worker)」）

三种语义共用一个词，且都在读者最需要精确的段落里。建议：run 一律叫「run」（全站已基本做到，就这 5 处漏了），后台作业叫「后台清理作业」，worker 的叫「子任务」。

### E-6 `session_id` / `thread_id` —— 处理得不错，但有一处口径漂移

- `chat.md:128`、`best-practices.md:63`、`sse-events.md:187` 三处都说清了「`thread_id` 与 `session_id` 是同一个值」✅；
- `sse-events.md:14` 的占位表用「会话 id(`thread_id` / `session_id`)」也 ✅;
- **但 `sse-events.md:732` 的 `approval` 字段表里 `thread_id` 的说明只有「这段会话的 id」**，没有像 `sse-events.md:187`（`metadata` 表）那样补「下一轮对话把它填进请求体的 `session_id`」。同一个字段在同一章的两张表里说明详略不同。

### E-7 ~~`available` 的缓存窗口数值对不上~~ —— **查实：文档对，代码注释错**

初查疑似不符，核到接线点后推翻：文档的「约 5 秒」是对的（`services/control-plane/src/control_plane/settings.py:933` → `app.py:861-863`），而 `services/control-plane/src/control_plane/api/external_agent_catalog.py:94-106` 的 docstring 里六处「30s TTL」是过期注释（30.0 只是 `agent_disable_status.py:33` 的类签名默认值，被接线覆盖了）。

**本条不是文档缺陷，列在这里是为了防止下一轮有人照那段代码注释把文档「改正」成错的。** 建议单独提一个「修代码注释」的小 issue。

### E-8 「附件 / 文件 / 上传」三个词指同一组东西

- `chat.md` 2.6 标题叫「带图片和文档」，正文叫「附件」；
- `conventions.md:36` 叫「文件上传」；
- `errors.md` 叫「上传接口」（`:87 :148 :256` 等）和「附件下载」（`:264 :294`）；
- `query.md` 5.6 的「工作区文件」是**另一个东西**（Agent 写出来的产出文件），和上传的附件不是一回事。

`errors.md:21` 那行 `UPLOAD_NOT_FOUND` 的含义里同时出现「`upload_id`」「图片」「底层内容」三种说法。建议全站统一：用户传上去的叫「附件」，Agent 写出来的叫「工作区文件」，登记成成果的叫「产物」，并在 `conventions.md` 加一个三者对照的小节 —— 现在读者要读完 `chat.md` 2.6 + `query.md` 5.6 + `query.md` 5.7 才能拼出这三者的关系。

### E-9 `end.status` 的四值表出现在两章，列数不同

`quickstart.md:82-89`（两列）vs `sse-events.md:879-887`（三列）。见 C-14。

### E-10 「产物下载配额是例外」的解释，`query.md` 与 `errors.md` 详略不一

`query.md:559`（410 字符一段）和 `errors.md:234`（397 字符一段）讲同一件事，两段文字高度重复但不完全相同。`conventions.md:83-85` 是第三份。建议留一份权威（`errors.md` 8.11），另两处只留一句 + 链接。

---

<a id="f"></a>
## F. 优先级排序（Top 15，按读者影响）

| # | 位置 | 问题 | 一行修法 |
|---|---|---|---|
| 1 | `sse-events.md:69-80` | **心跳段没答「谁发/发给谁/多久/多久没动静算断」**；`best-practices.md:40` 还向它开了张不存在的空头支票 | 重写这一段：服务端 SSE 端点发给当前连接（live 分支才有，replay 没有）→ 15 秒空闲计时器（`sse.py:1402`、`_run_event_stream.py:302`）→ 客户端跳过 `:` 开头行、不动游标 → **建议读超时设 45 秒**（明标「代码未定义，为建议值」），并让 3.5 骨架的 `readTimeoutMs` 与它对齐 |
| 2 | `sse-events.md:84-89` | **「哪些事件有 `id:`」按类别分组**，逼读者反查成员（负责人第 2 条投诉） | 改成 12 行、每事件一行，列 = 事件 / 有 `id:` / 断线重连会重发吗 / 参与游标吗 / 为什么（表见 C-1） |
| 3 | `sse-events.md:374` | **`tool.status` 写「取值不在这里穷举」，代码是闭集 2 值**（负责人第 3 条投诉） | 改成 `"success"`（默认，工具正常返回）/ `"error"`（失败或被平台拦），并列出 8 种 `error` 成因（`builder.py:1243/2341/2379/2812/2836`、`_approval.py:226/277`、`resume.py:59`） |
| 4 | `sse-events.md:546` + `:582` | **`worker.outcome` 写「不穷举」，代码是闭集 3 值；且示例值 `"completed"` 在代码里不存在** | 改成 `success` / `max_steps`（部分结果，不是失败）/ `cancelled`（`_child_run.py:199,223`），示例改用 `success`，并补「异常终止时不发 `end` 帧」 |
| 5 | `sse-events.md:230-234` | **token 的 `channel` 表头写着「除 `step` / `channel` 外还有」，且和下一张表重复回答同一问题**（负责人点名） | 合并成一张按字段组织的表，加「哪些 channel 有」列（表见 C-3） |
| 6 | `query.md:268-275` | **run `status` 八个值一个含义都没给**，还按「是否最终状态」分组 | 改成 8 行，列 = 取值 / 含义 / 是否最终状态 / 客户端该怎么做（表见 A-17）；`status` 查询参数补「枚举外的值 → 422 `INVALID_REQUEST`」 |
| 7 | `sse-events.md:659-663` vs `:886` | **`guard.tripped` 说「不是错误」，但 `max_steps` 那一路最终 `end.status` 就是 `error`** —— 全站最大的自相矛盾 | 两处互相点破：`guard` 一节补「平台会追加收尾指令、这一步不给工具（`builder.py:832-838`），用户仍拿到完整回答，**但这次 run 在 `end` 里仍算 `error`**」；`end` 一节补「`error` 包含步数耗尽」 |
| 8 | `sse-events.md:799` + `:786-792` | **`retry` 示例的 `error_class: "ReadTimeout"` 代码里不可能出现**；`attempt` 恒为 1 这条约束挂在表外 | 示例改 `AllProvidersExhaustedError`（今天唯一可能值，`run_retry.py:45`）；`attempt` 行直接写「恒为 `1`」；`backoff_s` 行补「默认 10.0，范围 [1,120]」 |
| 9 | `sse-events.md:363` | **`finish_reason` 被写成两值闭集，实际是厂商透传的开集，Anthropic 路径上整个字段不存在** | 改成「开集，厂商原样透传，**可能整个缺失**」+ 已知子集（`stop`/`tool_calls`/`length`/`content_filter`/`stream_idle_timeout`）+「不要拿它判轮次结束，判据是 `end.status`」 |
| 10 | `errors.md:9-53` | **43 行速查表里 3 个 code 各占多行，唯一区分维度「端点」不在表里** | 加一列「哪个端点」；`UPLOAD_FAILED` 的 `500 / 502` 拆成两行 |
| 11 | `sse-events.md:723-740` | **`approval` 段没说「平台不通知任何人、对外没有待审批列表端点」** —— 漏这一帧 = 永久错过这次审批 | 补三句：只有这一帧能知道有待审批（`/v1/approvals` 是 console-only，`approvals.py:106`）；必须立刻持久化；超时窗口默认 24h、Agent 可配 `[60,604800]`（`agent_spec.py:941-951`），超时按 reject 处理 |
| 12 | `sse-events.md:333-345` | **「节点写入可能整个是 `null`」的归因写错了** —— 7 个节点实现全都返回 dict，没有一个返回 None | 保留「客户端必须判 `null`」这条正确建议，把归因改成事实描述（「真栈上观察到某些节点这一步没有写入，`data` 里对应的值是 `null`」），不要写成节点实现的行为 |
| 13 | `errors.md` 全章 | **7 条 300+ 字符的单段 + 13 处把第 4 章叫错名** | 长段拆 bullet；「取消 run 与审批决策」全部改成「4 对话过程中的控制」；`conventions.md:81` 的 `[8.11 429](./errors)` 补锚点 |
| 14 | `sse-events.md:513-531` + `:574` | **worker 的 `label`/`agent_ref`/`role` 示例是一个不可能的组合**；`depth` 没给范围 | 按「静态子 Agent / 动态 `spawn_worker`」两条路径分别给取值（表见 A-14）；`depth` 写明 `1–3`（`subagent.py:52`）；示例二选一，不要混 |
| 15 | `query.md:5` + 三个列表小节 | **只有 Agent 目录返回 `total`，其余三个列表不返回 `total` 也不返回 `has_more`，文档从没告诉读者怎么判最后一页** | 在 5 章通则里写清：`sessions`/`messages`/`runs` 只回显 `limit`/`offset`（`external_sessions.py:203-209,274-280`、`external_runs.py:144-164`），判最后一页只能靠「这一页条目数 < limit」；并说明 `messages` 是内存切片分页（`external_sessions.py:263`） |

---

## 附：本轮统计

| 分区 | 条目数 |
|---|---|
| A 字段取值（应闭而未闭 / 示例值错误 / 缺含义） | 25 条（其中 **6 条标 ❗**：`tool.status` / `worker.outcome` / `retry.*` / `approval.node` / `approval.reason_kind` / `finish_reason`） |
| B 概念段缺「谁→谁 / 何时 / 做什么」 | 10 条（其中 4 条标 ❗） |
| C 表结构 | 15 条（其中 4 条标 ❗，含负责人点名的两张） |
| D 章节格式 | 9 类（3.4 模板五形态 / 6 个多主题框 / 9 条超长段 / 29 处无标题代码块 / 5 类粗体伪标题 / 2 处中文内联占位 / 「帧」已清零 / examples 巨型 code-group / 锚点 0 死链） |
| E 跨章一致性 | 10 条（其中 3 条标 ❗；E-7 经复核**推翻**，是代码注释错不是文档错） |
| 复核后推翻的疑似缺陷 | 1 条（E-7 缓存 TTL）—— 记录在案，防止下一轮照过期代码注释把对的改错 |
| **代码里核实过的取值集合** | `end.status`(4) · `token.channel`(3) · `tool.status`(2) · `worker.kind`(3) · `worker.outcome`(3) · `guard.kind`(2) · `guard.guard`(3) · `approval.reason_kind`(5) · `approval.node`(1) · `ApprovalStatus`(5) · `decision`(3) · `mode`(2) · `RunStatus`(8) · `ArtifactKind`(4) · upload `type`(2) · `UserUploadKind`(2) · `AgentSpecStatus`(3) · `stream-mode`(2) · 节点名(7) · MIME 白名单(7 文档 + 4 图片) · Content-Disposition 五分支(10/7/6/43/兜底) · 错误码(46 个，文档缺 7) |
