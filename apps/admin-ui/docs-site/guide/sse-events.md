# 3 读懂 SSE 流

本篇讲清楚 Agent run 的流式响应长什么样——有哪些事件类型、每种事件的字段、以及连接断了之后怎么接回去。

## 3.1 帧格式

- `mode: "stream"` 的 `POST /v1/agents/{agent_code}/runs` 响应体本身就是这条 SSE 流。
- `mode: "queue"` 不返回流(直接 `202`)；想看这次 run 的事件，用 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>`——run 还在跑就实时接进去，run 已经跑完就把持久化的帧按顺序回放一遍再收尾。

::: warning 这条接口对没跑完的 run 是长连接
`GET .../runs/{run_id}/events` 打在一个还没结束的 run 上时会**一直挂着**，直到那个 run 走到终态才返回——这是"实时接进去"的应有之义，不是卡死，但服务端不会替你设上限:run 跑多久，连接就开多久；run 因为排队一直没被执行，连接就一直不返回。

所以客户端必须自己兜:设一个符合你业务的读超时(别用默认的"无限等")，超时后直接重新发起同一条请求重连，而不是重新调 `/runs`(那会开启新的一轮 run)。

**重连时一定要带上 `since_seq`**，而且带的是你已经见过的**最大** seq——服务端会只发它之后的帧。不带 `since_seq` 不会报错，但服务端会把这个 run **从第 0 帧起整个重发一遍**，你已经处理过的部分会全部再来一次。细节和坑见下面 [断线重连](#_5-6-断线重连)。

只想粗粒度知道 run 结束没有、不想挂着等，调 `GET /v1/agents/{agent_code}/sessions?user_id=…` 看每项的 `running` 布尔字段。
:::

每一帧都是标准 SSE 格式:

```
id: <可回放的帧才有这一行,见下>
event: <事件名>
data: <JSON>

