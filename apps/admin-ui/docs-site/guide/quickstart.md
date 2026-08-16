# 1 快速开始

这套 API 让你的服务端程序调用 Expert-Work 上的 Agent：你发一段用户输入，Agent 处理后把回答以 SSE（Server-Sent Events）流的形式实时返回。

本篇用四步跑通第一次调用，大约五分钟。

## 一次调用是怎么走的

```mermaid
sequenceDiagram
    autonumber
    participant U as 终端用户
    participant S as 你的服务端
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

三个概念，先建立印象：

| 概念 | 是什么 | 从哪来 |
|---|---|---|
| `agent_code` | 要调用哪个 Agent | 租户管理员在管理控制台创建 Agent 时定的名字，也可以调 [Agent 目录](./query#_5-1-agent-目录) 查 |
| `user_id` | 你自己业务系统里终端用户的标识 | 你自己定，比如内部用户 id。同一个人每次都传同一个值 |
| `session_id` | 一段多轮会话（一次连续的来回对话） | 第一次调用不用传，服务端返回后记下来，下一轮传回去 |

## 1.1 拿一把 Key

调用需要一个 `aforge_pat_` 开头的 Bearer key，由你的租户管理员在管理控制台为一个服务账号创建。给外部对接方的 key 一般只需要 `write` 一档权限。

创建方式、权限档位、轮换规则见 [认证与 Key](./auth)。拿到后放进每次请求的请求头：

```
Authorization: Bearer aforge_pat_xxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 1.2 发起第一次对话

```bash
curl -N -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "你好",
    "mode": "stream"
  }'
```

- `-N` 关闭 curl 的输出缓冲，这样能实时看到流式返回，而不是等请求结束一次性吐出来。
- `user_id` 必填。第一次出现的 `user_id` 会自动创建一个对应的终端用户，之后同一个值复用同一个用户。取值建议见 [对接注意事项](./best-practices#_9-2-user-id-怎么取)。
- `mode` 默认就是 `"stream"`，这里显式写出来。想要"先返回、后台跑"的异步模式，见 [2.4 stream 还是 queue](./chat#_2-4-stream-还是-queue)。

## 1.3 读懂返回的流

成功会得到 `200`、`Content-Type: text/event-stream`，响应头里有两个值要记下来：

| 响应头 | 用途 |
|---|---|
| `X-Expert-Work-Session-Id` | 这段对话的 id，下一轮续聊要用 |
| `X-Expert-Work-Run-Id` | 这次执行的 id，断线重连、取消都要用 |

响应体是一串 SSE 事件：

```
event: metadata
data: {"run_id":"...","thread_id":"..."}

event: updates
data: {...}

event: end
data: {"status":"success","run_id":"..."}
```

最后一帧 `end` 的 `status` 是这次执行的最终状态，四个取值：

| `status` | 含义 |
|---|---|
| `success` | 正常答完 |
| `paused` | 停在人工审批节点等你决策，**不是失败**，见 [4.2 审批决策](./run-control#_4-2-审批决策) |
| `interrupted` | 被取消或中断 |
| `error` | 失败，超时也归这一档 |

可回放的事件还会多一行 `id: {毫秒时间戳}-{seq}`（`end`、`token` 这类事件没有）。断线重连时把见过的最大 `seq` 传回去，服务端只补它之后的事件。

完整事件类型、字段含义、断线重连见 [读懂 SSE 流](./sse-events)。

## 1.4 接着聊下一轮

把上一步拿到的 `X-Expert-Work-Session-Id` 作为下次请求体里的 `session_id` 传回去，就是同一段会话的下一轮；不传就是另开一段新会话。

```bash
curl -N -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "session_id": "<上一步响应头里的 Session-Id>",
    "input": "刚才那个再详细说说",
    "mode": "stream"
  }'
```

同一个 `user_id` 下可以并存多段互不相干的会话。

## 接下来看什么

| 你想做的事 | 去哪 |
|---|---|
| 完整的请求参数、带附件、模板变量、防重复下发 | [2 跟 Agent 对话](./chat) |
| 解析 SSE 事件、断线重连 | [3 读懂 SSE 流](./sse-events) |
| 取消正在执行的任务、处理人工审批 | [4 对话过程中的控制](./run-control) |
| 查历史会话、查执行记录、下载产出文件 | [5 查询与管理](./query) |
| 各语言完整可运行示例 | [10 多语言示例](./examples) |
