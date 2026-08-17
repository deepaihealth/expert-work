# 5 查询与管理

本章的接口用来读取和管理已经产生的数据：租户里可以调用的 Agent、某个终端用户的会话与历史消息、跑过的 run、Agent 产出的工作区文件与产物，以及会话的重命名与归档。

除各小节另有说明外，本章接口要求 key 带 `read` 权限（`write` 权限的 key 同时含读，可以直接使用），并且查询参数 `user_id` 必填。例外有两类：5.1 Agent 目录是租户级接口，不接受 `user_id`；5.5 的重命名与归档、5.7 的删除产物是写操作，要求 `write` 权限。

### 分页

支持分页的接口用两个查询参数控制：`limit` 取 1–200，默认 50；`offset` 取 0 及以上的整数，默认 0。传入范围外的值（例如 `limit=0`、`limit=201`、`offset=-1`）返回 422 `INVALID_REQUEST`。

[5.6 工作区文件](#_5-6-工作区文件) 的列出文件与 [5.7 产物](#_5-7-产物) 的列出产物不支持分页：这两个接口没有 `limit` 与 `offset` 参数，一次返回全部条目，传了也会被忽略。

判断是否已经翻到最后一页，两类接口的方法不同：

| 接口 | 判断方法 |
|---|---|
| 5.1 Agent 目录 | 响应里有 `total`。`offset` 加上这一页的条目数小于 `total` 时还有下一页 |
| 5.2 会话列表 / 5.3 历史消息 / 5.4 run 列表 | 响应里没有 `total`，也没有 `has_more`，只回显请求里的 `limit` 与 `offset`。这一页的条目数等于 `limit` 时可能还有下一页，小于 `limit` 时已经翻完 |

翻下一页时，把 `offset` 加上这一页的条目数。**Agent 目录不适用「条目数小于 `limit` 就是最后一页」这条规则**：同一个 `agent_code` 可能存在多个版本，目录按 `agent_code` 去重后返回，去重会让某一页天然短于 `limit`。

## 5.1 Agent 目录

列出当前租户里可以调用的 Agent。接入的第一步通常是调用它取得 `agent_code`，而不是把 Agent 的名字写死在客户端里。

这个接口是租户级的：路径里没有 `{agent_code}`，也不接受 `user_id`，目录与具体哪个终端用户无关。

### 请求

``` [端点]
GET /v1/agent-catalog
```

两个参数都在查询字符串里，都是可选的。

| 参数 | 必填 | 说明 |
|---|---|---|
| `limit` | 否 | 分页大小。取 1–200，默认 50 |
| `offset` | 否 | 分页偏移。取 0 及以上的整数，默认 0 |

### 响应

`data.agents` 是 Agent 条目的数组，每个条目有四个字段。

| 字段 | 类型 | 说明 |
|---|---|---|
| `agent_code` | string | 租户内唯一的 Agent 标识。发起对话时把它填进 `POST /v1/agents/{agent_code}/runs` 的路径 |
| `display_name` | string | 展示名，非空。没有配置展示名的 Agent 返回 `agent_code` 本身，客户端不需要自己判空 |
| `description` | string | Agent 的说明文本，由租户管理员填写，可能是空串 |
| `available` | boolean | 取值：`true`（管理员没有下线这个 Agent）/ `false`（管理员已下线，向它发起对话会返回 403 `AGENT_DISABLED`） |

`data` 里除 `agents` 外还有 `limit`、`offset` 与 `total`。`total` 是去重后的 Agent 总数，不是当前页的条目数，也不是版本记录数。

条目里没有 `status`、`mode` 或版本号字段。一个 Agent 只要有一个可用版本就会出现在列表里，是否上线由此表达；它当前能不能调用，只看 `available`。

### 示例

```bash [请求]
curl "https://<your-domain>/v1/agent-catalog?limit=50&offset=0" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{
  "success": true,
  "data": {
    "agents": [
      {
        "agent_code": "report-writer",
        "display_name": "报表助手",
        "description": "生成周报",
        "available": true
      }
    ],
    "limit": 50,
    "offset": 0,
    "total": 1
  },
  "error": null
}
```

### available 的含义

`available` 只表示一件事：管理员有没有把这个 Agent 下线。配额耗尽、租户被暂停、Agent 配置构建失败都不会让 `available` 变成 `false`，但都会让发起对话失败，各有自己的错误码，见 [8 错误码总表](./errors)。

### available 的时效

目录里的 `available` 是实时读取的，而发起对话时的下线检查会经过一层短缓存（默认约 5 秒，平台可配）。缓存过期之前，两个方向都可能出现不一致：

| 管理员刚做的操作 | 缓存过期前可能出现的现象 |
|---|---|
| 重新启用某个 Agent | 目录已显示 `available: true`，发起对话仍被拒绝 |
| 下线某个 Agent | 目录已显示 `available: false`，发起对话仍被接受 |

两个方向的偏差都有上限，也都会自行恢复。**不要把目录里的 `available` 当作发起对话前的前置校验**：按需要发起，再按返回的错误码处理。

### 目录里不会出现的 Agent

`available: false` 的 Agent 仍然出现在列表里，只是标记为不可用，客户端界面上置灰即可。下线是可逆的管理动作，Agent 随时可能恢复。

**只剩已废弃版本、没有任何可用版本的 Agent 不会出现在目录里。** 这种情况不会自行恢复，列出一个永远 `false` 的条目对客户端没有意义。

## 5.2 会话列表

列出某个终端用户在这个 Agent 下的会话。

### 请求

``` [端点]
GET /v1/agents/{agent_code}/sessions
```

`agent_code` 在路径里，其余参数在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `agent_code` | 是 | 要查询的 Agent 标识 |
| `user_id` | 是 | 只列出这个终端用户的会话，长度 1–255 字符 |
| `limit` | 否 | 分页大小。取 1–200，默认 50 |
| `offset` | 否 | 分页偏移。取 0 及以上的整数，默认 0 |

### 响应

`data.sessions` 是会话条目的数组；`data` 里同时回显请求用的 `limit` 与 `offset`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string（UUID） | 会话标识 |
| `title` | string | 会话标题，自动生成，可以由 [5.5 重命名与归档](#_5-5-重命名与归档) 覆盖 |
| `created_at` | string（ISO 8601） | 创建时间 |
| `updated_at` | string（ISO 8601） | 最近更新时间 |
| `running` | boolean | 这段会话里当前有没有正在执行的 run，取值规则见下文 |
| `message_count` | number \| null | 这段会话里客户端可见的消息条数。`null` 与 `0` 含义不同，见下文 |

### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/sessions?user_id=u-123&limit=20" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{
  "success": true,
  "data": {
    "sessions": [
      {
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "title": "退货咨询",
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

#### running 的取值规则

`running` 为 `true`，当且仅当这段会话里至少有一个 run 处于 `pending`、`queued` 或 `running` 状态，三个取值的含义见 [5.4 run 列表](#_5-4-run-列表)。轮询这个字段，可以粗粒度地判断这段会话里的 run 是否已经跑完。

**暂停等待人工审批的 run（`paused`）不计入 `running`**：一段有 run 在等待审批的会话，这个字段是 `false`。要发现有 run 在等待审批，读事件流里的 `approval` 事件（见 [3 读懂 SSE 流](./sse-events)），或者在 [5.4 run 列表](#_5-4-run-列表) 里按 `status=paused` 查询。

#### message_count 的 null 与 0

`message_count` 统计的是这段会话里客户端可见的消息，与 [5.3 历史消息](#_5-3-历史消息) 返回的是同一批消息。`null` 与 `0` 含义不同：

| 取值 | 含义 |
|---|---|
| `null` | 服务端还没有统计过这段会话：它从来没有跑过 run，或者最后一次运行发生在这个字段可用之前 |
| `0` | 已经统计过，确实没有消息 |

这个字段在每次 run 进入最终状态时重新计算一次，不是实时累加。客户端不要把 `null` 当作 `0` 处理。

### 其它规则

- `user_id` 是这个租户从未出现过的值时，返回空列表，不是 404。
- 已归档的会话不在这个列表里，见 [5.5 重命名与归档](#_5-5-重命名与归档)；这个接口没有查询参数可以取回已归档的会话。

## 5.3 历史消息

读取一段会话里的消息：终端用户发出的消息，以及 Agent 给出的回答与中间过程输出。平台内部使用的消息不包含在内。

### 请求

``` [端点]
GET /v1/agents/{agent_code}/sessions/{session_id}/messages
```

`agent_code` 与 `session_id` 在路径里，其余参数在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `agent_code` | 是 | 会话所属的 Agent 标识 |
| `session_id` | 是 | 要查询的会话，UUID |
| `user_id` | 是 | 必须是这段会话实际归属的终端用户，长度 1–255 字符 |
| `limit` | 否 | 分页大小。取 1–200，默认 50 |
| `offset` | 否 | 分页偏移。取 0 及以上的整数，默认 0 |

### 响应

`data.messages` 是消息条目的数组；`data` 里同时回显请求用的 `limit` 与 `offset`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `role` | string | 取值：`"user"`（终端用户发出的消息）/ `"assistant"`（Agent 发出的消息） |
| `content` | string | 消息正文 |
| `channel` | string \| null | 只对 `assistant` 消息有意义。取值：`"final"`（这一轮最终展示给终端用户的回答）/ `"commentary"`（中间过程的输出）；`role` 为 `"user"` 的消息恒为 `null` |
| `created_at` | string（ISO 8601） \| null | 这条消息产生的时间。这个字段是后来增加的，更早产生的消息为 `null`，服务端不做历史补齐 |
| `run_id` | string（UUID） \| null | 产生这条消息的 run。同样是后来增加的字段，更早产生的消息为 `null` |

### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}/messages?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{
  "success": true,
  "data": {
    "messages": [
      {
        "role": "user",
        "content": "帮我看看这份文件",
        "channel": null,
        "created_at": "2026-08-12T10:00:00+00:00",
        "run_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
      },
      {
        "role": "assistant",
        "content": "已经看过了，摘要如下……",
        "channel": "final",
        "created_at": "2026-08-12T10:00:03+00:00",
        "run_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
      }
    ],
    "limit": 50,
    "offset": 0
  },
  "error": null
}
```

### 其它规则

- `session_id` 不属于这个 `user_id` 与 `agent_code` 时返回 404 `SESSION_NOT_FOUND`，响应不透露这段会话是否存在。
- 服务端没有配置会话历史存储时，这个接口对所有会话返回空的消息列表，而不是报错。客户端无法从响应上区分这种情况；大面积出现空历史时，联系租户管理员确认。
- 这个接口的分页是对整段会话的完整历史做切片，不是数据库分页。会话越长，单次请求的开销越大。

## 5.4 run 列表

列出某个终端用户在这个 Agent 上跑过的 run。客户端不需要自己在本地维护一份 `run_id` 清单。

### 请求

``` [端点]
GET /v1/agents/{agent_code}/runs
```

`agent_code` 在路径里，其余参数在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `agent_code` | 是 | 要查询的 Agent 标识 |
| `user_id` | 是 | 只列出这个终端用户的 run，长度 1–255 字符。缺失时返回 422 `INVALID_REQUEST`，不会退化成列出整个租户的 run |
| `session_id` | 否 | 只列出这一段会话里的 run，UUID。这段会话不属于该 `user_id` 与 `agent_code` 时返回 404 `SESSION_NOT_FOUND`，不是空列表 |
| `status` | 否 | 只列出这个状态的 run，取值见下文。传入八个取值之外的内容返回 422 `INVALID_REQUEST` |
| `limit` | 否 | 分页大小。取 1–200，默认 50 |
| `offset` | 否 | 分页偏移。取 0 及以上的整数，默认 0 |

### 响应

`data.runs` 是 run 条目的数组；`data` 里同时回显请求用的 `limit` 与 `offset`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string（UUID） | 这次 run 的标识 |
| `session_id` | string（UUID） | 这次 run 所属的会话 |
| `status` | string | run 当前的状态，八个取值见下文 |
| `created_at` | string（ISO 8601） | 创建时间 |
| `finished_at` | string（ISO 8601） \| null | 进入最终状态的时间；还没有进入最终状态时为 `null` |
| `error` | string \| null | 失败诊断文本，只在失败时非空，读法见下文 |

### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/runs?user_id=u-123&limit=20" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{
  "success": true,
  "data": {
    "runs": [
      {
        "run_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "status": "success",
        "created_at": "2026-08-12T10:00:00+00:00",
        "finished_at": "2026-08-12T10:00:08+00:00",
        "error": null
      }
    ],
    "limit": 20,
    "offset": 0
  },
  "error": null
}
```

#### status 的取值

「最终状态」一列为「是」，表示这条 run 记录的状态不会再变化。

| 取值 | 含义 | 最终状态 | 客户端的处理 |
|---|---|---|---|
| `pending` | 刚创建，正在转入 `running` 的瞬时状态 | 否 | 继续等待，或者接上这次 run 的事件流 |
| `queued` | 已进入队列，还没有实例开始执行 | 否 | 继续等待，或者接上这次 run 的事件流 |
| `running` | 正在执行 | 否 | 继续等待，或者接上这次 run 的事件流 |
| `success` | 正常执行完毕 | 是 | 按正常结果处理 |
| `error` | 执行失败，包含步数预算耗尽的情况 | 是 | 读 `error` 字段，见下文 |
| `timeout` | 保留取值，当前不会出现 | 是 | 不需要为它单独写处理分支 |
| `interrupted` | 被调用方取消 | 是 | 视为调用方自己中止的结果 |
| `paused` | 暂停等待人工审批，可以续跑 | 是 | 去 [4.2 审批决策](./run-control#_4-2-审批决策) 下达决策，这个 run 才会继续 |

`finished_at` 只在 run 进入最终状态之后才有值。

事件流末尾的 `end` 事件也有一个 `status` 字段，但它只有四个取值，而且只在事件流收尾时出现一次，见 [3.4 的 `end`](./sse-events#end)。两个字段名字相同，取值集合不同，不要假设它们一一对应。

#### error 字段的读法

`error` 是一段失败诊断文本，只在 run 失败时非空。它有两种来源：

- run 在执行过程中失败：`error` 与这次 run 的事件流里 `error` 事件携带的 `message` 是同一个字符串。
- run 还没有开始执行就失败（例如排队期间找不到对应的 Agent），或者执行它的实例失联、由服务端接管收尾：这两种情况没有对应的 `error` 事件，`error` 是一段固定的平台文案。

两种来源的 `error` 都不是结构化错误码。**不要按文案内容做模式匹配去反推失败分类。**

### 其它规则

- 没有传 `session_id`、且 `user_id` 是这个租户从未出现过的值时，返回空列表。

## 5.5 重命名与归档

本节的两个操作都作用在一段会话上，都要求 key 带 `write` 权限：重命名改写会话标题，归档把会话从默认列表里移除。

### 重命名

#### 请求

``` [端点]
PATCH /v1/agents/{agent_code}/sessions/{session_id}
```

`agent_code` 与 `session_id` 在路径里，`user_id` 与 `title` 在请求体里。

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_id` | string | 必填。必须与这段会话实际归属的终端用户一致，长度 1–255 字符 |
| `title` | string | 必填。新标题，覆盖当前标题，长度 1–200 字符 |

去掉首尾空白后为空的 `title`（例如整串都是空格）返回 422 `INVALID_TITLE`。

成功时返回 200，`data` 里是这段会话的 `session_id` 与更新之后的 `title`。

#### 示例

```bash [请求]
curl -X PATCH https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id} \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "title": "退货咨询"}'
```

```json [响应 200]
{ "success": true, "data": { "session_id": "550e8400-e29b-41d4-a716-446655440000", "title": "退货咨询" }, "error": null }
```

### 归档

调用方随时可以把一段会话标记为归档，不要求这段会话当前没有正在执行的 run。

#### 请求

``` [端点]
DELETE /v1/agents/{agent_code}/sessions/{session_id}
```

`agent_code` 与 `session_id` 在路径里，`user_id` 在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 必须与这段会话实际归属的终端用户一致，长度 1–255 字符 |

这个操作要求 `write` 权限，不是 `delete`：对外 API 没有单独的删除权限档位，给外部调用方一把能归档会话的 key 只需要 `write`。

成功时返回 200，`data` 里是这段会话的 `session_id` 与 `status`，归档成功后 `status` 为 `archived`。

::: tip 两个操作的 user_id 位置不同
重命名（PATCH）的 `user_id` 在请求体里，归档（DELETE）的 `user_id` 在查询字符串里。按查询参数传 PATCH 的 `user_id` 会返回 422。
:::

#### 示例

```bash [请求]
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{ "success": true, "data": { "session_id": "550e8400-e29b-41d4-a716-446655440000", "status": "archived" }, "error": null }
```

#### 归档的影响

归档把会话状态改成 `archived`，唯一可见的变化是这段会话从 `GET /v1/agents/{agent_code}/sessions` 的默认列表里消失。历史消息、run 记录、工作区文件全部原样保留，也照常可以查询：例如 `GET /v1/agents/{agent_code}/sessions/{session_id}/messages` 仍然能读到已归档会话的内容。

归档不影响这段会话里正在执行的 run：已经在跑的 run 会照常跑完，不会被归档动作打断或取消。对外 API 没有取消归档的操作。

::: warning 归档不会删除数据
归档是软删除。彻底的物理删除只在管理控制台内部提供，对外 API 不提供这个操作。
:::

### 两个操作共同的错误

- `session_id` 不存在，或者不属于这个 `user_id` 与 `agent_code` 时，两个操作都返回 404 `SESSION_NOT_FOUND`，不区分是不存在还是不归属。
- key 失效相关的 401 见 [8 错误码总表](./errors)。

## 5.6 工作区文件

工作区是每个终端用户的一块持久存储空间，Agent 执行 run 时把产出文件（报表、导出文件等）放在这里。本节的两个接口用来列出和下载这些文件。

::: warning agent_code 不参与这两个接口的过滤
工作区按租户与终端用户两个维度存储，不按 Agent 区分。路径里的 `{agent_code}` 只是为了与本组接口的其它路径保持相同形状，既不参与过滤，也不参与权限判定：同一个 `user_id` 配任意 `agent_code`（包括并不存在的 `agent_code`）取到的都是同一份文件列表，同样返回 200。
:::

这一点与本组其它接口不同：会话列表、历史消息、事件接口、审批决策都把 `agent_code` 当作真实的过滤或归属校验维度，工作区接口是唯一的例外。

由此带来一个需要注意的后果：调用方如果为不同业务线注册了不同的 `agent_code`，而这些业务线复用同一批终端用户 `user_id`，那么一条业务线能看到另一条业务线在同一个 `user_id` 下产生的文件。**当前 API 不提供按 Agent 隔离工作区文件的能力。**

### 列出文件

#### 请求

``` [端点]
GET /v1/agents/{agent_code}/workspace/files
```

`agent_code` 在路径里，`user_id` 在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 要查看的终端用户，长度 1–255 字符 |

这个接口不支持分页：没有 `limit` 与 `offset` 参数，一次返回全部文件，传了也会被忽略。

#### 响应

`data.files` 是文件条目的数组。

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | string | 文件在工作区里的相对路径，可能包含子目录。下载时原样回传给下载接口 |
| `size` | number | 文件大小，单位是字节 |

#### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/workspace/files?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
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

#### 其它规则

- `user_id` 是这个租户从未出现过的值时返回空列表，不是 404。
- 服务端的工作区存储配置有问题时返回 500 `WORKSPACE_LIST_FAILED`。重试无效，请联系租户管理员。

### 下载单个文件

#### 请求

``` [端点]
GET /v1/agents/{agent_code}/workspace/file
```

`agent_code` 在路径里，其余参数在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 文件所属的终端用户，长度 1–255 字符 |
| `path` | 是 | 要下载的文件相对路径，最长 4096 字符。**原样回传列出文件接口给出的 `path`，不要自己拼接字符串** |

#### 响应

成功响应的正文是文件字节流本身，不套 `{success, data, error}` 信封，这个信封只用于错误响应。

| 响应头 | 说明 |
|---|---|
| `Content-Type` | 按文件扩展名推断 |
| `Content-Disposition` | `inline` 或 `attachment`，判定规则见下文 |
| `X-Content-Type-Options` | 固定为 `nosniff` |

#### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/workspace/file?user_id=u-123&path=report.pdf" \
  -H "Authorization: Bearer <key>" \
  -o report.pdf
```

#### Content-Disposition 的判定

判定按下表从上到下的顺序进行，一个扩展名只落进第一个匹配的分类。

| 扩展名 | `Content-Disposition` | 原因 |
|---|---|---|
| `.html` / `.htm` / `.xhtml` / `.xht` / `.svg` / `.svgz` / `.xml` / `.xsl` / `.xslt` / `.mathml` | `attachment` | 防止浏览器把这些内容当作同源页面内联渲染，执行其中夹带的脚本 |
| `.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.bmp` / `.ico` | `inline` | 图片可以直接预览，不会被当作脚本执行 |
| `.json` / `.jsonl` / `.ndjson` / `.yaml` / `.yml` / `.toml` | `inline` | 结构化文本可以直接预览，不会被当作脚本执行 |
| `.txt` / `.log` / `.md` / `.markdown` / `.rst` / `.csv` / `.tsv` / `.ini` / `.conf` / `.py` / `.js` / `.mjs` / `.cjs` / `.jsx` / `.ts` / `.tsx` / `.go` / `.rs` / `.java` / `.kt` / `.scala` / `.rb` / `.php` / `.sh` / `.bash` / `.zsh` / `.fish` / `.sql` / `.c` / `.h` / `.cc` / `.cpp` / `.hpp` / `.cs` / `.swift` / `.dart` / `.lua` / `.r` / `.jl` / `.pl` / `.vue` | `inline` | 纯文本与源码统一按文本展示，浏览器显示源码而不执行它 |
| 其它扩展名，以及没有扩展名的文件 | `attachment` | 类型无法确认时按下载处理 |

#### path 的合法形态

以下四种形态都会被拒绝，返回 400：

- 绝对路径（以 `/` 开头）
- 含 `..` 段
- 含 NUL 字节（`\x00`）
- 空字符串，或者去掉首尾空白后为空

```json [响应 400]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

避免这一类错误最直接的做法：`path` 用列出文件接口返回的值原样回传。

#### 错误

| 状态码 | `error.code` | 触发条件 |
|---|---|---|
| 400 | `WORKSPACE_FILE_FAILED` | `path` 不合法，见上文「path 的合法形态」 |
| 404 | `WORKSPACE_FILE_FAILED` | `user_id` 不存在，或者 `path` 指向的文件不存在。两种情况返回同一个 404，服务端刻意不区分 |
| 500 | `WORKSPACE_FILE_FAILED` | 服务端的工作区存储配置有问题。重试无效，请联系租户管理员 |

三种情况的 `error.code` 是同一个字符串，只能靠 HTTP 状态码区分：

```json [响应 404]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "file not found" } }
```

完整的错误码见 [8 错误码总表](./errors)。

## 5.7 产物

产物是 Agent 主动登记为成果的文件，例如一份周报或一份导出数据。它与工作区文件的区别：工作区包含 Agent 产生的全部文件，产物是其中被挑出来交付的那几份，带有名字、类别和版本号。本节的三个接口用来列出、下载和删除产物。

与 [5.6 工作区文件](#_5-6-工作区文件) 一样，这三个接口的 `agent_code` 不参与过滤：产物也按租户与终端用户两个维度存储。

### 列出产物

#### 请求

``` [端点]
GET /v1/agents/{agent_code}/artifacts
```

`agent_code` 在路径里，`user_id` 在查询字符串里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 要查看的终端用户，长度 1–255 字符 |

这个接口不支持分页：没有 `limit` 与 `offset` 参数，一次返回全部产物，传了也会被忽略。

#### 响应

`data.artifacts` 是产物条目的数组，按更新时间从新到旧排列。

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 产物名，在同一个终端用户下唯一。下载和删除都用这个值 |
| `kind` | string | 产物类别，由 Agent 保存时声明。取值：`document`（文稿或报表）/ `code`（源码）/ `data`（数据文件）/ `other`（其它） |
| `latest_version` | number | 版本号。Agent 每用同一个 `name` 保存一次就加 1 |
| `created_at` | string（ISO 8601） | 首次创建时间 |
| `updated_at` | string（ISO 8601） | 最近一次更新时间 |

服务端不校验 `kind`，对外也没有修改它的接口。客户端解析时遇到上述四个取值之外的内容，按 `other` 处理。

条目里没有文件大小：大小与校验和在首次下载时才记录，放进列表大多是 `null`。

条目里也没有状态字段：出现在列表里的产物就是存活的，已删除的产物不再出现。

#### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/artifacts?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{
  "success": true,
  "data": {
    "artifacts": [
      {
        "name": "2026-08 周报.docx",
        "kind": "document",
        "latest_version": 3,
        "created_at": "2026-08-12T10:00:00+00:00",
        "updated_at": "2026-08-14T09:30:00+00:00"
      }
    ]
  },
  "error": null
}
```

#### 其它规则

- `user_id` 是这个租户从未出现过的值时返回空列表，不是 404。

### 下载产物

#### 请求

``` [端点]
GET /v1/agents/{agent_code}/artifacts/download
```

`agent_code` 在路径里，其余参数在查询字符串里。下载的永远是最新版本，当前不提供按版本号下载。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 产物所属的终端用户，长度 1–255 字符 |
| `name` | 是 | 要下载的产物名，最长 512 字符。原样回传列出产物接口给出的 `name`；含 NUL 字节时返回 422 `INVALID_ARTIFACT_NAME` |

#### 响应

成功响应的正文是文件字节流本身，不套 `{success, data, error}` 信封，这个信封只用于错误响应。`Content-Disposition` 的判定规则与 [5.6 工作区文件](#_5-6-工作区文件) 完全一致，响应同样固定带 `X-Content-Type-Options: nosniff`。

这个接口计入 `artifact_download` 配额（30 天滑动窗口），超限时返回 429 `RATE_LIMIT_EXCEEDED`，完整说明见 [8.11 429](./errors#_8-11-429-——-两种情况-含义不同)。

#### 示例

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/artifacts/download?user_id=u-123&name=2026-08%20周报.docx" \
  -H "Authorization: Bearer <key>" \
  -o report.docx
```

#### 错误

| 状态码 | `error.code` | 触发条件 |
|---|---|---|
| 404 | `ARTIFACT_NOT_FOUND` | 产物不存在、已删除，或者不属于这个 `user_id`，三种情况不区分。服务端读取产物记录时的瞬时故障也落到这个 404，所以它不完全等价于「这份产物不存在」 |
| 422 | `INVALID_ARTIFACT_NAME` | `name` 含 NUL 字节 |
| 429 | `RATE_LIMIT_EXCEEDED` | `artifact_download` 配额耗尽，对应的配额维度是 `ARTIFACT_DOWNLOAD_COUNT_30D` |
| 500 | `ARTIFACT_CONTENT_UNAVAILABLE` | 产物记录存在，服务端读不到它的内容。重试无效，请联系租户管理员 |
| 503 | `ARTIFACT_CONTENT_UNAVAILABLE` | 服务端没有配置工作区存储通路，产物内容整体不可读。重试无效，请联系租户管理员 |

500 与 503 的 `error.code` 相同，靠 HTTP 状态码区分是「读取权限配置有问题」（500）还是「存储通路缺失」（503）。两者都不是退避重试能解决的。

### 删除产物

#### 请求

``` [端点]
DELETE /v1/agents/{agent_code}/artifacts
```

`agent_code` 在路径里，其余参数在查询字符串里。这个操作要求 `write` 权限，与归档会话一样，对外 API 没有单独的删除权限档位。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 产物所属的终端用户，长度 1–255 字符 |
| `name` | 是 | 要删除的产物名，最长 512 字符。含 NUL 字节时返回 422 `INVALID_ARTIFACT_NAME` |

成功时返回 200，`data.deleted` 是被删除的那个产物名。

#### 示例

```bash [请求]
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/artifacts?user_id=u-123&name=2026-08%20周报.docx" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{ "success": true, "data": { "deleted": "2026-08 周报.docx" }, "error": null }
```

#### 删除的语义

删除只把这份产物从产物这一组接口里移除：它不再出现在列出产物的结果里，下载它会返回 404。

::: danger 删除产物不会清除文件内容
工作区里的文件字节不会被这个接口清除。保留期结束后，后台清理作业清除的也只是产物的登记信息（名字、版本号等），底层文件不受影响。
:::

只要知道这份内容在工作区里的原始路径，删除之后仍然可以用 [5.6 工作区文件](#_5-6-工作区文件) 的下载接口取到它。**调用方如果需要向终端用户兑现「删除我的数据」，这个接口不满足这个承诺**：当前对外 API 没有清除底层文件的操作。

删除之后，Agent 如果再用同一个 `name` 保存一次，这份产物会恢复，版本号接着往上加。对外 API 没有撤销删除的操作。

#### 其它规则

- 产物不存在、已经删除过，或者不属于这个 `user_id` 时，都返回同一个 404 `ARTIFACT_NOT_FOUND`，不区分。