```

**除 `end` / `truncated` / `gap` / `token` 这四种帧外，每一帧都带 `id:`**——不管是实时连接直接收到的，还是断线后回放拿到的，同一帧的 **`seq` 是同一个值**。

::: warning 不要拿完整的 id 字符串当键
id 里的**毫秒段不保证两次一致**:实时连接和回放是两次独立取的服务端时钟，同一帧可能相差 1 毫秒(实测约千分之一的帧会这样，机器越忙比例越高)。

要做去重键、幂等键、或者跟自己系统里的记录对应，**一律只用 `seq`**，别用整个 id 字符串——否则同一帧会被当成两帧处理，而且这种问题是间歇性的、很难复现。
:::

id 的格式是 `"{毫秒时间戳}-{seq}"`。`seq` 是这一帧在这次 run 里的序号(从 `0` 开始)，也是断线重连时 `since_seq` 参数**唯一合法的取值来源**。取 `seq` 请按**最后一个** `-` 切分，别按第一个(前半段是时间戳，本身不含 `-`，但别赌这一点)。

那四种没有 `id:` 的帧各有原因:`token` 是一次性预览(不落库、不占序号)，`gap` / `truncated` 描述的是**这条连接**的状况而不是 run 本身的事件，`end` 是流的终止标记。它们都不参与 `since_seq` 的计算。

## 3.2 事件总表

| `event:` | 什么时候出现 | 有 `id:` | 断线重连能拿回来吗 |
|---|---|---|---|
| `metadata` | run 开始时发一次，`data` 是 `{"run_id": "…", "thread_id": "…"}` | 有 | 能 |
| `updates` | 每完成一步 agent/tool 步骤发一次——**这一步权威的最终结果** | 有 | 能 |
| `token` | LLM 生成过程中的逐 token 预览(见下) | 无 | **不能，只在实时连接里出现** |
| `worker` | Agent 委托子任务(worker)时，子任务的开始 / 每步 / 结束各发一次 | 有 | 能 |
| `guard` | 平台护栏触发或预警(步数上限 / token 预算 / 无进展) | 有 | 能 |
| `compaction` | 上下文过长被自动压缩时发一次 | 有 | 能 |
| `approval` | run 在人工审批节点暂停 | 有 | 能 |
| `retry` | 出现可重试的失败，发一条提示 | 有 | 能 |
| `error` | run 失败 | 有 | 能 |
| `gap` | 有一段帧在**这条连接**上补不到了(见下) | 无 | — |
| `truncated` | 回放一页装不下，这一页到此为止、**流还没结束**(见下) | 无 | — |
| `end` | 流正常收尾，`data` 里带这次 run 的最终状态(见下) | 无 | — |

这张表列的是当前主要会遇到的事件类型，不是穷举——平台演进可能会新增新的 `event:` 类型。**收到不认识的 `event:` 类型，忽略这一帧就好，不要因为它报错中断处理。**

此外，连接存活期间服务端大约每 15 秒发一个 `: heartbeat` 注释帧(不是 `event:`，只是 SSE 注释行)保活——客户端可以忽略它的内容，但如果一段时间里连心跳都没收到，就该判断连接已经断了。

### end 帧的最终状态

```
event: end
data: {"status":"success","run_id":"5ee4e7f0-9074-42c6-88ef-3c3ed2ceb63d"}
```

`status` 只有四个取值，这是全集:

| `status` | 含义 | 客户端该怎么做 |
|---|---|---|
| `success` | 正常跑完 | 展示最终回答 |
| `paused` | 停在人工审批节点，等人决策。**这不是失败**——批准或拒绝之后，这一轮对话还会继续 | 弹审批界面，**别当错误报** |
| `interrupted` | run 被中断(比如调用方主动取消) | 按"已取消"处理，不必重试 |
| `error` | 执行失败。**超时也归在这里**，没有单独的 timeout 值 | 按失败处理，细节查 [错误码与限流](./errors) |

::: warning 破坏性变更——`end` 帧的 `data` 以前是 `null`
旧版本的 `end` 帧是 `data: null`，第三方分不清"正常答完"和"被取消"，只能再查一次 REST 接口。现在 `data` 是一个带 `status` / `run_id` 的对象。

如果你的对接代码写死了"`end` 帧的 data 一定是 null"或者对它做了 `null` 断言，改成读 `data.status`。
:::

**唯一拿不到最终状态的情况是 `truncated`**:那一页以 `truncated` 收尾、**不发 `end`**，所以这一页里没有任何 status。要看到最终状态，必须按下面「回放分页」一节循环拉到收到 `end` 为止。

## 3.3 updates 帧怎么解析

`updates` 是这一步的**权威结果**——界面该拿它来重建交互过程；`token` 帧只是生成过程中的预览，不能拿来当状态用(细节见 5.4)。

### 形状:`data` 是 `{节点名: 节点写入}`

`data` 是一个对象，键是节点名，值是这个节点这一步写入的内容——**一帧通常只有一个节点键**。真实样例(B 场景):

```
event: updates
data: {"memory_recall":{"recalled_memories":[],"_duration_ms":198}}
```

::: warning 节点写入可能是 `null`——这是第一天就会踩到的坑
真栈实测，三个场景全部出现过:

```
event: updates
data: {"workspace_ingest": null}
```

```
event: updates
data: {"memory_writeback": null}
```

客户端不能无条件 `data[节点名].messages`——**先判断整个节点写入是不是 `null`，再取 `messages`**。真栈三个场景里，`workspace_ingest` 和 `memory_writeback` 每次都是整体 `null`。
:::

### 节点名不是固定枚举

真栈见过的节点名:`memory_recall`、`workspace_ingest`、`agent`、`tools`、`memory_writeback`(三个场景里出现的顺序大致是 `memory_recall` → `workspace_ingest` → `agent` → `tools`/`agent` 交替若干次 → `memory_writeback`)。还有一些节点按 Agent 配置才会注册、才会出现，不必列全。

**遇到不认识的节点名，忽略这一帧就好，不要报错**——节点词表会随平台演进变化。

### 只需要读三个字段

节点写入(非 `null` 时)里可能带不少通道，但对接方只需要读三个:

| 字段 | 含义 |
|---|---|
| `messages` | 这一步新产出的消息(数组，可能是空数组) |
| `step_count` | 这一步的编号，**从 `1` 开始**；只出现在 `agent` 节点的写入里，`tools` 节点没有这个字段 |
| `_duration_ms` | 距上一帧过去了多少毫秒(平台注入，每个非 `null` 的节点写入都有) |

其余通道是内部调度用的，不保证稳定——列在这里只是免得你以为自己漏读了:

- `agent` 节点还有:`escalate_next`、`last_plan_goal`、`no_progress_streak`、`step_count_refund_pending`、`tool_failures`
- `tools` 节点还有:`step_count_refund_pending`
- `memory_recall` 节点还有:`recalled_memories`(真栈实测里是空数组)

### `messages[]` 里的消息形状

`messages` 数组里的每一项按 `type` 分两种。

**`type: "ai"`**(来自 `agent` 节点)

| 字段 | 含义 |
|---|---|
| `content` | 这一步的文本产出；**空串是正常的**——这一步只发工具调用时就是空，别当异常或"答完了" |
| `tool_calls[]` | 这一步发起的工具调用，每项 `{name, args, id}` |
| `response_metadata.finish_reason` | `"tool_calls"`(还要继续下一步)/ `"stop"`(这一步已经答完) |
| `usage_metadata` | 这一步的 token 用量，可以直接拿来做用量展示 |
| `additional_kwargs.reasoning_content` | 模型的思维链原文；**不保证每个模型都有，也不保证长期存在**，别当结构化字段依赖 |

真实样例(B 场景，截断):

```json
{
  "content": "",
  "additional_kwargs": {
    "reasoning_content": "The user wants me to create a file called probe_note.txt in the workspace with content \"hello-probe\", then read it back and tell them what the content is. Let me do both the write and read. Since I want to write then read, these are dependent (I should write first, then read). Actually, the read depends on the write being complete, so I should do them sequentially. But actually, write_file is atomic and returns immediately, so I can do the write and then the read. Let me do the write first.\n\nActually, the instructions say if there are no dependencies between calls, make them in the same block. But here the read depends on the write being done first. So I should do them sequentially.\n\nLet me write the file first.",
    "expert_work_created_at": "2026-08-15T03:42:32.138398+00:00",
    "expert_work_run_id": "67262572-5470-41a4-800d-592762ec679d"
  },
  "response_metadata": {"finish_reason": "tool_calls", "model_name": "glm-5.2"},
  "type": "ai",
  "name": null,
  "id": "19bad813-…",
  "tool_calls": [
    {"name": "write_file", "args": {"path": "probe_note.txt", "content": "hello-probe"}, "id": "call_de58e676916d442d925bff27", "type": "tool_call"}
  ],
  "invalid_tool_calls": [],
  "usage_metadata": {
    "input_tokens": 6027, "output_tokens": 178, "total_tokens": 6205,
    "input_token_details": {"cache_read": 5952}, "output_token_details": {"reasoning": 156}
  }
}
```

**`type: "tool"`**(来自 `tools` 节点)

| 字段 | 含义 |
|---|---|
| `name` | 工具名 |
| `tool_call_id` | 配对键，见下 |
| `content` | 工具执行结果的文本；**经过防注入包装，直接渲染是乱码**，还原方法见下面的红色提示 |
| `status` | 执行状态；实测 `"success"`。非 `"success"` 即为这一步工具失败，具体取值不在这里穷举 |
| `artifact` | 工具产出的结构化数据，**形状按工具而定**——有就用，不认识就忽略 |
| `additional_kwargs.duration_ms` | 这个工具本身跑了多久(毫秒) |

真实样例(B 场景):

```json
{
  "content": "«UNTRUSTED nonce=<random>»\nWrote▁ 11▁ bytes▁ to▁ probe_note.txt\n«/UNTRUSTED nonce=<random>»",
  "additional_kwargs": {"duration_ms": 1848},
  "response_metadata": {},
  "type": "tool",
  "name": "write_file",
  "id": "89479877-…",
  "tool_call_id": "call_de58e676916d442d925bff27",
  "artifact": {"path": "probe_note.txt", "content_hash": "aded7388…", "size": 11},
  "status": "success"
}
```

### 配对键:`ai.tool_calls[].id` ↔ `tool.tool_call_id`

界面把"调用"和"结果"连成一条，**只能靠这一对 id**:上面 `ai` 消息里 `tool_calls[].id` 是 `call_de58e676916d442d925bff27`，下面 `tool` 消息里 `tool_call_id` 是同一个值——它们是同一次工具调用的两半。`tool_calls` 是数组，理论上一次 `agent` 步可以有多个并行调用(本次三个场景实测都只有一个，没观察到并行的例子)——配对时按 id 逐个对，不要按数组下标对。

::: danger 工具结果的文本是防注入包装过的——直接渲染是乱码
上面那条真实样例里的 `content` 原文长这样:

```
«UNTRUSTED nonce=<random>»
Wrote▁ 11▁ bytes▁ to▁ probe_note.txt
«/UNTRUSTED nonce=<random>»
```

这是平台对工具结果做的防间接提示注入处理，**流里带出去的就是处理后的文本**——不做处理直接显示，用户看到的就是这种夹着 `«UNTRUSTED …»` 围栏和 `▁` 字形的乱码。处理分两部分:

1. **围栏**:前后包一层 `«UNTRUSTED nonce=<随机串>»` / `«/UNTRUSTED nonce=…»`——这个随机串由平台生成，**不要把它写死在代码里，也不要基于它的取值做任何判断**，用下面的正则匹配任意值即可。
2. **空白标记**:每一段连续空白被替换成 `▁ `(一个 U+2581 字形加一个空格)——所以 `Wrote 11 bytes to probe_note.txt` 变成了 `Wrote▁ 11▁ bytes▁ to▁ probe_note.txt`。

要给人看就必须还原，三步(可以直接抄):

```
去掉  /«UNTRUSTED nonce=[^»]*»\n?/g           ← 开围栏
去掉  /\n?«\/UNTRUSTED nonce=[^»]*»/g          ← 闭围栏
删掉  /▁/g                                     ← 只删字形,后面那个空格留着,空白因此还原成一个空格
```

**还原是有损的**:原文里的换行在标记空白时变成了 `▁ `，删掉 `▁` 字形之后剩下的是一个空格——**原文的换行拿不回来**。

围栏在不在，本身是个有用信号(意味着这段内容来自外部、不可信)。建议在还原之前先记一个标志位(比如"这条消息是否包含过 `UNTRUSTED` 围栏")，而不是只留下还原后的文本。
:::

### 帧的数量级:别拿 `token` 重建状态

真栈实测三个场景的帧数:

| 场景 | `updates` | `token` |
|---|---|---|
| 简单问答 | 4 | 58 |
| 工具调用 | 8 | 146 |
| 分两步 | 6 | 954 |

`token` 帧占九成以上。界面该用 `updates` 重建状态、`token` 只做打字机预览，这张表是实测依据。

## 3.4 token 帧

流式能力的模型在生成答案时，会先把文本按 token 逐步预览出来:

```
event: token
data: {"step": 1, "channel": "content", "text": "部分答案片段"}

