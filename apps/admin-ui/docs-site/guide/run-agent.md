# 调用 Agent

本篇是 `POST /v1/agents/{agent_code}/runs` 的完整参数表——发起一次 Agent 对话/任务执行的核心接口。

## 端点

```
POST /v1/agents/{agent_code}/runs
Authorization: Bearer <key>   # 需要 write scope,见「认证」
Content-Type: application/json
```

`{agent_code}` 是你租户里已发布且状态为 ACTIVE 的 Agent 名字(在管理控制台创建/发布 Agent 时定的那个名字)。同一个名字只有一个 ACTIVE 版本会被解析——如果这个名字下没有 ACTIVE 版本,返回 404(`AGENT_NOT_FOUND`,见 [错误码与限流](./errors))。

## 请求体参数

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string,1–255 字符 | 是 | 你自己系统里这个终端用户的标识。**首次出现的 `user_id` 会被自动"铸造"成一个新的终端用户**——长期记忆、工作区文件、按用户维度的 token 计费都挂在这个铸造出来的用户身上,而不是挂在你的 API Key(服务账号)上。同一个 `user_id` 后续调用会复用同一个终端用户,不会重复铸造。取值建议见 [最佳实践](./best-practices)。 |
| `session_id` | UUID,可选 | 否 | 续接一段已有会话。省略则开一段新会话。传了但对应的会话不属于这个 `user_id` / 这个 `agent_code`,返回 404(`SESSION_NOT_FOUND`)。 |
| `input` | string,≤65536 字符,可选 | 否 | 这一轮用户说的话/任务描述。 |
| `mode` | `"stream"` \| `"queue"`,默认 `"stream"` | 否 | 见下方「`stream` vs `queue`」。 |
| `image_refs` | string[],最多 64 项,可选 | 否 | 多模态输入——图片上传接口返回的 `expert_work://image/...` 引用,不是原始字节。 |
| `untrusted_content` | string[],最多 16 项,可选 | 否 | 结构化的"不可信内容"(比如一封邮件正文、一段工单描述)。和 `input` 分开传,Agent 会把这部分当作**数据**而不是指令来处理——这是防止外部内容里挟带指令注入的推荐做法,优于把不可信文本直接拼进 `input` 里。 |

## 响应:`stream` vs `queue`

**`mode: "stream"`(默认)**——响应就是 SSE 流本身:`200`,`Content-Type: text/event-stream`,响应头带 `X-Expert-Work-Session-Id`(这次绑定/续接到的会话 id)和 `X-Expert-Work-Run-Id`。事件格式见 [SSE 事件格式](./sse-events)。

**`mode: "queue"`**——立即返回 `202`,不建立流,由后台某个 worker 实例异步执行:

```json
{
  "run_id": "...",
  "thread_id": "...",
  "status": "queued"
}
```

拿到 `run_id` / `thread_id` 后,轮询 `GET /v1/sessions/{thread_id}/runs/{run_id}` 看状态,或者用 `GET /v1/sessions/{thread_id}/runs/{run_id}/events` 拿完整的 SSE 事件(这条接口在 run 还在跑的时候会实时接进去,跑完了就把持久化下来的帧按顺序回放一遍,结尾补一条 `end`)。这两个接口本身没有额外的 scope 门槛,使用调用方合法的 API key 即可(仍然要求这个会话是你自己铸造的那个 `user_id` 下的)。

## 续接会话

把上一次响应里拿到的 `X-Expert-Work-Session-Id` 存下来,下次调用把它作为 `session_id` 传回去,就是同一段对话的下一轮;不传 `session_id` 就是另开一段全新会话——同一个 `user_id` 下可以并存很多段互不相干的会话。
