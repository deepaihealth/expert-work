# 6 错误码总表

本篇讲清楚调用失败时你会看到什么、为什么、该怎么应对——覆盖 400 / 401 / 403 / 404 / 409 / 410 / 413 / 422 / 429 / 500 / 502 / 503 / 504,以及限流和配额这两个容易混的概念。

## 6.1 错误码总表

线上收到一个 `code`,按这张表直接查,不用翻六节。每一行链接到下面对应小节的详解(取消 run / 审批决策的几个码详解在 [取消 run 与审批决策](./run-control))。

| `code` | HTTP 状态 | 含义 | 建议处理 |
|---|---|---|---|
| [`WORKSPACE_FILE_FAILED`](#_400-——-工作区文件路径不合法) | 400 | 工作区文件路径不合法 | 别自己拼路径,原样回传「列出文件」接口给的 `path` |
| [`INVALID_UPLOAD`](#_400-——-工作区文件路径不合法) | 400 | 上传接口自身的校验失败(文件为空、没有文件名等) | 检查文件内容后重传 |
| [`AUTH_MISSING_CREDENTIALS`](#_401-——-key-无效-过期) | 401 | 没带 `Authorization` 头,或格式不对 | 检查请求头 |
| [`AUTH_INVALID_TOKEN`](#_401-——-key-无效-过期) | 401 | key 不存在 / 已吊销 / 轮换宽限期已过 | 换一把新 key |
| [`AUTH_TOKEN_EXPIRED`](#_401-——-key-无效-过期) | 401 | key 本身没问题但已过期 | 换一把新 key |
| [`FORBIDDEN`](#_403-——-scope-不足-其它权限阻断)(裸 `detail`) | 403 | key 的 scope 不足 | 换一把带所需 scope 的 key |
| [`AGENT_DISABLED`](#_403-——-scope-不足-其它权限阻断) | 403 | 这个 agent 已被管理员下线 | 联系租户管理员 |
| [`TENANT_SUSPENDED`](#_403-——-scope-不足-其它权限阻断) | 403 | 租户被暂停 | 联系租户管理员 |
| [`AGENT_NOT_FOUND`](#_404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | `agent_code` 在你的租户下没有已发布版本 | 检查 `agent_code` 拼写与发布状态 |
| [`SESSION_NOT_FOUND`](#_404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | `session_id` 不存在,或不属于这个 `user_id` / `agent_code` | 核对三者是否匹配;不透明 404,不区分"不存在"与"不是你的" |
| [`WORKSPACE_FILE_FAILED`](#_404-——-agent-不存在-会话不存在-工作区文件不存在) | 404 | `user_id` 未识别,或文件不存在 | 核对 `user_id` / `path` |
| [`RUN_NOT_FOUND`](./run-control) | 404 | `run_id` 不存在,或不属于这个 `user_id` / `agent_code` | 核对三者是否匹配;不要当作"还没创建好"去重试 |
| [`APPROVAL_NOT_FOUND`](./run-control) | 404 | 这个 `run_id` 没有一条待审批记录 | 确认这个 run 真的处于等待审批状态 |
| [`APPROVAL_CONFLICT`](./run-control) | 409 | 这条审批已经被决定过 | 不要重复决策;要重放上次结果,带上当时用的 `idempotency_key` |
| [`SESSION_NOT_BOUND`](./run-control) | 409 | run 所在会话没有绑定 agent(内部状态异常) | 联系租户管理员 |
| [`AGENT_DELETED`](./run-control) | 410 | agent 已被(软)删除 | 不可恢复,换一个 `agent_code` |
| [`UPLOAD_TOO_LARGE`](#_413-——-文档-图片超限) | 413 | 文档 / 图片超过大小上限 | 压缩或裁剪后重传,或拆分成多份 |
| [`INVALID_REQUEST`](#_422-——-请求参数不合法) | 422 | 请求体字段本身没通过基础校验 | 检查字段类型 / 长度 / 取值范围 |
| [`INVALID_USER_ID`](#_422-——-请求参数不合法) | 422 | `user_id` 去掉首尾空白后是空字符串 | 传一个非空白的 `user_id` |
| [`INVALID_FILE_REF`](#_422-——-请求参数不合法) | 422 | `files[]` 里 document 条目的 `upload_id` 格式不对 | 原样回传上传接口返回的值,不要自己改写 |
| [`TOO_MANY_IMAGE_REFS`](#_422-——-请求参数不合法) | 422 | 图片条目(`image_refs` + `files[]` 里的 image)总数超过 64——这是外层的合计上限,单次 run 实际允许的图片数通常更低(默认 8,见下方 422 节) | 拆分成多次调用 |
| [`INVALID_IMAGE_REF`](#_422-——-请求参数不合法) | 422 | image 条目引用格式不对 | 用上传接口对图片返回的引用,不要用 document 的 |
| [`INVALID_IDEMPOTENCY_KEY`](./conventions) | 422 | `Idempotency-Key` 去空白后为空,或超过 255 字符 | 检查请求头取值 |
| [`IDEMPOTENCY_KEY_REUSED`](./conventions) | 422 | 同一个 key 换了请求体,或打给了不同的 `agent_code` | 换一个新 key,不要复用 |
| [`TOO_MANY_INPUT_KEYS`](#_422-——-请求参数不合法) | 422 | `inputs` 键数量超过 64 | 精简 `inputs` |
| [`INPUT_VALUE_TOO_LONG`](#_422-——-请求参数不合法) | 422 | `inputs` 某个字符串值超过 8192 字符 | 精简该值 |
| [`TOO_MANY_INPUT_BYTES`](#_422-——-请求参数不合法) | 422 | `inputs` 序列化后总字节数超过 65536 | 精简 `inputs` 整体 |
| [`UNTRUSTED_CONTENT_BLOCK_TOO_LONG`](#_422-——-请求参数不合法) | 422 | `untrusted_content` 单块超过 8192 字符 | 拆成多个数组元素 |
| [`INVALID_TITLE`](#_422-——-请求参数不合法) | 422 | 会话标题去掉首尾空白后是空字符串 | 传一个非空标题 |
| [`AGENT_BUILD_FAILED`](./run-control) | 422 | agent manifest 构建失败——发起 run、审批决策续跑都可能遇到,含义相同 | 服务端配置问题,不是你这边能解决的,联系租户管理员 |
| [`RATE_LIMIT_EXCEEDED`](#_429-——-两种情况-含义不同) | 429 | 触发限流(网关 / 租户 / 业务维度) | 按 `retry_after_s`(或 `Retry-After` 头)退避重试 |
| [`QUOTA_EXCEEDED`](#_429-——-两种情况-含义不同) | 429 | 工作区容量满(文档上传) | 清理资源或联系管理员提额度——重试没用 |
| [`WORKSPACE_LIST_FAILED`](#_500-——-工作区服务端配置问题) | 500 | 工作区存储服务端配置有问题 | 不是你这边能解决的,联系租户管理员 |
| [`WORKSPACE_FILE_FAILED`](#_500-——-工作区服务端配置问题) | 500 | 同上 | 联系租户管理员 |
| [`UPLOAD_FAILED`](#_500-——-工作区服务端配置问题) | 500 / [502](#_502-——-上传写入失败-上游错误) | 文件上传落盘失败(服务端配置问题,或写入时的上游错误) | 重试;持续失败联系租户管理员 |
| [`UPLOAD_UNAVAILABLE`](#_503-——-服务不可用-两种含义不同) | 503 | 上传接口专属:对象存储或沙箱工作区未就绪 | 稍后重试;持续失败联系租户管理员 |
| [`SERVER_OVERLOADED`](#_503-——-服务不可用-两种含义不同) | 503 | 全站过载保护——**任何端点都可能遇到**,不只是上传 | 按 `Retry-After` 头退避重试 |
| [`DEADLINE_EXCEEDED`](#_504-——-请求超过了你自己设的截止时间) | 504 | 你自己传的 `X-Expert-Work-Deadline-Ms` 已经过去 | 检查这个头的取值,或者干脆别传它 |

**这张表只覆盖有 `error.code` 的失败,不是全部失败的穷尽清单。** 有一部分失败连 `error.code` 都没有——有的是只有一个 `detail` 字段的简易格式(比如 scope 不足的 403、`inputs` 模板变量校验失败的 422,这类 `detail` 是字符串),有的干脆是内部一次校验函数直接抛出的裸文案(比如"这个 agent 不支持图片输入""单次 run 图片数超过上限""图片引用不属于这个会话",都是 4xx;配额引擎本身不可用是 503)。这类没有 `code` 的失败没法进这张按 `code` 查的表,完整列表分散在下面各节和「先说一件容易踩的坑」——别假设"表里没列到的码"就等于"这个失败不存在"。

## 先说一件容易踩的坑:错误响应的形状不统一

大多数错误会用**标准格式**返回,能读到 `error.code`:

```json
{
  "success": false,
  "data": null,
  "error": { "code": "AGENT_NOT_FOUND", "message": "..." }
}
```

但一部分错误(比如 scope 不足的 403、`inputs` 模板变量校验失败的 422)是**简易格式**,只有一个 `detail` 字段,读不到 `error.code`:

```json
{ "detail": "..." }
```

`detail` 有时是字符串,有时是 `{"code":..., "message":...}` 对象。写解析逻辑时不要假设所有错误都是标准格式——先看 HTTP 状态码兜底,body 里有 `error.code` 就用它,没有就退化读 `detail`。下面每一节会标出具体是哪种形状。

## 400 —— 工作区文件路径不合法

只发生在 `GET /v1/agents/{agent_code}/workspace/file`(下载工作区文件)的 `path` 查询参数上,能读到 `error.code`,固定 `WORKSPACE_FILE_FAILED`:

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "invalid workspace path" } }
```

以下几种 `path` 形态会触发:绝对路径(以 `/` 开头)、含 `..` 段、含 NUL 字节(`\x00`)、空字符串或者去掉首尾空白后为空。应对:别自己拼路径——直接把 `GET .../workspace/files` 返回的 `path` 字段原样回传,细节见 [调用 Agent](./run-agent) 的「工作区文件」一节。

**另一个 400 只发生在上传接口**(`POST /v1/agents/{agent_code}/uploads`),`error.code` 为 `INVALID_UPLOAD`:上传的文件本身没通过校验——最常见的是文件类型不在允许列表内(图片只收 png/jpeg/webp/gif,文档只收 pdf/docx/xlsx/pptx/txt/md/csv,传了别的类型就是这个码),其次是文件内容为空、没有文件名。检查文件类型和内容后重传。

## 401 —— key 无效 / 过期

认证失败一律 401,能读到 `error.code`,并带 `WWW-Authenticate: Bearer realm="expert-work"` 响应头:

```json
{ "success": false, "data": null, "error": { "code": "AUTH_INVALID_TOKEN", "message": "Invalid or unrecognised token" } }
```

`error.code` 会区分三种具体情况:

| `code` | 什么情况 |
|---|---|
| `AUTH_MISSING_CREDENTIALS` | 没带 `Authorization` 头,或者格式不对 |
| `AUTH_INVALID_TOKEN` | key 格式对但校验不过——不存在、已被吊销、或者轮换后的宽限期已经过了 |
| `AUTH_TOKEN_EXPIRED` | key 本身没问题,但 `expires_at` 已经过了 |

应对:检查 key 是不是复制错了 / 过期了 / 被吊销了,换一把新 key(见 [认证](./auth))。

## 403 —— scope 不足 / 其它权限阻断

**scope 不够**是最常见的 403 场景——比如拿一把只有 `read` scope 的 key 去调 `POST /v1/agents/{agent_code}/runs`(这个接口要求 `write`)。这类 403 读不到 `error.code`——码在 `detail.code`:

```json
{ "detail": { "code": "FORBIDDEN", "message": "principal lacks required role" } }
```

应对:换一把带 `write` scope 的 key(见 [认证](./auth) 的 scope 选择表)。

另外两种 403 能读到 `error.code`,和 scope 无关:当前 Agent 被管理员下线(`AGENT_DISABLED`)、或者租户本身被暂停(`TENANT_SUSPENDED`)——都不是靠换 key 能解决的,需要联系你的租户管理员。

## 404 —— agent 不存在 / 会话不存在 / 工作区文件不存在

能读到 `error.code`,三种情况:

```json
{ "success": false, "data": null, "error": { "code": "AGENT_NOT_FOUND", "message": "no active agent 'xxx' for this tenant" } }
```

- `AGENT_NOT_FOUND`——`{agent_code}` 在你的租户下没有已发布(ACTIVE)的版本:要么这个名字从没建过,要么建了但还没发布 / 已经下线,两种情况返回同一个 404,不做区分。
- `SESSION_NOT_FOUND`——传的 `session_id` 找不到,或者它不属于这个 `user_id` / `agent_code` 组合(跨用户 / 跨 agent 的会话 id 一律当不存在处理,不会告诉你"存在但不是你的")。历史消息、重命名(`PATCH .../sessions/{session_id}`)、归档(`DELETE .../sessions/{session_id}`)、run 列表(`GET .../runs?session_id=`)四个接口都走这同一条不透明规则。
- `WORKSPACE_FILE_FAILED`——`GET /v1/agents/{agent_code}/workspace/file` 下载文件时,`user_id` 未识别(不认识这个终端用户)和 `path` 指向的文件不存在,这两种情况返回同一个不透明的 404,不要试图从响应里区分是哪一种——这是刻意的存在性隐藏,不是 bug。同一个 `error.code` 在 400 / 500 也会出现,靠 HTTP 状态码区分,见下方「400」与「500」两节。
- `RUN_NOT_FOUND` / `APPROVAL_NOT_FOUND`——取消 run(`:cancel`)与审批决策(`:decide`)这两个端点各自的归属校验 / 审批查找失败码,完整的失败码表(含这两个端点特有的 409 / 403 / 410 / 422 情况)见 [取消 run 与审批决策](./run-control)。

**还有一种 404 没有 `error.code`,是裸 `{"detail": "image ref not found"}`**:`POST /v1/agents/{agent_code}/runs` 的 `image_refs` / `files[]` 里的图片引用是绑定会话的(引用里编了 `thread_id`),传了一个不属于这次请求最终绑定到的那个会话的图片引用就会撞上这个 404。**一个真实会踩的顺序**:先调上传接口传了一张图但没带 `session_id`(接口顺手给你铸了一个新会话 A,图片引用绑定的是 A),然后发起 run 时又没传 `session_id`(这次请求又铸了一个新会话 B)——图片引用属于 A,run 绑定到了 B,直接 404。避免办法:上传时如果打算紧接着发一次带这张图的 run,把上传响应里的 `session_id` 原样带进发起 run 的请求体。

## 409 —— 审批冲突

只发生在审批决策端点(`POST /v1/agents/{agent_code}/runs/{run_id}:decide`):`APPROVAL_CONFLICT`(这条审批已经被决定过,重复决策或并发竞争落败)、`SESSION_NOT_BOUND`(这个 run 所在会话没有绑定 agent,内部状态异常)。两个码的完整触发条件见 [取消 run 与审批决策](./run-control)。

## 410 —— agent 已被删除

只发生在审批决策端点续跑时:`AGENT_DELETED`,这个 run 所在会话绑定的 agent 版本已被(软)删除,不可恢复。详情见 [取消 run 与审批决策](./run-control)。

## 413 —— 文档 / 图片超限

只发生在上传接口(`POST /v1/agents/{agent_code}/uploads`),不是 `/runs` 本身——`/runs` 收的是 `image_refs` 引用,不是原始字节。这个接口把自己抛出的每一种拒绝都翻成能读到 `error.code` 的形状:

```json
{ "success": false, "data": null, "error": { "code": "UPLOAD_TOO_LARGE", "message": "document exceeds 26214400-byte limit" } }
```

默认上限:文档 25 MiB,图片 10 MiB(以你的部署实际配置为准)。应对:压缩/裁剪后重传,或者把大文档拆成多份。

## 422 —— 请求参数不合法

`POST /v1/agents/{agent_code}/runs` 的 422 分两类,形状不一样。

**第一类,请求体字段本身没通过校验**——比如 `files[].transfer_method` 传了 `local_file` 以外的值、`upload_id` 是空字符串、`files[]` / `image_refs` / `untrusted_content` 超过各自的条数上限。能读到 `error.code`,固定是 `INVALID_REQUEST`:

```json
{ "success": false, "data": null, "error": { "code": "INVALID_REQUEST", "message": "Input should be 'local_file'" } }
```

同一类里还有十个更具体的业务码,同样能读到 `error.code`:

| `code` | 什么情况 |
|---|---|
| `INVALID_FILE_REF` | `files[]` 里 `type: "document"` 的 `upload_id` 不是上传接口返回的那种 `uploads/<name>` 形状(比如自己截成了裸文件名、或者带了路径穿越) |
| `TOO_MANY_IMAGE_REFS` | `files[]` 里的图片条目和 `image_refs` 合并后总数超过 64 张——这是请求体层面的合计上限,不代表单次 run 真的能处理 64 张图,见下方「图片数还有一道更严的闸」 |
| `INVALID_IMAGE_REF` | `image_refs` 或 `files[]` 里 `type: "image"` 条目的引用格式不合法(不是上传接口对图片返回的那种 `expert_work://image/...` 引用)——两个入口都会触发这个码。最容易踩的坑:document 和 image 两种 `files[]` 条目字段名都叫 `upload_id`,把 document 形态的 `upload_id`(形如 `uploads/report.pdf`)填进了 `type: "image"` 的条目 |
| `INVALID_IDEMPOTENCY_KEY` | `Idempotency-Key` 头去空白后是空字符串,或超过 255 字符 |
| `IDEMPOTENCY_KEY_REUSED` | 同一个 `Idempotency-Key` 配了不同的请求体,或者配给了不同的 `agent_code` |
| `TOO_MANY_INPUT_KEYS` | `inputs` 的键数量超过 64 个(正好 64 个合法) |
| `INPUT_VALUE_TOO_LONG` | `inputs` 里某个字符串值超过 8192 字符(正好 8192 字符合法;只检查字符串值) |
| `TOO_MANY_INPUT_BYTES` | `inputs` 序列化后的总字节数(按 UTF-8 编码计算,不是字符数)超过 65536 字节(正好 65536 字节合法);与 `TOO_MANY_INPUT_KEYS` / `INPUT_VALUE_TOO_LONG` 是三条互相独立的限制,不是互相替代——单值用 list/dict 包一层绕开单值字符数检查时,这条总字节数上限仍然拦得住 |
| `UNTRUSTED_CONTENT_BLOCK_TOO_LONG` | `untrusted_content` 里某一块超过 8192 字符(正好 8192 字符合法);与 `untrusted_content` 最多 16 项的条数上限是两条互相独立的限制 |
| `INVALID_USER_ID` | `user_id` 去掉首尾空白后是空字符串(比如整串都是空格)——这条不止 `/runs`,凡是要求 `user_id` 的端点都会触发,包括会话绑定 / 取消 / 审批决策 / 会话列表与消息 / 重命名 / 归档 / 上传 / 工作区读取 |

**agent 构建失败是另一种独立的 422**,`error.code` 为 `AGENT_BUILD_FAILED`——命中已发布 Agent 的 manifest 因服务端配置问题构建失败(比如引用了不存在的模型 / 工具),`/runs` 与审批决策(`:decide`)续跑都可能遇到。这不是你这边能解决的,联系租户管理员。

**图片数还有一道更严的闸,而且撞上时拿到的是裸 `detail`,没有 `error.code`**:`TOO_MANY_IMAGE_REFS`(64 张)只是请求体字段层面的合计上限;`/runs` 内部对单次 run 实际处理的图片数另有一条独立限制(部署可配,默认 **8** 张),超过时是 422 `{"detail": "too many images: max 8 per run"}`,没有 `error.code`,和 `TOO_MANY_IMAGE_REFS` 是两道完全独立的闸——9~64 张这个区间会先过掉 `TOO_MANY_IMAGE_REFS` 那道闸,再被这道闸拦下,拿到的是这个裸 `detail`,读不到 `error.code`。同一道闸还有两种关联失败,同样是裸 `detail`、没有 `error.code`:

- 422 `{"detail": "agent does not accept image input: ..."}`——这个 Agent 没开启图片能力(既没声明支持视觉的模型,Agent 配置里也没声明 `vision` 相关能力),传了 `image_refs` / `files[]` 里的图片条目就会撞上,不管数量。
- 404 `{"detail": "image ref not found"}`——图片引用不属于这次请求最终绑定的会话,细节见上方「404」一节。

三条都不是 `{success, data, error}` 这个形状,写解析逻辑时不要假设"拿到 image 相关的 422/404 就一定有 `error.code` 可读"。

**第二类,`inputs`(提示词模板变量)与 Agent 声明不匹配——没有 `error.code`**,是只有一个 `detail` 字段的简易格式字符串:

```json
{ "detail": "unknown input variable: foo" }
```

三种情况都是这个形状:Agent 没声明模板变量却传了非空 `inputs`、`inputs` 里有未声明的键、Agent 声明的必填变量没给。**`inputs` 本身的三条硬上限(键数量 / 单值长度 / 序列化后总字节数)不属于这一类**——这三条和上面第一类一样能读到 `error.code`(`TOO_MANY_INPUT_KEYS` / `INPUT_VALUE_TOO_LONG` / `TOO_MANY_INPUT_BYTES`)。细节见 [调用 Agent](./run-agent) 的「`inputs`」一节。

另外,`PATCH /v1/agents/{agent_code}/sessions/{session_id}`(重命名会话)有自己独立的一个业务码,和上面 `/runs` 那套无关,同样能读到 `error.code`:`title` 去掉首尾空白后是空字符串(比如整串都是空格),422 `INVALID_TITLE`:

```json
{ "success": false, "data": null, "error": { "code": "INVALID_TITLE", "message": "title must not be empty" } }
```

`title` 完全不传,或者传的是字面意义上的空字符串 `""`,走的是请求体字段校验(`title` 要求至少 1 个字符),422 `INVALID_REQUEST`,不是这个码——区别在于有没有先经过服务端的 `strip()`。

## 429 —— 两种情况,含义不同

**第一种,限流 / 配额超限**(`RATE_LIMIT_EXCEEDED`)——能读到 `error.code`,带 `Retry-After` 响应头:

```json
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

`dimension` 告诉你具体是哪一层限流触发的——网关按 IP / 按 key 的频率闸、租户维度的整体频率闸,或者业务层按资源维度的配额闸(比如某个 agent 的调用频率、图片上传的次数或存储字节数)。**但网关这一层的 429 不带 `dimension` 字段**(只有 `code` / `message` / `retry_after_s`)——解析时给 `dimension` 缺省兜底,别假设它总在。应对:按 `retry_after_s`(或 `Retry-After` 头)退避后重试,不要立刻重试。

**第二种,工作区容量满了**(`QUOTA_EXCEEDED`)——只出现在文档上传接口,和 413 一样能读到 `error.code`,**但没有 `Retry-After` 响应头**,因为退避重试解决不了:

```json
{
  "success": false,
  "data": null,
  "error": { "code": "QUOTA_EXCEEDED", "message": "workspace is full — delete files to free space" }
}
```

应对:清理这个终端用户工作区里的旧文件(或者引导用户自己清理),不是退避重试能解决的。注意这两种 429 靠 `error.code` 区分(`RATE_LIMIT_EXCEEDED` vs `QUOTA_EXCEEDED`),别只看状态码。

## 500 —— 工作区服务端配置问题

以下两种 500 出现在两个工作区接口(`GET /v1/agents/{agent_code}/workspace/files` 列表、`GET .../workspace/file` 下载),能读到 `error.code`:

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_LIST_FAILED", "message": "workspace listing unavailable" } }
```

```json
{ "success": false, "data": null, "error": { "code": "WORKSPACE_FILE_FAILED", "message": "workspace file unavailable" } }
```

触发条件是服务端工作区存储的权限配置有问题(比如共享 uid 没配对),不是你这边的请求有问题,也不是"这个用户 / 文件不存在"——刻意不降级成空列表或 404,免得把服务端配置问题伪装成正常的业务响应。应对:不是退避重试能解决的,联系你的租户管理员核实工作区存储配置。

**上传接口(`POST /v1/agents/{agent_code}/uploads`)文档分支的落盘失败是另一个独立的 `code`**——`UPLOAD_FAILED`,能读到 `error.code`:

```json
{ "success": false, "data": null, "error": { "code": "UPLOAD_FAILED", "message": "workspace write failed" } }
```

500 对应服务端工作区权限配置问题(和上面两个 `WORKSPACE_*` 码同一类根因,但这是上传落盘、不是列出 / 下载);502 对应写入时遇到的上游错误(见下一节)。两种状态码下 `error.code` 和 `error.message` 完全一样,只能靠 HTTP 状态码分——但应对方式相同:都不是退避重试能解决的,持续失败联系租户管理员。

## 502 —— 上传写入失败(上游错误)

只发生在上传接口(`POST /v1/agents/{agent_code}/uploads`)的文档分支,`error.code` 为 `UPLOAD_FAILED`(与上面 500 节的同名码同一次落盘失败的两种可能状态码,消息也相同):写入工作区时遇到沙箱侧的上游错误,不是权限配置问题。应对同上——不是退避重试能解决的,持续失败联系租户管理员。

## 503 —— 服务不可用,两种含义不同

**第一种,全站过载保护——任何端点都可能遇到,不只是上传接口**:服务端同时处理的请求数超过软上限时,新请求直接被挡在外面,`error.code` 为 `SERVER_OVERLOADED`,带 `Retry-After` 响应头:

```json
{ "success": false, "data": null, "error": { "code": "SERVER_OVERLOADED", "message": "Server is shedding load; retry after a moment." } }
```

应对:按 `Retry-After` 头退避重试——这是典型的"这会儿太忙,等等再试就好"场景。

**第二种,只发生在上传接口**(`POST /v1/agents/{agent_code}/uploads`),`error.code` 为 `UPLOAD_UNAVAILABLE`,**没有 `Retry-After` 头**:

```json
{ "success": false, "data": null, "error": { "code": "UPLOAD_UNAVAILABLE", "message": "object store unavailable" } }
```

触发条件是服务端没有配置好对应的存储通路——图片走的对象存储、文档走的沙箱工作区,任一个没接好都会触发。这是部署 / 配置问题,不是你这边能解决的,联系你的租户管理员;重试没用。

**另外还有一种 503 没有 `error.code`**:发起 run 时,如果服务端的配额引擎本身不可用,响应是裸 `{"detail": "quota_engine_unavailable"}`,连 `{success, data, error}` 这个形状都不是(上传接口遇到同一种故障时会包成能读到 `error.code` 的 `UPLOAD_UNAVAILABLE`,不是这种裸形状)。概率很低,但要知道这种形状存在——别假设 503 一定能读到 `error.code`。

## 504 —— 请求超过了你自己设的截止时间

只有你自己在请求上带了 `X-Expert-Work-Deadline-Ms` 头(见 [通用约定](./conventions)),且这个时间戳已经过去,才会触发——服务端不会主动给你的请求安一个截止时间,不用这个头就不会遇到这个状态码。能读到 `error.code`:

```json
{ "success": false, "data": null, "error": { "code": "DEADLINE_EXCEEDED", "message": "X-Expert-Work-Deadline-Ms has already passed." } }
```

应对:检查这个头的取值是不是未来的 unix 毫秒时间戳;不需要端到端超时控制的话,别传这个头。

## 限流与配额,是两件事

限流(rate limit)按时间窗口限制"多快",配额(quota)按资源维度限制"多少"——**这两个 429 对应不同的 `error.code`**(见上方「429」一节):限流是 `RATE_LIMIT_EXCEEDED`(带 `dimension` 字段和 `Retry-After` 头,网关/租户层面的频率闸,不管你在做什么业务操作);配额是 `QUOTA_EXCEEDED`(工作区容量满了,不带 `Retry-After`,因为等待不会让容量变大)。拿到 429 先看 `error.code` 判断是哪一种(别只看状态码,也别指望 `dimension` 字段总在——网关这一层的限流就不带它),再决定退避重试还是清理资源 / 找管理员提额度。
