# run 做没做成，要有一个独立信号 —— 设计（B-85 ③）

> 立项来源：ROADMAP `B-85` 第 ③ 条。①② 已于 2026-09-19 修完并上测试环境（#1618），
> ③ 一直挂着「等用户拍板」。2026-09-20 用户拍板开工，并指定参考 `hermes-agent`
> 与 `openclaw` 两个成熟实现的做法。

## 1. 问题

**一个没做成事的 run 报了 `status=success`。**

2026-09-19 发 `233791e5` 时实测抓到一次完整的形状：

1. 沙箱认领撞上并发，工具报错，错误原文里明写 `retry shortly`
2. 错误分类器按关键词表判，表里没有 `retry` / `already being created` → 判 `unknown`
3. `unknown` → `_is_retryable` 为 `False` → `<recovery-advisory>` 给模型的话变成
   「This failed for an unclear reason … **avoid retrying the identical call**」
4. 模型照办，放弃，不再调任何工具
5. 图从 `_should_continue` 正常走到 `END`，没有任何异常 → **run 判 `success`，零产物**

抓出它的是**金丝雀**（它自己断言 `artifacts` 非空），**不是平台**。

①② 修的是第 1~3 步这一个具体来源。**第 4~5 步的形状与来源无关** ——
任何工具失败之后模型不再动作，都会复现同一次静默假绿。沙箱认领只是第一个被抓到的实例。

对外 API 的消费者（对接方）照 `status` 判成功，就会拿到空手。

### 1.1 `status` 今天的真实含义

`RunStatus`（`packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py:21`）
八个取值里，对外只暴露四个。`success` 的含义是**「图跑完了，没抛异常」**，
不是「事做成了」。这一点今天没有写在任何对外文档里，而对接方在读它。

## 2. 为什么不能直接改 `status` 的语义

那是**对外契约变更**。对接方正在用 `status` 判成功/失败，改语义会让他们那边**静默**变化 ——
既不报错也不告警，只是判断开始给出不同结论。

`hermes-agent` 撞过**逐字相同**的死结，并给出了答案。`agent/turn_failure_copy.py:40`：

```python
class ExitFailure(NamedTuple):
    reason: str
    retryable: bool
    fails_turn: bool = True
```

它的注释（原文）：

> `fails_turn` False = advisory：descriptor 字段照样盖上让 Desktop/TUI 显示具体 code，
> 但 **`failed`/`completed` 保持循环自己选的值** —— cron 静默、kanban 熔断器、
> gateway transcript 持久化**全都 key on `failed`，不能因为加了个 code 就变**。

**这就是本设计的形状**：新信号只加字段、只盖 code，**`status` 一个字都不动**。
这不是我们发明的折中，是被验证过的做法。

## 3. 两家怎么判「没做完」——去读过源码

### 3.1 hermes-agent：在每个退出点盖章，不事后推断

`turn_exit_reason` 是一个字符串，**由走那条退出路径的代码自己写**：

| 出处 | 值 |
|---|---|
| `agent/turn_tool_round.py:136,157` | `session_persistence_failed` |
| `agent/turn_tool_round.py:164` | `guardrail_halt` |
| `agent/turn_loop_errors.py:84` | `interpreter_shutdown` |
| `agent/turn_loop_errors.py:161,165,168` | `local_processing_error(…)` / `repeated_outer_errors(…)` / `error_near_max_iterations(…)` |
| `agent/turn_finalizer.py:134` | `max_iterations_reached(37/40)` |
| `agent/conversation_loop.py:1042,1268` | `context_compression_exhausted` / `context_compression_timeout` |
| `agent/turn_empty_response.py:284` | `empty_response_exhausted` |

`completed` 的算法（`agent/turn_finalizer.py:465`）：

```python
completed = (
    final_response is not None
    and not failed
    and (api_call_count < agent.max_iterations
         or str(_turn_exit_reason).startswith("text_response("))
)
```

**三个条件全是平台自己知道的客观事实**：有没有产出最终回复、有没有被某条出口标 failed、
有没有撞满迭代预算。**一个都不涉及「模型想干什么」。**

### 3.2 openclaw：归一化厂商停止原因 + 让模型自己声明

① `stopReason` 把厂商的停止原因归一化成 `stop` / `toolUse` / `aborted` / `error` /
`blocked` / `end_turn`，并**放进 `phase: "end"` 的 lifecycle 事件里**
（`src/agents/agent-command.ts:1084-1096`）—— 正是「挂在 end 帧上的一个字段」这个形状。

