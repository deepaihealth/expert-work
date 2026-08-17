# 5 查询与管理

本篇是发起对话之外的只读与管理接口：有哪些 Agent 可调、这个用户有哪些会话、某段会话说过什么、跑过哪些 run、Agent 产出的文件怎么下载、会话怎么重命名和归档。

除标注外，这些接口都要求 `read` 权限（`write` key 含读，可直接用），查询参数 `user_id` 必填，分页参数为 `limit`（1–200，默认 50）和 `offset`（≥0，默认 0）。**例外见各小节**——5.1 不需要 `user_id`；5.6「列出文件」与 5.7「列出产物」不支持分页。

## 5.1 Agent 目录

```
GET /v1/agent-catalog
```

列出你的租户里当前可以调用的 Agent。对接的第一步——发起对话之前先知道能发给谁，不用把 `agent_code` 写死在客户端里。

这条接口是租户级的：**不需要 `user_id`**（目录跟具体哪个终端用户无关），路径里也没有 `{agent_code}`。

### 请求参数

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `limit` | 查询 | 否 | 1–200，默认 50 | 分页大小 |
| `offset` | 查询 | 否 | ≥0，默认 0 | 分页偏移 |

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

### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `agent_code` | string | 发起对话时填进 `POST /v1/agents/{agent_code}/runs` 路径的那个值。 |
| `display_name` | string | 展示名。**永远非空**——没配置显示名的 Agent 这里回退成 `agent_code` 本身，不用自己做空值判断。 |
| `description` | string | Agent 描述，租户内员工在管理控制台自己填的自由文本。 |
| `available` | boolean | 现在能不能对它发起对话。 |
| `total` | number | 去重后的 Agent 总数，不是版本记录数，也不是当前页条目数。 |

### `available` 与实际可用性有最长约 5 秒的偏差

目录的 `available` 是实时读的，而发起对话时的禁用检查走一层短缓存（默认约 5 秒，平台可配）。**两个方向都可能不一致**：

| 刚发生的操作 | 缓存过期前可能出现的现象 |
|---|---|
| 管理员**重新启用**了某个 Agent | 目录已显示 `available: true`，但发起对话仍被拒 |
| 管理员**禁用**了某个 Agent | 目录已显示 `available: false`，但发起对话仍被接受 |

两个方向的偏差都有上限、都会自行恢复，重试或稍等即可。**不要把目录的 `available` 当成发起对话前的前置校验**——该发就发，按返回的错误码处理。

### 哪些 Agent 不会出现在目录里

`available: false` 的 Agent **仍然出现在列表里**，只是标记为不可用（界面上置灰即可）。这是"被管理员禁用"的状态，可逆，随时可能恢复。

**只剩已弃用版本、没有任何可用版本的 Agent，不会出现在目录里。** 这两种情况刻意不同：被禁用是临时的管理动作，列出来置灰对客户端有意义；没有可用版本是版本生命周期的终点，不会自行恢复，列一个永远 `false` 的条目只是噪音。

### 翻页要用 `total` 判断

当 `offset` 加上这一页的条目数小于 `total` 时，还有下一页。

**不要用"这一页条目数 < limit 就是最后一页"这个经验规则**：同一个 `agent_code` 可能同时存在多个版本，目录按 `agent_code` 去重后返回，去重会让某一页天然短于 `limit`，用这个规则会在没翻完时提前退出，漏掉排在后面的 Agent。

## 5.2 会话列表

```
GET /v1/agents/{agent_code}/sessions
```

### 请求参数

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `agent_code` | 路径 | 是 | — | 要查询的 Agent 代码 |
| `user_id` | 查询 | 是 | 1–255 字符 | 只看这个终端用户的会话 |
| `limit` | 查询 | 否 | 1–200，默认 50 | 分页大小 |
| `offset` | 查询 | 否 | ≥0，默认 0 | 分页偏移 |

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

### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string（UUID） | 会话 id。 |
| `title` | string | 会话标题，自动生成或由 [5.5 重命名](#_5-5-重命名与归档) 覆盖。 |
| `created_at` / `updated_at` | string（ISO 8601） | 创建 / 最近更新时间。 |
| `running` | boolean | 这段会话里当前还有没有 run 在执行——想粗粒度轮询"任务跑完没有"，看这个字段。 |
| `message_count` | number \| null | 见下方「`message_count` 的 `null` 与 `0`」。 |

#### `message_count` 的 `null` 与 `0`

`message_count` 是这段会话里第三方可见的消息条数，口径与 [5.3 历史消息](#_5-3-历史消息) 一致（内部编排产生的消息不计入）。**`null` 和 `0` 含义不同**：

| 值 | 含义 |
|---|---|
| `null` | 还没统计过——存量会话从没跑过 run，或者创建于本次更新上线前、之后也没再跑过 run |
| `0` | 已统计过，确实没有消息 |

`message_count` 在每次 run 走到最终状态时重新计算并写回，不是实时累加。不要把 `null` 当成 `0` 处理。

### 注意

- `user_id` 是这个租户从没见过的值时，返回空列表而不是 404。
- 已归档的会话默认不在这个列表里，见 [5.5](#_5-5-重命名与归档)；这个接口当前没有查询参数能拿回已归档的会话。

## 5.3 历史消息

```
GET /v1/agents/{agent_code}/sessions/{session_id}/messages
```

### 请求参数

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `agent_code` | 路径 | 是 | — | 会话所属的 Agent 代码 |
| `session_id` | 路径 | 是 | UUID | 要查询的会话 |
| `user_id` | 查询 | 是 | 1–255 字符 | 归属校验——必须是这段会话实际归属的用户 |
| `limit` | 查询 | 否 | 1–200，默认 50 | 分页大小 |
| `offset` | 查询 | 否 | ≥0，默认 0 | 分页偏移 |

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

### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `role` | string | `"user"` 或 `"assistant"`。 |
| `content` | string | 消息正文。 |
| `channel` | string \| null | 只对 `assistant` 消息有意义：`"final"` = 这一轮最终展示给用户的回答，`"commentary"` = 中间过程输出。`user` 消息恒为 `null`。 |
| `created_at` | string（ISO 8601）\| null | 本次更新新增的字段。**更新之前产生的历史消息这个字段是 `null`**——写入时才记录，不做历史回填。 |
| `run_id` | string（UUID）\| null | 同上，本次更新新增，更新之前产生的历史消息是 `null`。 |

### 注意

- `session_id` 不属于这个 `user_id` / `agent_code` 时返回 404（`SESSION_NOT_FOUND`），响应不会透露这段会话是否存在。
- 如果服务端没有配置对话持久化组件，这个接口会对**所有**会话返回空消息列表，而不是报错——这是纯服务端配置问题，不是"这段会话真的没有消息"，你这边查不出来，遇到大面积异常的空历史可以联系租户管理员确认。

## 5.4 run 列表

```
GET /v1/agents/{agent_code}/runs
```

列出这个终端用户在这个 Agent 上跑过的 run——不用自己在本地攒一份 `run_id` 清单来做"我的任务列表"。

### 请求参数

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `agent_code` | 路径 | 是 | — | 要查询的 Agent 代码 |
| `user_id` | 查询 | 是 | 1–255 字符 | 只看这个终端用户的 run。**没有默认值，漏传直接 422**，不会降级成"列出整个租户的 run"。 |
| `session_id` | 查询 | 否 | UUID | 只看这一段会话里的 run。传了但这段会话不属于这个 `user_id` / `agent_code`，返回 404（`SESSION_NOT_FOUND`），不是空列表。 |
| `status` | 查询 | 否 | 见下方「`status` 取值」 | 只看这个状态的 run |
| `limit` | 查询 | 否 | 1–200，默认 50 | 分页大小 |
| `offset` | 查询 | 否 | ≥0，默认 0 | 分页偏移 |

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

### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string（UUID） | 这次 run 的 id。 |
| `session_id` | string（UUID） | 这次 run 所属的会话 id。 |
| `status` | string | 见下表。 |
| `created_at` | string（ISO 8601） | 创建时间。 |
| `finished_at` | string（ISO 8601） \| null | 走到最终状态的时间；还没走到时为 `null`。 |
| `error` | string \| null | 失败原因，只在失败时非空，见下方「`error` 字段怎么读」。 |

#### `status` 取值

| 取值 | 是否最终状态 |
|---|---|
| `pending` / `queued` / `running` | 否 |
| `success` / `error` / `timeout` / `interrupted` / `paused` | 是。只有走到最终状态，`finished_at` 才会被写上 |

这里的取值比 SSE `end` 事件的 `status` 更细：`end` 只有四值，**这里的 `timeout` 在 `end` 里显示为 `error`**（见 [3.4 的 `end`](./sse-events#end)）。同一次 run 两处字样不同是正常的。

#### `error` 字段怎么读

`error` 是一段失败诊断文本，只在失败时非空。

- **run 在流式执行过程中失败**：这个字段与该次 run 在 SSE 事件流里 `error` 事件携带的 `message` 是同一个字符串（两者来自同一处代码）。
- **run 还没真正开始执行就失败**（比如排队时找不到对应的 Agent），或者**执行它的后台实例失联、被接管收尾**：这两种情况不会有对应的 SSE `error` 事件，`error` 是一段固定的平台文案。

不管哪种情况，它都**不是**另一套结构化错误码，不要按文案内容做模式匹配去反推分类（"是不是超时""是不是上游拒绝"）。

### 注意

- `user_id` 是这个租户从没见过的值、且没传 `session_id` 时，返回空列表——与会话列表同一条规则。

## 5.5 重命名与归档

### 重命名

```
PATCH /v1/agents/{agent_code}/sessions/{session_id}
```

要求 `write` 权限。

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | 请求体 | string，1–255 字符 | 是 | 必须与这段会话实际归属的用户一致。 |
| `title` | 请求体 | string，1–200 字符 | 是 | 新标题，覆盖当前标题。 |

```bash [请求]
curl -X PATCH https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id} \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "title": "退货咨询"}'
```

```json [响应 200]
{ "success": true, "data": { "session_id": "550e8400-e29b-41d4-a716-446655440000", "title": "退货咨询" }, "error": null }
```

#### 注意

- `title` 去掉首尾空白后为空（比如整串都是空格）时返回 422 `INVALID_TITLE`。

### 归档

```
DELETE /v1/agents/{agent_code}/sessions/{session_id}
```

要求 `write` 权限，**不是** `delete`——对外 API 没有单独的删除权限档位，给外部对接方一把能归档会话的 key 只需要 `write`。

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | 查询 | string，1–255 字符 | 是 | 必须与这段会话实际归属的用户一致。 |

```bash [请求]
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{ "success": true, "data": { "session_id": "550e8400-e29b-41d4-a716-446655440000", "status": "archived" }, "error": null }
```

::: warning 归档是软删除，不是彻底删除
归档只是把会话状态改成 `archived`。**历史消息、run 记录、工作区文件全部原样保留**，也照常能查（比如 `GET .../messages` 仍能读到已归档会话的内容）。彻底物理删除只在管理控制台内部提供，不对外开放。

归档后唯一可见的变化：这段会话从 `GET .../sessions` 的**默认**列表里消失。当前 API 没有对外的取消归档操作。
:::

::: tip 两个写操作的 `user_id` 位置不一样
重命名（PATCH）的 `user_id` 在**请求体**里，归档（DELETE）的 `user_id` 在**查询参数**里。这不是笔误，是两个端点现有的真实形状——按 query 传 PATCH 会拿到 422。
:::

### 注意

- 两个操作在 `session_id` 不存在、或不属于这个 `user_id` / `agent_code` 时，都返回同一个 404 `SESSION_NOT_FOUND`，不区分是"不存在"还是"存在但不属于你"。

401 相关的 key 失效情况见 [8 错误码总表](./errors)。

## 5.6 工作区文件

Agent 执行任务时会往终端用户的持久工作区里写产出文件（报表、导出文件等），这两个接口用来列出和下载。

::: warning `agent_code` 对这两个接口不生效
工作区按 **(租户, 终端用户)** 维度存储，不按 Agent 分。URL 里的 `{agent_code}` 只是为了和这组接口的其它路径保持同样形状，**不参与过滤或权限判定**。同一个 `user_id` 配任意 `agent_code`（哪怕是一个根本不存在的 `agent_code`）拿到的都是**同一份**文件列表，同样返回 200。

这一点与这组接口里的其它端点不一致——会话列表、历史消息、事件回放、审批操作都把 `agent_code` 当作真实的过滤或归属校验维度。工作区端点是唯一的例外。

**风险场景**：如果你给不同业务线注册了不同的 `agent_code`，而它们复用同一批终端用户 `user_id`，那么一条业务线能看到另一条业务线在同一个 `user_id` 下产生的文件。**当前 API 不提供按 Agent 隔离文件的能力。**
:::

### 列出文件

```
GET /v1/agents/{agent_code}/workspace/files
```

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `user_id` | 查询 | 是 | 1–255 字符 | 只看这个终端用户的工作区 |

这个接口**不支持分页**——没有 `limit`/`offset` 参数，一次返回全部文件；传了也会被忽略。

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

#### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | string | 文件相对路径，可能带子目录（如上面第二项）。下载时原样传给下面的下载接口。 |
| `size` | number | 文件大小（字节）。 |

#### 注意

- `user_id` 是这个租户从没见过的值时返回空列表而不是 404。
- 服务端工作区存储配置有问题时返回 500 `WORKSPACE_LIST_FAILED`——这不是你这边能解决的，重试没用，联系你的租户管理员。

### 下载单个文件

```
GET /v1/agents/{agent_code}/workspace/file
```

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `user_id` | 查询 | 是 | 1–255 字符 | 同上。 |
| `path` | 查询 | 是 | ≤4096 字符 | 要下载的文件相对路径。**直接原样回传上面列出文件返回的 `path` 值，不要自己拼字符串。** |

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/workspace/file?user_id=u-123&path=report.pdf" \
  -H "Authorization: Bearer <key>" \
  -o report.pdf
```

### 响应

成功响应是**文件字节流本身**，**不套 `{success, data, error}` 信封**——那个形状只用来包裹错误响应。

响应头：

| 响应头 | 值 |
|---|---|
| `Content-Type` | 按扩展名推断，规则见下方「`Content-Disposition` 分类」 |
| `Content-Disposition` | `inline` 或 `attachment`，规则同上 |
| `X-Content-Type-Options` | 始终是 `nosniff` |

#### `Content-Disposition` 分类

| 扩展名 | `Content-Disposition` | 原因 |
|---|---|---|
| 图片（`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.bmp` / `.ico`）、结构化文本与代码（`.json` / `.yaml` / `.toml` / `.txt` / `.py` / `.md` 等） | `inline`，浏览器可直接预览 | — |
| `.html` / `.htm` / `.xhtml` / `.xht` / `.svg` / `.svgz` / `.xml` / `.xsl` / `.xslt` / `.mathml` | 强制 `attachment` | XSS 防护：避免浏览器把这些当同源 HTML/SVG 内联渲染，执行其中夹带的脚本 |
| 任何未识别的扩展名（含无扩展名文件） | 强制 `attachment` | 宁可多一次没必要的下载，也不猜错类型 |

#### `path` 的合法形态

以下形态一律拒绝，返回 400：

- 绝对路径（以 `/` 开头）
- 含 `..` 段（试图跳出工作区）
- 含 NUL 字节（`\x00`）
- 空字符串，或去掉首尾空白后为空

```json [响应 400]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

避免这整类错误最简单的办法：`path` 直接用列出文件接口返回的值原样回传。

#### 错误情况

| 状态码 | `error.code` | 触发条件 |
|---|---|---|
| 400 | `WORKSPACE_FILE_FAILED` | `path` 不合法，见上方「`path` 的合法形态」 |
| 404 | `WORKSPACE_FILE_FAILED` | `user_id` 不认识，或 `path` 指向的文件不存在——**这两种情况返回同一个 404**，不要试图从响应里区分是哪一种，这是刻意的存在性隐藏 |
| 500 | `WORKSPACE_FILE_FAILED` | 服务端工作区存储配置有问题——不是你这边能解决的，重试没用，联系你的租户管理员 |

三种情况的 `error.code` 都是同一个字符串，只能靠 HTTP 状态码区分是哪一种：

```json [响应 404]
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "file not found" } }
```

细节见 [8 错误码总表](./errors)。

## 5.7 产物

Agent 在执行过程中可以把一份成果**登记成产物**（比如一份周报、一份导出数据）。产物和工作区文件的区别：工作区是 Agent 的原始文件系统，产物是 Agent 主动挑出来、给人看的那些，带名字、类型和版本号。下面这三条接口分别用来列出、下载和删除产物。

这三条接口的 `agent_code` **不参与过滤**——产物按 (租户, 终端用户) 维度存，与 [5.6 工作区文件](#_5-6-工作区文件) 同一条规则。

### 列出产物

```
GET /v1/agents/{agent_code}/artifacts
```

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `user_id` | 查询 | 是 | 1–255 字符 | 只看这个终端用户的产物 |

这个接口**不支持分页**——没有 `limit`/`offset` 参数，一次返回全部产物；传了也会被忽略。按更新时间从新到旧排列。

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

#### 响应字段

| 字段 | 说明 |
|---|---|
| `name` | 产物名，在同一个终端用户下唯一。下载和删除都用它 |
| `kind` | `document` / `code` / `data` / `other`，由 Agent 保存时声明 |
| `latest_version` | 版本号。Agent 每次用同名保存一次就 +1 |
| `created_at` / `updated_at` | 首次创建 / 最近一次更新时间 |

**列表里没有文件大小。** 大小和校验和是**首次下载时才记录**的，列表里给出来大部分是 `null`，反而误导。

#### 注意

- 已删除的产物不出现在这个列表里。
- `user_id` 是这个租户从没见过的值时返回空列表，不是 404。

### 下载产物

```
GET /v1/agents/{agent_code}/artifacts/download
```

下载的永远是**最新版本**，当前不提供按版本号下载。

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `user_id` | 查询 | 是 | 1–255 字符 | — |
| `name` | 查询 | 是 | ≤512 字符 | 原样回传列表里的 `name`，不要自己拼；含 NUL 字节会被拒绝，返回 422 `INVALID_ARTIFACT_NAME` |

```bash [请求]
curl "https://<your-domain>/v1/agents/{agent_code}/artifacts/download?user_id=u-123&name=2026-08%20周报.docx" \
  -H "Authorization: Bearer <key>" \
  -o report.docx
```

### 响应

成功响应是**文件字节流本身**，**不套 `{success, data, error}` 信封**——那个形状只包裹错误响应。`Content-Disposition` 的规则与 [5.6 工作区文件](#_5-6-工作区文件) 完全一致：HTML / SVG 这类可执行内容强制 `attachment`，响应始终带 `X-Content-Type-Options: nosniff`。

**这条接口计入配额。** 每次下载扣 1 次 `artifact_download` 额度（同时计入 `ARTIFACT_DOWNLOAD_COUNT_30D` 这个 30 天滑动窗口维度）。超限时返回 **429 `RATE_LIMIT_EXCEEDED`**（带 `Retry-After` 头）——**不是** `QUOTA_EXCEEDED`：这条接口的配额准入统一走限流引擎，任何维度耗尽都翻成同一个 `RATE_LIMIT_EXCEEDED`，见 [8.11 429](./errors#_8-11-429-——-两种情况-含义不同)。**不要**照 `Retry-After` 头做短退避重试——`ARTIFACT_DOWNLOAD_COUNT_30D` 是 30 天窗口慢速回补，短退避基本等于无限重试打空，命中这个 429 应该当作「这个终端用户的下载额度打满了」来处理，不是「这会儿太忙等等再试」。

#### 错误情况

| 状态码 | `error.code` | 含义 |
|---|---|---|
| 404 | `ARTIFACT_NOT_FOUND` | 产物不存在、已删除、或不属于这个 `user_id`——**三种情况统一返回这一个 404**，不区分。服务端读产物记录时的瞬时故障也会落到这个 404 上，所以它并非 100% 等价于「这东西不存在」 |
| 422 | `INVALID_ARTIFACT_NAME` | `name` 里含 NUL 字节 |
| 429 | `RATE_LIMIT_EXCEEDED` | `artifact_download` 配额（含 `ARTIFACT_DOWNLOAD_COUNT_30D` 30 天窗口）耗尽，见上方说明 |
| 500 | `ARTIFACT_CONTENT_UNAVAILABLE` | 产物记录在，但服务端读不到内容（存储配置问题）。**这不是"不存在"**，重试没用，联系你的租户管理员 |
| 503 | `ARTIFACT_CONTENT_UNAVAILABLE` | 服务端没有配置工作区存储通路，产物内容整体不可读——同一个 `error.code`，靠状态码区分「配置缺失」（503）与「权限配置有问题」（500）两种服务端故障，都不是退避重试能解决的 |

### 删除产物

```
DELETE /v1/agents/{agent_code}/artifacts
```

要求 `write` 权限（与归档会话一样，对外 API 没有单独的删除权限档位）。

| 参数 | 位置 | 必填 | 取值/默认 | 说明 |
|---|---|---|---|---|
| `user_id` | 查询 | 是 | 1–255 字符 | — |
| `name` | 查询 | 是 | ≤512 字符 | 含 NUL 字节会被拒绝，返回 422 `INVALID_ARTIFACT_NAME` |

```bash [请求]
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/artifacts?user_id=u-123&name=2026-08%20周报.docx" \
  -H "Authorization: Bearer <key>"
```

```json [响应 200]
{ "success": true, "data": { "deleted": "2026-08 周报.docx" }, "error": null }
```

::: warning 这是软删除——工作区里的字节不会被这个 API 清除
产物从「产物视图」（这三条接口：列表、下载、删除）里消失了，但**工作区里的文件字节从始至终没有被删除**，保留期后台清理任务到期后做的也**不是**删字节——那个任务只物理清掉产物的元数据行（名字、版本号这些），不碰底层文件本身。

也就是说：只要还记得这个文件在工作区里的原始路径，删除之后（甚至保留期过了、元数据行都被后台任务清掉之后）仍然可以用 [5.6 工作区文件](#_5-6-工作区文件) 的 `GET /v1/agents/{agent_code}/workspace/file` 原样下载到这份内容。**如果你把「删除产物」当成对终端用户「删除我的数据」的承诺，这条 API 目前不满足这个承诺**——它只是把这份产物从「成果清单」里摘下来，不是销毁数据；真要清除底层字节，眼下没有对外的操作能做到。

如果 Agent 之后又用同一个 `name` 保存了一次，这个产物会**恢复**（版本号接着往上加）。

当前 API 没有对外的撤销删除操作。
:::

#### 注意

- 产物不存在、已经删过、或不属于这个 `user_id`，都返回同一个 404 `ARTIFACT_NOT_FOUND`，不区分。