event: token
data: {"step": 1, "channel": "reasoning", "text": "让我想想..."}

event: token
data: {"step": 1, "channel": "tool_args", "tool_index": 0, "name": "search_web"}
```

- `step`——这个片段属于第几个 agent 步骤。
- `channel`——`"content"`(答案正文)/ `"reasoning"`(模型的思考过程，仅推理类模型有)/ `"tool_args"`(正在发起一次工具调用)。
- `content` / `reasoning` 帧带 `text`——已经过内容安全脱敏处理的文本片段。
- `tool_args` 帧带 `tool_index`(第几个并行工具调用)和 `name`(工具名，只在名字第一次出现时发一次)；**工具的调用参数不会通过 `token` 流式吐出**，完整参数只出现在后面那条权威的 `updates` 帧里。

把 `token` 当纯预览用:

1. 按 `step` 把 `token.text` 累积起来做实时打字机效果。
2. 同一个 `step` 的 `updates` 帧一到，就用它替换掉之前攒的预览——这里的 `step` 和 `updates` 帧里 `agent` 节点写入的 `step_count` 是同一个编号(都从 `1` 开始)，按它配对；`updates` 里的内容才是过了完整输出安全审查的最终结果；如果这一步被安全策略拦了，`updates` 里会是拒答文案，直接覆盖预览。
3. **断线重连时 `token` 帧不会被重新推给你**——只有 `metadata` / `updates` / `worker` / `guard` / `compaction` / `approval` / `retry` / `error` 这些落库的帧会回放。重连后凭这些帧重建状态，不要指望拿回丢失的逐 token 预览。
4. `token` 帧**没有 `id:`、不占 seq 序号**，所以它既不影响你的重连游标，也不会因为重复或丢失而破坏去重。

什么时候没有 `token` 事件:`mode: "queue"` 的 run、命中缓存的回答、不支持流式的 provider，以及开启了输出结果二次判定(judge)的 Agent——这些情况下只有 step 级的 `updates`。开启结构化输出(structured output)的 run 仍然会为主候选结果发 `token` 帧(schema 校验只发生在需要纠错重发的那一次，那一次不走流式)。

## 3.5 worker / guard / compaction 帧

这三种事件不是另一套跟 `updates` 平行的"结果"通道，而是三类平台内部机制向外暴露的可观测切面——子任务委托(`worker`)、护栏动作(`guard`)、自动压缩(`compaction`)。界面可以用它们把 agent 的内部动作展示给用户，但"这一步权威结果"仍然只认 `updates`(见 5.3)。

### worker 帧

Agent 委托子任务(worker)时，子任务的开始 / 每步 / 结束各发一次 `worker` 事件——比如子 agent、并行执行的子任务。

每一条 `worker` 帧都带这组固定字段，`data` 的形状再按 `kind` 分:

| 字段 | 含义 |
|---|---|
| `worker_id` | 这个 worker 实例的唯一标识 |
| `parent_worker_id` | 委托出这个 worker 的上一级 worker 的 `worker_id`；不是由另一个 worker 委托出来的(比如直接挂在主 run 下)时是 `null` |
| `parent_tool_call_id` | 触发这个 worker 的那次工具调用的 id，和 5.3 里 `ai` 消息 `tool_calls[].id` 是同一种值——**界面把子任务的时间线挂到对应的工具卡下面，靠的就是这个字段** |
| `label` | 这个子任务的人类可读标签 |
| `agent_ref` | 这个 worker 用的是哪个 Agent |
| `depth` | 委托层级，数值越大说明委托嵌套得越深 |
| `kind` | `start` / `update` / `end`，决定 `data` 的形状，见下面三张表 |
| `wseq` | 见下面的坑 |
| `data` | 按 `kind` 而定 |

::: warning `wseq` 不是 SSE 的 `seq`，不能拿它当重连游标
`wseq` 是**这一个 worker 自己的序号**，从 0 起数，作用域只在这个 worker 内部——同一个 run 里不同 worker 的 `wseq` 各自独立计数，互不相干。它和 5.1 讲的、决定 `since_seq` 的那个 `seq` 是两回事:断线重连、去重仍然只认帧的 `seq`，`wseq` 不参与。
:::

`kind: "start"` 的 `data`:

| 字段 | 含义 |
|---|---|
| `task_excerpt` | 委托给这个 worker 的任务描述(摘要) |
| `role` | 这个 worker 的角色 |
| `max_steps` | 这个 worker 允许执行的最大步数 |

`kind: "update"` 的 `data`:

| 字段 | 含义 |
|---|---|
| `node` | 触发这次更新的节点名，含义同 5.3 |
| `_duration_ms` | 距这个 worker 上一帧过去了多少毫秒 |
| `step_count` | 到这一步为止的步数(可选，只在部分节点出现) |
| `messages` | 这一步新产出消息的**摘要**——不是 5.3 那种原样消息，见下面的坑 |

::: warning `update` 帧里的 `messages` 是摘要，不是原样消息，而且同样带着防注入包装
这里的 `messages` 长得像 5.3 里的 `messages`，但**不是同一种东西**:每一项是摘要，字段名都带 `_excerpt` 后缀，而且有截断上限——正文摘要最长 500 字符，工具参数摘要最长 200 字符，工具结果摘要最长 500 字符，超过部分被截掉。

按 `type` 分两种形状:

- `type: "ai"`:`{type, content_excerpt, tool_calls: [{name, args_excerpt}]}`
- `type: "tool"`:`{type, name, tool_result_excerpt, exec?: {exit_code, timed_out, stdout_excerpt, stderr_excerpt}}`——`exec` 只在沙箱执行类工具上才有

**这些 `_excerpt` 字段同样没有剥掉防注入包装**(围栏 + 空白标记那一套，见 5.3「配对键」那一节的说明)，渲染给人看之前要照 5.3 讲的那三步还原，做法完全一样，这里不重复。
:::

`kind: "end"` 的 `data`:

| 字段 | 含义 |
|---|---|
| `outcome` | 这个 worker 执行完的结果；契约里没有列全取值，不在这里穷举，遇到没见过的值照常展示即可 |
| `iteration_used` | 实际用掉的步数 |
| `llm_call_count` | 这个 worker 内部发起的 LLM 调用次数 |
| `wall_clock_ms` | 这个 worker 从 `start` 到 `end` 的墙钟耗时(毫秒) |

### guard 帧

平台护栏触发或预警时发一条 `guard` 事件——覆盖步数上限、token 预算、检测到没有实际进展这三类护栏。

```
event: guard
data: {"kind": "tripped", "guard": "max_steps", "detail": {"steps": 32, "max": 32}}
```

| 字段 | 含义 |
|---|---|
| `kind` | `tripped`(护栏触发，见下面的说明)/ `warning`(预警，还没真的触发) |
| `guard` | 哪一类护栏:`max_steps`(步数上限)/ `token_budget`(token 预算)/ `no_progress`(检测到没有实际进展) |
| `detail` | 具体数值，形状按 `guard` 而定 |

`detail` 的形状:

| `guard` | `detail` |
|---|---|
| `max_steps` | `{steps, max}` —— 已执行步数 / 步数上限 |
| `token_budget` | `{spent, limit}` —— 已花费 token / token 预算上限 |
| `no_progress` | `{streak, max}` —— 连续无进展的步数 / 允许的上限 |

::: warning `guard` 不是错误——`tripped` 意味着这一轮被收尾，不是 run 崩了
收到 `kind: "tripped"` 的 `guard` 帧，意味着平台主动把这一轮对话收了尾(比如步数到了上限就不再继续往下执行)，**不是执行出错崩溃**。把 `guard: tripped` 当错误处理、弹错误提示给用户，是把一次正常的护栏收尾误报成了失败。

`kind: "warning"` 比 `tripped` 更轻，只是"快到上限了"的提示，不代表任何收尾动作已经发生。
:::

### compaction 帧

```
event: compaction
data: {"passes": 1, "tokens_before": 18420, "tokens_after": 6103, "summary_chars": 2048}
```

| 字段 | 含义 |
|---|---|
| `passes` | 这次压缩执行了几轮 |
| `tokens_before` | 压缩前的 token 数 |
| `tokens_after` | 压缩后的 token 数 |
| `summary_chars` | 压缩后摘要文本的字符数 |

上下文太长时，平台会自动把早于当前请求的历史对话压缩成摘要——`compaction` 帧就是这个动作发生时的通知。**这不影响这次回答的正确性**，但界面可以拿它给用户一个提示，比如"对话较长，已自动整理过历史记录"。

## 3.6 断线重连

### 基本流程

1. 从响应头(`X-Expert-Work-Session-Id` / `X-Expert-Work-Run-Id`)或第一条 `metadata` 帧里拿到 `thread_id` + `run_id`，尽早存好。
2. 边收帧边维护一个游标:**你见过的最大 seq**(见下面「游标怎么维护」)。
3. 连接断开后，调 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>&since_seq=<游标>` 重新接上，不要重新调 `/runs`(那会开启新的一轮 run)。`user_id` 必填，而且必须是发起这次 run 的那个，否则 404。
4. 一直重连到收到 `end` 帧为止。收到 `truncated` 帧不算结束，见下面「回放分页」。

