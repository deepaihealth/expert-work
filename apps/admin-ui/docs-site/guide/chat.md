# 2 跟 Agent 对话

发起一次对话是整套 API 的核心动作，只有一个端点：`POST /v1/agents/{agent_code}/runs`。

本篇讲清楚：这个端点怎么调、每个参数什么含义、两种执行模式怎么选、怎么带附件、怎么避免网络重试导致重复执行。

## 2.1 一次对话是怎么走的

一次调用会依次完成四件事：确认终端用户身份 → 绑定或续接一段会话 → 执行一次 Agent → 返回结果。这一次执行在文档里称作一个 **run**。

```mermaid
sequenceDiagram
    autonumber
    participant S as 你的服务端
    participant E as Expert-Work API
    participant A as Agent 执行

    S->>E: POST /runs {user_id, input, session_id?, mode}
    Note over E: 校验 key 与权限<br/>首次出现的 user_id 自动创建终端用户<br/>不带 session_id 则新建一段会话

    alt mode = "stream"（默认）
        E-->>S: 200 text/event-stream
        E->>A: 开始执行
        loop 执行过程
            A-->>S: event: updates / token / ……
        end
        A-->>S: event: end {status}
    else mode = "queue"
        E-->>S: 202 {run_id, thread_id, status:"queued"}
        E->>A: 后台异步执行
        S->>E: GET /runs/{run_id}/events（想看过程时再接）
        E-->>S: SSE 流（实时或回放）
    end
```

执行结束时 run 会落到一个最终状态：`success` / `error` / `timeout` / `interrupted` / `paused`。其中 `paused` 表示停在人工审批节点等待决策，需要你调审批接口推进，见 [4 对话过程中的控制](./run-control)。

## 2.2 发起对话

```
POST /v1/agents/{agent_code}/runs
Authorization: Bearer <key>   # 需要 write 权限，见「认证与 Key」
Content-Type: application/json
```

`{agent_code}` 是你租户里已发布、状态为 ACTIVE 的 Agent 名字。同一个名字只有一个 ACTIVE 版本会被使用；这个名字下没有 ACTIVE 版本时返回 404（`AGENT_NOT_FOUND`）。

