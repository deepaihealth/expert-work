# 8 错误码总表

本篇讲清楚调用失败时你会看到什么、为什么、该怎么应对——覆盖 400 / 401 / 403 / 404 / 409 / 410 / 413 / 422 / 429 / 500 / 502 / 503 / 504，以及限流和配额这两个容易混的概念。

## 8.1 错误码速查表

线上收到一个 `code`，按这张表直接查，不用翻六节。「哪个端点」列写的是这个码**只**可能从哪些接口返回，写着「全部端点」的则是任何一条对外接口都可能给你的。多数行链接到下面对应小节的详解（取消 run、审批决策这两个端点的几个码，详解在 [4 对话过程中的控制](./run-control)）。

| `code` | HTTP 状态 | 哪个端点 | 含义 | 建议处理 |
|---|---|---|---|---|
| [`WORKSPACE_FILE_FAILED`](#_8-3-400-——-工作区文件路径不合法) | 400 | 工作区文件下载 | 工作区文件路径不合法 | 别自己拼路径，原样回传「列出文件」接口给的 `path` |
| [`INVALID_UPLOAD`](#_8-3-400-——-工作区文件路径不合法) | 400 | 上传附件 | 上传接口自身的校验失败(文件类型不在白名单、文件为空、没有文件名) | 检查文件类型和内容后重传 |
| [`AUTH_MISSING_CREDENTIALS`](#_8-4-401-——-key-无效-过期) | 401 | 全部端点 | 没带 `Authorization` 头，或格式不对 | 检查请求头 |
| [`AUTH_INVALID_TOKEN`](#_8-4-401-——-key-无效-过期) | 401 | 全部端点 | key 不存在 / 已吊销 / 轮换宽限期已过 | 换一把新 key |
| [`AUTH_TOKEN_EXPIRED`](#_8-4-401-——-key-无效-过期) | 401 | 全部端点 | key 本身没问题但已过期 | 换一把新 key |
| `AUTH_UNAUTHENTICATED` | 401 | 全部端点 | **兜底码，一般遇不到**:认证失败，但没落进上面三种里的任何一种 | 和上面三条一样:检查请求头，换一把新 key |
| [`FORBIDDEN`](#_8-5-403-——-权限不足或被阻断)(裸 `detail`) | 403 | 全部端点 | key 的 权限不足 | 换一把带所需 scope 的 key |
| [`AGENT_DISABLED`](#_8-5-403-——-权限不足或被阻断) | 403 | 发起对话 / 预建会话 / 上传附件 / 审批决策 | 这个 agent 已被管理员下线 | 联系租户管理员 |
| [`TENANT_SUSPENDED`](#_8-5-403-——-权限不足或被阻断) | 403 | 全部端点 | 租户被暂停 | 联系租户管理员 |
| [`AGENT_NOT_FOUND`](#_8-6-404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | 发起对话 / 预建会话 / 上传附件 / 审批决策 | `agent_code` 在你的租户下没有已发布版本 | 检查 `agent_code` 拼写与发布状态 |
| [`SESSION_NOT_FOUND`](#_8-6-404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | 发起对话 / 预建会话 / 上传附件 / 历史消息 / 重命名会话 / 归档会话 / run 列表(带 `session_id` 时) | `session_id` 不存在，或不属于这个 `user_id` / `agent_code` | 核对三者是否匹配；统一的 404，不区分"不存在"与"不是你的" |
| [`UPLOAD_NOT_FOUND`](#_8-6-404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | 发起对话(`files[]` 里的附件) / 附件下载 | 这份附件不存在、不属于这个 `user_id`、已被软删；（仅发起对话时）图片不属于本次绑定的会话；（仅附件下载时）底层内容已被回收 | 核对 `upload_id` / `user_id`（图片还要核对 `session_id`）是否匹配；五种情况统一的 404，不区分 |
| [`WORKSPACE_FILE_FAILED`](#_8-6-404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | 工作区文件下载 | `user_id` 未识别，或文件不存在 | 核对 `user_id` / `path` |
| [`RUN_NOT_FOUND`](./run-control#_4-1-取消-run) | 404 | 取消 run / 审批决策 / 事件回放 | `run_id` 不存在，或不属于这个 `user_id` / `agent_code` | 核对三者是否匹配；不要当作"还没创建好"去重试 |
| [`APPROVAL_NOT_FOUND`](./run-control#_4-2-审批决策) | 404 | 审批决策 | 这个 `run_id` 没有一条待审批记录 | 确认这个 run 真的处于等待审批状态 |
| [`ARTIFACT_NOT_FOUND`](./query#_5-7-产物) | 404 | 产物下载 / 产物删除 | 产物不存在、已删除，或不属于这个 `user_id` | 核对 `user_id` / `name`；三种情况不区分 |
| [`APPROVAL_CONFLICT`](./run-control#_4-2-审批决策) | 409 | 审批决策 | 这条审批已经被决定过 | 不要重复决策；要重放上次结果，带上当时用的 `idempotency_key` |
| [`SESSION_NOT_BOUND`](./run-control#_4-2-审批决策) | 409 | 审批决策 | run 所在会话没有绑定 agent(内部状态异常) | 联系租户管理员 |
| [`AGENT_DELETED`](./run-control#_4-2-审批决策) | 410 | 审批决策 | agent 已被(软)删除 | 不可恢复，换一个 `agent_code` |
| [`UPLOAD_TOO_LARGE`](#_8-9-413-——-文档-图片超限) | 413 | 上传附件 | 文档 / 图片超过大小上限 | 压缩或裁剪后重传，或拆分成多份 |
| [`INVALID_REQUEST`](#_8-10-422-——-请求参数不合法) | 422 | 全部端点 | 请求体字段或查询参数没通过基础校验 | 检查字段类型 / 长度 / 取值范围 |
| [`INVALID_USER_ID`](#_8-10-422-——-请求参数不合法) | 422 | 凡是要求 `user_id` 的端点 | `user_id` 去掉首尾空白后是空字符串 | 传一个非空白的 `user_id` |
| [`INVALID_UPLOAD_ID`](#_8-10-422-——-请求参数不合法) | 422 | 发起对话 | `files[]` 里的 `upload_id` 不是 `upl_<uuid>` 那个形状 | 原样回传上传接口返回的 `data.upload_id`，不要自己改写 |
| [`INVALID_IDEMPOTENCY_KEY`](./conventions#_7-7-幂等性) | 422 | 发起对话 | `Idempotency-Key` 去空白后为空，或超过 255 字符 | 检查请求头取值 |
| [`IDEMPOTENCY_KEY_REUSED`](./conventions#_7-7-幂等性) | 422 | 发起对话 | 同一个 key 换了请求体，或打给了不同的 `agent_code` | 换一个新 key，不要复用 |
| [`TOO_MANY_INPUT_KEYS`](#_8-10-422-——-请求参数不合法) | 422 | 发起对话 | `inputs` 键数量超过 64 | 精简 `inputs` |
| [`INPUT_VALUE_TOO_LONG`](#_8-10-422-——-请求参数不合法) | 422 | 发起对话 | `inputs` 某个字符串值超过 8192 字符 | 精简该值 |
| [`TOO_MANY_INPUT_BYTES`](#_8-10-422-——-请求参数不合法) | 422 | 发起对话 | `inputs` 序列化后总字节数超过 65536 | 精简 `inputs` 整体 |
| [`UNTRUSTED_CONTENT_BLOCK_TOO_LONG`](#_8-10-422-——-请求参数不合法) | 422 | 发起对话 | `untrusted_content` 单块超过 8192 字符 | 拆成多个数组元素 |
| [`INVALID_TITLE`](#_8-10-422-——-请求参数不合法) | 422 | 重命名会话 | 会话标题去掉首尾空白后是空字符串 | 传一个非空标题 |
| [`INVALID_ARTIFACT_NAME`](./query#_5-7-产物) | 422 | 产物下载 / 产物删除 | 产物 `name` 含 NUL 字节 | 原样回传列表接口给的 `name`，不要自己拼 |
| [`AGENT_BUILD_FAILED`](#_8-10-422-——-请求参数不合法) | 422 | 发起对话 / 审批决策 | agent manifest 构建失败——两个端点上含义相同 | 服务端配置问题，不是你这边能解决的，联系租户管理员 |
| [`RATE_LIMIT_EXCEEDED`](#_8-11-429-——-两种情况-含义不同) | 429 | 全部端点(网关 / 租户层的频率限制)；上传图片、产物下载还各有一条业务维度额度 | 触发限流 | 按 `retry_after_s`(或 `Retry-After` 头)退避重试；产物下载那条是例外，见 [8.11](#_8-11-429-——-两种情况-含义不同) |
| [`QUOTA_EXCEEDED`](#_8-11-429-——-两种情况-含义不同) | 429 | 上传附件(文档通路) | 这个终端用户的工作区容量满了 | 清理资源或联系管理员提额度——重试没用 |
| [`WORKSPACE_LIST_FAILED`](#_8-12-500-——-工作区服务端配置问题) | 500 | 工作区文件列表 | 工作区存储服务端配置有问题 | 不是你这边能解决的，联系租户管理员 |
| [`WORKSPACE_FILE_FAILED`](#_8-12-500-——-工作区服务端配置问题) | 500 | 工作区文件下载 | 工作区存储服务端配置有问题 | 不是你这边能解决的，联系租户管理员 |
| [`UPLOAD_FAILED`](#_8-12-500-——-工作区服务端配置问题) | 500 | 上传附件(文档通路) | 落盘失败:服务端工作区权限配置有问题 | 重试无效，联系租户管理员 |
| [`UPLOAD_CONTENT_UNAVAILABLE`](#_8-12-500-——-工作区服务端配置问题) | 500 | 附件下载 | 登记还在，服务端读不到附件内容(权限配置问题) | 服务端存储配置问题，重试无效，联系租户管理员 |
| [`ARTIFACT_CONTENT_UNAVAILABLE`](./query#_5-7-产物) | 500 | 产物下载 | 产物记录在，服务端读不到内容(权限配置问题) | 服务端存储配置问题，重试无效，联系租户管理员 |
| [`UPLOAD_FAILED`](#_8-13-502-——-上传写入失败-上游错误) | 502 | 上传附件(文档通路) | 落盘失败:写入时遇到上游错误 | 重试；持续失败联系租户管理员 |
| [`UPLOAD_UNAVAILABLE`](#_8-14-503-——-服务不可用-两种含义不同) | 503 | 上传附件 | 对象存储或沙箱工作区未就绪 | 稍后重试；持续失败联系租户管理员 |
| [`UPLOAD_CONTENT_UNAVAILABLE`](#_8-14-503-——-服务不可用-两种含义不同) | 503 | 附件下载 | 服务端整体没有配置对应的存储通路(与上面 500 是同一个 `code`,靠状态码区分两种服务端故障) | 部署/配置问题，重试无效，联系租户管理员 |
| [`ARTIFACT_CONTENT_UNAVAILABLE`](#_8-14-503-——-服务不可用-两种含义不同) | 503 | 产物下载 | 服务端整体没有配置工作区存储通路(与上面 500 是同一个 `code`,靠状态码区分两种服务端故障) | 部署/配置问题，重试无效，联系租户管理员 |
| [`SERVER_OVERLOADED`](#_8-14-503-——-服务不可用-两种含义不同) | 503 | 全部端点 | 全站过载保护 | 按 `Retry-After` 头退避重试 |
| `AUTH_BACKEND_UNAVAILABLE` | 503 | 全部端点 | **兜底码，一般遇不到**:认证后端不可用，这次请求没法校验 | 稍后重试；持续失败联系租户管理员 |
| [`DEADLINE_EXCEEDED`](#_8-15-504-——-请求超过了你自己设的截止时间) | 504 | 全部端点(只在你自己传了 `X-Expert-Work-Deadline-Ms` 时) | 你自己传的截止时间已经过去 | 检查这个头的取值，或者干脆别传它 |
| `UPLOAD_ERROR` | 原始失败的状态码 | 上传附件 | **兜底码，一般遇不到**:上传接口内部的一次失败没有对应的专用码 | 按 HTTP 状态码兜底处理 |
| `APPROVAL_ERROR` | 原始失败的状态码 | 审批决策 | **兜底码，一般遇不到**:审批决策端点内部的一次失败没有对应的专用码 | 按 HTTP 状态码兜底处理 |

**这张表只覆盖有 `error.code` 的失败，不是全部失败的穷尽清单。** 表外还有两类:

- **读不到 `error.code` 的失败**:只有一个 `detail` 字段的简易格式，比如权限不足的 403、`inputs` 模板变量校验失败的 422；还有内部校验函数直接抛出的裸文案，比如"这个 agent 不支持图片输入""单次 run 图片数超过上限"(都是 422)、配额引擎本身不可用(503)。形状说明见 [8.2](#_8-2-错误响应的形状不统一)，具体条目分散在下面各节。
- **个别没有列进这张表的信封码**:写解析逻辑时别假设"表里没有的码就不会出现"，拿到不认识的 `code` 时按 HTTP 状态码兜底处理即可。

## 8.2 错误响应的形状不统一

大多数错误会用**标准格式**返回，能读到 `error.code`:

```json [响应 · 标准格式]
{
  "success": false,
  "data": null,
  "error": { "code": "AGENT_NOT_FOUND", "message": "..." }
}
```

但一部分错误(比如 权限不足的 403、`inputs` 模板变量校验失败的 422)是**简易格式**，只有一个 `detail` 字段，读不到 `error.code`:

```json [响应 · 简易格式]
{ "detail": "..." }
```

`detail` 有时是字符串，有时是 `{"code":..., "message":...}` 对象。写解析逻辑时不要假设所有错误都是标准格式——先看 HTTP 状态码兜底，body 里有 `error.code` 就用它，没有就退化读 `detail`。下面每一节会标出具体是哪种形状。

**为什么会有两种形状**:绝大多数对外端点会把自己内部的失败翻译成标准格式再返回，今天只剩两个地方漏到简易格式:

- **权限检查不通过的 403**——全部端点通用。
- **发起对话端点内部直接抛出的几条校验**——"这个 agent 不支持图片输入""单次 run 图片数超过上限"、`inputs` 模板变量与 Agent 声明不匹配(都是 422)，以及配额引擎本身不可用(503)。

其余端点(上传附件、附件下载、审批决策、取消 run、工作区、产物、会话与 run 的查询)都自己把失败包成了标准格式，能读到 `error.code`。另外，请求体 / 查询参数的**格式**校验失败是统一处理的，一律是标准格式的 422 `INVALID_REQUEST`。

## 8.3 400 —— 工作区文件路径不合法

只发生在 `GET /v1/agents/{agent_code}/workspace/file`(下载工作区文件)的 `path` 查询参数上，能读到 `error.code`，固定 `WORKSPACE_FILE_FAILED`:

```json [响应 400]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

以下几种 `path` 形态会触发:绝对路径(以 `/` 开头)、含 `..` 段、含 NUL 字节(`\x00`)、空字符串或者去掉首尾空白后为空。应对:别自己拼路径——直接把 `GET .../workspace/files` 返回的 `path` 字段原样回传，细节见 [5.6 工作区文件](./query#_5-6-工作区文件) 的「工作区文件」一节。

**另一个 400 只发生在上传接口**(`POST /v1/agents/{agent_code}/uploads`)，`error.code` 为 `INVALID_UPLOAD`，三种触发方式:

- 声明的 `Content-Type` 不在允许列表里(判据是 `Content-Type` 而不是文件名后缀，完整清单见 [2.6 能传哪些文件类型](./chat#能传哪些文件类型))。
- 文件内容为空。
- 没有文件名。

三种都是检查文件本身后重传，不用改调用方式。

## 8.4 401 —— key 无效 / 过期

认证失败一律 401，能读到 `error.code`，并带 `WWW-Authenticate: Bearer realm="expert-work"` 响应头:

```json [响应 401]
{ "success": false, "data": null, "error": { "code": "AUTH_INVALID_TOKEN", "message": "Invalid or unrecognised token" } }
```

`error.code` 会区分三种具体情况:

| `code` | 什么情况 |
|---|---|
| `AUTH_MISSING_CREDENTIALS` | 没带 `Authorization` 头，或者格式不对 |
| `AUTH_INVALID_TOKEN` | key 格式对但校验不过——不存在、已被吊销、或者轮换后的宽限期已经过了 |
| `AUTH_TOKEN_EXPIRED` | key 本身没问题，但 `expires_at` 已经过了 |

应对:检查 key 是不是复制错了 / 过期了 / 被吊销了，换一把新 key(见 [6 认证与 Key](./auth))。

## 8.5 403 —— 权限不足或被阻断

**scope 不够**是最常见的 403 场景——比如拿一把只有 `read` scope 的 key 去调 `POST /v1/agents/{agent_code}/runs`(这个接口要求 `write`)。这类 403 读不到 `error.code`——码在 `detail.code`:

```json [响应 · 简易格式]
{ "detail": { "code": "FORBIDDEN", "message": "principal lacks required role" } }
```

应对:换一把带 `write` scope 的 key(见 [6.3 三档权限怎么选](./auth#_6-3-三档权限怎么选))。

另外两种 403 能读到 `error.code`，和 scope 无关:当前 Agent 被管理员下线(`AGENT_DISABLED`)、或者租户本身被暂停(`TENANT_SUSPENDED`)——都不是靠换 key 能解决的，需要联系你的租户管理员。

## 8.6 404 —— agent 不存在 / 会话不存在 / 工作区文件不存在

能读到 `error.code`，四种情况:

```json [响应 404]
{ "success": false, "data": null, "error": { "code": "AGENT_NOT_FOUND", "message": "no active agent 'xxx' for this tenant" } }
```

- `AGENT_NOT_FOUND`——`{agent_code}` 在你的租户下没有已发布(ACTIVE)的版本:要么这个名字从没建过，要么建了但还没发布 / 已经下线，两种情况返回同一个 404，不做区分。
- `SESSION_NOT_FOUND`——传的 `session_id` 找不到，或者它不属于这个 `user_id` / `agent_code` 组合(跨用户 / 跨 agent 的会话 id 一律当不存在处理，不会告诉你"存在但不是你的")。历史消息、重命名(`PATCH .../sessions/{session_id}`)、归档(`DELETE .../sessions/{session_id}`)、run 列表(`GET .../runs?session_id=`)四个接口都走这同一条不透明规则。
- `UPLOAD_NOT_FOUND`——**这个码说的是附件没找到**。两个入口:发起 run 请求体 `files[]` 里的 `upload_id`，或附件下载接口 `GET /v1/agents/{agent_code}/uploads/{upload_id}` 的路径参数。撞上以下任意一种都返回这同一个 404，不区分是哪一种；但折叠的具体条件按两个入口有差别:
  - **两处都会触发**:`upload_id` 查不到；查到了但不属于这个 `user_id`；已被软删。
  - **只在发起 run 时触发**(`files[]` 解析)：图片行还额外要求属于当前请求最终绑定的那段会话(`session_id`)，不属于同样是这个 404——下载接口本身不带 `session_id` 参数，不做这项检查，文档类附件也没有这条限制。
  - **只在附件下载时触发**:附件的登记还在、也确实属于这个 `user_id`，但底层字节已经不在了(比如对象存储 / 工作区侧的内容已被回收)，`error.message` 是 `"upload content not found"`(仍是同一个 `code`，同样是这个 404)。

  **一个真实会踩的顺序**(图片会话绑定这条):先调上传接口传了一张图但没带 `session_id`(接口顺手给你铸了一个新会话 A，这次上传绑定的是 A)，然后发起 run 时又没传 `session_id`(这次请求又铸了一个新会话 B)——上传绑定的是 A，run 绑定到了 B，直接 404。避免办法:上传时如果打算紧接着发一次带这个附件的 run，把上传响应里的 `session_id` 原样带进发起 run 的请求体。完整的上传 / 携带流程见 [2.6 带图片和文档](./chat#_2-6-带图片和文档)。
- `WORKSPACE_FILE_FAILED`——`GET /v1/agents/{agent_code}/workspace/file` 下载文件时，`user_id` 未识别(不认识这个终端用户)和 `path` 指向的文件不存在，这两种情况返回同一个统一的 404，不要试图从响应里区分是哪一种——这是刻意的存在性隐藏，不是 bug。同一个 `error.code` 在 400 / 500 也会出现，靠 HTTP 状态码区分，见下方「400」与「500」两节。
- `RUN_NOT_FOUND` / `APPROVAL_NOT_FOUND`——取消 run(`:cancel`)与审批决策(`:decide`)这两个端点各自的归属校验 / 审批查找失败码，完整的失败码表(含这两个端点特有的 409 / 403 / 410 / 422 情况)见 [4 对话过程中的控制](./run-control)。

## 8.7 409 —— 审批冲突

只发生在审批决策端点(`POST /v1/agents/{agent_code}/runs/{run_id}:decide`):`APPROVAL_CONFLICT`(这条审批已经被决定过，重复决策或并发竞争落败)、`SESSION_NOT_BOUND`(这个 run 所在会话没有绑定 agent，内部状态异常)。两个码的完整触发条件见 [4.2 审批决策](./run-control#_4-2-审批决策)。

## 8.8 410 —— agent 已被删除

只发生在审批决策端点续跑时:`AGENT_DELETED`，这个 run 所在会话绑定的 agent 版本已被(软)删除，不可恢复。详情见 [4.2 审批决策](./run-control#_4-2-审批决策)。

## 8.9 413 —— 文档 / 图片超限

只发生在上传接口(`POST /v1/agents/{agent_code}/uploads`)，不是 `/runs` 本身——`/runs` 收的是 `upload_id` 引用(`files[]` 里的条目)，不是原始字节。这个接口把自己抛出的每一种拒绝都翻成能读到 `error.code` 的形状:

```json [响应 413]
{ "success": false, "data": null, "error": { "code": "UPLOAD_TOO_LARGE", "message": "document exceeds 26214400-byte limit" } }
```

默认上限:文档 25 MiB，图片 10 MiB(以你的部署实际配置为准)。应对:压缩/裁剪后重传，或者把大文档拆成多份。

## 8.10 422 —— 请求参数不合法

`POST /v1/agents/{agent_code}/runs` 的 422 分两类，形状不一样；本节最后再列另外三种独立的 422。

### 第一类:请求体字段本身没通过校验

比如 `files[]` 条目多传了 `upload_id` 以外的字段(这个请求体不允许未声明字段)、`upload_id` 是空字符串、`files[]` / `untrusted_content` 超过各自的条数上限。能读到 `error.code`，固定是 `INVALID_REQUEST`:

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "INVALID_REQUEST", "message": "Extra inputs are not permitted" } }
```

同一类里还有八个更具体的业务码，同样能读到 `error.code`:

| `code` | 什么情况 |
|---|---|
| `INVALID_UPLOAD_ID` | `files[]` 里某一项的 `upload_id` 不是 `upl_<uuid>` 那个形状(比如自己拼了别的字符串、截断了、大小写不对) |
| `INVALID_IDEMPOTENCY_KEY` | `Idempotency-Key` 头去空白后是空字符串，或超过 255 字符 |
| `IDEMPOTENCY_KEY_REUSED` | 同一个 `Idempotency-Key` 配了不同的请求体，或者配给了不同的 `agent_code` |
| `TOO_MANY_INPUT_KEYS` | `inputs` 的键数量超过 64 个(正好 64 个合法) |
| `INPUT_VALUE_TOO_LONG` | `inputs` 里某个字符串值超过 8192 字符(正好 8192 字符合法；只检查字符串值) |
| `TOO_MANY_INPUT_BYTES` | `inputs` 序列化后的总字节数(按 UTF-8 编码计算，不是字符数)超过 65536 字节(正好 65536 字节合法)；与 `TOO_MANY_INPUT_KEYS` / `INPUT_VALUE_TOO_LONG` 是三条互相独立的限制，不是互相替代——单值用 list/dict 包一层绕开单值字符数检查时，这条总字节数上限仍然拦得住 |
| `UNTRUSTED_CONTENT_BLOCK_TOO_LONG` | `untrusted_content` 里某一块超过 8192 字符(正好 8192 字符合法)；与 `untrusted_content` 最多 16 项的条数上限是两条互相独立的限制 |
| `INVALID_USER_ID` | `user_id` 去掉首尾空白后是空字符串(比如整串都是空格)——这条不止 `/runs`，凡是要求 `user_id` 的端点都会触发，包括预建会话 / 会话列表与消息 / 重命名 / 归档 / 上传附件 / 附件下载 / 工作区读取(取消 run、审批决策这两个端点比较特殊:空 `user_id` 不会走到这条校验，而是被 `run_id` 归属校验统一折成 404 `RUN_NOT_FOUND`，见 [4 对话过程中的控制](./run-control)) |

### 第二类:模板变量 inputs 与 Agent 声明不匹配

这一类**没有 `error.code`**，是只有一个 `detail` 字段的简易格式字符串:

```json [响应 · 简易格式]
{ "detail": "unknown input variable: foo" }
```

三种情况都是这个形状:

- Agent 没声明模板变量，却传了非空 `inputs`。
- `inputs` 里有 Agent 没声明过的键。
- Agent 声明的必填变量，`inputs` 里没给。

**`inputs` 本身的三条上限(键数量 / 单值长度 / 序列化后总字节数)不属于这一类**——这三条和第一类一样能读到 `error.code`(`TOO_MANY_INPUT_KEYS` / `INPUT_VALUE_TOO_LONG` / `TOO_MANY_INPUT_BYTES`)。细节见 [2.7 外部内容与模板变量](./chat#_2-7-外部内容与模板变量) 的「`inputs`」一节。

### 另外三种独立的 422

**一、agent 构建失败**——`error.code` 为 `AGENT_BUILD_FAILED`，命中的已发布 Agent 的 manifest 因服务端配置问题构建失败(比如引用了不存在的模型 / 工具)，发起对话与审批决策(`:decide`)续跑都可能遇到。这不是你这边能解决的，联系租户管理员。

**二、图片相关的两条校验**——它们拿到的是裸 `detail`，**没有 `error.code`**，不是 `{success, data, error}` 这个形状:

- 422 `{"detail": "too many images: max 8 per run"}`——单次 run 实际处理的图片数超过上限(部署可配，默认 **8** 张)。这条和 `files[]` 最多 64 项是两道完全独立的校验:64 项是请求体字段层面的合计上限(图片和文档一起算，超过是上面第一类的 `INVALID_REQUEST`)。
- 422 `{"detail": "agent does not accept image input: ..."}`——这个 Agent 没开启图片能力(既没声明支持视觉的模型，Agent 配置里也没声明 `vision` 相关能力)，`files[]` 里带了图片条目就会撞上，不管数量。

写解析逻辑时不要假设"拿到 image 相关的 422 就一定有 `error.code` 可读"。

**三、会话标题为空**——`PATCH /v1/agents/{agent_code}/sessions/{session_id}`(重命名会话)有自己独立的一个业务码，和发起对话那套无关，同样能读到 `error.code`:`title` 去掉首尾空白后是空字符串(比如整串都是空格)，422 `INVALID_TITLE`:

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "INVALID_TITLE", "message": "title must not be empty" } }
```

`title` 完全不传，或者传的是字面意义上的空字符串 `""`，走的是请求体字段校验(`title` 要求至少 1 个字符)，422 `INVALID_REQUEST`，不是这个码——区别在于有没有先经过服务端的空白裁剪。

## 8.11 429 —— 两种情况，含义不同

**第一种，限流**(`RATE_LIMIT_EXCEEDED`)——能读到 `error.code`，带 `Retry-After` 响应头:

```json [响应 429]
{
  "success": false,
  "data": null,
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "tenant exceeded its rate limit for this dimension",
    "dimension": "qps",
    "retry_after_s": 3
  }
}
```

`dimension` 告诉你具体是哪一层限流触发的——网关按 IP / 按 key 的频率限制、租户维度的整体频率限制，或者业务层按资源维度的配额限制(比如某个 agent 的调用频率、图片上传的次数或存储字节数)。**但网关这一层的 429 不带 `dimension` 字段**(只有 `code` / `message` / `retry_after_s`)——解析时给 `dimension` 缺省兜底，别假设它总在。应对:按 `retry_after_s`(或 `Retry-After` 头)退避后重试，不要立刻重试。

**第二种，工作区容量满了**(`QUOTA_EXCEEDED`)——只出现在文档上传接口，和 413 一样能读到 `error.code`，**但没有 `Retry-After` 响应头**，因为退避重试解决不了:

```json [响应 429]
{
  "success": false,
  "data": null,
  "error": { "code": "QUOTA_EXCEEDED", "message": "workspace is full — delete files to free space" }
}
```

应对:清理这个终端用户工作区里的旧文件(或者引导用户自己清理)，不是退避重试能解决的。

一般配额走的都是这条 `QUOTA_EXCEEDED` 规则、不带 `Retry-After`。**唯一的例外是产物下载**(`GET /v1/agents/{agent_code}/artifacts/download`)，这个例外要分四条看:

- 它的配额准入统一走限流引擎，超限时翻成 `RATE_LIMIT_EXCEEDED`，不是 `QUOTA_EXCEEDED`。
- 它**带** `Retry-After` 响应头。
- 但它扣的是 `ARTIFACT_DOWNLOAD_COUNT_30D` 这个 **30 天滑动窗口**，额度不会在几秒内回补。
- 所以照 `Retry-After` 做短退避重试对它无效——命中它应当按「这个终端用户的下载额度打满了」处理，不是「这会儿太忙等等再试」。

产物这一侧的说明见 [5.7 产物](./query#_5-7-产物)。

注意这两种 429(以及产物下载那个例外)靠 `error.code` 区分，别只看状态码，也别只看有没有 `Retry-After` 头。

## 8.12 500 —— 工作区服务端配置问题

以下三种 500 出现在三条接口(`GET /v1/agents/{agent_code}/workspace/files` 列表、`GET .../workspace/file` 下载、`GET .../artifacts/download` 下载产物)，能读到 `error.code`:

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_LIST_FAILED", "message": "workspace listing unavailable" } }
```

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "workspace file unavailable" } }
```

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "ARTIFACT_CONTENT_UNAVAILABLE", "message": "artifact content unavailable" } }
```

触发条件是服务端工作区存储的权限配置有问题(比如共享 uid 没配对)，不是你这边的请求有问题，也不是"这个用户 / 文件不存在"——刻意不降级成空列表或 404，免得把服务端配置问题伪装成正常的业务响应。应对:不是退避重试能解决的，联系你的租户管理员核实工作区存储配置。

**上传接口(`POST /v1/agents/{agent_code}/uploads`)文档分支的落盘失败是另一个独立的 `code`**——`UPLOAD_FAILED`，能读到 `error.code`:

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "UPLOAD_FAILED", "message": "workspace write failed" } }
```

500 对应服务端工作区权限配置问题(和上面两个 `WORKSPACE_*` 码同一类根因，但这是上传落盘、不是列出 / 下载)；502 对应写入时遇到的上游错误(见下一节)。两种状态码下 `error.code` 和 `error.message` 完全一样，只能靠 HTTP 状态码分——但应对方式相同:都不是退避重试能解决的，持续失败联系租户管理员。

**附件下载(`GET /v1/agents/{agent_code}/uploads/{upload_id}`)读文档内容失败也是一个独立的 `code`**——`UPLOAD_CONTENT_UNAVAILABLE`，与 `ARTIFACT_CONTENT_UNAVAILABLE` 同一类:

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "UPLOAD_CONTENT_UNAVAILABLE", "message": "upload content unavailable" } }
```

触发条件按附件类型分两种，两者共用同一个 `UPLOAD_CONTENT_UNAVAILABLE` 码、HTTP 状态码也一样，只能靠你自己知道传的是文档还是图片来区分根因:

- **文档**类附件:服务端工作区权限配置有问题(和上一段同一类根因)。
- **图片**类附件:对象存储读取失败(凭证 / bucket 策略 / 存储侧 5xx 等，除「对象不存在」以外的其它错误)。

图片分支完整的三种结果:

- 存储没配置 → 503 `UPLOAD_CONTENT_UNAVAILABLE`，见下一节 [8.14](#_8-14-503-——-服务不可用-两种含义不同)。
- 读取时对象已经不在了 → 404 `UPLOAD_NOT_FOUND`。
- 对象存储读取失败(其它错误)→ 500 `UPLOAD_CONTENT_UNAVAILABLE`，和文档分支的这个 500 是同一个码。

应对:不是退避重试能解决的，联系你的租户管理员。

## 8.13 502 —— 上传写入失败(上游错误)

只发生在上传接口(`POST /v1/agents/{agent_code}/uploads`)的文档分支，`error.code` 为 `UPLOAD_FAILED`(与上面 500 节的同名码同一次落盘失败的两种可能状态码，消息也相同):写入工作区时遇到沙箱侧的上游错误，不是权限配置问题。应对同上——不是退避重试能解决的，持续失败联系租户管理员。

## 8.14 503 —— 服务不可用，两种含义不同

**第一种，全站过载保护——任何端点都可能遇到，不只是上传接口**:服务端同时处理的请求数超过软上限时，新请求直接被挡在外面，`error.code` 为 `SERVER_OVERLOADED`，带 `Retry-After` 响应头:

```json [响应 503]
{ "success": false, "data": null, "error": { "code": "SERVER_OVERLOADED", "message": "Server is shedding load; retry after a moment." } }
```

应对:按 `Retry-After` 头退避重试——这是典型的"这会儿太忙，等等再试就好"场景。

**第二种，只发生在上传接口**(`POST /v1/agents/{agent_code}/uploads`)，`error.code` 为 `UPLOAD_UNAVAILABLE`，**没有 `Retry-After` 头**:

```json [响应 503]
{ "success": false, "data": null, "error": { "code": "UPLOAD_UNAVAILABLE", "message": "object store unavailable" } }
```

触发条件是服务端没有配置好对应的存储通路——图片走的对象存储、文档走的沙箱工作区，任一个没接好都会触发。这是部署 / 配置问题，不是你这边能解决的，联系你的租户管理员；重试没用。

**附件下载(`GET /v1/agents/{agent_code}/uploads/{upload_id}`)也有一个 503**，`error.code` 为 `UPLOAD_CONTENT_UNAVAILABLE`——和 [8.12](#_8-12-500-——-工作区服务端配置问题) 里那个 500 是同一个 `code`，靠 HTTP 状态码区分:

- **503**:服务端整体没有配置对应的存储通路(图片走对象存储、文档走工作区，任一个没接好都会触发)。
- **500**:通路配了但读不动——文档分支是权限问题，图片分支是对象存储读取失败(「对象不存在」那一种走 404，不在这两个状态码里)，完整对照见 [8.12](#_8-12-500-——-工作区服务端配置问题)。

两种都不是退避重试能解决的，都要联系租户管理员。

**产物下载(`GET /v1/agents/{agent_code}/artifacts/download`)也有一个 503**，`error.code` 是 `ARTIFACT_CONTENT_UNAVAILABLE`——和 [5.7 产物](./query#_5-7-产物) 里同名码的 500 形态是同一个 `code`，同样靠状态码区分:

- **503**:服务端整体没有配置工作区存储通路(部署问题)。
- **500**:配了但权限有问题(比如共享 uid 没配对，见 [8.12](#_8-12-500-——-工作区服务端配置问题))。

两种都不是退避重试能解决的，都要联系租户管理员。

**另外还有一种 503 没有 `error.code`**:发起 run 时，如果服务端的配额引擎本身不可用，响应是裸 `{"detail": "quota_engine_unavailable"}`，连 `{success, data, error}` 这个形状都不是(上传接口遇到同一种故障时会包成能读到 `error.code` 的 `UPLOAD_UNAVAILABLE`，不是这种裸形状)。概率很低，但要知道这种形状存在——别假设 503 一定能读到 `error.code`。

## 8.15 504 —— 请求超过了你自己设的截止时间

只有你自己在请求上带了 `X-Expert-Work-Deadline-Ms` 头(见 [7.3 公共请求头](./conventions#_7-3-公共请求头))，且这个时间戳已经过去，才会触发——服务端不会主动给你的请求安一个截止时间，不用这个头就不会遇到这个状态码。能读到 `error.code`:

```json [响应 504]
{ "success": false, "data": null, "error": { "code": "DEADLINE_EXCEEDED", "message": "X-Expert-Work-Deadline-Ms has already passed." } }
```

应对:检查这个头的取值是不是未来的 unix 毫秒时间戳；不需要端到端超时控制的话，别传这个头。

## 8.16 限流与配额的区别

限流(rate limit)按时间窗口限制"多快"，配额(quota)按资源维度限制"多少"，两者都是 429 但 `error.code` 不同——完整对照(含 `dimension` 字段、`Retry-After` 头、产物下载那个例外)见 [8.11 429](#_8-11-429-——-两种情况-含义不同)。
