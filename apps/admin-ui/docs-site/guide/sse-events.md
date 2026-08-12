# SSE 事件格式

本篇讲清楚 Agent run 的流式响应长什么样——有哪些事件类型、每种事件的字段、以及连接断了之后怎么接回去。

## 流从哪来

- `mode: "stream"` 的 `POST /v1/agents/{agent_code}/runs` 响应体本身就是这条 SSE 流。
- `mode: "queue"` 不返回流(直接 `202`);想看这次 run 的事件,用 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>`——run 还在跑就实时接进去,run 已经跑完就把持久化的帧按顺序回放一遍再收尾。

::: warning 这条接口对没跑完的 run 是长连接
`GET .../runs/{run_id}/events` 打在一个还没结束的 run 上时会**一直挂着**,直到那个 run 走到终态才返回——这是"实时接进去"的应有之义,不是卡死,但服务端不会替你设上限:run 跑多久,连接就开多久;run 因为排队一直没被执行,连接就一直不返回。

所以客户端必须自己兜:设一个符合你业务的读超时(别用默认的"无限等"),超时后直接重新发起同一条请求重连,而不是重新调 `/runs`(那会开启新的一轮 run)。**这里有个坑**:超时说明 run 大概率还没跑完,重连接的还是实时(live)分支,而 `since_seq` 只在 [断线重连](#断线重连) 一节说的"run 已经结束"的回放分支上才生效——live 分支会完全忽略这个参数,重连会把连接缓冲区里当前还留着的帧(最多 256 帧,超出会丢最旧的)从头重推一遍,大概率包含你断线前已经处理过的帧。所以带不带 `since_seq` 结果一样:客户端必须按每帧的 `id`(`"{created_at_ms}-{seq}"`)里的 `seq` 自己去重,不能指望服务端替你跳过已处理的部分。只想粗粒度知道 run 结束没有、不想挂着等,调 `GET /v1/agents/{agent_code}/sessions?user_id=…` 看每项的 `running` 布尔字段。
:::

每一帧都是标准 SSE 格式:

```
id: <事件名不是 end 时都有,end 帧没有这一行>
event: <事件名>
data: <JSON>

```

除 `end` 帧外,每一帧都带 `id:`——不管是实时连接直接收到的,还是断线后回放拿到的,id 都是同一套(格式和用法见下面「断线重连」一节)。

## 事件类型一览

| `event:` | 什么时候出现 | 断线重连能拿回来吗 |
|---|---|---|
| `metadata` | run 开始时发一次(`run_id`、`thread_id`、trace id) | 能 |
| `updates` | 每完成一步 agent/tool 步骤发一次——**这一步权威的最终结果** | 能 |
| `token` | LLM 生成过程中的逐 token 预览(见下) | **不能,只在实时连接里出现** |
| `approval` | run 在人工审批节点暂停 | 能 |
| `retry` | 出现可重试的失败,发一条提示 | 能 |
| `error` | run 失败 | 能 |
| `end` | 流结束(终态标记) | — |

这张表列的是当前主要会遇到的事件类型,不是穷举——平台演进可能会新增新的 `event:` 类型。**收到不认识的 `event:` 类型,忽略这一帧就好,不要因为它报错中断处理。**

此外,连接存活期间服务端大约每 15 秒发一个 `: heartbeat` 注释帧(不是 `event:`,只是 SSE 注释行)保活——客户端可以忽略它的内容,但如果一段时间里连心跳都没收到,就该判断连接已经断了。

## `token` 事件:仅供预览,不是最终结果

流式能力的模型在生成答案时,会先把文本按 token 逐步预览出来:

```
event: token
data: {"step": 0, "channel": "content", "text": "部分答案片段"}

event: token
data: {"step": 0, "channel": "reasoning", "text": "让我想想..."}

event: token
data: {"step": 0, "channel": "tool_args", "tool_index": 0, "name": "search_web"}
```

- `step`——这个片段属于第几个 agent 步骤。
- `channel`——`"content"`(答案正文)/ `"reasoning"`(模型的思考过程,仅推理类模型有)/ `"tool_args"`(正在发起一次工具调用)。
- `content` / `reasoning` 帧带 `text`——已经过内容安全脱敏处理的文本片段。
- `tool_args` 帧带 `tool_index`(第几个并行工具调用)和 `name`(工具名,只在名字第一次出现时发一次);**工具的调用参数不会通过 `token` 流式吐出**,完整参数只出现在后面那条权威的 `updates` 帧里。

把 `token` 当纯预览用:

1. 按 `step` 把 `token.text` 累积起来做实时打字机效果。
2. 同一个 `step` 的 `updates` 帧一到,就用它替换掉之前攒的预览——`updates` 里的内容才是过了完整输出安全审查的最终结果;如果这一步被安全策略拦了,`updates` 里会是拒答文案,直接覆盖预览。
3. **断线重连时 `token` 帧不会被重新推给你**——只有 `metadata` / `updates` / `approval` / `retry` / `error` 这些落库的帧会回放。重连后凭这些帧重建状态,不要指望拿回丢失的逐 token 预览。

什么时候没有 `token` 事件:`mode: "queue"` 的 run、命中缓存的回答、不支持流式的 provider,以及开启了输出结果二次判定(judge)的 Agent——这些情况下只有 step 级的 `updates`。开启结构化输出(structured output)的 run 仍然会为主候选结果发 `token` 帧(schema 校验只发生在需要纠错重发的那一次,那一次不走流式)。

## 断线重连

1. 从响应头(`X-Expert-Work-Session-Id` / `X-Expert-Work-Run-Id`)或第一条 `metadata` 帧里拿到 `thread_id` + `run_id`,尽早存好。
2. 连接断开后,调 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>` 重新接上,不要重新调 `/runs`(那会开启新的一轮 run)。`user_id` 必填,而且必须是发起这次 run 的那个,否则 404。
3. 这条接口有两种情况:run 还在跑,直接实时续接最新事件;run 已经结束,把落库的帧按顺序回放一遍,结尾补一条 `end`——这种情况下可以加查询参数 `since_seq` 只回放某个位置之后的帧(SSE 帧的 `id` 是 `"{created_at_ms}-{seq}"` 形式,取你已经处理到的那个 `seq`)。
4. 不管哪种情况,回放都拿不到 `token` 预览帧——这是预期行为,不是丢帧;重连后的界面状态应该以最近一条 `updates` / `metadata` 为准,而不是试图拼回断连前的逐 token 预览。
