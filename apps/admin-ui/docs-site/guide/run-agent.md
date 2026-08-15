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
| `untrusted_content` | string[],最多 16 项,可选 | 否 | 结构化的"不可信内容"(比如一封邮件正文、一段工单描述)。和 `input` 分开传,Agent 会把这部分当作**数据**而不是指令来处理——这是防止外部内容里挟带指令注入的推荐做法,优于把不可信文本直接拼进 `input` 里。单块长度上限见下方「`untrusted_content`」。 |
| `inputs` | object,可选 | 否 | 提示词模板变量,见下方「`inputs`」。 |
| `files` | 数组,最多 64 项,可选 | 否 | 统一的附件引用(图片 / 文档),见下方「`files[]`」。 |

## `untrusted_content` —— 结构化的不可信内容

推荐把外部来源的文本(一封邮件正文、一段工单描述)放进这个字段而不是拼进 `input`——细节见上方参数表这一行的说明。条数(最多 16 项)和单块长度是两条互相独立的硬上限,不是同一条限制的两种说法:

| 上限 | 超限时的 `error.code` |
|---|---|
| 最多 16 项(**正好 16 项合法**,第 17 项才拒) | 走请求体字段校验,`error.code` 为 `INVALID_REQUEST` |
| 单块最多 8192 字符(**正好 8192 字符合法**,第 8193 个字符才拒) | `UNTRUSTED_CONTENT_BLOCK_TOO_LONG` |

```json
{ "success": false, "data": null, "error": { "code": "UNTRUSTED_CONTENT_BLOCK_TOO_LONG", "message": "untrusted_content[0] 超过 8192 字符" } }
```

一封长邮件 / 一段长工单描述超过单块 8192 字符时,自己在客户端按块切开、放进数组的多个元素里(仍然要留在 16 项以内),不要拼成一个超长字符串塞进单个元素。

## `inputs` —— 提示词模板变量

只对系统提示词开启了 Jinja 模板、并声明了变量的 Agent 有意义(在管理控制台配置)。除了下面这三条上限,还有三种情况会 422:

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

### 三条硬上限

不管 Agent 声明了多少模板变量,`inputs` 本身还有三条硬上限,**这三条走统一信封**(与上面三种"裸 `detail`"形状不同),三条互相独立、不是互相替代:

| 上限 | 超限时的 `error.code` |
|---|---|
| 键的数量最多 64 个(**正好 64 个合法**,第 65 个才拒) | `TOO_MANY_INPUT_KEYS` |
| 单个字符串值最多 8192 字符(**正好 8192 字符合法**,第 8193 个字符才拒);只检查字符串值,数字/数组/对象类型的值不受此限 | `INPUT_VALUE_TOO_LONG` |
| `inputs` 整体序列化后的总字节数最多 65536 字节(**正好 65536 字节合法**,第 65537 字节才拒);按 **UTF-8 编码后的字节数**计算,不是字符数——一个中文字约占 3 字节,同样字符数的中文 `inputs` 比英文更容易撞上这条上限;数字/数组/对象类型的值也计入这条总量(不像上一条只查字符串值) | `TOO_MANY_INPUT_BYTES` |

```json
{ "success": false, "data": null, "error": { "code": "TOO_MANY_INPUT_KEYS", "message": "inputs 最多 64 个键" } }
```

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

`type: "image"` 的条目会并入 `image_refs` 一起校验,但实际是**两道独立的闸**,撞上哪一道要看总张数:

| 张数(`image_refs` + `files[]` 里 image 条目合计) | 响应 |
|---|---|
| 超过 64 张 | 422,能读到 `error.code`,值为 `TOO_MANY_IMAGE_REFS` |
| 9~64 张(没撞上面那道,但超过平台配置的单次上限) | 422,只有 `detail`:`{"detail": "too many images: max 8 per run"}`,**没有 `error.code`** |

实际能过的张数是平台配置的单次上限,**默认 8 张**——64 只是"请求体里图片条目总数"这一步的合计上限,不代表单次 run 真能处理到 64 张。两档错误响应形状不一样,解析出错逻辑时两种都要认。细节见 [错误码与限流](./errors)。

**最容易踩的另一个坑**:document 和 image 两种 `files[]` 条目字段名都叫 `upload_id`,但格式完全不同——document 的 `upload_id` 长得像 `uploads/report.pdf`,image 的 `upload_id`(不管是走 `image_refs` 还是 `files[]` 的 `type: "image"`)必须是上传接口对图片返回的那种 `expert_work://image/...` 引用(见上面「图片」示例响应)。两个入口(顶层 `image_refs` 字段、`files[]` 里 `type: "image"` 的条目)校验的是同一道格式闸。把 document 形态的 `upload_id` 填进了 `type: "image"` 的条目,会 422、`error.code` 为 `INVALID_IMAGE_REF`:

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