`since_seq` 的语义是**开区间**:服务端只发 seq **严格大于**它的帧，你传回去的那一帧不会重复发给你。取值必须 ≥ 0(负数直接 422)。

### 两条分支:live 与 replay

这条接口有两种情况，`since_seq` 在**两条分支上都生效**:

| | run 还在跑(live) | run 已经结束(replay) |
|---|---|---|
| 响应头 `X-Expert-Work-Stream-Mode` | `live` | `replay` |
| `since_seq` | 生效——先把它之后已落库的帧补齐，再接上实时流 | 生效——只回放它之后的帧 |
| 不带 `since_seq` | **从第 0 帧起**把整个 run 重发一遍，再接实时流 | **从第 0 帧起**回放整个 run |
| 遇到落库空洞 | 补得上的晚一点补发给你(所以帧会乱序到达)；补不上的发一帧 `gap` 告诉你(见下) | **静默跳过，不发 `gap`** |
| 分页 | 不分页，一直流到 run 结束 | 一页装不下时以 `truncated` 收尾(见下) |
| 收尾 | `end` | `end`，或者 `truncated`(还有下一页) |

::: warning 不带 `since_seq` = 重放整个 run
这是本次更新的行为变化。以前不带 `since_seq` 重连只会把服务端内存里最近缓冲的一小段帧重推给你；**现在它会从落库的第 0 帧开始把整个 run 重发一遍**。长 run 上这意味着一大堆重复帧(而且可能触发下面的回放分页)。

