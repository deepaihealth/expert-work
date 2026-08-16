# 5 查询与管理

本篇是发起对话之外的只读与管理接口：有哪些 Agent 可调、这个用户有哪些会话、某段会话说过什么、跑过哪些 run、Agent 产出的文件怎么下载、会话怎么重命名和归档。

除标注外，这些接口都要求 `read` 权限（`write` key 含读，可直接用），查询参数 `user_id` 必填，分页参数为 `limit`（1–200，默认 50）和 `offset`（≥0，默认 0）。

## 5.1 Agent 目录

```
GET /v1/agent-catalog
```

列出你的租户里当前可以调用的 Agent。对接的第一步——发起对话之前先知道能发给谁，不用把 `agent_code` 写死在客户端里。

这条接口是租户级的：**不需要 `user_id`**（目录跟具体哪个终端用户无关），路径里也没有 `{agent_code}`。

```bash
curl "https://<your-domain>/v1/agent-catalog?limit=50&offset=0" \
  -H "Authorization: Bearer <key>"
```

```json
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

| 字段 | 类型 | 说明 |
|---|---|---|
| `agent_code` | string | 发起对话时填进 `POST /v1/agents/{agent_code}/runs` 路径的那个值。 |
| `display_name` | string | 展示名。**永远非空**——没配置显示名的 Agent 这里回退成 `agent_code` 本身，不用自己做空值判断。 |
| `description` | string | Agent 描述，租户内员工在管理控制台自己填的自由文本。 |
| `available` | boolean | 现在能不能对它发起对话。 |
| `total` | number | 去重后的 Agent 总数，不是版本记录数，也不是当前页条目数。 |

### `available` 与实际可用性有最长 30 秒的偏差

目录的 `available` 是实时读的，而发起对话时的禁用检查走 30 秒缓存。**两个方向都可能不一致**：

| 刚发生的操作 | 30 秒内可能出现的现象 |
|---|---|
| 管理员**重新启用**了某个 Agent | 目录已显示 `available: true`，但发起对话仍被拒 |
| 管理员**禁用**了某个 Agent | 目录已显示 `available: false`，但发起对话仍被接受 |

两个方向的偏差都有上限、都会自行恢复，重试或稍等即可。**不要把目录的 `available` 当成发起对话前的前置校验**——该发就发，按返回的错误码处理。

### 哪些 Agent 不会出现在目录里

`available: false` 的 Agent **仍然出现在列表里**，只是标记为不可用（界面上置灰即可）。这是"被管理员禁用"的状态，可逆，随时可能恢复。

**只剩已弃用版本、没有任何可用版本的 Agent，不会出现在目录里。** 这两种情况刻意不同：被禁用是临时的管理动作，列出来置灰对客户端有意义；没有可用版本是版本生命周期的终点，不会自行恢复，列一个永远 `false` 的条目只是噪音。

### 翻页要用 `total` 判断

`offset + 这一页的条目数 < total` 时还有下一页。

**不要用"这一页条目数 < limit 就是最后一页"这个经验规则**：同一个 `agent_code` 可能同时存在多个版本，目录按 `agent_code` 去重后返回，去重会让某一页天然短于 `limit`，用这个规则会在没翻完时提前退出，漏掉排在后面的 Agent。

## 5.2 会话列表

```
GET /v1/agents/{agent_code}/sessions
```

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

`running` 表示这段会话里当前还有没有 run 在执行——想粗粒度轮询"任务跑完没有"，看这个字段。

`message_count` 是这段会话里第三方可见的消息条数，口径与 [5.3 历史消息](#_5-3-历史消息) 一致（内部编排产生的消息不计入）。**`null` 和 `0` 含义不同**：

| 值 | 含义 |
|---|---|
| `null` | 还没统计过——存量会话从没跑过 run，或者创建于本次更新上线前、之后也没再跑过 run |
| `0` | 已统计过，确实没有消息 |

`message_count` 在每次 run 走到最终状态时重新计算并写回，不是实时累加。不要把 `null` 当成 `0` 处理。

`user_id` 是这个租户从没见过的值时，返回空列表而不是 404。已归档的会话默认不在这个列表里，见 [5.5](#_5-5-重命名与归档)。

## 5.3 历史消息

```
GET /v1/agents/{agent_code}/sessions/{session_id}/messages
```

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
        "content": "已经看过了，摘要如下……",
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

| 字段 | 说明 |
|---|---|
| `role` | `"user"` 或 `"assistant"`。 |
| `channel` | 只对 `assistant` 消息有意义：`"final"` = 这一轮最终展示给用户的回答，`"commentary"` = 中间过程输出。`user` 消息恒为 `null`。 |
| `created_at` / `run_id` | 本次更新新增的字段。**更新之前产生的历史消息这两个字段是 `null`**——写入时才记录，不做历史回填。 |

`session_id` 不属于这个 `user_id` / `agent_code` 时返回 404（`SESSION_NOT_FOUND`），响应不会透露这段会话是否存在。

## 5.4 run 列表

```
GET /v1/agents/{agent_code}/runs
```

列出这个终端用户在这个 Agent 上跑过的 run——不用自己在本地攒一份 `run_id` 清单来做"我的任务列表"。

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/runs?user_id=u-123&limit=20" \
  -H "Authorization: Bearer <key>"
```

