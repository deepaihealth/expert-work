# 12 更新记录

本页按时间倒序记录对外接口的变更，最新的一条在最上面。本页从 2026-10-28 开始记录，此前的变更已逐项单独通知，不在本页补录。

## 12.1 变更的通知方式

平台调整对外接口时，按变更的类别决定是否提前通知调用方：

| 类别 | 例子 | 通知方式 |
|---|---|---|
| 新增可选字段 | 响应、SSE 事件或 Webhook 通知里多出一个字段 | 不单独通知，发布时记在本页 |
| 已有枚举新增取值 | `status` 多出一个取值、SSE 多出一种事件 | 提前通知，并在本页以「待发布」列出 |
| 行为变化 | 字段不变，但取值的含义或出现条件改变 | 提前通知，并在本页以「待发布」列出 |

客户端按以下约定解析，平台新增内容时就不会出错：

- 遇到不认识的字段时忽略它，不要报错。
- 遇到不认识的 SSE 事件时跳过它，继续读流，见 [3.3 事件一览](./sse-events#_3-3-事件一览)。
- 遇到不认识的枚举取值时，按这个字段说明里写的规则处理，例如 `exit_reason` 的未知取值按「没有正常完成」处理。

「待发布」的条目在正式发布之后改为实际日期。

## 12.2 变更记录

### run 被平台限制截停时单独标记

| 项目 | 内容 |
|---|---|
| 日期 | 待发布，计划日期待定 |
| 摘要 | 被平台的步数、token 预算或无进展限制截停的 run，`status` 由 `success` 改为新取值 `incomplete`。run 列表、对话条目与 Webhook 通知同时给出要做的事是否做成，以及没有解决的工具失败 |
| 影响范围 | 读取 run 最终状态的调用方：解析 SSE `end`、轮询 run 列表或对话条目、订阅 Webhook、渲染子任务状态或 `guard` 事件的客户端 |
| 调用方的改动 | 需要改动：对 `status` 做分支或枚举校验的代码要能处理 `incomplete`，判断这一轮是否交付改为看 `completed` |

#### 字段变化

| 位置 | 变化 | 调用方处理 |
|---|---|---|
| SSE `end` 事件，包括续传时重新发送的 `end` | `status` 新增取值 `incomplete`，表示 run 被平台限制截停，此时 `exit_reason` 为 `max_steps`、`token_budget` 或 `no_progress`。这类 run 以前是 `success` | 对 `status` 做分支或枚举校验的代码要认这个新值 |
| SSE `end` 事件 | 新增 `unresolved_failures`。`completed` 与 `exit_reason` 原已存在 | 按需读取 |
| 5.4 run 列表的每条 run，5.8 对话条目的 `runs[]` | `status` 同样会出现 `incomplete`。新增 `completed`、`exit_reason`、`unresolved_failures` | 用这两个接口判断结果的，读新字段 |
| 5.4 run 列表的 `status` 筛选参数 | 新增可选值 `incomplete`，只返回被限制截停的 run；按 `success` 筛选时不再包含它们 | 按 `success` 筛选、再自己判断是否截停的，可改为直接筛 `incomplete` |
| Webhook `run.completed` 与 `run.failed` 的 `payload` | 新增 `completed`、`exit_reason`、`unresolved_failures`。被限制截停的 run 照样发 `run.completed`，`status` 为 `incomplete`，不新增事件类型 | 不要只凭事件名判断成功，再看 `status` 与 `completed` |
| SSE `worker` 事件 `kind` 为 `end` 时的 `outcome` | 改为开放取值，写明 `approval_blocked`（遇到需要人工审批的工具而停下）。子任务没有给出回答时，由 `success` 改报已有的取值 `cancelled` | 只有 `success` 表示子任务完成，未知取值按「没有正常完成」处理 |
| 5.8 对话条目里 `tool_call` 的 `worker.status` | 如实给出子任务的结果，会出现 `approval_blocked`，今后还可能新增取值。没有结束结果的子任务保持 `running` | 用严格枚举渲染子任务状态的界面要处理新值，未知值按「没有正常完成」处理 |
| SSE `guard` 事件 | 接近步数上限时，每个 run 额外发一条 `kind` 为 `warning`、`guard` 为 `max_steps` 的事件。`guard` 事件也可能来自子任务，事件里不标明来源 | 不要据此判断这一轮已经结束或失败，最终结果以 `end` 为准 |
| 停在审批处、又因同一会话发起新一轮而作废的 run | `status` 变为 `interrupted`，同时保留 `completed` 为 `false`、`exit_reason` 为 `approval_pending`，不是 `null` | 读 `completed` 时，不要假设 `interrupted` 的 run 一定是 `null` |

`unresolved_failures` 是到 run 结束时仍未解决的工具失败，形如 `[{"tool": "bash", "kind": "unknown"}]`，最多 20 条，只有工具名和失败类型，不含参数、路径或工具输出。没有记录时，run 列表、对话条目与 Webhook 里是 `null`，`end` 事件里不出现这个字段；出错或被取消的 run 以及这次变更之前的 run 都没有记录。这次变更之前结束的 run 可能有 `completed` 却没有这个字段，按「不清楚」处理，不要当成 `[]`。`[]` 表示没有未解决的失败，`kind` 的取值见 [11.5](./webhooks#run-completed-与-run-failed)。

三个字段各回答一个问题：`status` 说的是这次 run 怎么结束的，`completed` 说的是要做的事做成了没有，`unresolved_failures` 说的是没做成时哪些工具失败了。

#### 行为变化

`completed` 为 `false` 的情况会比现在多。以下情况以前算做成，改后算没做成：

- 脚本执行失败，退出码不为 0。例外：退出码为 1 且没有错误输出，通常表示搜索没有结果，不算失败。
- 外部工具（MCP）返回错误。服务端标记为只读的查询类工具不算。
- 提交、修改、删除类的网页请求返回 4xx 或 5xx。查询类（`GET`）请求不算。
- 子任务没有做完。
- 「执行有风险的操作」这类审批被用户拒绝。
- 动作被平台的安全审查拦下。

之后对同一个对象的调用成功，之前的失败就算已解决：写文件、改文件看工作区里的文件路径，登记产物看产物名，其它工具看工具本身。只读的查询类工具失败和可以重试的临时失败不会列出。完整规则见 [3.4 的 `end`](./sse-events#end)。

其它行为变化：

- Agent 连续多次重复同一个调用时，平台先提醒它换一种做法，这算一次原地打转。同一次 run 里原地打转的次数达到上限时这一轮收尾，`status` 为 `incomplete`，`exit_reason` 为 `no_progress`。上限取 Agent 配置的无进展上限，没有配置时是 2 次；次数按整次 run 累计，中间有正常的调用也不清零。以前第一次出现就以 `success` 结束。
- 被限制截停时，最后的回复会分开说明做完了什么、没做完什么，不再把没有登记的文件说成已经交付。终端用户发一条新消息（例如「继续」）可以接着做。
- `status` 为 `incomplete` 的 run 照样有最终回复文本。它的产物清单 `artifacts` 可能为空，也可能只有一部分。
- 文档更正：`reason_kind` 为 `policy_gate` 的审批被拒绝，不一定终止 run。Agent 配置里列为强制审批的工具被拒绝时 run 终止；工具调用审查转来的审批被拒绝时 run 继续。审批事件里没有字段能在决策之前区分这两种来源，拒绝之后以 `end` 的 `exit_reason` 为准，详见 [4.2 审批决策](./run-control#_4-2-审批决策)。

#### 调用方的改动

服务端：

1. 读取 `status` 的地方都要认 `incomplete`，包括 SSE 解析、run 列表轮询和 Webhook 处理。它的含义是「已结束，但没有做完」，既不是失败也不是成功。轮询 run 列表或用量接口、等 run 结束才停的，要把 `incomplete` 算作结束状态，否则被截停的 run 会让轮询一直停不下来。
2. 不认识的状态值按「没有正常结束」处理，不要报错，也不要丢弃这条结果。
3. 判断这一轮是否交付的逻辑改为看 `completed`，例如是否回写业务结果、是否把产物同步给终端用户。
4. 订阅了 Webhook 的，收到 `run.completed` 之后再看 `status` 与 `completed`。
5. 子任务状态留意 `approval_blocked`、`cancelled`，以及今后可能新增的取值。

界面：

1. `incomplete` 或 `completed` 为 `false` 的一轮，与「成功」区分显示，例如「未完成：步数用完」，并引导终端用户发「继续」接着做。
2. 这一轮不要显示成失败或报错：它有正常的回复文本，照常展示。
3. `status` 到文案、颜色的映射表加上 `incomplete`，并给未知值一个兜底显示。
4. 子任务状态加上 `approval_blocked` 的显示，未知值兜底；没有给出回答的子任务现在显示为 `cancelled`。
5. `guard` 事件只作提示，最终结果以 `end` 为准；新增的 `warning` 不要显示成「已截停」。
6. 依赖「每轮必有产物」的界面要容错，`incomplete` 的 run 可能没有产物。
7. 可选：在面向内部人员的界面展示 `unresolved_failures`，例如「未解决：bash（unknown）」，帮助定位没做成的原因。不建议展示给终端用户。

上线前，按 [9.6 联调自测清单](./best-practices#_9-6-联调自测清单) 复核一遍事件处理与状态显示。

#### 相关章节

- [1.3 读懂返回的事件流](./quickstart#_1-3-读懂返回的事件流)：`status` 取值的摘要。
- [2.1 一次对话的执行过程](./chat#_2-1-一次对话的执行过程)：run 的最终状态列表。
- [3.4 的 `end`](./sse-events#end)、[`guard`](./sse-events#guard)、[`worker`](./sse-events#worker)：三个事件的字段与处理方式。
- [4.2 的 decision 取值](./run-control#decision-的取值)：审批被拒绝之后怎么判断这一轮的结果。
- [4.2 决策之前发起新一轮](./run-control#决策之前发起新一轮)：作废的审批与 `interrupted`。
- [5.4 run 列表](./query#_5-4-run-列表)、[5.8 对话条目](./query#_5-8-对话条目)：新增的三个字段。
- [11.5 Webhook 的 run 事件](./webhooks#run-completed-与-run-failed)：`run.completed` 与 `run.failed` 的 `payload`。