重连时永远带上游标。
:::

### 游标怎么维护:用"见过的最大 seq"，不是"最后一帧的 seq"

**帧的到达顺序不保证等于 seq 递增顺序。** 实时分支上，某些帧会被补发得比它后面的帧更晚，你实际收到的可能是:

```
seq: 0, 1, 2, 5, 6, 3, 4, 7
```

所以**重连游标**要写成 `cursor = max(cursor, seq)`，而不是"记住最后一帧的 seq"。按后者写，上面这个序列会让你在收到 `4` 之后把游标退回 `4`，重连时 `5` `6` 就会重复发给你。

**去重是另一回事，别拿游标当去重判据。** 判断"这一帧我是不是已经处理过"要按 seq 精确判(记一个已处理 seq 的集合)；**不能**写成"seq ≤ 游标就丢"——上面序列里的 `3` `4` seq 就低于当时的游标，那样写会把两帧真实事件误丢。

代价是:如果你在收到 `3` `4` 之前就断线重连(游标已经是 `6`)，这两帧在新连接上就补不回来了。要一帧不落，等 run 走到终态之后做一次完整回放(不带 `since_seq`)。

### `gap` 事件:这一段在这条连接上补不到了

```
event: gap
data: {"from": 3, "to": 7}
```

含义是:seq `3` 到 `7`(闭区间，两端都含)这一段，**在这条连接上没法给你了**。

