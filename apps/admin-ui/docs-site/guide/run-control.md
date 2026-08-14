# 4.2 / 4.7 取消 run 与审批决策

本篇是「4 接口详情」的两个补充端点——取消一次正在执行的 run,以及对一个暂停等待人工审批的 run 下达决策。参数表之外的通用约定(信封形状、`user_id` 规则、幂等性作用域)见 [通用约定](./conventions);认证与 scope 见 [认证](./auth)。

## 4.2 取消 run

```
POST /v1/agents/{agent_code}/runs/{run_id}:cancel
Authorization: Bearer <key>   # 需要 write scope
Content-Type: application/json
```

`stream` 与 `queue` 两种执行模式都能取消。取消是幂等的——重复取消同一个 run 不会报错,只是第二次起 `stopped` 字段变成 `false`。

### 请求参数

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `agent_code` | 路径 | string | 是 | |
| `run_id` | 路径 | UUID | 是 | 要取消的 run |
| `user_id` | 请求体 | string,1–255 字符 | 是 | 归属校验——必须是发起这次 run 的那个 `user_id` |

```bash
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}:cancel \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123"}'
```

### 响应

```json
{ "success": true, "data": { "run_id": "...", "stopped": true }, "error": null }
```

`stopped: true` 表示这次调用真的触发了取消(run 当时确实在执行中);`stopped: false` 表示这个 run 已经处于终态——**包括正常结束(SUCCESS)、失败(ERROR/TIMEOUT)、已被取消(INTERRUPTED),也包括暂停等待审批(PAUSED)**——这几种状态下取消端点不做任何操作,直接报告 `stopped: false`,不会报错。换句话说,对一个暂停等待审批的 run 调用取消,不会让它消失或改变状态——它仍然停在原地等审批决策(见下方「4.7」)。

取消生效后这次 run 在 SSE 流上会怎样收尾,见 [SSE 事件格式](./sse-events)。

### 失败情况

| 状态码 | `error.code` | 触发条件 |
|---|---|---|
| 404 | `RUN_NOT_FOUND` | `run_id` 不存在,或者存在但不属于这个 `(user_id, agent_code)` 组合——**两种情况返回同一个不透明 404,不区分**。这里的"不透明"和其它端点的存在性隐藏是同一条规则,但取消场景容易被误判:收到这个 404 **不代表"run 还没创建好、稍后重试就有"**——如果你确信 `run_id` / `user_id` / `agent_code` 三者都对,这个 404 就是"这不是你的 run",重试不会有不同结果。 |
| 422 | `INVALID_REQUEST` | `user_id` 缺失、为空、或超过 255 字符(请求体字段校验) |
| 403 | `FORBIDDEN`(裸 `detail` 形状,非统一信封) | key 的 scope 不足(缺 `write`) |

401 相关的 key 失效情况和全站规则一致,见 [错误码与限流](./errors)。

## 4.7 审批决策

```
POST /v1/agents/{agent_code}/runs/{run_id}:decide
Authorization: Bearer <key>   # 需要 write scope
Content-Type: application/json
```

对一个暂停在审批点的 run(`PAUSED`)下达人工决策——同意、拒绝、或者修改参数后继续。决策生效后会**续跑**这个会话,续跑用的是一个**全新的 `run_id`**,不是被决策的那个 `run_id`。

::: warning 续跑用的是新 run_id,不是路径里传的那个
无论 `mode` 是 `stream` 还是 `queue`,响应头 `X-Expert-Work-Run-Id` 给的都是**续跑（continuation）的新 run_id**——路径参数 `{run_id}` 只是用来定位"决策哪一个待审批的 run",不是续跑后事件流所属的 run。拿旧的 `{run_id}` 去调事件回放端点(`GET .../runs/{run_id}/events`),读到的是审批之前那一段,看不到决策之后发生的事——必须切到响应头给的新 `run_id`。
:::

