# 调用 Agent

本篇是 `POST /v1/agents/{agent_code}/runs` 的完整参数表——发起一次 Agent 对话/任务执行的核心接口。

## 端点

```
POST /v1/agents/{agent_code}/runs
Authorization: Bearer <key>   # 需要 write scope,见「认证」
Content-Type: application/json
```

`{agent_code}` 是你租户里已发布且状态为 ACTIVE 的 Agent 名字(在管理控制台创建/发布 Agent 时定的那个名字)。同一个名字只有一个 ACTIVE 版本会被解析——如果这个名字下没有 ACTIVE 版本,返回 404(`AGENT_NOT_FOUND`,见 [错误码与限流](./errors))。

## 请求体参数

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string,1–255 字符 | 是 | 你自己系统里这个终端用户的标识。**首次出现的 `user_id` 会被自动"铸造"成一个新的终端用户**——长期记忆、工作区文件、按用户维度的 token 计费都挂在这个铸造出来的用户身上,而不是挂在你的 API Key(服务账号)上。同一个 `user_id` 后续调用会复用同一个终端用户,不会重复铸造。取值建议见 [最佳实践](./best-practices)。 |
| `session_id` | UUID,可选 | 否 | 续接一段已有会话。省略则开一段新会话。传了但对应的会话不属于这个 `user_id` / 这个 `agent_code`,返回 404(`SESSION_NOT_FOUND`)。 |
| `input` | string,≤65536 字符,可选 | 否 | 这一轮用户说的话/任务描述。 |
| `mode` | `"stream"` \| `"queue"`,默认 `"stream"` | 否 | 见下方「`stream` vs `queue`」。 |
| `image_refs` | string[],最多 64 项,可选 | 否 | 多模态输入——图片上传接口返回的 `expert_work://image/...` 引用,不是原始字节。 |
| `untrusted_content` | string[],最多 16 项,可选 | 否 | 结构化的"不可信内容"(比如一封邮件正文、一段工单描述)。和 `input` 分开传,Agent 会把这部分当作**数据**而不是指令来处理——这是防止外部内容里挟带指令注入的推荐做法,优于把不可信文本直接拼进 `input` 里。 |
| `inputs` | object,可选 | 否 | 提示词模板变量,见下方「`inputs`」。 |
| `files` | 数组,最多 64 项,可选 | 否 | 统一的附件引用(图片 / 文档),见下方「`files[]`」。 |

## `inputs` —— 提示词模板变量

只对系统提示词开启了 Jinja 模板、并声明了变量的 Agent 有意义(在管理控制台配置)。三种情况都会 422:

- Agent 没声明任何模板变量,却传了非空 `inputs`。
- `inputs` 里出现了 Agent 没声明过的键。
- Agent 声明的某个 `required` 变量,`inputs` 里没给。

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

**注意**:这三种 `inputs` 校验失败**不是** `{success, data, error}` 信封,而是 FastAPI 默认的裸 `{"detail": "..."}` 字符串——比如未声明键会是 `{"detail": "unknown input variable: <key>"}`。别假设这条路径上也能读到 `error.code`,细节见 [错误码与限流](./errors)。

## `files[]` —— 统一附件引用(图片 / 文档)

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | `"image"` \| `"document"` | 附件种类。 |
| `transfer_method` | `"local_file"` | **目前只有这一个取值**,可以省略(默认就是它)。传其它值会 422。 |
| `upload_id` | string,1–1024 字符 | 上传接口 `POST /v1/agents/{agent_code}/uploads` 返回的 `data.upload_id`,原样回传。 |

先调上传接口拿到 `upload_id`:

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

拿到 `upload_id` 后原样放进 `files[]` 发起 run:

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

**`upload_id` 必须原样回传,不要自己截取或改写。** 文档类型的 `upload_id` 长得像 `uploads/report.pdf`——带着 `uploads/` 前缀,这不是巧合,是上传接口生成路径的固定形状。把它当成"文件名"自己截成 `report.pdf` 回传会被拒绝:

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