- **这不代表这些帧不存在。** 多数情况下它们只是当时还没落盘(帧的持久化是异步批量做的)，或者已经滚出了服务端的实时缓冲。**run 结束后重新发起一次不带 `since_seq` 的回放，通常能完整拿到。**
- `gap` **只出现在 live 分支**。回放分支遇到落库空洞是静默跳过的，不会有 `gap` 帧。
- **实时流上不要用"seq 必须连续"来判丢帧**——补发的帧可能乱序到达(见上面「到达顺序」那节)，连续性要等流结束后再算。
- **回放上可以校验连续性，而且命中了就是真的少帧。** 一次完整回放(不带 `since_seq`)返回的 seq 应当是连续的；出现跳号意味着那几帧**从来没有落盘**(服务端持久化是异步批量做的，极端情况下会整批失败)，而回放分支**不会**为此发 `gap` 帧。所以对归档 / 审计这类"一帧不能少"的场景，**连续性校验是你唯一的探测手段，该做**；发现跳号时，那段内容确实已经拿不回来了，应当据此在你自己的记录里标注缺失，而不是当成正常噪声忽略。
- `gap` 帧没有 `id:`、不落库，不参与游标计算。收到它之后，继续把后面的帧按 seq 正常处理即可。

需要"一帧不落"的场景(比如要把完整事件流归档)，做法是等 run 走到终态之后，再做一次完整回放，而不是指望实时流不丢。