```json
{
  "success": true,
  "data": {
    "runs": [
      {
        "run_id": "...",
        "session_id": "...",
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

查询参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 只看这个终端用户的 run。**没有默认值，漏传直接 422**，不会降级成"列出整个租户的 run"。 |
| `session_id` | 否 | 只看这一段会话里的 run。传了但这段会话不属于这个 `user_id` / `agent_code`，返回 404（`SESSION_NOT_FOUND`），不是空列表。 |
| `status` | 否 | 只看这个状态的 run。 |
| `limit` / `offset` | 否 | 1–200，默认 50 / ≥0，默认 0。 |

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string（UUID） | 这次 run 的 id。 |
| `session_id` | string（UUID） | 这次 run 所属的会话 id。 |
| `status` | string | 见下表。 |
| `created_at` | string（ISO 8601） | 创建时间。 |
| `finished_at` | string（ISO 8601） \| null | 走到最终状态的时间；还没走到时为 `null`。 |
| `error` | string \| null | 失败原因，只在失败时非空。 |

### `status` 取值

| 取值 | 是否最终状态 |
|---|---|
| `pending` / `queued` / `running` | 否 |
| `success` / `error` / `timeout` / `interrupted` / `paused` | 是。只有走到最终状态，`finished_at` 才会被写上 |

### `error` 字段怎么读

`error` 是一段失败诊断文本，只在失败时非空。

- **run 在流式执行过程中失败**：这个字段与该次 run 在 SSE 事件流里 `error` 事件携带的 `message` 是同一个字符串（两者来自同一处代码）。
- **run 还没真正开始执行就失败**（比如排队时找不到对应的 Agent），或者**执行它的后台实例失联、被接管收尾**：这两种情况不会有对应的 SSE `error` 事件，`error` 是一段固定的平台文案。

不管哪种情况，它都**不是**另一套结构化错误码，不要按文案内容做模式匹配去反推分类（"是不是超时""是不是上游拒绝"）。

`user_id` 是这个租户从没见过的值、且没传 `session_id` 时，返回空列表——与会话列表同一条规则。

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

```bash
curl -X PATCH https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id} \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "title": "退货咨询"}'
```

```json
{ "success": true, "data": { "session_id": "...", "title": "退货咨询" }, "error": null }
```

`title` 去掉首尾空白后为空（比如整串都是空格）时返回 422 `INVALID_TITLE`。

### 归档

```
DELETE /v1/agents/{agent_code}/sessions/{session_id}
```

要求 `write` 权限，**不是** `delete`——对外 API 没有单独的删除权限档位，给外部对接方一把能归档会话的 key 只需要 `write`。

```bash
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json
{ "success": true, "data": { "session_id": "...", "status": "archived" }, "error": null }
```

::: warning 归档是软删除，不是彻底删除
归档只是把会话状态改成 `archived`。**历史消息、run 记录、工作区文件全部原样保留**，也照常能查（比如 `GET .../messages` 仍能读到已归档会话的内容）。彻底物理删除只在管理控制台内部提供，不对外开放。

归档后唯一可见的变化：这段会话从 `GET .../sessions` 的**默认**列表里消失。当前 API 没有对外的取消归档操作。
:::

::: tip 两个写操作的 `user_id` 位置不一样
重命名（PATCH）的 `user_id` 在**请求体**里，归档（DELETE）的 `user_id` 在**查询参数**里。这不是笔误，是两个端点现有的真实形状——按 query 传 PATCH 会拿到 422。
:::

两个操作在 `session_id` 不存在、或不属于这个 `user_id` / `agent_code` 时，都返回同一个 404 `SESSION_NOT_FOUND`，不区分是"不存在"还是"存在但不属于你"。

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

`user_id` 是这个租户从没见过的值时返回空列表而不是 404。`path` 可能带子目录，如上面第二项。

### 下载单个文件

```
GET /v1/agents/{agent_code}/workspace/file
```

| 查询参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 同上。 |
| `path` | 是 | 要下载的文件相对路径。**直接原样回传上面列出文件返回的 `path` 值，不要自己拼字符串。** |

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/workspace/file?user_id=u-123&path=report.pdf" \
  -H "Authorization: Bearer <key>" \
  -o report.pdf
```

成功响应是文件字节流本身，**不是** `{success, data, error}` 形状——那个形状只用来包裹错误响应。

`Content-Type` 按扩展名推断，`Content-Disposition` 分两类：

| 扩展名 | `Content-Disposition` | 原因 |
|---|---|---|
| 图片（`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.bmp` / `.ico`）、结构化文本与代码（`.json` / `.yaml` / `.toml` / `.txt` / `.py` / `.md` 等） | `inline`，浏览器可直接预览 | — |
| `.html` / `.htm` / `.xhtml` / `.xht` / `.svg` / `.svgz` / `.xml` / `.xsl` / `.xslt` / `.mathml` | 强制 `attachment` | XSS 防护：避免浏览器把这些当同源 HTML/SVG 内联渲染，执行其中夹带的脚本 |
| 任何未识别的扩展名（含无扩展名文件） | 强制 `attachment` | 宁可多一次没必要的下载，也不猜错类型 |

响应始终带 `X-Content-Type-Options: nosniff`。

#### `path` 的合法形态

以下形态一律拒绝，返回 400：

- 绝对路径（以 `/` 开头）
- 含 `..` 段（试图跳出工作区）
- 含 NUL 字节（`\x00`）
- 空字符串，或去掉首尾空白后为空

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

避免这整类错误最简单的办法：`path` 直接用列出文件接口返回的值原样回传。

#### 错误情况

`user_id` 不认识、`path` 指向的文件不存在，这两种情况**返回同一个 404**（`WORKSPACE_FILE_FAILED`）——不要试图从响应里区分是哪一种，这是刻意的存在性隐藏：

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "file not found" } }
```

服务端工作区存储配置有问题时返回 500，同样是 `WORKSPACE_FILE_FAILED`。这种情况不是你这边能解决的，重试没用，联系你的租户管理员：

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "workspace file unavailable" } }
```

细节见 [8 错误码总表](./errors)。
