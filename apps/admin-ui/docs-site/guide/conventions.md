# 7 通用约定

贯穿全部对外接口的共性约定：环境地址、协议、公共请求头与响应头、统一响应格式、限流与配额的区别、幂等性。

具体端点的参数表见 [2 跟 Agent 对话](./chat) 和 [5 查询与管理](./query)。

## 7.1 环境地址

测试环境：

```
https://expert-work-test.deepaihealth.com
```

全部对外端点挂在 `/v1` 前缀下，比如：

```
POST https://expert-work-test.deepaihealth.com/v1/agents/{agent_code}/runs
```

生产环境：本对外 API 目前**未开放**，上线时间另行通知。

## 7.2 协议约定

- **HTTPS**，不提供明文 HTTP。
- **字符编码 UTF-8**。
- **请求/响应体为 JSON**，两处例外：SSE 端点（`Content-Type: text/event-stream`，见 [3 读懂 SSE 流](./sse-events)）和工作区文件下载（响应体是文件字节流，见 [5.6 工作区文件](./query#_5-6-工作区文件)）。
- **时间戳**：ISO-8601 带时区偏移，比如 `2026-08-12T10:00:00+00:00`。
- **id 形态**：`session_id`、`run_id` 是标准 UUID（带连字符的小写十六进制，如 `550e8400-e29b-41d4-a716-446655440000`）；`user_id` **不是** UUID，是你自己业务系统里的标识字符串（1–255 字符）。

## 7.3 公共请求头

| 请求头 | 用在哪 | 说明 |
|---|---|---|
| `Authorization: Bearer <key>` | 全部端点 | 见 [6 认证与 Key](./auth) |
| `Content-Type: application/json` | 除文件上传外的全部写请求 | 文件上传（`POST /v1/agents/{agent_code}/uploads`）用 `multipart/form-data` |
| `Idempotency-Key` | 仅 `POST /v1/agents/{agent_code}/runs` | 见 [7.6 幂等性](#_7-6-幂等性) |
| `X-Expert-Work-Deadline-Ms`（可选） | 任意端点 | 你自己设的绝对截止时间（unix 毫秒时间戳）。已经过去就直接 504（`DEADLINE_EXCEEDED`）。不需要端到端超时控制就别传 |

## 7.4 响应头

这三个响应头**不是每种响应都会一起出现**，别假设它们成套出现：

| 响应头 | 出现在哪 | 含义 |
|---|---|---|
| `X-Expert-Work-Run-Id` | 发起对话的 `stream` 模式（含幂等重放）、事件回放、审批决策（两种模式都带） | 这次响应对应的 `run_id`。**发起对话的 `queue` 模式不带这个头**——`run_id` 只在响应体 `data.run_id` 里。审批决策给的是**续跑的新 run id** |
| `X-Expert-Work-Session-Id` | 发起对话的 `stream` 模式（含幂等重放）、事件回放 | 这次绑定或续接到的 `session_id`。**审批决策的两种响应都不带**——续跑的会话就是原会话，没有变化 |
| `X-Expert-Work-Stream-Mode` | 事件回放；以及发起对话在 `stream` 模式下命中幂等重放时 | `live`（run 还在跑，接的是实时流）或 `replay`（run 已结束，按存下来的事件顺序回放）。**首次发起的 `stream` 响应不带**（直接就是新流，没有实时/回放之分）；**审批决策的两种响应也都不带** |

## 7.5 统一响应格式

成功：

```json
{ "success": true, "data": { /* 具体端点的数据 */ }, "error": null }
```

失败：

```json
{ "success": false, "data": null, "error": { "code": "SOME_CODE", "message": "..." } }
```

两条例外要注意：

- `error: null` 这个键不保证每个成功响应都给，解析时按键存在与否读，别假设它一定在。
- **不是所有错误都能读到 `error.code`**。一部分错误（比如权限不足的 403、`inputs` 模板变量校验失败的 422）只有一个 `detail` 字段，`detail` 有时是字符串，有时是 `{"code":..., "message":...}` 对象。完整对照表见 [8 错误码总表](./errors)。

## 7.6 限流与配额

两者都返回 `429`，但含义和应对方式完全不同：

| | 限流（rate limit） | 配额（quota） |
|---|---|---|
| 限制什么 | 多快——按时间窗口限制调用频率 | 多少——按资源维度限制用量（比如工作区存储） |
| `error.code` | `RATE_LIMIT_EXCEEDED` | `QUOTA_EXCEEDED` |
| `Retry-After` 响应头 | 带 | **不带** |
| 怎么办 | 退避重试 | 退避重试解决不了，先清理占用的资源 |

拿到 429 先看 `error.code` 判断是哪一种。完整的 `dimension` 字段含义和响应样例见 [8.11 429](./errors)。

## 7.7 幂等性

`Idempotency-Key` 请求头只用在 `POST /v1/agents/{agent_code}/runs` 上，作用域是 **key + 请求体 + `agent_code` 三者的组合**。完整规则与重放行为见 [2.8 防重复下发](./chat#_2-8-防重复下发-idempotency-key)。

**审批决策端点有自己独立的一套幂等**：字段名是 `idempotency_key`，位置在请求体（不是请求头），作用域按这次决策计算而非按 run 创建。见 [4.2 审批决策](./run-control#_4-2-审批决策)。