`type: "image"` 的条目会并入 `image_refs` 一起校验——两者合计仍然不能超过 64 张,超了是 422、`error.code` 为 `TOO_MANY_IMAGE_REFS`(统一信封,不是裸 `detail`)。`remote_url` 传输方式(直接给一个外部 URL,不经过上传接口)**目前还不支持**——只有 `local_file` 一条路。

## `Idempotency-Key` —— 避免重复下发

同一次业务操作(比如"用户点了一次下单按钮")网络重试时,别让它在服务端跑成两次 run。带上这个请求头,`stream` / `queue` 两种 `mode` 都支持:

```
Idempotency-Key: order-8899
```

- **同一个 key + 同一个请求体**(且打给同一个 `agent_code`)——不会新建 run,直接把原来那次的结果原样返回给你。
- **同一个 key + 不同的请求体**——422:

  ```json
  { "success": false, "data": null, "error": { "code": "IDEMPOTENCY_KEY_REUSED", "message": "this Idempotency-Key was already used with a different request" } }
  ```

- **同一个 key 打给不同的 `agent_code`,即便请求体完全一样**——同样 422 `IDEMPOTENCY_KEY_REUSED`,不会把 A agent 的 run 结果错发给 B agent 的调用方。**key 要按 agent 维度保证唯一**,不能只在租户维度唯一。
- **key 本身不合法**(去掉首尾空白后是空字符串,或者超过 255 字符)——422:

  ```json
  { "success": false, "data": null, "error": { "code": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key must be 1-255 non-blank characters" } }
  ```

- **这份"key → run"的记忆永久保留,没有过期时间**——不会因为时间久了就自动允许同一个 key 再次绑定不同的请求体。

`mode: "queue"` 的重试拿到的是和首次请求**完全同形状**的 202 信封(见下方),`data.run_id` 与首次一致:

```bash
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-8899" \
  -d '{"user_id": "u-123", "input": "帮我下单", "mode": "queue"}'
```

`mode: "stream"` 的重试是 `200`,一条 SSE 流,重新接上(不是报错、也不是静默丢弃这个 header)**原来那次 run** 的事件——`X-Expert-Work-Run-Id` 与首次请求一致,并且多一个首次请求没有的响应头 `X-Expert-Work-Stream-Mode: live`(原 run 还没跑完,实时接上)或 `replay`(原 run 已经跑完,把落库的帧按顺序回放一遍再收尾 `end`)。这一段的行为和断线重连用的 `GET .../runs/{run_id}/events` 是同一份实现,细节见 [SSE 事件格式](./sse-events)。

## 响应:`stream` vs `queue`

**`mode: "stream"`(默认)**——响应就是 SSE 流本身:`200`,`Content-Type: text/event-stream`,响应头带 `X-Expert-Work-Session-Id`(这次绑定/续接到的会话 id)和 `X-Expert-Work-Run-Id`。事件格式见 [SSE 事件格式](./sse-events)。

**`mode: "queue"`**——立即返回 `202`,不建立流,由后台某个 worker 实例异步执行:

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

::: warning 破坏性变更——202 响应体已改成统一信封
这个信封形状是当前的、也是唯一正确的形状。如果你的对接代码是在这次更新之前写的,请检查它有没有直接读顶层的 `run_id` / `thread_id` / `status`——旧版本的响应体是不带信封的扁平结构:

```json
{ "run_id": "...", "thread_id": "...", "status": "queued" }
```

现在这三个字段都挪进了 `data` 里,顶层多了 `success` / `error`。按 `data.run_id` / `data.thread_id` / `data.status` 读取,不要再假设它们在顶层。
:::