### 回放分页:`truncated` 帧与 `X-Expert-Work-Next-Seq`

回放一次只返回一页。这一页装不下时，流以 `truncated` 帧收尾，**并且不发 `end`**:

```
event: truncated
data: {"next_seq": 499}
```

同一个值也在响应头 `X-Expert-Work-Next-Seq` 里(帧和头一定同时给，值一定一致)。

**为什么帧和响应头都给:**中间代理会剥掉它不认识的响应头，而 body 里的帧不会被剥——信号放在流里比放在头里稳。能读到响应头的客户端直接用头，读不到的以帧为准，两条都实现最省心。

客户端的处置:拿 `next_seq` 当下一次请求的 `since_seq`，请求**事件回放接口** `GET /v1/agents/{agent_code}/runs/{run_id}/events`，把各页的帧拼起来，**直到收到 `end` 为止**。

::: warning 如果这条 `truncated` 是从 `POST .../runs` 的重试里收到的
带同一个 `Idempotency-Key` 重试 `POST .../runs`(`mode: "stream"`)时，拿到的是同一份回放实现的输出，**同样会截断**。但 `POST .../runs` 的请求体和查询参数里**都没有 `since_seq`** ——原样重发那个 POST 只会永远拿回同一个第一页。

翻页必须换成 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=…&since_seq=N`。`run_id` 从响应头 `X-Expert-Work-Run-Id` 里取。
:::

```bash
# 第一页
curl -N "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}/events?user_id=u-123" \
  -H "Authorization: Bearer <key>"
