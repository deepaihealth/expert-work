# 1 快速开始

这套 API 供调用方的服务端程序调用 Expert-Work 上的 Agent：调用方发送一段终端用户的输入，Agent 处理后把回答以 SSE（Server-Sent Events）流的形式实时返回。

本章用 1.1 至 1.4 四个步骤完成第一次调用，预计五分钟。

```mermaid
sequenceDiagram
    autonumber
    participant U as 终端用户
    participant S as 调用方服务端
    participant E as Expert-Work API
    U->>S: 用户提问
    S->>E: POST /v1/agents/{agent_code}/runs<br/>Authorization: Bearer <key>
    E-->>S: 200 text/event-stream<br/>响应头带 Session-Id / Run-Id
    loop 边生成边推送
        E-->>S: event: updates（新消息、工具调用……）
        S-->>U: 实时展示
    end
    E-->>S: event: end（最终状态）
```

先明确三个概念：

| 概念 | 含义 | 来源 |
|---|---|---|
| `agent_code` | 要调用哪个 Agent | 租户管理员在管理控制台创建 Agent 时定的名字，也可以调 [5.1 Agent 目录](./query#_5-1-agent-目录) 查询 |
| `user_id` | 终端用户在调用方系统里的标识 | 由调用方自行确定，例如内部用户 id。同一个人每次都传同一个值 |
| `session_id` | 一段多轮会话，即一次连续的来回对话 | 第一次调用不用传，服务端返回后记下来，下一轮传回去 |

## 1.1 取得 API Key

调用需要一个以 `aforge_pat_` 开头的 Bearer key，由租户管理员在管理控制台为一个服务账号创建。给外部对接方的 key 一般只需要 `write` 一档权限。

创建方式、权限档位、轮换规则见 [6 认证与 Key](./auth)。取得 key 后放进每次请求的请求头：

``` [请求头]
Authorization: Bearer aforge_pat_xxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 1.2 发起第一次对话

```bash [请求]
curl -N -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "你好",
    "mode": "stream"
  }'
```

- `-N` 关闭 curl 的输出缓冲，这样能实时看到流式返回，而不是等请求结束后一次性输出。
- `user_id` 必填。首次出现的 `user_id` 会自动创建一个对应的终端用户，之后同一个值复用同一个终端用户。取值要求见 [9.2 user_id 的取值要求](./best-practices#_9-2-user-id-的取值要求)。
- `mode` 的默认值就是 `"stream"`，示例中显式写出。需要「先返回、后台执行」的异步模式时，见 [2.4 stream 还是 queue](./chat#_2-4-stream-还是-queue)。

## 1.3 读懂返回的事件流

成功时返回 `200` 与 `Content-Type: text/event-stream`，响应头里有两个值需要记下来：

| 响应头 | 用途 |
|---|---|
| `X-Expert-Work-Session-Id` | 这段会话的 id，下一轮续接时要用 |
| `X-Expert-Work-Run-Id` | 这次执行的 id，断线重连、取消都要用 |

响应体是一串 SSE 事件：

``` [事件流片段]
event: metadata
data: {"run_id":"...","thread_id":"..."}

event: updates
data: {...}

event: end
data: {"status":"success","run_id":"..."}
```

最后一个 `end` 事件的 `status` 是这次执行的最终状态，四个取值：

| `status` | 含义 |
|---|---|
| `success` | 正常答完 |
| `paused` | 停在人工审批处等待调用方决策，不是失败，处理方式见 [4.2 审批决策](./run-control#_4-2-审批决策) |
| `interrupted` | 被取消或中断 |
| `error` | 失败，超时和步数用尽也归这一档 |

这张表只是摘要。每个取值下客户端具体该做什么，见 [3.4 的 `end` 事件](./sse-events#end)。

run 本身发生的事件还多一行 `id:`，格式是「毫秒时间戳-序号」，`end`、`token` 这类事件没有这一行。断线重连时把已收到的最大 `seq` 传回服务端，服务端只补发这个序号之后的事件，这一过程称为续传。

完整的事件类型、字段含义与断线重连见 [3 读懂 SSE 流](./sse-events)。

## 1.4 继续下一轮对话

把上一步得到的 `X-Expert-Work-Session-Id` 作为下一次请求体里的 `session_id` 传回去，就是同一段会话的下一轮；不传就是另开一段新会话。

```bash [请求]
curl -N -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "session_id": "{session_id}",
    "input": "刚才那个再详细说说",
    "mode": "stream"
  }'
```

同一个 `user_id` 下可以并存多段互不相干的会话。

## 1.5 后续阅读

| 需要做的事 | 参考章节 |
|---|---|
| 完整的请求参数、带附件、模板变量、防重复下发 | [2 跟 Agent 对话](./chat) |
| 解析 SSE 事件、断线重连 | [3 读懂 SSE 流](./sse-events) |
| 取消正在执行的 run、处理人工审批 | [4 对话过程中的控制](./run-control) |
| 查历史会话、查执行记录、下载产出文件 | [5 查询与管理](./query) |
| 各语言完整可运行示例 | [10 多语言示例](./examples) |