`remote_url` 传输方式(直接给一个外部 URL,不经过上传接口)**目前还不支持**——只有 `local_file` 一条路。

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

`mode: "stream"` 的重试是 `200`,一条 SSE 流,重新接上(不是报错、也不是静默丢弃这个 header)**原来那次 run** 的事件——`X-Expert-Work-Run-Id` 与首次请求一致,并且多一个首次请求没有的响应头 `X-Expert-Work-Stream-Mode: live`(原 run 还没跑完,实时接上)或 `replay`(原 run 已经跑完,把落库的帧按顺序回放一遍再收尾)。这一段的行为和断线重连用的 `GET .../runs/{run_id}/events` 是同一份实现——包括回放分页(帧太多时以 `truncated` 帧收尾而不是 `end`),细节见 [SSE 事件格式](./sse-events)。

**这里有一个必须知道的岔口**:重试如果以 `truncated` 收尾,**不能靠再发一次这个 POST 来翻页** —— `POST .../runs` 的请求体和查询参数里都没有 `since_seq`,原样重发只会永远拿回同一个第一页。翻页要换成 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=…&since_seq=<next_seq>`,`run_id` 从 `X-Expert-Work-Run-Id` 响应头里取。

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

拿到 `run_id` / `thread_id` 后,用 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=<同一个 user_id>` 拿完整的 SSE 事件(这条接口在 run 还在跑的时候会实时接进去,跑完了就把持久化下来的帧按顺序回放一遍,结尾补一条带终局状态的 `end`;帧多到一页装不下时,这一页改以 `truncated` 帧收尾、**不发 `end`**,要带它给的 `next_seq` 再拉一次——见 [SSE 事件格式](./sse-events))。`user_id` 是必填查询参数,且必须是发起这次 run 的那个——对不上一律 404(`RUN_NOT_FOUND`),不会告诉你这个 run 到底存不存在。这条接口要 `read` scope,`write` key 含读所以也能直接调。

**注意**:run 还没结束时这条接口是长连接,会一直挂到 run 走到终态才返回,服务端不设上限——客户端必须自己设读超时,超时后直接重连。重连时带上查询参数 `since_seq=<你已经见过的最大 seq>`(seq 从帧的 `id:` 里取,格式是 `"{毫秒时间戳}-{seq}"`):run 还在跑、run 已经结束这两种情况下它**都生效**,服务端只发这个序号之后的帧。**不带 `since_seq` 不会报错,但会从第 0 帧起把整个 run 重发一遍**,不是只补最近一段。细节见 [SSE 事件格式](./sse-events)。

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

`message_count` 在每次 run 跑完终态时重新计算并写回,不是实时累加——不要把 `null` 当成 `0` 处理。`user_id` 是这个租户从没见过的值时,返回空列表而不是 404。这条列表默认不包含已归档(`archived`)的会话——见下方「会话管理:重命名 / 归档」一节。

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

## 工作区文件

Agent 执行任务时会往终端用户的持久工作区里写产出物(报表、导出文件等),这两个接口用来列出 / 下载这些文件。两条都要求 `read` scope(`write` key 含读)。

::: warning `agent_code` 对这两个接口完全不生效
工作区是按 **(租户, 终端用户)** 维度存的,不按 agent 分——URL 里的 `{agent_code}` 只是为了和这组接口里其它路径(`/v1/agents/{agent_code}/sessions` 等)保持同款形状,实际**不参与过滤或权限判定**。同一个 `user_id` 配任意 `agent_code`(甚至一个压根没建过的 `agent_code`)拿到的都是**同一份**文件列表,同样 200。

这一点和这组接口里的其它端点行为不一致——会话列表 / 历史消息 / run 事件 / 审批操作全都把 `agent_code` 当成真实的过滤或归属校验维度(比如一个会话不属于这个 `agent_code` 会 404);工作区端点是这组里唯一的例外,同样的 URL 形状不代表同样的语义。

**风险场景**:如果你给不同业务线注册了不同的 `agent_code`(比如"财务规划"和"公开问答"复用同一批终端用户 `user_id`),这两个接口会让一条业务线看到另一条业务线在同一个 `user_id` 下产生的文件。**如果需要按 agent 隔离文件,当前 API 不提供**——同一个 `user_id` 下所有 agent 共享同一份工作区。
:::

### `GET /v1/agents/{agent_code}/workspace/files` —— 列出文件

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/workspace/files?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json
{
  "success": true,
  "data": {
    "files": [
      { "path": "report.pdf", "size": 235112 },
      { "path": "charts/q3.png", "size": 88213 }
    ]
  },
  "error": null
}
```

`user_id` 是这个租户从没见过的值时,返回空文件列表而不是 404(这是读操作,不会为一个陌生 `user_id` 铸造终端用户)。`path` 可能带子目录,如上面第二项。

### `GET /v1/agents/{agent_code}/workspace/file` —— 下载单个文件

| 查询参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 同上面「列出文件」。 |
| `path` | 是 | 要下载的文件相对路径。**直接原样回传上面「列出文件」返回的 `path` 字段值,不要自己拼**——见下方「`path` 的合法形态」。 |

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/workspace/file?user_id=u-123&path=report.pdf" \
  -H "Authorization: Bearer <key>" \
  -o report.pdf
```

