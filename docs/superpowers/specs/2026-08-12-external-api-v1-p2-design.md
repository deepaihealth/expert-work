# 第三方对接 API v1 P2 设计

> 2026-08-12 定稿。承接 [2026-08-11-external-api-v1-design.md](./2026-08-11-external-api-v1-design.md)
> (P1 已交付,PR #1155,mergeCommit `e26b86ed`)。
>
> 范围三块:①P1 挪过来的三个缺失字段;②请求体做厚;③`POST .../runs` 响应信封化。

## 一、背景

P1 交付了对外契约地基(7 个端点)+ 安全收口。四个"展示增强"字段当时判定不阻碍第三方跑通,
挪到 P2:

| 字段 | 端点 | P1 状态 |
|---|---|---|
| `message_count` | `GET .../sessions` | 未交付 |
| `created_at` | `GET .../sessions/{id}/messages` | 未交付 |
| `run_id` | 同上 | 未交付 |

另外 P1 spec §五(请求侧改造)与 §六 的信封问题也留在 P2。

### 勘误(P2 brainstorm 逐条实查后修正 P1 spec 的判断)

P1 spec 对这几件事的成本估计有三处偏差,都会误导 P2 的排期,在此纠正。

**勘误一 —— 写入侧盖戳并不"影响所有 agent 执行路径"。**

P1 spec §四-5 的原话是「改**写入**路径,把时间戳 / `run_id` 塞进消息元数据 —— 影响所有
agent 执行路径」。实查结论:盖戳落点只有 **4 处**,且全部已经手握 `run_id`。

- 用户消息:`api/runs.py:build_run_graph_input`(两个调用方 `runs.py:861` /
  `run_queue_worker.py:266` 都在紧邻几行处往 `configurable` 塞 `run_id`)
- 用户消息(定时触发另一条路):`trigger_firing.py:255`,自己拼 `graph_input`,同样手握 `run_id`
- 助手消息:`graph_builder/builder.py` `agent_node` 的两个 return(`:1094` 中间件路径 /
  `:1122` 无中间件路径),两处 `config` 都在作用域内,且已有现成 helper
  `graph_builder/_config.py:60 current_run_id(config)`
- 定时投递的助手消息:`trigger_delivery.py:87`

其余往 `messages` 通道写的地方都不需要动:`tools_node` 与审批拒绝/action-screen block 走的是
`ToolMessage`(`read_turns` 只保留 human/ai,进不了对外视图);`builder.py:834 / 1502 / 1687 /
1882` 那几处 `HumanMessage` 是 **prompt-only 的临时拼接**,不进 state 通道。

更关键的是:**盖戳是这个仓库现成的惯用法,不是新花样**。`additional_kwargs` 的自有键都是
**写入时塞的**(`reflect.py:228` / `builder.py:1750` / `loop_detection.py:178` /
`trigger_delivery.py:87`),读取侧 `transcript.py:80` 只读不算。其中 `trigger_delivery.py:87`
已经在塞 `expert_work_source_run_id` 并拿它做投递幂等 —— **仓库里已经跑着一个 run_id 盖戳
的先例**。

**勘误二 —— `inputs` 已经存在。**

P1 spec §五C 把 `inputs` 列为待做。实际 `RunRequest.inputs`(`api/runs.py:139`)全套都在:
64 键上限、单值 8192 字符上限、`validate_prompt_inputs` 在 `spawn_run` 内部已经跑、
`build_run_graph_input` 已经把它喂给 jinja 渲染 system_prompt。缺的只是对外的
`ExternalRunRequest`(`api/agents.py:418`)没暴露这个字段。这条是接线,不是造功能。

**勘误三 —— `/uploads` 已经收文档。**

`api/external_uploads.py:249` 已有 `document_allowed_content_types` 分支,文档落到声明的
终端用户持久工作区,返回 `{upload_id: <工作区文件名>, type: "document", mime, size}`。

所以 `files[]` 真正补的洞不是"多支持一种传输方式",而是:**文档传上去了,run 请求体只认
`image_refs`,那个文档引用无处可放**。第三方传完文档没法说"针对这份文件干活",只能指望
agent 自己拿工作区工具去翻。

### 真正的难点是 `message_count`,不是时间戳

P1 spec 把三个字段并列,实际工作量差一个量级。

- `thread_meta` 表**没有**计数列(`models/thread_meta.py` 逐字段核过)
- 会话列表端点一次返回 N 条(`limit` 上限 200),每条要计数就得 N 次 checkpoint 读 ——
  `read_turns` 是把整个 checkpoint blob 反序列化。不可接受
- 镜像表 `thread_message_sync.message_count` 语义三重不对:只计文本轮次、是水位行(刚建
  的会话根本没这行)、而且 sweep 用 `include_hidden=True` 数出来比第三方**应该**看到的多

## 二、范围

### 做什么

1. **块 2 · 三个缺失字段** —— 消息盖戳(`created_at` + `run_id`)+ `thread_meta.message_count`
2. **块 1 · 请求体做厚** —— `files[]`、`inputs` 透传、`Idempotency-Key`
3. **块 3 · 响应信封化** —— `POST /v1/agents/{code}/runs` 的 202 响应
4. **块 4 · 客户端补完** —— 工作区文件列表/下载 + 会话重命名/删除(见 §六;与前三块无代码耦合,
   建议拆独立 PR)

### 不做什么

- **`transfer_method: remote_url` 推迟**(用户 2026-08-12 裁定)。不是取消:P1 spec §五B 的
  五道防护设计(禁跳转 / DNS 钉住 / 流式限长 / 嗅探真实类型 / 紧超时+预算)原样保留待用。
  推迟的理由是这一条的工作量约等于 `files[]` 本身再翻一倍,而第三方"先下下来再调 `/uploads`"
  的代价只是多一次调用,那次调用他们本来就在做。
- **不回填存量历史消息**(P1 已定的决策 2)。旧消息的 `created_at` / `run_id` 返回 `null`。
- **不改控制台平面的响应形状**。`POST /v1/sessions/{thread_id}/runs` 保持裸 JSON,它的消费者
  是 admin-ui。

## 三、块 2:三个缺失字段

### A. 消息盖戳

写入侧盖,键走 `additional_kwargs`,沿用仓库现成惯用法:

```
expert_work_created_at: <ISO8601 字符串>
expert_work_run_id:     <run_id 字符串>
```

时间格式用 **ISO8601**,与其余端点一致,不学 Dify / OpenAI 的 unix 秒(P1 已定的决策 1)。

四个盖戳点见 §一「勘误一」的清单。`agent_node` 的两处**不各写一遍** —— 抽一个
`_stamp(messages, run_id, now)` helper,两个 return 各调一次,顺带覆盖同列表里的 CM-1 advisory。

**盖戳必须放在最后、紧挨 return**:`response` 在 DLP 重写、`_reconcile_parsed_after_rewrite`、
judge 那几步会被重新绑定,盖早了会被覆盖掉。

消息对象按不可变方式更新(`model_copy(update=...)`),不原地改已有对象。

### B. 读出侧

`MessageTurn`(`persistence/thread_message/base.py:23`)加 `created_at: datetime | None` 与
`run_id: UUID | None`。

> ⚠️ **这个 dataclass 是 `transcript.py` 与五个消费者的共享契约**:`quality_monitor_worker.py:294`、
> `transcript_mirror_sweep.py:157`、`trigger_delivery.py:142/172`、`api/runs.py:1405`、
> `api/external_sessions.py:215`。扩字段时五处都要过一遍 —— 这正是仓库既有教训
> 「同一语义分散多处实现,加约束要全处一起加」的适用场景。

对外 `GET .../sessions/{id}/messages` 每项变成 `{role, content, channel, created_at, run_id}`。
盖戳之前写入的消息两个字段都是 `null`。

### C. `message_count`

**第一步:拆 `transcript.py`。**

现在「取 checkpoint」与「抽轮次」揉在 `read_turns` 一个函数里。拆成:

- `_extract_turns(raw_messages, *, include_hidden) -> list[MessageTurn]` —— 纯函数
- `read_turns(checkpointer, thread_id, *, include_hidden)` —— 取 blob 后调上面那个

拆的理由不是好看,是**让计数与列表用同一个定义**。镜像表那摊语义债的根因就是两套定义各写
各的然后漂了;拆完之后 `count == len(_extract_turns(...))`,想漂都漂不了。

**第二步:落 `thread_meta.message_count`。**

新增列 `message_count INTEGER NULL`。语义:`NULL` = 尚未算过(迁移之前就存在的会话);
迁移之后由 `ThreadMetaStore.create` 写 `0`。**不给 server_default** —— 存量行必须留 `NULL`
而不是被填成 `0`,否则"没算过"与"真的空会话"就分不开了。

**第三步:谁来写。**

orchestrator `sse.py` `run_agent` 的 `finally` 块,照 `_dispatch_trajectory` /
`_dispatch_skill_run_usage` 的 fire-and-forget 范式,新增一个由控制面注入的
`thread_stats_recorder`。

选 `finally` 而不是控制面侧的理由:控制面有 **6 个 `run_agent` 启动点**
(`api/runs.py:730`、`api/runs.py:884`、`run_queue_worker.py:300`、`trigger_firing.py:280`、
`orphan_sweep.py:354`,以及经 `spawn_run` 共用的那条),挂控制面 = 6 处;挂
`run_agent` 的 `finally` = **1 处覆盖全部 6 个调用方 + 全部终局分支**
(自然结束 / `RunCancelledError` / `asyncio.CancelledError` / `MaxStepsExceededError` /
兜底 `Exception`)。

另一个写点:`trigger_delivery.inject_delivery` —— 它在 run 之外往会话追加助手消息,写完同步
更新计数。

**口径钉死 `include_hidden=False`**(第三方可见口径),列的 docstring 必须写明。否则将来有人
拿它当"全部消息数"用,就是镜像表那个坑再走一遍。

失败语义:best-effort,只记日志,不影响 run 终局 —— 与 `update_title`(`api/runs.py:1070`)
和 trajectory 记录一致。

### D. 已知软肋(三条,均判定可接受)

1. **teardown 路径可能漏。** `asyncio.CancelledError`(事件循环拆除)那条路上 await 不可靠 ——
   现有代码正因为这个故意不在那儿 dispatch trajectory。漏了的后果:该会话计数停在上一轮,
   **下次跑 run 自动修好**(recompute 而非 increment,天然自愈)。
2. **跑动中的会话计数偏旧。** 列表里正在执行的会话,`message_count` 是上一轮的值。列表本身
   已带 `running` 字段,前端能区分。
3. **存量会话是 `null`。** 不写回填迁移(要逐个读 checkpoint)。随各会话下次跑 run 自动填上。

单会话那个 messages 端点**不用**存的值 —— 它本来就在调 `read_turns`,直接 `len(turns)`,
永远精确。

## 四、块 1:请求体做厚

改的是 `ExternalRunRequest`(`api/agents.py:418`)。

### A. `inputs`

加 `inputs: dict[str, Any] = Field(default_factory=dict)`,透传给 `spawn_run` 拼的 `RunRequest`。
校验/渲染/422 全部现成(见 §一「勘误二」)。

### B. `files[]`

形状照 Dify,第三方眼熟:

```json
{"type": "image" | "document", "transfer_method": "local_file", "upload_id": "<从 /uploads 拿的>"}
```

> **与 P1 spec §五A 的命名差异**:P1 spec 写的是 `transfer_method ∈ {upload_id, url}`。本 spec
> 改用 Dify 的 `{local_file, remote_url}` —— P1 的写法把"传输方式"与"承载字段名"混成了一个词,
> 而 `upload_id` 同时又是字段名。以本 spec 为准。

`transfer_method` 当前唯一合法值是 `local_file`,**但字段现在就带上**:将来开 `remote_url` 只是
加一个枚举值(向后兼容),现在省掉则将来要改形状(破坏性)。

两类分发到已有的两条路,**不新增第三条存储路径**:

- `type: image` → `upload_id` 是 `expert_work://image/...`,并进 `image_refs`,复用现有
  `_validate_image_refs`(thread_id 绑定校验、`max_per_run`、`supports_vision` 全套)
- `type: document` → `upload_id` 是工作区文件名,拼进 HumanMessage 文本
  `[file attached: <name>]`,与现有 no-vision 图片路径的 `[image attached: ...]`
  (`api/runs.py:300`)同构。agent 有工作区工具,拿到名字即可读

**安全闸(文档路径)**:客户端给的是字符串,不能信。上传时走过 `_safe_workspace_name` 净化,
run 这侧必须**再净化一遍** —— 只接受纯文件名,含路径分隔符或 `..` 一律 422。

工作区按 `(tenant, user)` 隔离,而 `user_id` 就在同一个请求体里,所以越权读他人文件需要谎报
`user_id` —— 那属于「任一租户 key 可代理本租户任一用户」这个 P1 已文档化的既有信任模型,
不是本次新开的洞。

条数上限:`files[]` 整个数组 ≤ 64 项(沿用 `image_refs` 的上限)。其中 `type: image` 的那些
**另外**还要过 `_validate_image_refs` 的 `multimodal_max_images_per_run`,两道闸都生效。

`files[]` 与既有 `image_refs` 字段并存(不废弃,P1 已对接方不受影响);同一次请求两者都给时,
图片引用合并后再一起过上述两道闸。

### C. 幂等键

`Idempotency-Key` **请求头**,可选。依据:仓库自己的
`docs/architecture/07-INFRASTRUCTURE-GAPS.md:213` 就是这么记的,也是 Stripe/PayPal 惯例。

`agent_run` 加两列 + 一个部分唯一索引:

```sql
idempotency_key  TEXT NULL
request_digest   TEXT NULL
-- 部分唯一索引
(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL
```

**判定流程**:

1. 查该 `(tenant_id, key)`
2. 命中且 `request_digest` 相同 → 返回原 run,不新建
3. 命中但 `request_digest` 不同 → **422**,机器可读错误码
4. 未命中 → 插入。并发由唯一索引裁决:输的那个捕获 `IntegrityError` 后重查,返回赢家的 run

这是 CAS 单赢家,与 `approval/sql.py:mark_decided`(Stream 13.2)同构。

**关键:唯一索引建在 `agent_run` 上,所以"占键"与"建 run 行"是同一次插入,天然原子。**
不要实现成"先查键表、再建 run"—— 那样并发下会留下抢键失败但 run 行已建的孤儿。
键必须与 `run_manager.create` 的那次写入一起落。

`request_digest` = 规范化请求体的 sha256(键排序、`ensure_ascii=False`),**不含** header 里的
key 本身。

存指纹而非只认 key 的理由:第三方改了 `input` 却忘换 key 时,只认 key 的实现会静默返回旧 run
的结果 —— 调用方以为发了新活,拿回来的是旧答案,而且**没有任何信号**。

**窗口:永久。** 不设独立 TTL —— 索引只覆盖真带 key 的行,而 `agent_run` 行本来就为计费/分析
永久保留(`purge/user_purge.py` 对它是 ANONYMIZE 不是 DELETE),不额外撑存储。与 P1 spec §五D
「不设独立 TTL(随 run 行的既有留存策略)」一致。

**stream 模式的重放**(这条单独说,是本块唯一的结构性问题):

queue 模式好办 —— 重放直接返回原 `run_id`。stream 模式是 SSE,重试打过来时原 run 可能早已终态,
没法"重新流一遍"。

不采用「幂等键只支持 queue 模式」的方案:那等于把功能掏空。第三方 HTTP 客户端超时重发 stream
POST **恰恰**是幂等最该救的场景 —— 否则一次网络抖动 = 两个 run、两份账单。

采用:**重放时内部复用 `GET .../runs/{run_id}/events` 的响应体**。该端点(P1 已交付,
`api/external_events.py`)已经做了「终态 run 从持久 store replay,活着的 run 直接 live-attach」,
并带 `X-Expert-Work-Stream-Mode: replay|live` 响应头。客户端那一次 POST 重试透明地拿到同一个流,
不管原 run 是死是活。

降级:部署未配 `run_event_store`(P1 允许 opt-out)时,终态 run replay 不出内容 —— 退回返回
JSON + `run_id`,并在文档写明这一降级条件。

## 五、块 3:响应信封化

`POST /v1/agents/{code}/runs` 是**唯一形状不统一的对外端点**:其余 5 个 external 端点全走
`{success, data, error}` 信封,它在 queue 模式裸返 `{run_id, thread_id, status}`。

`spawn_run`(`api/runs.py:757`)被控制台(`api/runs.py:1079`)与对外(`api/agents.py:945`)共用,
202 响应在它内部拼。加一个 `envelope: bool = False` 参数,对外调用方传 `True`:

```json
{"success": true, "data": {"run_id": "...", "thread_id": "...", "status": "queued"}, "error": null}
```

stream 模式是 SSE,无信封可言(帧格式的文档化属 P3 范围)。错误响应 P1 已走 `external_error`
信封。所以本块实际改动就是 202 这一个形状。

**这是破坏性改动。** #1155 于 2026-08-12 合入并当日发上测试集群,尚无第三方接入,现在破的
成本约等于零;再拖就不是了。

## 六、块 4:客户端补完(建议在 P2 内拆独立 PR)

### 为什么在这里

2026-08-12 用户问:「这波改完,前端能做出 workbuddy / Claude 那类 agent 客户端的效果吗?」

据实回答是**不能**,而且缺的东西不在 P2 的前三块里:

- **还原 agent 交互过程**靠的是 SSE 帧,数据早就全在(对外流与调试台同源零裁剪,现发
  `updates` / `token` / `worker` / `guard` / `compaction` / `metadata` / `retry` / `approval` /
  `error` / `end` 十种帧),卡点是 `updates` 帧公开文档只有一行 —— 那是 **P3** 的头号内容。
- 但 P3 做完仍差两样**契约面**的东西,而它们与 P2 前三块同形状(对外端点补齐),放 P3 是错配。

所以列为块 4。它与前三块无代码耦合,**建议拆独立 PR** —— 前三块已含 2 次 DB 迁移 + 一个新的
orchestrator recorder,再塞 5 个端点会让单个 PR 过大。

### A. 工作区文件:列表 + 下载(高优先)

**问题场景**:agent 生成了一份报表 Excel 落在用户工作区,第三方界面上**给不出下载按钮** ——
文件出不来,这一整类"agent 产出物"的交互就不存在。

现状:`api/workspace.py` 有完整的概览(`GET ""`)/ 列表(`GET /files`)/ 下载(`GET /file`)/
删除(`DELETE /file`),但**四个全挂 `console_only()`** —— P1 的控制台平面收口是刻意锁的。

对外镜像 `GET /v1/agents/{code}/workspace/files` 与 `GET /v1/agents/{code}/workspace/file`:

- 复用 `_safe_workspace_relpath`、`infer_content_type`、`content_disposition_header`、
  `workspace_store.read_file`(MIME 嗅探、XSS 安全的 `attachment` + `nosniff`、路径二次校验、
  「权限失败 vs 文件不存在」分开 —— 全部白拿)
- 把 `ensure_single_tenant_scope` + `resolve_target_user_id` 换成 P1 的 `_external.py` 解析
  (`user_id` query → `external_subject_id` → `tenant_user`),`mint=False`
- 走 `require("session", "read")` scope 闸 + 对外信封

**不镜像 `DELETE /file`**:删文件是破坏性的,而第三方拿不到"这个文件重不重要"的上下文。
需要时单独拍。

### B. 会话管理:重命名 / 删除(中优先)

现状对外只有 `GET .../sessions` 与 `GET .../sessions/{id}/messages` —— **读得到,管不了**。
控制台侧 `api/sessions.py:879` rename(PATCH)、`:914` archive(DELETE)都在,同样 console-only。

对外补 `PATCH /v1/agents/{code}/sessions/{session_id}`(改标题)与
`DELETE /v1/agents/{code}/sessions/{session_id}`(归档)。归属校验走 `load_owned_session`,
`require("session", "write")`。

优先级低于 A 的理由:第三方 app 多半在自己库里存会话标题,重命名可以不依赖平台;但**删除**
关系到终端用户「删掉这段对话」的诉求,而记录在平台这边,绕不过去。

## 七、兼容与迁移

| 改动 | 兼容性 |
|---|---|
| 消息两个新字段 | 加字段,向后兼容;老消息返回 `null` |
| `message_count` | 加字段,向后兼容;存量会话 `null`,随下次 run 自愈 |
| `inputs` / `files[]` | 新增可选请求字段,向后兼容 |
| `Idempotency-Key` | 可选请求头,不带 = 今天的行为 |
| **202 信封化** | **破坏性** —— 见 §五 |
| 块 4 的 4 个新端点 | 纯新增,向后兼容;控制台侧的 `console_only()` 端点一个不动 |

数据库迁移两处:`thread_meta` 加一列;`agent_run` 加两列 + 一个部分唯一索引。

> alembic revision 标识符上限 32 字符(仓库既有教训)。

## 八、验收

1. 第三方调 `GET .../sessions` 拿到的每条会话带 `message_count`,数值与该会话
   `GET .../sessions/{id}/messages` 返回的条数一致(同一 `include_hidden=False` 口径)
2. 盖戳后新产生的消息,`created_at` 是 ISO8601、`run_id` 指向真实 run;同一次 run 产生的
   用户消息与助手消息 `run_id` 相同
3. 中间过程的 `commentary` 消息与最终 `final` 消息各带**自己的**时间戳(这是 P1 决策 1 选
   OpenAI Assistants 形状而非 Dify 形状的原因 —— Dify 把一轮压成一条,拿不到中间几句的分别时间)
4. 传一份文档 → `POST .../runs` 带 `files[]` 引用它 → agent 能读到该文件
5. 文档路径带 `../` 或路径分隔符 → 422
6. 同一 `Idempotency-Key` 重发同体请求(queue)→ 返回同一个 `run_id`,`agent_run` 只有一行
7. 同一 key 配不同请求体 → 422
8. 同一 key 重发 stream 请求 → 拿到原 run 的事件流(响应头 `X-Expert-Work-Stream-Mode`
   为 `replay` 或 `live`),而非新建 run
9. queue 模式 202 响应是 `{success, data, error}` 信封

块 4(若同期交付):

10. agent 在工作区生成一个文件 → 第三方用 `GET .../workspace/files` 列得到 →
    `GET .../workspace/file` 下得下来,且响应头是 `attachment` + `nosniff`
11. 拿 A 用户的 `user_id` 去下 B 用户工作区的文件 → 404(与控制台侧同款不区分语义的 404)
12. 工作区路径带 `../` → 400
13. `PATCH .../sessions/{id}` 改标题、`DELETE .../sessions/{id}` 归档,均只对本 `(user, agent)`
    名下的会话生效,越权 404

14. 真栈验收:测试集群端到端跑一遍上述全部

## 九、明确不做 / 留待拍板

以下四项在 2026-08-12 的能力盘点中被识别为「离一个完整 agent 客户端还差的东西」,**本 spec
不含**,记在这里以免下一轮又从零盘一遍。

| # | 缺口 | 性质 | 判断 |
|---|---|---|---|
| 1 | `updates` 帧解析文档 + 三帧补录 + SSE 三个 bug | **P3 范围** | 这是「看得见 agent」的真正阻塞项,能力已在流里,缺文档 |
| 2 | 重新生成 / 编辑重发 | **产品语义缺口** | 会话是 append-only checkpoint,没有「回退到某条消息重跑」的概念。要做是一道真设计题,不是加端点 |
| 3 | 消息级点赞/点踩 | 接口缺口 | 内部已有 feedback 表,对外零暴露。薄,但需先定「反馈给谁看、进不进评测回路」 |
| 4 | 工作区文件删除对外暴露 | 接口缺口 | 见 §六A —— 破坏性操作,第三方缺上下文,需单独拍 |

第 2 项是其中唯一的**产品**问题;其余三项都是工程量确定的补齐。