② 另一条**独立**路线，在系统提示词里（`src/agents/gpt5-prompt-overlay.ts:91`）：

> Treat the task as incomplete until every requested item is handled or
> **explicitly marked `[blocked]`** with the missing input.

让模型**自己**声明没做完，而不是平台去猜。

### 3.3 由此推翻的一条

立项讨论时提过一个判据：「有非 transient 工具失败 **且** 这一轮零产物 → 判定模型放弃」。
**这条作废** —— 它是在**事后推断模型意图**，而两家成熟实现都刻意不这么做。

需要说清楚的是：**hermes 也没有解决 B-85 这个具体形状**（它不跟踪「工具失败后模型是否恢复」）。
它给我们的是**机制**（出口盖章 + advisory 两档），不是**谓词**。谓词要我们自己定，
但必须守住同一条纪律：**只用客观事实，不推断意图**。

## 4. 谓词：把「模型放弃」换成「run 在一批失败的工具调用之后立刻结束了」

这两句话描述同一个现象，但**前者是意图，后者是事实**。

- 「模型放弃了」—— 猜不准。模型合理地换了个方法、或判断已经够了，都长这样。
- 「**最后一批工具调用里有非 transient 失败，之后模型没有再调任何工具，run 就结束了**」
  —— 每一项都能客观判定，零推断。

而这正是 B-85 那次的**逐字形状**。

## 5. 对外契约

### 5.1 `end` 帧加两个字段

唯一构造口是 `end_frame_data()`（`services/orchestrator/src/orchestrator/sse.py:1821`）。
两条 SSE 路径共用它，它的 docstring 明写「两条流的 `end` 帧字段集合分叉过一次同类问题，
所以这里做成一个函数而不是两处字面量」。**本次只改这一个函数。**

```jsonc
event: end
data: {
  "status": "success",            // ← 一个字都不动
  "run_id": "…",
  "artifacts": [],
  "usage_by_model": [ … ],

  "completed": false,             // ← 新增
  "exit_reason": "text_response"  // ← 新增
}
```

### 5.2 字段缺席语义，照抄 `artifacts` / `usage_by_model` 的既有口径

`end_frame_data` 已经定死了一条口径：**`None` 时字段缺席，而不是放 `null`；
缺席 = 老 run 无记录，别当成「确有其事的零值」。**

本次两个新字段照同一条：

| | 含义 |
|---|---|
| 字段**缺席** | 这个 run 没有记录（本改动上线前的历史 run，或重放老事件） |
| `completed: true` | run 正常结束且最后一批工具调用没有未解决的失败 |
| `completed: false` | 反之。**`status` 仍可能是 `success`** —— 这正是本设计要暴露的那种情况 |
| `exit_reason` | 见 §5.3，恒为下表中的一个值 |

### 5.3 `exit_reason` 的取值集合

取值来自图里**真实存在的出口**，逐个去数过（`graph_builder/builder.py`）：

| 值 | 什么时候 | 谁盖的章 |
|---|---|---|
| `text_response` | 模型不再发 tool_calls，自然结束 | `agent_node`（响应里没有 tool_calls 且非 `budget_exhausted`） |
| `max_steps` | 步数预算用尽 | `agent_node`（`step_count >= max_steps`，:613） |
| `no_progress` | 循环检测连续 N 轮无进展 | `agent_node`（`stuck`，:604,613） |
| `token_budget` | 全树共享 token 池耗尽 | `agent_node`（`token_tripped`，:612,613） |
| `approval_pending` | 挂在审批门上（`RunStatus.PAUSED`） | `tools` 节点（写 `pending_approval` 的同一处） |
| `approval_rejected` | 声明式门否决 | `tools` 节点（写 `approval_outcome="rejected"` 的同一处） |

> ⚠️ **盖章只能在节点里，不能在路由函数里。** `_should_continue`（:2229）与
> `_after_tools`（:2245）是 LangGraph 的 conditional-edge 函数，签名是
> `(state) -> Literal[...]` —— **只返回路由，不写 state**。本设计第一版把盖章
> 安排在这两个函数里，**是错的**，自检时逮到并改掉。两个路由函数本次一行不动。