拿到 `run_id` / `thread_id` 后,用 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>` 拿完整的 SSE 事件(这条接口在 run 还在跑的时候会实时接进去,跑完了就把持久化下来的帧按顺序回放一遍,结尾补一条 `end`)。`user_id` 是必填查询参数,且必须是发起这次 run 的那个——对不上一律 404(`RUN_NOT_FOUND`),不会告诉你这个 run 到底存不存在。这条接口要 `read` scope,`write` key 含读所以也能直接调。

**注意**:run 还没结束时这条接口是长连接,会一直挂到 run 走到终态才返回,服务端不设上限——客户端必须自己设读超时,超时后直接重连。重连时 run 通常还没跑完,接的是实时分支,`since_seq` 在这条分支上不生效,重连会把最近缓冲的帧重推一遍,客户端要按帧 `id` 里的 `seq` 自行去重。细节见 [SSE 事件格式](./sse-events)。

想粗粒度知道"这段会话里还有没有 run 在跑",调 `GET /v1/agents/{agent_code}/sessions?user_id=<同一个 user_id>`,返回的每一项都带一个 `running` 布尔字段。

## 续接会话

把上一次响应里拿到的 `X-Expert-Work-Session-Id` 存下来,下次调用把它作为 `session_id` 传回去,就是同一段对话的下一轮;不传 `session_id` 就是另开一段全新会话——同一个 `user_id` 下可以并存很多段互不相干的会话。

## 会话列表与历史消息

两条都要求 `read` scope(`write` key 含读),查询参数都必填 `user_id`(1–255 字符),都支持 `limit`(1–200,默认 50)/ `offset`(≥0,默认 0)分页。

### `GET /v1/agents/{agent_code}/sessions` —— 会话列表

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/sessions?user_id=u-123&limit=20" \
  -H "Authorization: Bearer <key>"
```

```json
{
  "success": true,
  "data": {
    "sessions": [
      {
        "session_id": "...",
        "title": "...",
        "created_at": "2026-08-12T10:00:00+00:00",
        "updated_at": "2026-08-12T10:05:00+00:00",
        "running": false,
        "message_count": 6
      }
    ],
    "limit": 20,
    "offset": 0
  },
  "error": null
}
```

`message_count` 是这段会话里第三方可见的消息条数(与下面「历史消息」接口同一个口径——隐藏的编排脚手架消息不计入)。**`null` 和 `0` 含义不同**:

- `null` = 这段会话还没被算过(存量会话还没跑过任何一次 run,或者这次更新上线前创建、之后也一直没再跑过 run)。
- `0` = 已经算过,确实没有消息。

`message_count` 在每次 run 跑完终态时重新计算并写回,不是实时累加——不要把 `null` 当成 `0` 处理。`user_id` 是这个租户从没见过的值时,返回空列表而不是 404。

### `GET /v1/agents/{agent_code}/sessions/{session_id}/messages` —— 历史消息

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}/messages?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json
{
  "success": true,
  "data": {
    "messages": [
      {
        "role": "user",
        "content": "帮我看看这份文件",
        "channel": null,
        "created_at": "2026-08-12T10:00:00+00:00",
        "run_id": "..."
      },
      {
        "role": "assistant",
        "content": "已经看过了,摘要如下……",
        "channel": "final",
        "created_at": "2026-08-12T10:00:03+00:00",
        "run_id": "..."
      }
    ],
    "limit": 50,
    "offset": 0
  },
  "error": null
}
```

`role` 是 `"user"` 或 `"assistant"`;`channel` 只对 `assistant` 消息有意义(`"final"` = 这一步是这一轮对话里最终会展示的回答,`"commentary"` = 中间过程性输出),`user` 消息恒为 `null`。

`created_at` / `run_id` 是这次更新新加的字段。**这次更新之前产生的历史消息,这两个字段是 `null`**——写入时才会盖上时间戳和归属 run id,不做历史回填,不要假设它们一定有值。`session_id` 不属于这个 `user_id` / `agent_code`,返回 404(`SESSION_NOT_FOUND`),不会告诉你这个会话到底存不存在。
