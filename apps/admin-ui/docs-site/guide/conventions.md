# 7 通用约定

本章是全部对外接口共同遵守的约定：环境地址、协议、三类文件的区别、公共请求头与响应头、统一响应格式、限流与配额、幂等性。

具体端点的参数表见 [2 跟 Agent 对话](./chat) 和 [5 查询与管理](./query)。

## 7.1 环境地址

测试环境：

``` [测试环境地址]
https://expert-work-test.deepaihealth.com
```

全部对外端点挂在 `/v1` 前缀下，例如：

``` [端点示例]
POST https://expert-work-test.deepaihealth.com/v1/agents/{agent_code}/runs
```

生产环境目前未开放，开放时间另行通知。

## 7.2 协议约定

- 传输协议为 HTTPS，不提供明文 HTTP。
- 字符编码为 UTF-8。
- 请求体与响应体为 JSON，有四处例外：SSE 端点的响应是 `text/event-stream`（见 [3 读懂 SSE 流](./sse-events)），工作区文件下载（见 [5.6 工作区文件](./query#_5-6-工作区文件)）、产物下载（见 [5.7 产物](./query#_5-7-产物)）、附件下载（见 [2.6 带图片和文档](./chat#_2-6-带图片和文档)）的响应是文件字节流。
- 时间戳为 ISO-8601 格式并带时区偏移，例如 `2026-08-12T10:00:00+00:00`。
- `session_id` 与 `run_id` 是标准 UUID（带连字符的小写十六进制，例如 `550e8400-e29b-41d4-a716-446655440000`）。`user_id` 不是 UUID，它是调用方自己业务系统里的标识字符串，长度 1–255 字符。

### 三类文件的区别

附件、工作区文件、产物是三种不同的东西，产生方、接口与标识都不一样。响应里出现文件类字段时，先按这张表确认它属于哪一种：

| 名称 | 谁产生的 | 在哪个接口获取 | 用什么标识 |
|---|---|---|---|
| 附件 | 调用方上传的图片或文档 | 上传 `POST /v1/agents/{agent_code}/uploads`、下载 `GET /v1/agents/{agent_code}/uploads/{upload_id}`，见 [2.6 带图片和文档](./chat#_2-6-带图片和文档) | `upload_id` |
| 工作区文件 | Agent 执行过程中写出的文件，构成这个终端用户工作区的全部内容 | 列出 `GET /v1/agents/{agent_code}/workspace/files`、下载 `GET /v1/agents/{agent_code}/workspace/file`，见 [5.6 工作区文件](./query#_5-6-工作区文件) | 文件在工作区里的 `path` |
| 产物 | Agent 登记为成果的那几份文件，例如一份周报 | 列出 `GET /v1/agents/{agent_code}/artifacts`、下载 `GET /v1/agents/{agent_code}/artifacts/download`，见 [5.7 产物](./query#_5-7-产物) | 产物 `name` |

删除方式：只有产物有对外的删除接口（`DELETE /v1/agents/{agent_code}/artifacts`），删除后不再出现在产物列表里，也不能再通过产物下载接口下载；文件本身仍留在工作区中，仍可用工作区文件下载接口取回。附件与工作区文件没有对外的删除接口。

三者之间还有两条关系：

- 文档类附件会保存进这个终端用户的工作区，因此也会出现在工作区文件列表里；图片类附件不会进入工作区，由服务端单独存储。
- 产物是工作区文件中被挑选出来的一部分，登记为产物之后，同一份内容仍然留在工作区里。

## 7.3 公共请求头

| 请求头 | 用在哪 | 说明 |
|---|---|---|
| `Authorization: Bearer <key>` | 全部端点 | 见 [6 认证与 Key](./auth) |
| `Content-Type: application/json` | 除上传附件外的全部写请求 | 上传附件（`POST /v1/agents/{agent_code}/uploads`）用 `multipart/form-data` |
| `Idempotency-Key` | 仅 `POST /v1/agents/{agent_code}/runs` | 见 [7.7 幂等性](#_7-7-幂等性) |
| `X-Expert-Work-Deadline-Ms` | 任意端点，可选 | 调用方为这次请求设定的绝对截止时间，取值是 unix 毫秒时间戳。时间已过则直接返回 504 `DEADLINE_EXCEEDED`。不需要端到端超时控制时不要传 |

## 7.4 响应头

先说明三个词：

- 「事件接口」指按 `run_id` 拉取事件的接口（`GET /v1/agents/{agent_code}/runs/{run_id}/events`）。
- 「续传」指断线重连或 run 结束之后，服务端把客户端未收到的那一段事件重新发送，操作步骤见 [3.6 断线重连](./sse-events#_3-6-断线重连与回放分页)。
- 「幂等重放」指用同一个 `Idempotency-Key` 重复发起对话时，服务端直接返回上一次的结果，规则见 [7.7 幂等性](#_7-7-幂等性)。

`X-Expert-Work-Trace-Id` 是唯一一个每个响应都带的头，**其余四个按端点和模式出现，不要假设它们成套出现**：

| 响应头 | 出现在哪 | 含义 |
|---|---|---|
| `X-Expert-Work-Trace-Id` | 每一个响应，成功和失败都有 | 这次请求在服务端的关联 id。自行排查不出原因、需要向租户管理员报障时，请一并提供这个值 |
| `X-Expert-Work-Run-Id` | 发起对话的 `stream` 模式（含幂等重放）、事件接口、审批决策的两种模式 | 这次响应对应的 `run_id`。发起对话的 `queue` 模式不带这个头，`run_id` 只在响应体 `data.run_id` 里；审批决策返回的是继续执行时新产生的 run id |
| `X-Expert-Work-Session-Id` | 发起对话的 `stream` 模式（含幂等重放）、事件接口 | 这次绑定或续接到的 `session_id`。审批决策的两种响应都不带，因为继续执行用的就是原会话，没有变化 |
| `X-Expert-Work-Stream-Mode` | 事件接口；以及发起对话在 `stream` 模式下命中幂等重放时 | 取值 `live`（run 还在执行，接的是实时流）或 `replay`（run 已结束，按服务端记录的顺序重新发送事件）。首次发起的 `stream` 响应不带，审批决策的两种响应也不带 |
| `X-Expert-Work-Next-Seq` | 只在续传被分页截断时 | 下一页应当传回的 `since_seq`。同一个值也在流末尾的 `truncated` 事件里，以事件为准更可靠，因为中间代理可能剥掉它不认识的响应头。见 [3.6 断线重连](./sse-events#_3-6-断线重连与回放分页) |

## 7.5 统一响应格式

成功：

```json [响应 · 成功]
{ "success": true, "data": { /* 具体端点的数据 */ }, "error": null }
```

失败：

```json [响应 · 失败]
{ "success": false, "data": null, "error": { "code": "SOME_CODE", "message": "..." } }
```

两条例外：

- `error` 这个键不保证出现在每一个成功响应里，解析时按键是否存在来读，不要假设它一定在。
- 不是所有错误都能读到 `error.code`。一部分错误（例如权限不足的 403、`inputs` 模板变量校验失败的 422）只有一个 `detail` 字段，`detail` 有时是字符串，有时是 `{"code": ..., "message": ...}` 对象。完整对照见 [8 错误码总表](./errors)。

## 7.6 限流与配额

两者都返回 429，但含义和处理方式不同：

| 对比维度 | 限流 | 配额 |
|---|---|---|
| 限制什么 | 调用频率，按时间窗口计算 | 资源用量，按资源维度计算，例如工作区存储 |
| `error.code` | `RATE_LIMIT_EXCEEDED` | `QUOTA_EXCEEDED` |
| `Retry-After` 响应头 | 带 | 不带 |
| 处理方式 | 退避后重试 | 退避重试无效，先清理占用的资源 |

拿到 429 时先读 `error.code` 判断属于哪一种。`dimension` 字段的含义、两种响应的样例，以及产物下载这个例外（它的配额也返回 `RATE_LIMIT_EXCEEDED`、也带 `Retry-After`，但短退避重试对它无效），都在 [8.11 429](./errors#_8-11-429-请求过于频繁或配额用尽)，那一节是这件事的完整说明。

## 7.7 幂等性

`Idempotency-Key` 请求头只用在 `POST /v1/agents/{agent_code}/runs` 上，作用域是 key、请求体、`agent_code` 三者的组合。完整规则与重放行为见 [2.8 防重复下发](./chat#_2-8-防重复下发-idempotency-key)。

审批决策端点有自己独立的一套幂等：字段名是 `idempotency_key`，位置在请求体而不是请求头，作用域按这次决策计算，不按 run 的创建计算。见 [4.2 审批决策](./run-control#_4-2-审批决策)。
