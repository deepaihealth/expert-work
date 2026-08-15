# 2 通用约定

本篇是贯穿全部对外端点的共性约定——环境地址、协议、公共请求头、统一响应格式、限流与配额的区别、以及幂等性怎么用。具体某个端点的参数表见 [调用 Agent](./run-agent)。

## 2.1 环境地址

测试环境:

```
https://expert-work-test.deepaihealth.com
```

全部对外端点都挂在 `/v1` 前缀下,比如:

```
POST https://expert-work-test.deepaihealth.com/v1/agents/{agent_code}/runs
```

生产环境:本对外 API 目前**未开放**,上线时间另行通知。

## 2.2 协议约定

- **HTTPS**——不提供明文 HTTP。
- **字符编码 UTF-8**。
- **请求 / 响应体为 JSON**,例外两处:SSE 端点(`Content-Type: text/event-stream`,见 [SSE 事件格式](./sse-events))和工作区文件下载(响应体是文件字节流本身,见 [调用 Agent](./run-agent) 的「工作区文件」一节)。
- **时间戳**:ISO-8601,带时区偏移,比如 `2026-08-12T10:00:00+00:00`。会话 / 消息的 `created_at`、`updated_at` 都是这个形态。
- **id 形态**:`session_id`、`run_id` 是标准 UUID(带连字符的小写十六进制字符串,如 `"550e8400-e29b-41d4-a716-446655440000"`);`user_id` 不是 UUID,是你自己业务系统里的标识字符串(1–255 字符),见 [调用 Agent](./run-agent) 参数表里 `user_id` 一行的说明。

## 2.3 公共请求头

| 请求头 | 用在哪 | 说明 |
|---|---|---|
| `Authorization: Bearer <key>` | 全部端点 | 见 [认证](./auth)。 |
| `Content-Type: application/json` | 除文件上传外的全部写请求 | 文件上传接口(`POST /v1/agents/{agent_code}/uploads`)用 `multipart/form-data`。 |
| `Idempotency-Key` | 仅 `POST /v1/agents/{agent_code}/runs` | 请求头形式的幂等键,见下方「2.6 幂等性」。 |
| `X-Expert-Work-Deadline-Ms`(可选) | 任意端点 | 你自己设的一个绝对截止时间(unix 毫秒时间戳)。已经过去就直接 504(`DEADLINE_EXCEEDED`,见 [错误码与限流](./errors));不需要端到端超时控制就别传它。 |

对应地,以下三个响应头**不是每种响应都会一起出现**——具体哪个端点、哪种情况带哪个,以下表为准,别假设它们总是成套出现:

| 响应头 | 出现在哪 | 含义 |
|---|---|---|
| `X-Expert-Work-Run-Id` | 发起 run 的 `stream` 模式(含幂等重放)、事件回放、审批决策(`stream` / `queue` 两种响应路径都带) | 这次响应对应的 `run_id`。审批决策续跑后,这里给的是**新** run 的 id,不是被决策的那个 run。**发起 run 的 `queue` 模式响应不带这个头**——`run_id` 只在响应体的 `data.run_id` 字段里,这是和审批决策端点不一样的地方(审批决策 `queue` 模式响应头也带 `Run-Id`)。 |
| `X-Expert-Work-Session-Id` | 发起 run(`stream` 模式,含幂等重放)、事件回放 | 这次响应绑定/续接到的 `session_id`。**审批决策(`:decide`)的两种响应路径都不带这个头**——续跑的会话就是原会话,没有变化,所以没必要重复给。 |
| `X-Expert-Work-Stream-Mode` | 事件回放;以及发起 run 在 `stream` 模式下命中 `Idempotency-Key` 幂等重放时的响应 | `live`(run 还在跑,接的是实时流)或 `replay`(run 已终态,按落库帧顺序回放)。**首次发起(非重放)的 `stream` 响应不带这个头**(直接就是新开的实时流,没有"重放/实时"的区分);**审批决策的两种响应路径也都不带**。 |

::: warning 同一资源的两个写操作,`user_id` 位置不一致
`PATCH /v1/agents/{agent_code}/sessions/{session_id}`(重命名会话)的 `user_id` 在**请求体**里;`DELETE /v1/agents/{agent_code}/sessions/{session_id}`(归档会话)的 `user_id` 在**查询参数**里。这不是笔误,是两个端点现有的真实形状——按 query 传 PATCH 会拿到 422。发请求前照 [调用 Agent](./run-agent) 里各自的参数表核对,不要假设两个写操作同一种传法。
:::

## 2.4 统一响应格式

大多数响应是这个标准格式:

成功:

```json
{ "success": true, "data": { /* 具体端点的数据 */ }, "error": null }
```

（绝大多数成功响应都是这个形状,但不是没有例外——`error: null` 这个键不保证每个端点都给,解析时按键存在与否读,别假设它一定在。）

失败:

```json
{ "success": false, "data": null, "error": { "code": "SOME_CODE", "message": "..." } }
```

**但不是所有错误都能读到 `error.code`**——一部分错误(比如 scope 不足的 403、`inputs` 模板变量校验失败的 422)是只有一个 `detail` 字段的简易格式,`detail` 有时是字符串,有时是 `{"code":..., "message":...}` 对象。写响应解析逻辑时不要假设所有错误都能读到 `error.code`——完整的形状对照表见 [错误码与限流](./errors)。

## 2.5 限流与配额

两者都会返回 `429`,但含义不同:

- **限流(rate limit)**——按时间窗口限制"多快",网关 / 租户层面的频率闸,和你在做什么业务操作无关。`error.code` 为 `RATE_LIMIT_EXCEEDED`,带 `Retry-After` 响应头,应对方式是退避重试。
- **配额(quota)**——按资源维度限制"多少"(比如工作区存储用量)。`error.code` 为 `QUOTA_EXCEEDED`,**不带** `Retry-After`,因为退避重试解决不了——得先清理占用的资源。

拿到 429 先看 `error.code` 判断是哪一种,再决定退避重试还是清理资源。完整的 `dimension` 字段含义、两种 429 的响应样例,见 [错误码与限流](./errors) 的「429」一节。

## 2.6 幂等性

`Idempotency-Key` 请求头只用在 `POST /v1/agents/{agent_code}/runs` 上,`stream` / `queue` 两种 `mode` 都支持。作用域是 **key + 请求体 + `agent_code` 三者的组合**——同一个 key 换了请求体、或者打给了不同的 `agent_code`,都被当成"这个 key 被复用了",不是"这是同一次重试"。

- **同一个 key + 同一个请求体 + 同一个 `agent_code`**——不新建 run,直接把原来那次的结果返回(`queue` 模式给回同一个 `run_id`;`stream` 模式接到原 run 的事件流上)。
- **同一个 key,但请求体或 `agent_code`变了**——422,`error.code` 为 `IDEMPOTENCY_KEY_REUSED`。
- **key 本身不合法**(去空白后为空,或超过 255 字符)——422,`error.code` 为 `INVALID_IDEMPOTENCY_KEY`。

这份"key → run"的绑定关系永久保留,没有过期时间。完整的重放行为(两种 `mode` 分别怎么响应)转述自 [调用 Agent](./run-agent) 的「`Idempotency-Key`」一节,参数细节以那一节为准。

**审批决策端点(`:decide`)有自己独立的一个 `idempotency_key`**,是请求体字段,不是请求头,作用域也不同(按这次决策而非按 run 创建)。
