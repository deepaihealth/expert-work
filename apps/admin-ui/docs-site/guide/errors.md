# 8 错误码总表

调用失败时，服务端返回 400 / 401 / 403 / 404 / 409 / 410 / 413 / 422 / 429 / 500 / 502 / 503 / 504 中的一个状态码。本章先给出全部错误码的速查表与两种响应格式，再按状态码逐节说明触发条件、响应内容与处理方式。

## 8.1 错误码速查表

「端点」列写明这个错误码只可能来自哪些接口；写「全部端点」的表示任何一条对外接口都可能返回。多数错误码链接到本章对应小节；取消 run 与审批决策两个端点的错误码详解在 [4 对话过程中的控制](./run-control)，产物与幂等相关的几个码分别链接到 [5.7 产物](./query#_5-7-产物) 与 [7.7 幂等性](./conventions#_7-7-幂等性)。

表中这几个端点名对应的路径如下，其余端点的路径写在下面各节里：

- 提前获取 session_id：`POST /v1/agents/{agent_code}/sessions`
- 会话列表：`GET /v1/agents/{agent_code}/sessions`
- 历史消息：`GET /v1/agents/{agent_code}/sessions/{session_id}/messages`
- 事件接口：`GET /v1/agents/{agent_code}/runs/{run_id}/events`，断线后的续传也走它，见 [3.6 断线重连](./sse-events#_3-6-断线重连与续传)
- 产物删除：`DELETE /v1/agents/{agent_code}/artifacts`

| 错误码 | HTTP 状态 | 端点 | 含义与处理 |
|---|---|---|---|
| [`WORKSPACE_FILE_FAILED`](#_8-3-400-路径或上传内容不合法) | 400 | 工作区文件下载 | 请求的工作区文件路径不合法。原样回传「列出工作区文件」接口返回的 `path`，不要自行拼接 |
| [`INVALID_UPLOAD`](#_8-3-400-路径或上传内容不合法) | 400 | 上传附件 | 文件类型不在允许列表内、文件为空，或没有文件名。检查文件本身后重传 |
| [`AUTH_MISSING_CREDENTIALS`](#_8-4-401-认证失败) | 401 | 全部端点 | 未携带 `Authorization` 头，或格式不正确。检查请求头 |
| [`AUTH_INVALID_TOKEN`](#_8-4-401-认证失败) | 401 | 全部端点 | key 不存在、已吊销，或轮换宽限期已过。更换一把有效的 key |
| [`AUTH_TOKEN_EXPIRED`](#_8-4-401-认证失败) | 401 | 全部端点 | key 本身有效但已过期。更换一把有效的 key |
| [`AUTH_UNAUTHENTICATED`](#_8-4-401-认证失败) | 401 | 全部端点 | 其余认证失败情况的默认错误码，很少出现。处理方式与上面三条相同 |
| [`FORBIDDEN`](#_8-5-403-权限不足或被阻断) | 403 | 全部端点 | key 的权限档位不足，响应只有 `detail` 字段。更换一把具备所需权限的 key |
| [`AGENT_DISABLED`](#_8-5-403-权限不足或被阻断) | 403 | 发起对话 / 提前获取 session_id / 上传附件 / 审批决策 | 这个 Agent 已被管理员下线。联系租户管理员 |
| [`TENANT_SUSPENDED`](#_8-5-403-权限不足或被阻断) | 403 | 全部端点 | 租户已被暂停。联系租户管理员 |
| [`AGENT_NOT_FOUND`](#_8-6-404-目标不存在) | 404 | 发起对话 / 提前获取 session_id / 上传附件 / 审批决策 | `agent_code` 在本租户下没有已发布的版本。核对 `agent_code` 的拼写与发布状态 |
| [`SESSION_NOT_FOUND`](#_8-6-404-目标不存在) | 404 | 发起对话 / 提前获取 session_id / 上传附件 / 历史消息 / 重命名会话 / 归档会话 / run 列表 | `session_id` 不存在，或不属于这个 `user_id` 与 `agent_code`。核对三者是否匹配 |
| [`UPLOAD_NOT_FOUND`](#_8-6-404-目标不存在) | 404 | 发起对话 / 附件下载 | 附件不存在、不属于这个 `user_id`、已被删除，或内容已被回收。核对 `upload_id` 与 `user_id`，图片还要核对 `session_id` |
| [`WORKSPACE_FILE_FAILED`](#_8-6-404-目标不存在) | 404 | 工作区文件下载 | `user_id` 未被识别，或该路径下没有文件。核对 `user_id` 与 `path` |
| [`RUN_NOT_FOUND`](./run-control#_4-1-取消-run) | 404 | 取消 run / 审批决策 / 事件接口 | `run_id` 不存在，或不属于这个 `user_id` 与 `agent_code`。核对三者是否匹配，不要当作「run 尚未创建」重试 |
| [`APPROVAL_NOT_FOUND`](./run-control#_4-2-审批决策) | 404 | 审批决策 | 这个 run 没有待审批记录。确认该 run 处于等待审批的状态 |
| [`ARTIFACT_NOT_FOUND`](./query#_5-7-产物) | 404 | 产物下载 / 产物删除 | 产物不存在、已删除，或不属于这个 `user_id`。核对 `user_id` 与产物 `name` |
| [`APPROVAL_CONFLICT`](./run-control#_4-2-审批决策) | 409 | 审批决策 | 这条审批已经被决定过。不要重复决策；需要取回上次结果时，带上当时用的 `idempotency_key` |
| [`SESSION_NOT_BOUND`](./run-control#_4-2-审批决策) | 409 | 审批决策 | run 所在会话没有绑定 Agent。联系租户管理员 |
| [`AGENT_DELETED`](./run-control#_4-2-审批决策) | 410 | 审批决策 | 会话绑定的 Agent 已被删除，不可恢复。改用其它 `agent_code` |
| [`UPLOAD_TOO_LARGE`](#_8-9-413-附件超过大小上限) | 413 | 上传附件 | 文档或图片超过大小上限。压缩、裁剪或拆分后重传 |
| [`INVALID_REQUEST`](#_8-10-422-请求参数不合法) | 422 | 全部端点 | 请求体字段或查询参数未通过基础校验。检查字段的类型、长度与取值范围 |
| [`INVALID_USER_ID`](#_8-10-422-请求参数不合法) | 422 | 要求 `user_id` 的端点 | `user_id` 去掉首尾空白后是空字符串。传一个非空白的 `user_id` |
| [`INVALID_UPLOAD_ID`](#_8-10-422-请求参数不合法) | 422 | 发起对话 / 附件下载 | `upload_id` 不是 `upl_<uuid>` 这个形式。原样回传上传接口返回的 `data.upload_id` |
| [`INVALID_IDEMPOTENCY_KEY`](./conventions#_7-7-幂等性) | 422 | 发起对话 | `Idempotency-Key` 去掉空白后为空，或超过 255 字符。检查该请求头的取值 |
| [`IDEMPOTENCY_KEY_REUSED`](./conventions#_7-7-幂等性) | 422 | 发起对话 | 同一个 `Idempotency-Key` 配了不同的请求体，或配给了不同的 `agent_code`。换一个新的取值，不要复用 |
| [`TOO_MANY_INPUT_KEYS`](#_8-10-422-请求参数不合法) | 422 | 发起对话 | `inputs` 的键数量超过 64。精简 `inputs` |
| [`INPUT_VALUE_TOO_LONG`](#_8-10-422-请求参数不合法) | 422 | 发起对话 | `inputs` 里某个字符串值超过 8192 字符。精简该值 |
| [`TOO_MANY_INPUT_BYTES`](#_8-10-422-请求参数不合法) | 422 | 发起对话 | `inputs` 序列化后的总字节数超过 65536。精简 `inputs` 整体 |
| [`UNTRUSTED_CONTENT_BLOCK_TOO_LONG`](#_8-10-422-请求参数不合法) | 422 | 发起对话 | `untrusted_content` 里某一块超过 8192 字符。拆成多个数组元素 |
| [`INVALID_TITLE`](#_8-10-422-请求参数不合法) | 422 | 重命名会话 | 会话标题去掉首尾空白后是空字符串。传一个非空标题 |
| [`INVALID_ARTIFACT_NAME`](./query#_5-7-产物) | 422 | 产物下载 / 产物删除 | 产物 `name` 含 NUL 字节。原样回传产物列表接口返回的 `name`，不要自行拼接 |
| [`AGENT_BUILD_FAILED`](#_8-10-422-请求参数不合法) | 422 | 发起对话 / 审批决策 | Agent 的配置构建失败，两个端点上含义相同。属于服务端配置问题，联系租户管理员 |
| [`RATE_LIMIT_EXCEEDED`](#_8-11-429-请求过于频繁或配额用尽) | 429 | 全部端点；另有按维度配置的配额 | 调用频率超限，或按次数、按字节计的配额用尽。先读 `error.dimension`：频率类按 `Retry-After` 退避后重试，配额类短退避无效 |
| [`QUOTA_EXCEEDED`](#_8-11-429-请求过于频繁或配额用尽) | 429 | 上传附件（文档） | 这个终端用户的工作区容量已满。退避重试无效，处理方式见 8.11 |
| [`WORKSPACE_LIST_FAILED`](#_8-12-500-服务端内部错误) | 500 | 工作区文件列表 | 服务端的工作区存储配置有问题。重试无效，联系租户管理员 |
| [`WORKSPACE_FILE_FAILED`](#_8-12-500-服务端内部错误) | 500 | 工作区文件下载 | 服务端的工作区存储配置有问题。重试无效，联系租户管理员 |
| [`UPLOAD_FAILED`](#_8-12-500-服务端内部错误) | 500 | 上传附件（文档） | 保存文件失败，原因是服务端的工作区权限配置有问题。重试无效，联系租户管理员 |
| [`UPLOAD_CONTENT_UNAVAILABLE`](#_8-12-500-服务端内部错误) | 500 | 附件下载 | 附件的记录还在，但服务端读不到它的内容。重试无效，联系租户管理员 |
| [`ARTIFACT_CONTENT_UNAVAILABLE`](#_8-12-500-服务端内部错误) | 500 | 产物下载 | 产物的记录还在，但服务端读不到它的内容。重试无效，联系租户管理员 |
| [`UPLOAD_FAILED`](#_8-13-502-上传文件时遇到上游错误) | 502 | 上传附件（文档） | 保存文件时遇到上游存储服务的错误。重试无效，联系租户管理员 |
| [`UPLOAD_UNAVAILABLE`](#_8-14-503-服务不可用) | 503 | 上传附件 | 服务端没有配置对应的存储。重试无效，联系租户管理员 |
| [`UPLOAD_CONTENT_UNAVAILABLE`](#_8-14-503-服务不可用) | 503 | 附件下载 | 服务端没有配置对应的存储，与上面 500 是同一个错误码，靠状态码区分。重试无效，联系租户管理员 |
| [`ARTIFACT_CONTENT_UNAVAILABLE`](#_8-14-503-服务不可用) | 503 | 产物下载 | 服务端没有配置工作区存储，与上面 500 是同一个错误码，靠状态码区分。重试无效，联系租户管理员 |
| [`SERVER_OVERLOADED`](#_8-14-503-服务不可用) | 503 | 全部端点 | 服务端整体过载，本次请求被拒绝。按 `Retry-After` 头退避重试 |
| [`AUTH_BACKEND_UNAVAILABLE`](#_8-14-503-服务不可用) | 503 | 全部端点 | 认证服务不可用，本次请求无法校验，很少出现。稍后重试，持续失败则联系租户管理员 |
| [`DEADLINE_EXCEEDED`](#_8-15-504-请求超过截止时间) | 504 | 全部端点，仅在请求带了 `X-Expert-Work-Deadline-Ms` 时 | 请求携带的截止时间已经过去。检查该头的取值，或者不传这个头 |
| `UPLOAD_ERROR` | 与原始失败一致 | 上传附件 | 上传接口内部的失败没有对应专用错误码时的默认取值，很少出现。按 HTTP 状态码处理 |
| `APPROVAL_ERROR` | 与原始失败一致 | 审批决策 | 审批决策端点内部的失败没有对应专用错误码时的默认取值，很少出现。按 HTTP 状态码处理 |

这张表覆盖的是带 `error.code` 的失败，**不是全部失败的穷尽清单**。表外还有三类：

- 读不到 `error.code` 的失败：只有一个 `detail` 字段，例如权限不足的 403、`inputs` 模板变量校验失败的 422，以及「这个 Agent 不支持图片输入」「单次 run 的图片数超过上限」（都是 422）和配额服务不可用（503）。格式说明见 [8.2](#_8-2-错误响应的两种格式)，具体条目分散在下面各节。
- 响应体不是 JSON 的失败：服务端遇到未预期内部错误时的 500，响应体是纯文本。任何端点都可能出现，见 [8.12](#_8-12-500-服务端内部错误)。
- 没有列进这张表的少数错误码：解析响应时不要假设表里没有的取值就不会出现，遇到不认识的 `error.code` 按 HTTP 状态码处理即可。

## 8.2 错误响应的两种格式

服务端的失败响应有两种格式，客户端在写解析逻辑时需要同时兼容。

大多数失败使用标准格式，能读到 `error.code`：

```json [标准格式]
{
  "success": false,
  "data": null,
  "error": { "code": "AGENT_NOT_FOUND", "message": "..." }
}
```

少数失败使用简易格式，只有一个 `detail` 字段，读不到 `error.code`。`detail` 有时是字符串，有时是 `{"code": ..., "message": ...}` 这样的对象：

```json [简易格式]
{ "detail": "..." }
```

返回简易格式的只有以下几处，其余对外端点的失败都是标准格式：

- 权限检查不通过的 403，全部端点通用。
- 发起对话端点的三条校验，都是 422：「这个 Agent 不支持图片输入」「单次 run 的图片数超过上限」，以及 `inputs` 模板变量与 Agent 的声明不匹配。
- 发起对话与产物下载时配额服务不可用，503。

请求体与查询参数的格式校验失败是统一处理的，固定为标准格式的 422 `INVALID_REQUEST`。

此外有一种失败完全不是 JSON：服务端遇到未预期内部错误时的 500，响应体是纯文本 `Internal Server Error`。触发条件与处理方式见 [8.12](#_8-12-500-服务端内部错误)。

**解析顺序：先按 HTTP 状态码分类，响应体里有 `error.code` 就用它，没有再读 `detail`，连 JSON 都解析不出的按状态码处理（只有 500 会这样）。** 下面每一节都会写明该节的失败属于哪一种格式。

## 8.3 400 路径或上传内容不合法

工作区文件下载（`GET /v1/agents/{agent_code}/workspace/file`）在 `path` 查询参数不合法时返回 400，错误码固定为 `WORKSPACE_FILE_FAILED`，标准格式：

```json [响应 400]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

以下几种 `path` 会触发：绝对路径（以 `/` 开头）、含 `..` 段、含 NUL 字节（`\x00`）、空字符串，或去掉首尾空白后为空。

处理方式：不要自行拼接路径，把 `GET .../workspace/files` 返回的 `path` 字段原样回传即可，细节见 [5.6 工作区文件](./query#_5-6-工作区文件)。

另一种 400 只出现在上传附件（`POST /v1/agents/{agent_code}/uploads`），错误码为 `INVALID_UPLOAD`，同样是标准格式，三种触发方式：

- 声明的 `Content-Type` 不在允许列表内。判断依据是 `Content-Type` 而不是文件名后缀，完整清单见 [2.6 允许的文件类型](./chat#允许的文件类型)。
- 文件内容为空。
- 没有文件名。

处理方式：三种都是检查文件本身之后重传，不需要改调用方式。

## 8.4 401 认证失败

任何端点在认证失败时都返回 401，标准格式，并带 `WWW-Authenticate: Bearer realm="expert-work"` 响应头：

```json [响应 401]
{ "success": false, "data": null, "error": { "code": "AUTH_INVALID_TOKEN", "message": "Invalid or unrecognised token" } }
```

`error.code` 区分三种具体情况：

| 错误码 | 触发条件 |
|---|---|
| `AUTH_MISSING_CREDENTIALS` | 请求没有 `Authorization` 头，或者头的格式不正确 |
| `AUTH_INVALID_TOKEN` | key 的格式正确但校验不通过，包括不存在、已被吊销、轮换后的宽限期已过 |
| `AUTH_TOKEN_EXPIRED` | key 有效，但 `expires_at` 已经过去 |

其余认证失败情况返回 `AUTH_UNAUTHENTICATED`，处理方式与上面三种相同。

处理方式：确认 key 是否复制完整、是否过期、是否已被吊销，必要时更换一把新 key，见 [6 认证与 Key](./auth)。

## 8.5 403 权限不足或被阻断

权限档位不足是最常见的 403，例如用只有 `read` 档位的 key 调用 `POST /v1/agents/{agent_code}/runs`（该接口要求 `write`）。这类 403 是简易格式，错误码在 `detail.code` 里：

```json [响应 403]
{ "detail": { "code": "FORBIDDEN", "message": "principal lacks required role" } }
```

处理方式：更换一把带 `write` 档位的 key，见 [6.3 权限档位](./auth#_6-3-权限档位)。

另有两种 403 是标准格式，与权限档位无关：这个 Agent 被管理员下线（`AGENT_DISABLED`），或者租户被暂停（`TENANT_SUSPENDED`）。这两种换 key 无法解决，需要联系租户管理员。

## 8.6 404 目标不存在

请求指向的对象不存在、或不属于当前调用方时返回 404，标准格式：

```json [响应 404]
{ "success": false, "data": null, "error": { "code": "AGENT_NOT_FOUND", "message": "no active agent 'xxx' for this tenant" } }
```

服务端不区分「对象不存在」与「对象存在但不属于这个调用方」，两种情况返回同一个 404。这是为了不泄露其它租户或其它终端用户的对象是否存在，不是缺陷。各个错误码的触发条件如下。

`AGENT_NOT_FOUND`：`{agent_code}` 在本租户下没有已发布的版本，包括这个名字从未创建过、创建了但尚未发布、已经下线三种情况。

`SESSION_NOT_FOUND`：`session_id` 不存在，或者它不属于请求里的 `user_id` 与 `agent_code` 组合。历史消息、重命名会话（`PATCH .../sessions/{session_id}`）、归档会话（`DELETE .../sessions/{session_id}`）、run 列表（`GET .../runs?session_id=`）四个接口适用同一条规则。

`UPLOAD_NOT_FOUND`：附件没有找到。两个入口会返回它——发起对话请求体 `files[]` 里的 `upload_id`，以及附件下载接口 `GET /v1/agents/{agent_code}/uploads/{upload_id}` 的路径参数。具体触发条件按入口有区别：

- 两个入口都会触发：`upload_id` 查不到；查到了但不属于这个 `user_id`；附件已被删除。
- 只有发起对话会触发：`files[]` 里的图片还要求属于本次请求最终绑定的那段会话（`session_id`），不属于同样返回这个 404。附件下载接口没有 `session_id` 参数，不做这项检查；文档类附件也没有这条限制。
- 只有附件下载会触发：附件的记录还在、也确实属于这个 `user_id`，但内容已经被回收，`error.message` 为 `"upload content not found"`，错误码与状态码不变。

::: warning 图片附件与 run 必须绑定同一段会话
上传图片时若没有传 `session_id`，上传接口会自动创建一段新会话并把图片绑定到它；随后发起对话时若也没有传 `session_id`，这次请求又会创建另一段新会话，两者不是同一段，结果是 404。

上传之后若要立刻发起带这份附件的对话，把上传响应里的 `session_id` 原样带进发起对话的请求体。完整流程见 [2.6 带图片和文档](./chat#_2-6-带图片和文档)。
:::

`WORKSPACE_FILE_FAILED`：工作区文件下载（`GET /v1/agents/{agent_code}/workspace/file`）时 `user_id` 未被识别，或者 `path` 指向的文件不存在。两种情况返回同一个 404，无法从响应里区分。同一个错误码在 400 与 500 下另有含义，靠 HTTP 状态码区分，见 [8.3](#_8-3-400-路径或上传内容不合法) 与 [8.12](#_8-12-500-服务端内部错误)。

`RUN_NOT_FOUND` 与 `APPROVAL_NOT_FOUND`：取消 run（`:cancel`）与审批决策（`:decide`）两个端点的归属校验与审批查找失败。这两个端点还有各自特有的 409 / 403 / 410 / 422，完整说明见 [4 对话过程中的控制](./run-control)。

## 8.7 409 审批冲突

只有审批决策端点（`POST /v1/agents/{agent_code}/runs/{run_id}:decide`）会返回 409，标准格式，两个错误码：`APPROVAL_CONFLICT` 表示这条审批已经被决定过，包括重复提交和并发提交中落败的一方；`SESSION_NOT_BOUND` 表示这个 run 所在的会话没有绑定 Agent。

处理方式：`APPROVAL_CONFLICT` 不要重复提交决策，`SESSION_NOT_BOUND` 联系租户管理员。两个错误码的完整触发条件见 [4.2 审批决策](./run-control#_4-2-审批决策)。

## 8.8 410 Agent 已被删除

只有审批决策端点在批准后继续执行 run 时会返回 410，错误码为 `AGENT_DELETED`，标准格式：这个 run 所在会话绑定的 Agent 版本已被删除，不可恢复。

处理方式：该 run 无法继续，改用其它 `agent_code` 重新发起对话。详情见 [4.2 审批决策](./run-control#_4-2-审批决策)。

## 8.9 413 附件超过大小上限

只有上传附件（`POST /v1/agents/{agent_code}/uploads`）会返回 413，标准格式。发起对话接口不会返回 413，因为它收到的是 `files[]` 里的 `upload_id` 引用，不是文件本身。

```json [响应 413]
{ "success": false, "data": null, "error": { "code": "UPLOAD_TOO_LARGE", "message": "document exceeds 26214400-byte limit" } }
```

默认上限：文档 25 MiB，图片 10 MiB，实际取值以部署配置为准。

处理方式：压缩或裁剪后重传，大文档也可以拆分成多份分别上传。

## 8.10 422 请求参数不合法

发起对话（`POST /v1/agents/{agent_code}/runs`）的 422 分两类，格式不同；本节最后另有三种独立的 422。下面表中的个别错误码同样适用于其它端点，已在触发条件里标明。

### 请求体字段没通过校验

例如 `files[]` 条目里多传了 `upload_id` 以外的字段（这个请求体不接受未声明的字段）、`upload_id` 是空字符串、`files[]` 或 `untrusted_content` 超过各自的条数上限。这一类是标准格式，错误码固定为 `INVALID_REQUEST`：

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "INVALID_REQUEST", "message": "Extra inputs are not permitted" } }
```

同一类里还有八个更具体的错误码，同样是标准格式：

| 错误码 | 触发条件 |
|---|---|
| `INVALID_UPLOAD_ID` | `files[]` 里某一项的 `upload_id` 不是 `upl_<uuid>` 这个形式。附件下载端点路径里的 `{upload_id}` 形式不对时也返回这个错误码 |
| `INVALID_IDEMPOTENCY_KEY` | `Idempotency-Key` 头去掉空白后是空字符串，或超过 255 字符 |
| `IDEMPOTENCY_KEY_REUSED` | 同一个 `Idempotency-Key` 配了不同的请求体，或者配给了不同的 `agent_code` |
| `TOO_MANY_INPUT_KEYS` | `inputs` 的键数量超过 64，正好 64 个合法 |
| `INPUT_VALUE_TOO_LONG` | `inputs` 里某个字符串值超过 8192 字符，正好 8192 字符合法。只检查字符串类型的值 |
| `TOO_MANY_INPUT_BYTES` | `inputs` 序列化后的总字节数超过 65536，正好 65536 字节合法。字节数按 UTF-8 编码计算，不是字符数 |
| `UNTRUSTED_CONTENT_BLOCK_TOO_LONG` | `untrusted_content` 里某一块超过 8192 字符，正好 8192 字符合法 |
| `INVALID_USER_ID` | `user_id` 去掉首尾空白后是空字符串 |

三条补充说明：

- `inputs` 的三条上限（键数量、单值长度、序列化后总字节数）互相独立，不能互相替代。把一个长字符串包进数组或对象可以绕开单值长度检查，但总字节数上限仍然会拦下它。
- `untrusted_content` 的单块字符数上限与最多 16 项的条数上限，同样是两条互相独立的限制。
- `INVALID_USER_ID` 不止发起对话会触发，凡是要求 `user_id` 的端点都会：提前获取 session_id、会话列表与历史消息、重命名会话、归档会话、上传附件、附件下载、工作区文件读取。取消 run 与审批决策是例外，空 `user_id` 在这两个端点上不会走到这条校验，而是被 `run_id` 的归属校验统一处理成 404 `RUN_NOT_FOUND`，见 [4 对话过程中的控制](./run-control)。

### 模板变量与 Agent 的声明不匹配

这一类是简易格式，只有一个 `detail` 字符串，读不到 `error.code`：

```json [响应 422]
{ "detail": "unknown input variable: foo" }
```

三种情况都是这个格式：Agent 没有声明模板变量却传了非空 `inputs`；`inputs` 里有 Agent 未声明过的键；Agent 声明的必填变量在 `inputs` 里没有给。

`inputs` 本身的三条上限不属于这一类，它们和上一小节一样能读到 `error.code`。模板变量的细节见 [2.7 外部内容与模板变量](./chat#_2-7-外部内容与模板变量)。

### 另外三种独立的 422

Agent 配置构建失败：错误码为 `AGENT_BUILD_FAILED`，标准格式。命中的已发布 Agent 因服务端配置问题（例如引用了不存在的模型或工具）无法构建，发起对话、以及审批决策后继续执行 run 时都可能遇到。处理方式：联系租户管理员。

图片相关的两条校验：都是简易格式，读不到 `error.code`。

- `{"detail": "too many images: max 8 per run"}`——单次 run 实际处理的图片数超过上限，默认 8 张，可由部署配置调整。这条与 `files[]` 最多 64 项是两条独立的校验，后者是请求体字段层面的合计上限（图片与文档一起计数），超出时返回上面的 `INVALID_REQUEST`。
- `{"detail": "agent does not accept image input: ..."}`——这个 Agent 没有开启图片能力，`files[]` 里只要带图片条目就会触发，与数量无关。

会话标题为空：重命名会话（`PATCH /v1/agents/{agent_code}/sessions/{session_id}`）在 `title` 去掉首尾空白后为空字符串时返回 `INVALID_TITLE`，标准格式：

```json [响应 422]
{ "success": false, "data": null, "error": { "code": "INVALID_TITLE", "message": "title must not be empty" } }
```

`title` 完全不传、或者传字面上的空字符串 `""`，走的是请求体字段校验（`title` 要求至少 1 个字符），返回 422 `INVALID_REQUEST`，不是这个错误码。区别在于有没有先经过服务端的空白裁剪。

## 8.11 429 请求过于频繁或配额用尽

429 表示调用频率超限，或者某项配额用尽。错误码只有两个：`RATE_LIMIT_EXCEEDED`（频率限制，以及按次数、按字节计的配额）和 `QUOTA_EXCEEDED`（只在上传文档时工作区容量已满）。两者都是标准格式；先读 `error.code`，再按下文处理。

### RATE_LIMIT_EXCEEDED

响应带 `Retry-After` 头，`error` 里多两个字段：`dimension`（哪一项限制被触发）和 `retry_after_s`（与 `Retry-After` 头同值，单位秒）。

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

`dimension` 的取值和对应的处理方式：

| `dimension` | 含义 | 处理方式 |
|---|---|---|
| 字段不存在 | 网关按来源 IP 的频率限制 | 按 `Retry-After` 退避后重试 |
| `tenant` | 租户整体的调用频率限制 | 按 `Retry-After` 退避后重试 |
| `qps` | 租户或某个 Agent 的每秒调用次数限制 | 按 `Retry-After` 退避后重试 |
| `image_upload_count_30d` | 30 天内的图片上传次数配额 | 短退避重试无效。按「图片上传配额已用尽」处理，联系租户管理员 |
| `artifact_download_count_30d` | 30 天内的产物下载次数配额 | 短退避重试无效。按「产物下载配额已用尽」处理，联系租户管理员 |
| `image_storage_bytes` | 图片存储总字节数配额 | 重试无效。这项配额不随时间回补，联系租户管理员调整 |
| `artifact_storage_bytes` | 产物存储总字节数配额 | 重试无效。同上 |

频率类（前三行）的 `Retry-After` 通常是几秒。按次数计的 30 天配额（中间两行）也带 `Retry-After`，但它的值是下一次配额回补的时间，可能长达数小时；按字节计的配额（最后两行）不会随时间回补。**不要对所有 `RATE_LIMIT_EXCEEDED` 都按 `Retry-After` 循环重试**，先读 `dimension`。解析时给 `dimension` 留缺省值：网关这一层的 429 没有这个字段。

哪些接口会遇到：调用频率限制作用于全部接口。按次数与按字节计的配额由租户管理员按维度配置；配置成租户级时，发起对话、上传图片、产物下载都可能因为同一个维度被拒。因此按 `dimension` 判断处理方式，不要按接口反推。

### QUOTA_EXCEEDED

只出现在上传附件（文档）时，含义是这个终端用户的工作区容量已满。不带 `Retry-After` 头，因为退避重试解决不了：

```json [响应 429]
{
  "success": false,
  "data": null,
  "error": { "code": "QUOTA_EXCEEDED", "message": "workspace is full — delete files to free space" }
}
```

处理方式：对外 API 没有删除工作区文件的接口（[5.6 工作区文件](./query#_5-6-工作区文件) 只提供列表与下载），请联系租户管理员清理这个终端用户的工作区或调整容量，之后再重试上传。

产物一侧的说明见 [5.7 产物](./query#_5-7-产物)。

## 8.12 500 服务端内部错误

500 有两类。一类带专用错误码，标准格式，含义是服务端的工作区存储配置有问题，只出现在工作区与附件、产物相关的接口上；另一类读不到任何错误码，响应体也不是 JSON，是服务端遇到的未预期内部错误，任何端点都可能出现。先看响应体能否解析成 JSON，再按对应小节处理。

### 存储配置问题

工作区文件列表（`GET /v1/agents/{agent_code}/workspace/files`）、工作区文件下载（`GET .../workspace/file`）、产物下载（`GET .../artifacts/download`）在服务端存储配置有问题时返回 500，标准格式，三个错误码：

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_LIST_FAILED", "message": "workspace listing unavailable" } }
```

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "workspace file unavailable" } }
```

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "ARTIFACT_CONTENT_UNAVAILABLE", "message": "artifact content unavailable" } }
```

触发条件是服务端工作区存储的权限配置有问题，既不是请求本身有问题，也不表示「这个终端用户或这个文件不存在」。服务端在这种情况下不会降级成空列表或 404，以免把配置问题伪装成正常的业务响应。

处理方式：退避重试无效，请联系租户管理员核实工作区存储配置。

上传附件的文档分支保存失败是另一个独立的错误码 `UPLOAD_FAILED`，标准格式：

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "UPLOAD_FAILED", "message": "workspace write failed" } }
```

这个错误码在 500 与 502 下的 `error.message` 完全相同，只能靠 HTTP 状态码区分：500 表示服务端工作区权限配置有问题，502 表示保存时遇到上游存储服务的错误（见 [8.13](#_8-13-502-上传文件时遇到上游错误)）。两种的处理方式一致：重试无效，持续失败时联系租户管理员。

附件下载（`GET /v1/agents/{agent_code}/uploads/{upload_id}`）读不到内容时返回 `UPLOAD_CONTENT_UNAVAILABLE`，标准格式：

```json [响应 500]
{ "success": false, "data": null, "error": { "code": "UPLOAD_CONTENT_UNAVAILABLE", "message": "upload content unavailable" } }
```

文档与图片共用这一个错误码，状态码也相同，只能由调用方根据自己上传的是哪一类来判断原因：文档是服务端工作区权限配置有问题，图片是服务端读取图片存储失败。

图片类附件完整的三种结果：服务端没有配置图片存储返回 503 `UPLOAD_CONTENT_UNAVAILABLE`（见 [8.14](#_8-14-503-服务不可用)）；读取时内容已经不在了返回 404 `UPLOAD_NOT_FOUND`；其它读取失败返回 500 `UPLOAD_CONTENT_UNAVAILABLE`，与文档类附件的 500 是同一个错误码。

处理方式：退避重试无效，请联系租户管理员。

### 未预期的内部错误

任何端点都可能在服务端内部出错时返回 500。这种响应读不到任何错误码，响应体是纯文本，不是 JSON：

```txt [响应 500]
Internal Server Error
```

触发原因在服务端一侧，与请求本身无关。既可能是瞬时故障，稍后同样的请求就能成功；也可能是持续故障，例如 Agent 引用的外部 MCP 服务器连接中断，恢复前每次发起对话都会失败。

处理方式：间隔几秒重试一次；仍然失败就停止重试，记录发生时间、请求的端点路径与 `agent_code`，联系租户管理员排查服务端日志。

## 8.13 502 上传文件时遇到上游错误

只有上传附件（`POST /v1/agents/{agent_code}/uploads`）的文档分支会返回 502，错误码为 `UPLOAD_FAILED`，标准格式：保存文件到工作区时遇到上游存储服务的错误，不是权限配置问题。它的错误码和 `error.message` 与 [8.12](#_8-12-500-服务端内部错误) 里的 500 完全相同，只能靠 HTTP 状态码区分。

处理方式：重试无效，持续失败时联系租户管理员。

## 8.14 503 服务不可用

503 的含义有多种，靠 `error.code` 区分。

第一种是服务端整体过载（`SERVER_OVERLOADED`），任何端点都可能返回，标准格式，带 `Retry-After` 响应头：

```json [响应 503]
{ "success": false, "data": null, "error": { "code": "SERVER_OVERLOADED", "message": "Server is shedding load; retry after a moment." } }
```

处理方式：按 `Retry-After` 头退避重试。

第二种只出现在上传附件（`POST /v1/agents/{agent_code}/uploads`），错误码为 `UPLOAD_UNAVAILABLE`，标准格式，没有 `Retry-After` 头：

```json [响应 503]
{ "success": false, "data": null, "error": { "code": "UPLOAD_UNAVAILABLE", "message": "object store unavailable" } }
```

触发条件是服务端没有配置对应的存储——图片使用的图片存储、文档使用的工作区存储，任意一处没有配置好都会触发。处理方式：重试无效，联系租户管理员。

第三种出现在附件下载（`GET /v1/agents/{agent_code}/uploads/{upload_id}`），错误码为 `UPLOAD_CONTENT_UNAVAILABLE`，与 [8.12](#_8-12-500-服务端内部错误) 里的 500 是同一个错误码，靠状态码区分：503 表示服务端整体没有配置对应的存储，500 表示存储配了但读不到内容。

第四种出现在产物下载（`GET /v1/agents/{agent_code}/artifacts/download`），错误码为 `ARTIFACT_CONTENT_UNAVAILABLE`，同样与 500 共用一个错误码：503 表示服务端没有配置工作区存储，500 表示配了但权限有问题。第三种与第四种的处理方式相同：重试无效，联系租户管理员。产物下载在配额服务不可用时另有一个只有 `detail` 字段的 503，见 [8.2](#_8-2-错误响应的两种格式)。

第五种是认证服务不可用（`AUTH_BACKEND_UNAVAILABLE`），任何端点都可能返回，标准格式：本次请求无法完成认证校验。这种情况很少出现，处理方式是稍后重试，持续失败时联系租户管理员。

还有一种 503 读不到 `error.code`：发起对话时若服务端的配额服务不可用，响应是简易格式的 `{"detail": "quota_engine_unavailable"}`。上传附件遇到同一种故障时返回的是标准格式的 `UPLOAD_UNAVAILABLE`，两者形式不同。这种情况很少出现，但解析时不要假设 503 一定能读到 `error.code`。

## 8.15 504 请求超过截止时间

只有当请求带了 `X-Expert-Work-Deadline-Ms` 头（见 [7.3 公共请求头](./conventions#_7-3-公共请求头)）、且这个时间戳已经过去时，才会返回 504，标准格式。服务端不会主动给请求设置截止时间，不使用这个头就不会遇到这个状态码：

```json [响应 504]
{ "success": false, "data": null, "error": { "code": "DEADLINE_EXCEEDED", "message": "X-Expert-Work-Deadline-Ms has already passed." } }
```

处理方式：确认这个头的取值是一个未来的 unix 毫秒时间戳；不需要端到端超时控制时，不要传这个头。