# → 末帧 event: truncated / data: {"next_seq":499}

# 第二页:把 next_seq 原样当 since_seq 传回去
curl -N "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}/events?user_id=u-123&since_seq=499" \
  -H "Authorization: Bearer <key>"
```

三条容易踩的地方:

- **`truncated` 不是终点。** 那一页里没有 `end`，也就**没有最终 `status`**——不循环拉完，你根本不知道这次 run 是成功、被取消，还是在等审批。把 `truncated` 当成流结束会静默丢掉后面所有帧。
- **别把页大小写死。** 一页当前最多 500 帧，但这个数字可能调整；判断依据永远是"有没有收到 `truncated` 帧 / `X-Expert-Work-Next-Seq` 头"，不是"这一页收了多少帧"。
- **给循环加个上限。** 别写一个理论上能无限拉下去的循环；超过你设的页数上限就报警，别默默转圈。

### `since_seq` 只能来自服务端发过的帧

`since_seq` 唯一合法的来源是:服务端发给过你的某一帧 `id:` 里的 `seq`，或者 `truncated` 帧 / `X-Expert-Work-Next-Seq` 头给的 `next_seq`。**别自己算、别自己加一、别用你本地的消息条数去凑。**

传一个超过这个 run 真实尾部的值时，**服务端不会报错**，而是安安静静什么都不发。两条分支的表现还不一样:run 还在跑时，你只会收到心跳，以及它走到终态时的那条 `end`；run 已经结束时，连心跳都没有——流立刻返回一条 `end` 就关掉。两种都看起来像"这个 run 没有任何事件"，非常难查。

**这是有意的，不是漏掉了校验。**帧的落库是异步批量进行的，落库的尾部本来就合法地落后于实时流，服务端分不清"客户端传了个错的值"和"这几帧还没落盘"；在这里做钳制反而会把已经发过的帧再发一遍。所以口径是:服务端如实按你给的游标发，游标的正确性由客户端保证。

### 其它

不管哪条分支，回放都拿不到 `token` 预览帧——这是预期行为，不是丢帧；重连后的界面状态应该以最近一条 `updates` / `metadata` 为准，而不是试图拼回断连前的逐 token 预览。
