# 7 通用约定

贯穿全部对外接口的共性约定：环境地址、协议、附件 / 工作区文件 / 产物三个词的区别、公共请求头与响应头、统一响应格式、限流与配额的区别、幂等性。

具体端点的参数表见 [2 跟 Agent 对话](./chat) 和 [5 查询与管理](./query)。

## 7.1 环境地址

测试环境：

``` [测试环境地址]
https://expert-work-test.deepaihealth.com
```

全部对外端点挂在 `/v1` 前缀下，比如：

``` [端点示例]
POST https://expert-work-test.deepaihealth.com/v1/agents/{agent_code}/runs
```

生产环境：本对外 API 目前**未开放**，上线时间另行通知。

## 7.2 协议约定

- **HTTPS**，不提供明文 HTTP。
- **字符编码 UTF-8**。
- **请求/响应体为 JSON**，四处例外：SSE 端点（`Content-Type: text/event-stream`，见 [3 读懂 SSE 流](./sse-events)）、工作区文件下载（响应体是文件字节流，见 [5.6 工作区文件](./query#_5-6-工作区文件)）、产物下载（响应体是文件字节流，见 [5.7 产物](./query#_5-7-产物)）、附件下载（响应体是文件字节流，见 [2.6 带图片和文档](./chat#_2-6-带图片和文档)）。
- **时间戳**：ISO-8601 带时区偏移，比如 `2026-08-12T10:00:00+00:00`。
- **id 形态**：`session_id`、`run_id` 是标准 UUID（带连字符的小写十六进制，如 `550e8400-e29b-41d4-a716-446655440000`）；`user_id` **不是** UUID，是你自己业务系统里的标识字符串（1–255 字符）。

### 附件、工作区文件、产物 —— 三个词的区别

这三个词在本站指三种不同的东西，产生方、接口、标识、删除方式都不一样。拿到一个文件类的字段时，先按这张表确认它是哪一种：

| 名称 | 谁产生的 | 在哪个接口拿 | 用什么标识 | 怎么删 |
|---|---|---|---|---|
| 附件 | 你上传的（终端用户发来的图片或文档） | 上传 `POST /v1/agents/{agent_code}/uploads`、下载 `GET /v1/agents/{agent_code}/uploads/{upload_id}`，见 [2.6 带图片和文档](./chat#_2-6-带图片和文档) | `upload_id` | 对外没有删除接口 |
| 工作区文件 | Agent 执行时写出来的，是这个终端用户工作区里的全部文件 | 列出 `GET /v1/agents/{agent_code}/workspace/files`、下载 `GET /v1/agents/{agent_code}/workspace/file`，见 [5.6 工作区文件](./query#_5-6-工作区文件) | 文件在工作区里的 `path` | 对外没有删除接口 |
| 产物 | Agent 主动登记成成果的那几份文件（比如一份周报） | 列出 `GET /v1/agents/{agent_code}/artifacts`、下载 `GET /v1/agents/{agent_code}/artifacts/download`、删除 `DELETE /v1/agents/{agent_code}/artifacts`，见 [5.7 产物](./query#_5-7-产物) | 产物 `name` | `DELETE /v1/agents/{agent_code}/artifacts`，软删，底层字节不清除 |

三者之间的两条关系：

- 文档类附件会落进这个终端用户的工作区，所以它也会出现在工作区文件列表里；图片类附件不会，图片存在对象存储里。
- 产物是工作区文件里被挑出来的那一部分，登记成产物之后，同一份内容仍然留在工作区里。

## 7.3 公共请求头

| 请求头 | 用在哪 | 说明 |
|---|---|---|
| `Authorization: Bearer <key>` | 全部端点 | 见 [6 认证与 Key](./auth) |
| `Content-Type: application/json` | 除上传附件外的全部写请求 | 上传附件（`POST /v1/agents/{agent_code}/uploads`）用 `multipart/form-data` |
| `Idempotency-Key` | 仅 `POST /v1/agents/{agent_code}/runs` | 见 [7.7 幂等性](#_7-7-幂等性) |
| `X-Expert-Work-Deadline-Ms`（可选） | 任意端点 | 你自己设的绝对截止时间（unix 毫秒时间戳）。已经过去就直接 504（`DEADLINE_EXCEEDED`）。不需要端到端超时控制就别传 |

## 7.4 响应头

只有第一行的 `X-Expert-Work-Trace-Id` 是每个响应都带的；**其余四个按端点和模式出现，别假设它们成套出现**：

| 响应头 | 出现在哪 | 含义 |
|---|---|---|
| `X-Expert-Work-Trace-Id` | **每一个响应**，成功和失败都有 | 这次请求在服务端的关联 id。自己排查不出原因、要找租户管理员报障时，把这个值一起给出去 |
| `X-Expert-Work-Run-Id` | 发起对话的 `stream` 模式（含幂等重放）、事件回放、审批决策（两种模式都带） | 这次响应对应的 `run_id`。**发起对话的 `queue` 模式不带这个头**——`run_id` 只在响应体 `data.run_id` 里。审批决策给的是**续跑的新 run id** |
| `X-Expert-Work-Session-Id` | 发起对话的 `stream` 模式（含幂等重放）、事件回放 | 这次绑定或续接到的 `session_id`。**审批决策的两种响应都不带**——续跑的会话就是原会话，没有变化 |
| `X-Expert-Work-Stream-Mode` | 事件回放；以及发起对话在 `stream` 模式下命中幂等重放时 | `live`（run 还在跑，接的是实时流）或 `replay`（run 已结束，按存下来的事件顺序回放）。**首次发起的 `stream` 响应不带**（直接就是新流，没有实时/回放之分）；**审批决策的两种响应也都不带** |
| `X-Expert-Work-Next-Seq` | 只在回放被分页截断时 | 下一页应当传回去的 `since_seq`。同一个值也在流末尾的 `truncated` 事件里，**以事件为准更稳**（中间代理会剥掉不认识的响应头）。见 [3.6 断线重连与回放分页](./sse-events#_3-6-断线重连与回放分页) |

## 7.5 统一响应格式

成功：

```json [响应 · 成功]
{ "success": true, "data": { /* 具体端点的数据 */ }, "error": null }
```

失败：

```json [响应 · 失败]
{ "success": false, "data": null, "error": { "code": "SOME_CODE", "message": "..." } }
```

两条例外要注意：

- `error: null` 这个键不保证每个成功响应都给，解析时按键存在与否读，别假设它一定在。
- **不是所有错误都能读到 `error.code`**。一部分错误（比如权限不足的 403、`inputs` 模板变量校验失败的 422）只有一个 `detail` 字段，`detail` 有时是字符串，有时是 `{"code":..., "message":...}` 对象。完整对照表见 [8 错误码总表](./errors)。

## 7.6 限流与配额

两者都返回 `429`，但含义和应对方式完全不同：

| 对比维度 | 限流（rate limit） | 配额（quota） |
|---|---|---|
| 限制什么 | 多快——按时间窗口限制调用频率 | 多少——按资源维度限制用量（比如工作区存储） |
| `error.code` | `RATE_LIMIT_EXCEEDED` | `QUOTA_EXCEEDED` |
| `Retry-After` 响应头 | 带 | **不带** |
| 怎么办 | 退避重试 | 退避重试解决不了，先清理占用的资源 |

拿到 429 先看 `error.code` 判断是哪一种。完整的 `dimension` 字段含义、两种响应样例，以及产物下载这个例外（它的配额也翻成 `RATE_LIMIT_EXCEEDED`、也带 `Retry-After`，但短退避重试对它无效），都在 [8.11 429](./errors#_8-11-429-——-两种情况-含义不同) 这一节，那里是这件事的权威说明。

## 7.7 幂等性

`Idempotency-Key` 请求头只用在 `POST /v1/agents/{agent_code}/runs` 上，作用域是 **key + 请求体 + `agent_code` 三者的组合**。完整规则与重放行为见 [2.8 防重复下发](./chat#_2-8-防重复下发-idempotency-key)。

**审批决策端点有自己独立的一套幂等**：字段名是 `idempotency_key`，位置在请求体（不是请求头），作用域按这次决策计算而非按 run 创建。见 [4.2 审批决策](./run-control#_4-2-审批决策)。