不确定有哪些 `agent_code` 可用，先查 [5.1 Agent 目录](./query#_5-1-agent-目录)。

最小请求：

```bash
curl -N -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "input": "你好"}'
```

## 2.3 请求参数详解

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string，1–255 字符 | 是 | 你自己系统里终端用户的标识。见下方「`user_id` 的作用」。 |
| `session_id` | UUID | 否 | 续接一段已有会话。省略则新建一段。传了但这段会话不属于这个 `user_id` / `agent_code`，返回 404（`SESSION_NOT_FOUND`）。 |
| `input` | string，≤65536 字符 | 否 | 这一轮用户说的话或任务描述。 |
| `mode` | `"stream"` \| `"queue"` | 否 | 默认 `"stream"`。见 [2.4](#_2-4-stream-还是-queue)。 |
| `image_refs` | string[]，最多 64 项 | 否 | 图片引用，由上传接口返回。见 [2.6](#_2-6-带图片和文档)。 |
| `untrusted_content` | string[]，最多 16 项 | 否 | 来自外部、不可信任的文本内容。见 [2.7](#_2-7-外部内容与模板变量)。 |
| `inputs` | object | 否 | 提示词模板变量。见 [2.7](#_2-7-外部内容与模板变量)。 |
| `files` | 数组，最多 64 项 | 否 | 附件引用（图片或文档）。见 [2.6](#_2-6-带图片和文档)。 |

### `user_id` 的作用

第一次出现的 `user_id` 会自动创建一个对应的终端用户。这个终端用户是长期记忆、工作区文件、按用户维度的用量计费的归属对象——它们挂在终端用户身上，不是挂在你的 API Key（服务账号）上。

同一个 `user_id` 后续调用复用同一个终端用户，不会重复创建。取值要求见 [9.2 `user_id` 怎么取](./best-practices#_9-2-user-id-怎么取)。

## 2.4 stream 还是 queue

| | `mode: "stream"`（默认） | `mode: "queue"` |
|---|---|---|
| 响应 | `200`，`Content-Type: text/event-stream`，响应体就是 SSE 流 | `202`，立即返回 JSON，由后台异步执行 |
| 响应头 | 带 `X-Expert-Work-Session-Id` 和 `X-Expert-Work-Run-Id` | **不带**这两个头 |
| 怎么拿 session_id | 响应头 `X-Expert-Work-Session-Id` | 响应体的 `data.thread_id` 字段 |
| 适合 | 需要实时展示生成过程 | 长任务、不需要盯着看、或者你的架构不方便维持长连接 |

`queue` 模式的响应：

```json
{
  "success": true,
  "data": {
    "run_id": "...",
    "thread_id": "...",
    "status": "queued"
  },
  "error": null
}
```

::: warning 破坏性变更：202 响应体已改成 `{success, data, error}` 形状
这个形状是当前的、也是唯一正确的形状。如果你的对接代码写于这次更新之前，请检查它有没有直接读顶层的 `run_id` / `thread_id` / `status`——旧版本的响应体是不带 `success` / `data` / `error` 包裹的扁平结构：

```json
{ "run_id": "...", "thread_id": "...", "status": "queued" }
```

现在这三个字段都在 `data` 里。按 `data.run_id` / `data.thread_id` / `data.status` 读取。
:::

`queue` 模式下想看执行过程或拿最终结果，有两条路：

**接事件流**：`GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>`。run 还在跑就实时接进去，跑完了就把存下来的事件按顺序回放一遍，结尾补一条带最终状态的 `end`。

- `user_id` 是必填查询参数，且必须是发起这次 run 的那个。对不上一律 404（`RUN_NOT_FOUND`），响应不会透露这个 run 是否存在。
- 需要 `read` 权限（`write` key 含读，可直接用）。
- run 没结束时这是一条长连接，会一直挂到 run 走到最终状态才返回，服务端不设上限。**客户端必须自己设读超时**，超时后重连，重连时带上 `since_seq=<已见过的最大 seq>`，服务端只发这个序号之后的事件。不带 `since_seq` 不会报错，但会从第 0 帧起把整个 run 重发一遍。
- 事件多到一页装不下时，这一页以 `truncated` 事件收尾而**不发** `end`，要带它给的 `next_seq` 再拉一次。细节见 [3 读懂 SSE 流](./sse-events)。

**只看状态**：调 [5.4 run 列表](./query#_5-4-run-列表) 看这次 run 的 `status`；或调 [5.2 会话列表](./query#_5-2-会话列表) 看每段会话的 `running` 字段，粗粒度判断"还有没有在跑的任务"。

## 2.5 多轮会话

不传 `session_id` 就是新开一段会话；把上次拿到的 session id 传回去就是同一段会话的下一轮。同一个 `user_id` 下可以并存多段互不相干的会话。

拿 session id 的位置**按模式不同**：

```mermaid
sequenceDiagram
    participant S as 你的服务端
    participant E as Expert-Work API
    S->>E: 第 1 轮：POST /runs（不带 session_id）
    E-->>S: 响应头 X-Expert-Work-Session-Id = sess-A<br/>（queue 模式则是 data.thread_id）
    Note over S: 存下 sess-A
    S->>E: 第 2 轮：POST /runs { session_id: "sess-A" }
    E-->>S: 同一段会话的下一轮
```

`queue` 模式响应体里那个字段叫 `thread_id` 而不是 `session_id`，两个名字指的是同一个值——下次请求时仍然填进请求体的 `session_id` 字段。

会话的重命名、归档、历史消息查询见 [5 查询与管理](./query)。

## 2.6 带图片和文档

附件分两步：先调上传接口拿到 `upload_id`，再把它放进发起对话请求的 `files[]` 里。

```mermaid
sequenceDiagram
    autonumber
    participant S as 你的服务端
    participant E as Expert-Work API
    S->>E: POST /uploads（multipart，带文件）
    E-->>S: { upload_id, type, mime, size }
    S->>E: POST /runs { files: [{ type, upload_id }] }
    E-->>S: SSE 流 / 202
```

`files[]` 每一项的字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | `"image"` \| `"document"` | 附件种类。 |
| `transfer_method` | `"local_file"` | 目前只有这一个取值，可以省略（默认就是它）。传其它值会 422。 |
| `upload_id` | string，1–1024 字符 | 上传接口返回的 `data.upload_id`，**原样回传**。 |

### 第一步：上传

```bash
# 文档
curl -X POST https://<your-domain>/v1/agents/{agent_code}/uploads \
  -H "Authorization: Bearer <key>" \
  -F "user_id=u-123" \
  -F "file=@report.pdf;type=application/pdf"
```

```json
{
  "success": true,
  "data": {
    "upload_id": "uploads/report.pdf",
    "session_id": "...",
    "type": "document",
    "mime": "application/pdf",
    "size": 235112
  },
  "error": null
}
```

```bash
# 图片
curl -X POST https://<your-domain>/v1/agents/{agent_code}/uploads \
  -H "Authorization: Bearer <key>" \
  -F "user_id=u-123" \
  -F "file=@photo.png;type=image/png"
```

```json
{
  "success": true,
  "data": {
    "upload_id": "expert_work://image/...",
    "session_id": "...",
    "type": "image",
    "mime": "image/png",
    "size": 88213
  },
  "error": null
}
```

### 第二步：带进对话

```bash
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "session_id": "<上一步返回的 session_id>",
    "input": "帮我看看这份文件和这张图",
    "mode": "queue",
    "files": [
      { "type": "document", "transfer_method": "local_file", "upload_id": "uploads/report.pdf" },
      { "type": "image", "transfer_method": "local_file", "upload_id": "expert_work://image/..." }
    ]
  }'
```

### 三个容易踩的地方

**一、`upload_id` 必须原样回传。** 文档类型的 `upload_id` 长得像 `uploads/report.pdf`，带着 `uploads/` 前缀——这是上传接口生成的完整引用，不是文件名。自己截成 `report.pdf` 回传会被拒绝：

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "INVALID_FILE_REF",
    "message": "document upload_id must be a workspace ref returned by POST /v1/agents/{agent_code}/uploads (uploads/<name>)"
  }
}
```

**二、图片和文档的 `upload_id` 格式完全不同。** 两者字段名都叫 `upload_id`，但文档是 `uploads/xxx` 形态，图片必须是 `expert_work://image/...` 形态。两个入口（顶层 `image_refs` 字段、`files[]` 里 `type: "image"` 的条目）用同一套格式校验。把文档形态的 `upload_id` 填进 `type: "image"` 会 422：

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "INVALID_IMAGE_REF",
    "message": "image ref must start with 'expert_work://image/': 'uploads/report.pdf'"
  }
}
```

**三、图片张数有两道独立的限制，撞上哪道要看总数。** 这里的总数是 `image_refs` 加上 `files[]` 里 `type: "image"` 条目的合计：

| 张数 | 响应 |
|---|---|
| 超过 64 张 | 422，`error.code` 为 `TOO_MANY_IMAGE_REFS` |
| 9~64 张（没超过 64，但超过平台配置的单次上限） | 422，响应体只有 `detail`：`{"detail": "too many images: max 8 per run"}`，**没有 `error.code`** |

实际能通过的张数是平台配置的单次上限，**默认 8 张**。64 只是"请求体里图片条目总数"这一步的合计上限，不代表单次 run 真能处理 64 张。两档错误的响应形状不同，解析时两种都要认，见 [8 错误码总表](./errors)。

`remote_url` 传输方式（直接给一个外部 URL、不经过上传接口）**目前不支持**，只有 `local_file` 一条路。

## 2.7 外部内容与模板变量

### `untrusted_content` —— 来自外部的文本

把外部来源的文本（一封邮件正文、一段工单描述）放进这个字段，而不是拼进 `input`。Agent 会把这部分当作**数据**而不是指令处理，避免外部内容里夹带的指令被当成用户意图执行。

条数和单块长度是两条互相独立的限制：

| 限制 | 超限时的 `error.code` |
|---|---|
| 最多 16 项（**正好 16 项合法**，第 17 项才拒） | `INVALID_REQUEST`（走请求体字段校验） |
| 单块最多 8192 字符（**正好 8192 字符合法**，第 8193 个字符才拒） | `UNTRUSTED_CONTENT_BLOCK_TOO_LONG` |

```json
{ "success": false, "data": null, "error": { "code": "UNTRUSTED_CONTENT_BLOCK_TOO_LONG", "message": "untrusted_content[0] 超过 8192 字符" } }
```

一段文本超过 8192 字符时，在客户端切成多块放进数组的多个元素（仍要留在 16 项以内），不要拼成一个超长字符串塞进单个元素。

### `inputs` —— 提示词模板变量

只对"系统提示词开启了 Jinja 模板、并声明了变量"的 Agent 有意义（在管理控制台配置）。

```bash
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "你好",
    "mode": "queue",
    "inputs": { "lang": "zh" }
  }'
```

三种情况会 422，且响应形状**不是** `{success, data, error}`，而是只有一个 `detail` 字段的简易格式（比如 `{"detail": "unknown input variable: <key>"}`）：

- Agent 没声明任何模板变量，却传了非空 `inputs`。
- `inputs` 里出现了 Agent 没声明过的键。
- Agent 声明的某个 `required` 变量，`inputs` 里没给。

另外还有三条容量限制，**这三条能读到 `error.code`**，互相独立：

| 限制 | 超限时的 `error.code` |
|---|---|
| 键最多 64 个（**正好 64 个合法**，第 65 个才拒） | `TOO_MANY_INPUT_KEYS` |
| 单个字符串值最多 8192 字符（**正好 8192 合法**，第 8193 个字符才拒）；只检查字符串值，数字 / 数组 / 对象类型的值不受此限 | `INPUT_VALUE_TOO_LONG` |
| 整体序列化后最多 65536 字节（**正好 65536 合法**，第 65537 字节才拒）；按 UTF-8 **字节数**算，不是字符数——一个中文字约占 3 字节，同样字符数的中文比英文更容易超；数字 / 数组 / 对象类型的值也计入 | `TOO_MANY_INPUT_BYTES` |

```json
{ "success": false, "data": null, "error": { "code": "TOO_MANY_INPUT_KEYS", "message": "inputs 最多 64 个键" } }
```

## 2.8 防重复下发 Idempotency-Key

同一次业务操作（比如"用户点了一次下单按钮"）在网络重试时，不应该在服务端跑成两次 run。带上这个请求头即可，`stream` / `queue` 两种模式都支持：

```
Idempotency-Key: order-8899
```

判定规则：**key + 请求体 + `agent_code` 三者的组合**。

| 情况 | 结果 |
|---|---|
| 同一个 key + 同一个请求体 + 同一个 `agent_code` | 不新建 run，直接返回原来那次的结果 |
| 同一个 key，但请求体变了 | 422，`IDEMPOTENCY_KEY_REUSED` |
| 同一个 key，请求体一样但打给了另一个 `agent_code` | 422，`IDEMPOTENCY_KEY_REUSED`。**key 要按 agent 维度保证唯一**，只在租户维度唯一不够 |
| key 本身不合法（去掉首尾空白后为空，或超过 255 字符） | 422，`INVALID_IDEMPOTENCY_KEY` |

```json
{ "success": false, "data": null, "error": { "code": "IDEMPOTENCY_KEY_REUSED", "message": "this Idempotency-Key was already used with a different request" } }
```

这份"key → run"的绑定关系**永久保留，没有过期时间**，不会因为时间久了就允许同一个 key 再绑定另一个请求体。

### 重试时拿到什么

**`queue` 模式**：和首次请求完全同形状的 202 响应体，`data.run_id` 与首次一致。

```bash
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-8899" \
  -d '{"user_id": "u-123", "input": "帮我下单", "mode": "queue"}'
```

**`stream` 模式**：`200`，一条 SSE 流，重新接上**原来那次 run** 的事件（不是报错，也不会静默忽略这个请求头）。`X-Expert-Work-Run-Id` 与首次一致，并且多一个首次请求没有的响应头：

| `X-Expert-Work-Stream-Mode` | 含义 |
|---|---|
| `live` | 原 run 还没跑完，实时接上 |
| `replay` | 原 run 已经跑完，把存下来的事件按顺序回放一遍再收尾 |

这一段的行为和断线重连用的 `GET .../runs/{run_id}/events` 是同一份实现，包括事件太多时以 `truncated` 收尾的分页行为。

::: warning 重放以 `truncated` 收尾时，不能靠重发 POST 翻页
`POST .../runs` 的请求体和查询参数里都没有 `since_seq`，原样重发只会永远拿回同一个第一页。

翻页要换成 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=…&since_seq=<next_seq>`，其中 `run_id` 从 `X-Expert-Work-Run-Id` 响应头取。见 [3 读懂 SSE 流](./sse-events)。
:::