**为什么必须在 `agent_node` 分辨 `budget_exhausted` 的三种**：这三种都走
「一次无工具的收尾轮」，然后**把 tool_calls 剥掉**让路由从 `_should_continue` 出去
（:601-607 的注释写着这个机制）。到了路由那一步三者已经**分不出来**，
连「是不是预算用尽」都看不出来 —— 而 `agent_node` 在 :613 算出 `budget_exhausted`
的那一刻，三个布尔分得清清楚楚。这正是 hermes「由走那条退出路径的代码自己写」的直接应用：
**盖章要盖在知道答案的那一行，不是盖在最后一道门上。**

**覆盖顺序**：`budget_exhausted` 为真时，`agent_node` 盖 `max_steps` / `no_progress` /
`token_budget` 三者之一，**不再盖 `text_response`** —— 收尾轮的响应天然没有 tool_calls，
不加这条就会被 `text_response` 覆盖掉。三者同时为真时按
`max_steps` > `no_progress` > `token_budget` 取第一个，**并在测试里钉死这个顺序**，
否则它是隐式的、随分支顺序漂移。

对外契约上 `exit_reason` 是**封闭取值集合**，将来新增是向后兼容的新值
（与 `approval.node` / `end.status` 同一口径，见
`docs/superpowers/specs/2026-08-17-external-docs-readability-w3-audit.md:100`）。

### 5.4 加字段安全，加事件类型不安全

对接方**非 strict 解析，自动忽略多出的字段** —— 2026-09-09 用户确认，记在
`docs/superpowers/specs/2026-09-09-external-feedback-eval-loop-design.md:121`。
仓里也有先例：`/v1/runs` 的 `thread_window_capped`，设计文档注明「老客户端忽略新字段无害」。

**但对接方的流处理有事件白名单**，不在白名单里的**事件类型**会被静默丢掉
（`item.*` 那轮踩过，是当时的头号坑）。所以本设计的信号**必须挂在已有的 `end` 帧上当字段**，
**不做成新的事件类型**。这是硬约束，不是偏好。

## 6. 数据怎么传到 `end_frame_data`

### 6.1 现成的原料：`tool_failures` 已经在分类了

`AgentState.tool_failures`（`state.py:224`）累积 `ClassifiedToolError`，
每条带 `error_class`，`transient` 与否已经分好。

**但它是按批重置的** —— `agent_node` 读完、发完 `<recovery-advisory>` 就写回 `[]`
（`builder.py:1244` / `:1284`）。走到 `END` 时它已经是空的，拿不到。

### 6.2 新增一个不重置的通道，且由 `tools` 节点写

```python
# AgentState 新增
last_batch_failures: NotRequired[list[ClassifiedToolError]]
```

**在 `tools` 节点写，不在 `agent` 节点写**，理由是可分辨性：

- `agent` 节点看到 `tool_failures == []` 时，分不清「这一批工具全成功」与「这一轮压根没跑工具」
- `tools` 节点**只在跑了一批工具时才执行**，所以它写的值天然带着「确实跑过一批」这个前提

且**每批都写，包括空列表**（`builder.py:1731` 现在是 `if tool_failures:` 才写，
本次要改成无条件写）。不这么做的话：批 1 失败 → 批 2 全成功 → 结束，
通道里还留着批 1 的失败，**误报**。

只存非 transient 的（`error_class != "transient"`），与 `error_signal`（:925）同一条谓词。

### 6.3 `completed` 的定义

```python
completed = exit_reason == "text_response" and not last_batch_failures
```

- 非 `text_response` 的出口（撞预算 / 挂审批 / 被否决）一律 `completed=false` ——
  平台**主动**中止的，按定义就没跑完
- `text_response` 且最后一批工具没有未解决的失败 → `true`
- `text_response` 但最后一批有非 transient 失败 → **`false`，而 `status` 仍是 `success`**
  ——**这一格就是 B-85 那次**

## 7. 第二条腿：让模型自己声明（openclaw 的做法）

`_assemble_system_prompt()`（`agent_factory.py:1677`）是平台计算出的提示词片段的唯一拼装口，
已有 `tool_use_enforcement` / `spotlight` / `worker_delegation` 三个同形状的先例。

本次加一段 completion contract，**平台级、无开关、默认开**
（按「平台缺口先改默认，别设计成让人去配」：opt-in 开关有人会忘，忘了静默跑偏比统一的坏更糟）：