### 请求参数

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `agent_code` | 路径 | string | 是 | |
| `run_id` | 路径 | UUID | 是 | 暂停等待审批的那个 run |
| `user_id` | 请求体 | string,1–255 字符 | 是 | 归属校验 |
| `decision` | 请求体 | `"approve"` \| `"reject"` \| `"modify"` | 是 | |
| `modified_args` | 请求体 | object | 仅 `decision: "modify"` 时必填;其余两种 `decision` 下**禁止**传(传了会 422) | |
| `reason` | 请求体 | string,≤2048 字符 | 否 | |
| `idempotency_key` | 请求体 | string,≤255 字符 | 否 | **这是独立于运行创建端点 `Idempotency-Key` 请求头的另一套幂等域**,按这次决策而非按 run 创建计算,见 [通用约定](./conventions) 的「幂等性」一节 |
| `mode` | 请求体 | `"stream"` \| `"queue"`,默认 `"stream"` | 否 | 见下方「响应」 |

```bash
curl -X POST https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}:decide \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "decision": "approve", "mode": "queue"}'
```

### 响应

行为取决于 `mode`,以及这次决策是不是一次**幂等重放**(带着和某次已完成决策相同的 `idempotency_key` 重试):

- **`mode: "stream"`(默认)且不是幂等重放**——响应就是续跑的 SSE 流本身:`200`,`Content-Type: text/event-stream`,响应头带 `X-Expert-Work-Run-Id`(续跑的新 run_id)。事件格式见 [SSE 事件格式](./sse-events)。
- **`mode: "queue"`**,或**命中幂等重放**(不管 `mode` 是什么)——JSON 响应,不建立流:

```json
{ "success": true, "data": { "run_id": "..." }, "error": null }
```

  状态码:`mode: "queue"` 恒为 `202`;`mode: "stream"` 但命中幂等重放为 `200`(此时没有正在执行的续跑可供接流,和运行创建端点的 stream 幂等重放不是同一回事——这里重放的是"决策"本身,不是"run"本身)。两种情况响应头都带 `X-Expert-Work-Run-Id`(续跑的 run_id)。

拿到续跑的 `run_id` 后,用 `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=...` 接上它的事件流。

### 失败情况

| 状态码 | `error.code` | 触发条件 |
|---|---|---|
| 404 | `RUN_NOT_FOUND` | `run_id` 不存在,或者不属于这个 `(user_id, agent_code)` 组合——归属校验先于审批逻辑执行,和「4.2 取消 run」同一条不透明规则 |
| 404 | `APPROVAL_NOT_FOUND` | 归属校验通过,但这个 `run_id` 名下压根没有一条审批记录 |
| 409 | `APPROVAL_CONFLICT` | 这条审批已经被决定过(重复决策,或与另一次并发决策竞争后落败),且这次请求的 `idempotency_key` 对不上已落库的那次决策(对上了则不是失败,走幂等重放) |
| 409 | `SESSION_NOT_BOUND` | 这个 run 所在的会话没有绑定 `agent_name` / `agent_version`——内部状态异常,正常对接流程不会遇到 |
| 403 | `AGENT_DISABLED`(统一信封,和 scope 无关) | 这个 `agent_code` 已被管理员下线 |
| 403 | `TENANT_SUSPENDED`(统一信封,和 scope 无关) | 租户被暂停 |
| 404 | `AGENT_NOT_FOUND` | agent 的 manifest 记录本身已经不存在(比这个会话绑定的版本更底层的缺失) |
| 410 | `AGENT_DELETED` | agent 已被(软)删除 |
| 422 | `AGENT_BUILD_FAILED` | agent manifest 构建失败——服务端配置问题,不是你这边能解决的 |
| 422 | `INVALID_REQUEST` | 请求体字段没通过校验,比如 `decision: "modify"` 却没传 `modified_args`,或者非 `modify` 却传了 `modified_args` |
| 403 | `FORBIDDEN`(裸 `detail` 形状,非统一信封) | key 的 scope 不足(缺 `write`) |

401 相关的 key 失效情况和全站规则一致,见 [错误码与限流](./errors)。
