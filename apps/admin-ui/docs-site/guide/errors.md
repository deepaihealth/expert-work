# 错误码与限流

本篇讲清楚调用失败时你会看到什么、为什么、该怎么应对——覆盖 401 / 403 / 404 / 413 / 422 / 429,以及限流和配额这两个容易混的概念。

## 先说一件容易踩的坑:错误响应的信封形状不统一

大多数错误会用统一信封返回:

```json
{
  "success": false,
  "data": null,
  "error": { "code": "AGENT_NOT_FOUND", "message": "..." }
}
```

但一部分错误(比如 scope 不足的 403、`inputs` 模板变量校验失败的 422)直接用了 FastAPI 默认的 `{"detail": ...}` 形状(`detail` 有时是字符串,有时是 `{"code":..., "message":...}` 对象)。写解析逻辑时不要假设所有错误都是同一个信封——先看 HTTP 状态码兜底,body 里有 `error.code` 就用它,没有就退化读 `detail`。下面每一节会标出具体是哪种形状。

## 401 —— key 无效 / 过期

认证失败一律 401,统一信封形状,并带 `WWW-Authenticate: Bearer realm="expert-work"` 响应头:

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

**scope 不够**是最常见的 403 场景——比如拿一把只有 `read` scope 的 key 去调 `POST /v1/agents/{agent_code}/runs`(这个接口要求 `write`)。这类 403 走 FastAPI 默认 `detail` 形状,不是统一信封:

```json
{ "detail": { "code": "FORBIDDEN", "message": "principal lacks required role" } }
```

应对:换一把带 `write` scope 的 key(见 [认证](./auth) 的 scope 选择表)。

另外两种 403 是统一信封形状,和 scope 无关:当前 Agent 被管理员下线(`AGENT_DISABLED`)、或者租户本身被暂停(`TENANT_SUSPENDED`)——都不是靠换 key 能解决的,需要联系你的租户管理员。

## 404 —— agent 不存在 / 会话不存在

统一信封形状,两种情况:

```json
{ "success": false, "data": null, "error": { "code": "AGENT_NOT_FOUND", "message": "no active agent 'xxx' for this tenant" } }
```

- `AGENT_NOT_FOUND`——`{agent_code}` 在你的租户下没有已发布(ACTIVE)的版本:要么这个名字从没建过,要么建了但还没发布 / 已经下线,两种情况返回同一个 404,不做区分。
- `SESSION_NOT_FOUND`——传的 `session_id` 找不到,或者它不属于这个 `user_id` / `agent_code` 组合(跨用户 / 跨 agent 的会话 id 一律当不存在处理,不会告诉你"存在但不是你的")。

## 413 —— 文档 / 图片超限

只发生在上传接口(`POST /v1/agents/{agent_code}/uploads`),不是 `/runs` 本身——`/runs` 收的是 `image_refs` 引用,不是原始字节。这个接口把自己抛出的每一种拒绝都翻成统一信封:

```json
{ "success": false, "data": null, "error": { "code": "UPLOAD_TOO_LARGE", "message": "document exceeds 26214400-byte limit" } }
```

默认上限:文档 25 MiB,图片 10 MiB(以你的部署实际配置为准)。应对:压缩/裁剪后重传,或者把大文档拆成多份。

## 422 —— 请求参数不合法

`POST /v1/agents/{agent_code}/runs` 的 422 分两类,形状不一样。

**第一类,请求体字段本身没通过校验**——比如 `files[].transfer_method` 传了 `local_file` 以外的值、`upload_id` 是空字符串、`files[]` / `image_refs` / `untrusted_content` 超过各自的条数上限。统一信封,`error.code` 固定是 `INVALID_REQUEST`:

```json
{ "success": false, "data": null, "error": { "code": "INVALID_REQUEST", "message": "Input should be 'local_file'" } }
```

同一类里还有六个更具体的业务码,同样走统一信封:

| `code` | 什么情况 |
|---|---|
| `INVALID_FILE_REF` | `files[]` 里 `type: "document"` 的 `upload_id` 不是上传接口返回的那种 `uploads/<name>` 形状(比如自己截成了裸文件名、或者带了路径穿越) |
| `TOO_MANY_IMAGE_REFS` | `files[]` 里的图片条目和 `image_refs` 合并后总数超过 64 张 |
| `INVALID_IDEMPOTENCY_KEY` | `Idempotency-Key` 头去空白后是空字符串,或超过 255 字符 |
| `IDEMPOTENCY_KEY_REUSED` | 同一个 `Idempotency-Key` 配了不同的请求体,或者配给了不同的 `agent_code` |
| `TOO_MANY_INPUT_KEYS` | `inputs` 的键数量超过 64 个(正好 64 个合法) |
| `INPUT_VALUE_TOO_LONG` | `inputs` 里某个字符串值超过 8192 字符(正好 8192 字符合法;只检查字符串值) |

**第二类,`inputs`(提示词模板变量)与 Agent 声明不匹配——不走统一信封**,是裸的 FastAPI `{"detail": ...}` 字符串,没有 `error.code`:

```json
{ "detail": "unknown input variable: foo" }
```

三种情况都是这个形状:Agent 没声明模板变量却传了非空 `inputs`、`inputs` 里有未声明的键、Agent 声明的必填变量没给。**`inputs` 本身的两条硬上限(键数量 / 单值长度)不属于这一类**——那两条走上面第一类的统一信封(`TOO_MANY_INPUT_KEYS` / `INPUT_VALUE_TOO_LONG`)。细节见 [调用 Agent](./run-agent) 的「`inputs`」一节。

## 429 —— 两种情况,含义不同

**第一种,限流 / 配额超限**(`RATE_LIMIT_EXCEEDED`)——统一信封形状,带 `Retry-After` 响应头:

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

**第二种,工作区容量满了**(`QUOTA_EXCEEDED`)——只出现在文档上传接口,和 413 一样走统一信封,**但没有 `Retry-After` 响应头**,因为退避重试解决不了:

```json
{
  "success": false,
  "data": null,
  "error": { "code": "QUOTA_EXCEEDED", "message": "workspace is full — delete files to free space" }
}
```

应对:清理这个终端用户工作区里的旧文件(或者引导用户自己清理),不是退避重试能解决的。注意这两种 429 靠 `error.code` 区分(`RATE_LIMIT_EXCEEDED` vs `QUOTA_EXCEEDED`),别只看状态码。

## 限流与配额,是两件事

限流(rate limit)按时间窗口限制"多快",配额(quota)按资源维度限制"多少"——两者都复用同一个 `RATE_LIMIT_EXCEEDED` 错误码 + `dimension` 字段区分,但成因不同:限流是网关/租户层面的频率闸,不管你在做什么业务操作;配额是业务层面按资源计的账本(这个 agent 这段时间跑了多少次、这个用户的存储用了多少),超了同样 429,但解决办法可能不是"等等再试",而是"先把占用的资源清掉"或者"找管理员提额度"。拿到 429 先看 `error.message` / `error.dimension` 判断是哪一种,再决定退避重试还是清理资源。