成功响应是文件字节流本身(**不是** `{success, data, error}` 信封——信封只包裹错误响应)。`Content-Type` 按 `path` 的扩展名推断:图片(`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.bmp` / `.ico`)和结构化文本 / 代码类扩展名(`.json` / `.yaml` / `.toml` 等,以及 `.txt` / `.py` / `.md` 这类纯文本 / 代码)带 `Content-Disposition: inline`,浏览器可以直接预览。`.html` / `.htm` / `.xhtml` / `.xht` / `.svg` / `.svgz` / `.xml` / `.xsl` / `.xslt` / `.mathml` 这类"可执行 / 可交互内容"扩展名,以及任何未识别的扩展名(含无扩展名文件),一律强制 `Content-Disposition: attachment`——前一类是刻意的 XSS 防护(避免浏览器把这些当成同源 HTML/SVG 内联渲染,执行里面夹带的脚本),后一类是"宁可多一次没必要的下载,也不要猜错类型"。响应始终带 `X-Content-Type-Options: nosniff`。

#### `path` 的合法形态

以下几种 `path` 形态一律拒绝,返回 400:

- 绝对路径(以 `/` 开头)
- 含 `..` 段(试图跳出工作区)
- 含 NUL 字节(`\x00`)
- 空字符串,或者去掉首尾空白后是空字符串

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

避免这整类错误最简单的办法:`path` 直接用上面「列出文件」接口返回的 `path` 字段值原样回传,不要自己用字符串拼接构造路径。

#### 错误情况

`user_id` 未识别(不认识这个终端用户)、`path` 指向的文件不存在,这两种情况**返回同一个不透明的 404**(`WORKSPACE_FILE_FAILED`)——不要试图从响应里区分是哪一种,这是刻意的存在性隐藏,不是 bug:

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "file not found" } }
```

服务端工作区存储配置有问题(比如权限没配对)时返回 500,同样是 `WORKSPACE_FILE_FAILED`——这种情况不是你这边能解决的,重试没用,联系你的租户管理员:

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "workspace file unavailable" } }
```

细节见 [错误码与限流](./errors)。

## 会话管理:重命名 / 归档

### `PATCH /v1/agents/{agent_code}/sessions/{session_id}` —— 重命名

要求 `write` scope。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string,1–255 字符 | 是 | 会话所属的终端用户——必须与这个 `session_id` 实际归属的用户一致。 |
| `title` | string,1–200 字符 | 是 | 新标题,覆盖当前标题(不管是自动生成的还是上次手动设置的)。 |

```bash
curl -X PATCH https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id} \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "title": "退货咨询"}'
```

```json
{ "success": true, "data": { "session_id": "...", "title": "退货咨询" }, "error": null }
```

`title` 去掉首尾空白后是空字符串(比如整串都是空格),422 `INVALID_TITLE`:

```json
{ "success": false, "data": null, "error": { "code": "INVALID_TITLE", "message": "title must not be empty" } }
```

`session_id` 不存在、或者不属于这个 `user_id` / `agent_code` 组合,404 `SESSION_NOT_FOUND`——同一个不透明 404,不会告诉你是"不存在"还是"存在但不是你的"。

### `DELETE /v1/agents/{agent_code}/sessions/{session_id}` —— 归档(软删除)

要求 `write` scope,**不是** `delete`——external API 没有单独的 delete 档位对第三方开放,给外部对接方发一把能归档会话的 key 只需要 `write`,不需要更高权限的 `admin`。

```bash
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json
{ "success": true, "data": { "session_id": "...", "status": "archived" }, "error": null }
```

::: warning 这是软删除,不是彻底删除
归档只是把会话状态改成 `archived`。**checkpoint / 历史消息 / run 记录 / 工作区文件全部原样保留**,能照常查询(比如 `GET .../messages` 仍然能读到已归档会话的内容,归档不影响这条接口)。彻底物理删除(`purge`)只在管理控制台内部提供,不对外开放。

归档后唯一可见的行为变化:这个会话会从 `GET /v1/agents/{agent_code}/sessions` 的**默认**列表里消失(该接口默认不返回 `archived` 状态的会话)。想恢复可见性(比如允许用户"取消归档"),当前 API 没有对外的反向操作。
:::

`session_id` 不存在、或者不属于这个 `user_id` / `agent_code` 组合,同样是不透明的 404 `SESSION_NOT_FOUND`。

细节见 [错误码与限流](./errors)。