```
<completion-contract>
在每一项被要求的事都做完、或者被你显式标成 [blocked] 并说明缺什么之前，
不要把任务当作已完成。工具失败导致做不下去时，明说是哪一步、缺什么，
不要用一段看起来完整的话把它盖过去。
</completion-contract>
```

### 7.1 诚实说明它产出什么

**这条腿产出的是给人读的文本，不是机器信号。** 平台**不解析** `[blocked]`
——解析模型自由文本是脆的，且一旦解析就变成了另一种形式的猜。

它的价值是让**最终答复本身**诚实，补的是 A 看不见的那一格：
模型换了个方法、绕过了失败、最后交出的东西其实没做成 ——
这种情况 `last_batch_failures` 是空的，A 判 `completed=true`，只有 B 能让它在正文里说出来。

反过来 B 也有盲区（模型就是不说），那一格由 A 兜。两条腿互补，但**互补的方式不同**：
A 给机器判据，B 给人类可读的诚实。

## 8. 要改的地方

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `orchestrator/state.py` | `AgentState` 加 `last_batch_failures` / `exit_reason` 两个通道 |
| 2 | `orchestrator/graph_builder/builder.py` | `tools` 节点无条件写 `last_batch_failures`（只留非 transient），并在写 `pending_approval` / `approval_outcome` 的同一处盖对应 `exit_reason`；`agent_node` 在 :613 算出 `budget_exhausted` 时按三个布尔盖章，否则（响应无 tool_calls）盖 `text_response`。**两个路由函数一行不改** |
| 3 | `orchestrator/sse.py` | `end_frame_data()` 加 `completed` / `exit_reason` 两个可选参数，沿用「None → 字段缺席」 |
| 4 | `control_plane/api/_run_event_stream.py` | 三处调用点把新参数传下去（否则两条流字段集合分叉——docstring 明确警告过） |
| 5 | run 记录 + 审计 | 落 `exit_reason` 与 `completed`，我们自己能查（不依赖对外流） |
| 6 | `orchestrator/agent_factory.py` | `_assemble_system_prompt` 加 completion contract 块 |
| 7 | `docs/api/streaming-events.md` + `apps/admin-ui/docs-site/guide/sse-events.md` | 两处对外文档都要写；**必须明写 `status=success` 不代表事做成了** |

## 9. 明确不做

- **不改 `status` 的取值或语义**（§2）
- **不新增 SSE 事件类型**（§5.4）
- **不解析模型正文里的 `[blocked]`**（§7.1）
- **不推断模型意图**——包括不做「零产物 = 放弃」这类判据（§3.3、§4）
- **不给 completion contract 加开关**（§7）
- **不改 `error_classifier` 的关键词表** —— ①② 已经定过调子：词表追不上错误文本，
  类型追得上；本次也不走词表

## 10. 一条不在本次范围的已知风险

对接方**加了字段也得去读**才有用。做完 §8 之后必须**主动通知对接方**，
并在对外文档里把 `status=success` 的真实含义写清 —— 否则这就是又一次
「把该做的事留给人」：平台这边看着接通了，对面一行代码没改。

通知这一步**不是可选的收尾**，是第 3 步交付物。

## 11. 验收判据

1. **单测**：`text_response` + 最后一批有非 transient 失败 → `completed=false` 且 `status=success`
   （B-85 那次的逐字重放）
2. **单测**：批 1 失败、批 2 全成功、然后结束 → `completed=true`（§6.2 的误报防线）
3. **单测**：`max_steps` / `no_progress` / `token_budget` 三种 `budget_exhausted`
   分别盖出三个**不同**的 `exit_reason`（证明 §5.3 那条「在 `_should_continue` 里分不出来」被绕开了）
4. **契约测**：两条 SSE 路径的 `end` 帧字段集合**逐字相同**
5. **契约测**：老 run（无记录）的 `end` 帧里两个字段**缺席**，不是 `null`
6. **变异**：把 `tools` 节点改回 `if tool_failures:` 才写 → 判据 2 必须变红
7. **真栈**：测试环境跑一个必然失败工具的 run，`end` 帧里 `completed=false`、`status=success`
8. **单测**：`budget_exhausted` 与「响应无 tool_calls」同时成立时，`exit_reason`
   **不是** `text_response`（收尾轮的覆盖顺序，§5.3）
9. **单测**：三个布尔同时为真时按 `max_steps` > `no_progress` > `token_budget` 取值
   （把隐式的分支顺序钉死）
10. **对外文档**两处都写到，且都明写 `status=success` ≠ 事做成了
