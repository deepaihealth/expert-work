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

执行结束时 run 会落到一个最终状态：`success` / `error` / `interrupted` / `paused`(还有一个保留值 `timeout`，今天不会出现)。其中 `paused` 表示停在人工审批节点等待决策，需要你调审批接口推进，见 [4 对话过程中的控制](./run-control);八个状态值的完整含义见 [5.4 run 列表](./query#_5-4-run-列表)。

## 2.2 发起对话

``` [端点]
POST /v1/agents/{agent_code}/runs
Authorization: Bearer <key>   # 需要 write 权限，见「认证与 Key」
Content-Type: application/json
```

`{agent_code}` 是你租户里已发布、状态为 ACTIVE 的 Agent 名字。同一个名字只有一个 ACTIVE 版本会被使用；这个名字下没有 ACTIVE 版本时返回 404（`AGENT_NOT_FOUND`）。

不确定有哪些 `agent_code` 可用，先查 [5.1 Agent 目录](./query#_5-1-agent-目录)。

最小请求：

```bash [请求]
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
| `untrusted_content` | string[]，最多 16 项 | 否 | 来自外部、不可信任的文本内容。见 [2.7](#_2-7-外部内容与模板变量)。 |
| `inputs` | object | 否 | 提示词模板变量。见 [2.7](#_2-7-外部内容与模板变量)。 |
| `files` | 数组，最多 64 项 | 否 | 附件。每项 `{ "upload_id": "…" }`，值来自上传接口。见 [2.6](#_2-6-带图片和文档)。 |

### `user_id` 的作用

第一次出现的 `user_id` 会自动创建一个对应的终端用户。这个终端用户是长期记忆、工作区文件、按用户维度的用量计费的归属对象——它们挂在终端用户身上，不是挂在你的 API Key（服务账号）上。

同一个 `user_id` 后续调用复用同一个终端用户，不会重复创建。取值要求见 [9.2 `user_id` 怎么取](./best-practices#_9-2-user-id-怎么取)。

## 2.4 stream 还是 queue

| 对比维度 | `mode: "stream"`（默认） | `mode: "queue"` |
|---|---|---|
| 响应 | `200`，`Content-Type: text/event-stream`，响应体就是 SSE 流 | `202`，立即返回 JSON，由后台异步执行 |
| 响应头 | 带 `X-Expert-Work-Session-Id` 和 `X-Expert-Work-Run-Id` | **不带**这两个头（每个响应都有的 `X-Expert-Work-Trace-Id` 仍然有） |
| 怎么拿 session_id | 响应头 `X-Expert-Work-Session-Id` | 响应体的 `data.thread_id` 字段 |
| 适合 | 需要实时展示生成过程 | 耗时长的 run、不需要盯着看、或者你的架构不方便维持长连接 |

`queue` 模式的响应：

```json [响应 202]
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

`queue` 模式下想看执行过程或拿最终结果，有两条路。

### 接事件流

``` [端点]
GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id={user_id}
```

run 还在跑就实时接进去，跑完了就把存下来的事件按顺序回放一遍，结尾补一条带最终状态的 `end`。

- `{user_id}` 是必填查询参数，填发起这次 run 时用的那个值。对不上一律 404（`RUN_NOT_FOUND`），响应不会透露这个 run 是否存在。
- 需要 `read` 权限（`write` key 含读，可直接用）。
- run 没结束时这是一条长连接，会一直挂到 run 走到最终状态才返回，服务端不设上限。
- **客户端必须自己设读超时**，超时后重连。
- 重连时带上 `since_seq={max_seq}`，`{max_seq}` 填你已经见过的最大 `seq`，服务端只发这个序号之后的事件。
- 不带 `since_seq` 不会报错，但会从 `seq` 为 `0` 的那个事件起把整个 run 重发一遍。
- 事件多到一页装不下时，这一页以 `truncated` 事件收尾而**不发** `end`，要带它给的 `next_seq` 再拉一次。细节见 [3 读懂 SSE 流](./sse-events)。

### 只看状态

调 [5.4 run 列表](./query#_5-4-run-列表) 看这次 run 的 `status`；或调 [5.2 会话列表](./query#_5-2-会话列表) 看每段会话的 `running` 字段，粗粒度判断"还有没有在跑的 run"。

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

### 提前拿一个 session_id

想在发起第一次对话**之前**就拿到一个 `session_id`（最常见的用途：先把附件绑到这段会话上，再发起带这些附件的 run），调这个端点：

``` [端点]
POST /v1/agents/{agent_code}/sessions
Authorization: Bearer <key>   # 需要 write 权限
Content-Type: application/json
```

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string，1–255 字符 | 是 | 你自己系统里终端用户的标识，与发起对话用的是同一个值 |
| `session_id` | UUID | 否 | 传了就只校验这段会话确实属于这个 `user_id` / `agent_code`，不新建；不传才新建一段 |

```bash [请求]
curl -X POST https://<your-domain>/v1/agents/{agent_code}/sessions \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123"}'
```

```json [响应 201]
{
  "success": true,
  "data": {
    "session_id": "9f2c1a44-6d3b-4f18-9a70-2b5c8e1d0c37",
    "agent_code": "my-agent",
    "agent_version": "1.0.0",
    "user_id": "3b0c7f26-51ad-4a92-8f0e-1c7d9b6e4a35"
  }
}
```

这个端点的成功响应**不带 `error` 这个键**（见 [7.5 统一响应格式](./conventions#_7-5-统一响应格式) 的第一条例外）。响应字段：

| 字段 | 类型 | 含义 | 取值 |
|---|---|---|---|
| `session_id` | UUID | 这次新建（或续接）的会话 id | 标准 UUID。原样填进后续请求体的 `session_id` |
| `agent_code` | string | 路径里传的那个 `agent_code`，原样回显 | 与你请求路径里的值相同 |
| `agent_version` | string | 这段会话绑定到的 Agent 版本号 | 该 Agent 当前已发布版本的版本号字符串 |
| `user_id` | UUID | 平台为这个终端用户铸出的内部标识 | 标准 UUID，**不是**你传进来的那个 `user_id`——后续请求仍然传你自己的值 |

失败情况（都能读到 `error.code`）：

| 状态码 | `error.code` | 什么情况 |
|---|---|---|
| 403 | `AGENT_DISABLED` | 这个 Agent 已被管理员下线 |
| 404 | `AGENT_NOT_FOUND` | `{agent_code}` 在你的租户下没有已发布版本 |
| 404 | `SESSION_NOT_FOUND` | 传了 `session_id`，但它不属于这个 `user_id` / `agent_code` |
| 422 | `INVALID_REQUEST` | 请求体没通过基础校验：漏了 `user_id`、`user_id` 超过 255 字符、带了未声明的字段、`session_id` 不是合法 UUID |
| 422 | `INVALID_USER_ID` | `user_id` 去掉首尾空白后是空字符串 |

认证失败、权限不足这类与端点无关的失败见 [8 错误码总表](./errors)。

这个端点只建会话、不发起 run；真正跑一次 Agent 仍然要调 [2.2 发起对话](#_2-2-发起对话)。发起对话本身不传 `session_id` 时也会新建一段会话，所以**不需要**每轮都先调这个端点。

## 2.6 带图片和文档

附件三步：上传拿 `upload_id` → 放进 `files[]` 发起对话 →（需要回显时）用同一个 `upload_id` 下载。

```mermaid
sequenceDiagram
    autonumber
    participant S as 你的服务端
    participant E as Expert-Work API
    S->>E: POST /uploads（multipart，带文件）
    E-->>S: { upload_id, session_id, type, mime, size }
    S->>E: POST /runs { files: [{ upload_id }] }
    E-->>S: SSE 流 / 202
    S->>E: GET /uploads/{upload_id}?user_id=...（需要回显时）
    E-->>S: 裸字节
```

`files[]` 每一项的字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `upload_id` | string | 上传接口返回的 `data.upload_id`，**原样回传**。 |

### 第一步：上传

``` [端点]
POST /v1/agents/{agent_code}/uploads
Authorization: Bearer <key>   # 需要 write 权限
Content-Type: multipart/form-data
```

#### 能传哪些文件类型

服务端按你在 `multipart` 里声明的 `Content-Type` 判断走哪条通路，**不看文件名后缀**——后缀写对了但 `Content-Type` 不在下表里，一样被拒（400 `INVALID_UPLOAD`）：

| 通路 | 允许的 `Content-Type` | 单个文件大小上限 |
|---|---|---|
| 文档 | `application/pdf`、`application/vnd.openxmlformats-officedocument.wordprocessingml.document`（.docx）、`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`（.xlsx）、`application/vnd.openxmlformats-officedocument.presentationml.presentation`（.pptx）、`text/plain`、`text/markdown`、`text/csv` | 25 MiB |
| 图片 | `image/png`、`image/jpeg`、`image/webp`、`image/gif` | 10 MiB |

两条上限以你的部署实际配置为准，超限是 413 `UPLOAD_TOO_LARGE`，见 [8.9 413](./errors#_8-9-413-——-文档-图片超限)。

#### 上传文档

```bash [请求]
curl -X POST https://<your-domain>/v1/agents/{agent_code}/uploads \
  -H "Authorization: Bearer <key>" \
  -F "user_id=u-123" \
  -F "file=@report.pdf;type=application/pdf"
```

```json [响应 201]
{
  "success": true,
  "data": {
    "upload_id": "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",
    "session_id": "9f2c1a44-6d3b-4f18-9a70-2b5c8e1d0c37",
    "type": "document",
    "mime": "application/pdf",
    "size": 235112
  },
  "error": null
}
```

同名文档会覆盖同一份工作区文件：文档类附件按文件名落进**这个 `user_id` 的工作区**，同一个 `user_id` 再传一次同名文件时，上一次的内容就被覆盖掉——之后两个 `upload_id` 下载到的都是最后一次上传的内容。

- 覆盖只发生在同一个 `user_id` 的工作区内，不同 `user_id` 之间互不影响。
- 要把两份都留着，上传前先改文件名（比如加上时间戳）。
- 图片类附件不落工作区，不会互相覆盖。

#### 上传图片

```bash [请求]
curl -X POST https://<your-domain>/v1/agents/{agent_code}/uploads \
  -H "Authorization: Bearer <key>" \
  -F "user_id=u-123" \
  -F "file=@photo.png;type=image/png"
```

```json [响应 201]
{
  "success": true,
  "data": {
    "upload_id": "upl_9b7d2c40-1e5a-4f88-b3c6-7a0d4e2f9c11",
    "session_id": "9f2c1a44-6d3b-4f18-9a70-2b5c8e1d0c37",
    "type": "image",
    "mime": "image/png",
    "size": 88213
  },
  "error": null
}
```

`session_id` 不传时接口顺手给你新建一段会话并在响应里带回来；已经有一段在跑的会话时，把它传进 `session_id` 表单字段，附件就落在同一段会话下。

#### 上传响应的字段

两条通路的 201 响应是同一个形状：

| 字段 | 类型 | 含义 | 取值 |
|---|---|---|---|
| `upload_id` | string | 这份附件的标识 | 形如 `upl_<uuid>` 的不透明字符串。原样存下来，原样回传 |
| `session_id` | UUID | 这份附件绑定到的会话 | 你传进来的那个 `session_id`；没传时是接口这次新建的那一段 |
| `type` | string | 这份附件走的是哪条通路 | 恰好两个值：`document`（文档通路——内容落进这个终端用户的工作区，Agent 执行时能读到它）/ `image`（图片通路——内容落进对象存储，拍摄参数等 EXIF 信息已被剥离） |
| `mime` | string | 服务端记下来的 `Content-Type` | 就是上面「能传哪些文件类型」表里的那几个值 |
| `size` | int | 这份附件的字节数 | 非负整数，不超过对应通路的大小上限 |

### 第二步：带进对话

```bash [请求]
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "session_id": "9f2c1a44-6d3b-4f18-9a70-2b5c8e1d0c37",
    "input": "帮我看看这份文件和这张图",
    "mode": "queue",
    "files": [
      { "upload_id": "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17" },
      { "upload_id": "upl_9b7d2c40-1e5a-4f88-b3c6-7a0d4e2f9c11" }
    ]
  }'
```

`session_id` 用的是上一步上传响应里返回的那个值——原因见下方「容易踩的地方」第二条。

### 第三步：下载 / 回显

需要把附件内容拿回来（比如在你自己的界面上预览用户刚上传的图片）时，原样拿 `upload_id` 去调下载接口：

```bash [请求]
# 需要 read 权限（write key 含读）
curl -X GET "https://<your-domain>/v1/agents/{agent_code}/uploads/upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17?user_id=u-123" \
  -H "Authorization: Bearer <key>" \
  -o report.pdf
```

``` [响应头]
Content-Type: application/pdf
Content-Disposition: attachment; filename="report.pdf"; filename*=UTF-8''report.pdf
X-Content-Type-Options: nosniff
```

成功响应是**裸文件字节**，不套 `{success, data, error}` 信封。`Content-Type` 是上传时记录的 MIME。这条接口不计配额、不写审计。

`Content-Disposition` 只按**文件扩展名**决定，与响应里的 `type` 无关。附件只可能是下面这三行里的扩展名：

| 附件的扩展名 | `Content-Disposition` |
|---|---|
| `.png` / `.jpg` / `.jpeg` / `.webp` / `.gif` | `inline` |
| `.txt` / `.md` / `.csv` | `inline` |
| `.pdf` / `.docx` / `.xlsx` / `.pptx` | `attachment` |

HTML、SVG 这类可执行内容在平台里一律强制 `attachment`，不过上传接口根本不收这些类型，附件下载撞不到这条分支。

`Content-Disposition` 的 `filename` 两种附件取值方式不同：文档保留净化后的原始文件名（如上例的 `report.pdf`）；图片用的是服务端生成的图片 id 加扩展名（形如 `{image_id}.png`），不是上传时用的原始文件名——拿它当展示名会文不对题。

图片可以直接喂给 `<img src>` 显示——但这个地址需要带 `Authorization` 头才能访问，浏览器没法直接当图片 URL 用，通常做法是由你的服务端转发一层，再把字节交给前端。

### 三个容易踩的地方

**一、`upload_id` 原样回传，不要解析或改写。** 拿到的值是一个不透明字符串（形如 `upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17`），内部形状可能变化，只需要原样存下来、原样传给 `files[]` 或下载接口。改写或截断它会被拒绝：

```json [响应 422]
{
  "success": false,
  "data": null,
  "error": {
    "code": "INVALID_UPLOAD_ID",
    "message": "upload_id must be the value returned by POST /v1/agents/{agent_code}/uploads"
  }
}
```

**二、图片必须与发起对话的会话是同一段。** 上传接口返回的 `session_id` 要原样传回发起 run 请求体的 `session_id`；传去了另一段会话，这张图片就会因为「不属于这次会话」而 404：

```json [响应 404]
{
  "success": false,
  "data": null,
  "error": { "code": "UPLOAD_NOT_FOUND", "message": "upload not found" }
}
```

**三、未知的、别人的、已删除的 `upload_id`，一律是同一个 404，不区分原因。** 这是刻意的存在性隐藏，和 `SESSION_NOT_FOUND` / `RUN_NOT_FOUND` 同一模式——完整规则见 [8 错误码总表](./errors)。

`files` 最多 64 项只是请求体字段层面的合计上限（图片和文档一起算）；图片还有一道更严的单次 run 处理上限（默认 8 张），撞上是没有 `error.code` 的裸 `detail` 422，完整规则见 [8.10 422 —— 请求参数不合法](./errors#_8-10-422-——-请求参数不合法)。

## 2.7 外部内容与模板变量

### `untrusted_content` —— 来自外部的文本

把外部来源的文本（一封邮件正文、一段工单描述）放进这个字段，而不是拼进 `input`。Agent 会把这部分当作**数据**而不是指令处理，避免外部内容里夹带的指令被当成用户意图执行。

条数和单块长度是两条互相独立的限制：

| 限制项 | 上限值 | 边界 | 超限时的 `error.code` |
|---|---|---|---|
| 数组条数 | 16 项 | 正好 16 项合法，第 17 项才拒 | `INVALID_REQUEST`（走请求体字段校验） |
| 单块字符数 | 8192 字符 | 正好 8192 字符合法，第 8193 个字符才拒 | `UNTRUSTED_CONTENT_BLOCK_TOO_LONG` |

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "UNTRUSTED_CONTENT_BLOCK_TOO_LONG", "message": "untrusted_content[0] 超过 8192 字符" } }
```

一段文本超过 8192 字符时，在客户端切成多块放进数组的多个元素（仍要留在 16 项以内），不要拼成一个超长字符串塞进单个元素。

### `inputs` —— 提示词模板变量

只对"系统提示词开启了 Jinja 模板、并声明了变量"的 Agent 有意义（在管理控制台配置）。

```bash [请求]
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

| 限制项 | 上限值 | 边界 | 超限时的 `error.code` |
|---|---|---|---|
| 键数量 | 64 个 | 正好 64 个合法，第 65 个才拒 | `TOO_MANY_INPUT_KEYS` |
| 单个字符串值的字符数 | 8192 字符 | 正好 8192 字符合法，第 8193 个字符才拒 | `INPUT_VALUE_TOO_LONG` |
| 整体序列化后的字节数 | 65536 字节 | 正好 65536 字节合法，第 65537 字节才拒 | `TOO_MANY_INPUT_BYTES` |

两条容易算错的地方：

- 单值字符数只检查字符串值，数字 / 数组 / 对象类型的值不受这一条限制。
- 整体字节数按 UTF-8 **字节数**算而不是字符数（一个中文字约占 3 字节，同样字符数的中文比英文更容易超），并且数字 / 数组 / 对象类型的值也计入。

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "TOO_MANY_INPUT_KEYS", "message": "inputs 最多 64 个键" } }
```

## 2.8 防重复下发 Idempotency-Key

同一次业务操作（比如"用户点了一次下单按钮"）在网络重试时，不应该在服务端跑成两次 run。带上这个请求头即可，`stream` / `queue` 两种模式都支持：

``` [请求头]
Idempotency-Key: order-8899
```

判定规则：**key + 请求体 + `agent_code` 三者的组合**。

| 情况 | 结果 |
|---|---|
| 同一个 key + 同一个请求体 + 同一个 `agent_code` | 不新建 run，直接返回原来那次的结果 |
| 同一个 key，但请求体变了 | 422，`IDEMPOTENCY_KEY_REUSED` |
| 同一个 key，请求体一样但打给了另一个 `agent_code` | 422，`IDEMPOTENCY_KEY_REUSED`。**key 要按 agent 维度保证唯一**，只在租户维度唯一不够 |
| key 本身不合法（去掉首尾空白后为空，或超过 255 字符） | 422，`INVALID_IDEMPOTENCY_KEY` |

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "IDEMPOTENCY_KEY_REUSED", "message": "this Idempotency-Key was already used with a different request" } }
```

这份"key → run"的绑定关系**永久保留，没有过期时间**，不会因为时间久了就允许同一个 key 再绑定另一个请求体。

### 重试时拿到什么

**`queue` 模式**：和首次请求完全同形状的 202 响应体，`data.run_id` 与首次一致。

```bash [请求]
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-8899" \
  -d '{"user_id": "u-123", "input": "帮我下单", "mode": "queue"}'
```

**`stream` 模式**：`200`，一条 SSE 流，重新接上**原来那次 run** 的事件（不是报错，也不会静默忽略这个请求头）。`X-Expert-Work-Run-Id` 与首次一致，并且多一个首次请求没有的响应头 `X-Expert-Work-Stream-Mode`，它恰好两个取值：

| 取值 | 含义 |
|---|---|
| `live` | 原 run 还没跑完，实时接上 |
| `replay` | 原 run 已经跑完，把存下来的事件按顺序回放一遍再收尾 |

这一段的行为和断线重连用的 `GET .../runs/{run_id}/events` 是同一份实现，包括事件太多时以 `truncated` 收尾的分页行为。

::: warning 重放以 `truncated` 收尾时，不能靠重发 POST 翻页
`POST .../runs` 的请求体和查询参数里都没有 `since_seq`，原样重发只会永远拿回同一个第一页。

翻页要换成 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=…&since_seq=<next_seq>`，其中 `run_id` 从 `X-Expert-Work-Run-Id` 响应头取。见 [3 读懂 SSE 流](./sse-events)。
:::
