# 对外文档站第四轮整改(读者视角 + 企业级语气)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `apps/admin-ui/docs-site/guide/` 十章全部改写成**第三方开发工程师(前端 / 后端)视角**、**企业级对外文档**的写法——读者只需要知道「什么时候来 / 里面是什么 / 我该怎么处理」,看不到任何平台内部实现与内部过程用语;语气正式克制,格式统一。

**Architecture:** 纯文档改动。写作规范 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md` 是唯一的风格依据;事实来源是当前文档 + 第三轮审计 `docs/superpowers/specs/2026-08-17-external-docs-readability-w3-audit.md`(枚举值已代码核实)。**这一轮的重点是减法与换视角,不是再加内容**:事实一条不丢(事实清单),但表述全部按读者需要重组。五个任务按文件切分并行;标题可以改(去标点、去内部词),锚点变化由各任务在 report 里登记 old→new,合并后由控制器统一修跨文件链接与侧栏。

**Tech Stack:** VitePress 1.6。

## Global Constraints

- **写作规范全文生效**:`docs/superpowers/specs/2026-08-17-external-docs-style-guide.md` 的 §1–§12。实施者开工前通读一遍;§3 词表、§6 标题、§7 加粗与提示框、§9 主语、§12 清单是硬约束。
- **读者视角**:每一句话问「第一次接入的外部工程师需要知道这个吗?」不需要就删;「他能看懂这个词吗?」看不懂就换。平台内部实现(编排、图、节点、写入、落库、checkpoint、supervisor)与内部过程(真栈实测、场景、审计、上一轮、Task、评审)一律不出现。
- **企业级语气**:正式、克制,不用口语、俏皮话、感叹号,不以「我们」谈实现,尽量不用第二人称(用「客户端」「调用方」)。
- **事实一条不丢**:改每一节前把该节所有规范性陈述(数值、上限、默认值、取值、错误码、必须/不会)抄进 report 做事实清单,改后逐条勾落点。**不新增未经代码核实的事实**;审计里的取值可以直接用。
- **公开文档红线**:不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径(含 `expert_work://`、`expert_work.`、`control_plane.`、`orchestrator.`、`packages/`、`services/`、`.py` 文件名、`file:line`)。
- **标题**:按规范 §6 改(去标点、去行内代码后接标点、去内部词);`##` 保留章节编号。**每改一个标题,在 report 里登记 `旧锚点 → 新锚点`**(锚点规则:VitePress slugify——小写、空格转 `-`、去标点、中文原样、`_` 转 `-`;数字开头的加 `_` 前缀);本文件内的链接自己改好;跨文件链接与侧栏由控制器合并后统一改。
- **示例值全站一致**:`user_id: "u-123"`;文档 `upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17`;图片 `upl_9b7d2c40-1e5a-4f88-b3c6-7a0d4e2f9c11`;run/session 用现有的固定 UUID。
- 每个 task 结束:`cd apps/admin-ui/docs-site && pnpm exec vitepress build .` 通过 + 死链脚本(`docs/superpowers/plans/2026-08-16-phase3-pr-b-artifacts.md` 1051–1081 行)——**本文件内**零死链(跨文件的死链若是因为别的任务改标题造成,登记在 report,不算失败)+ 红线 `rg` 为空 + 禁用词 `rg -n "节点|写入|落库|游标|帧|平台注入|真栈|场景|审计|上一轮|铸造|闸|终态|裸 |不透明|口径|载荷|落地|接线|坑|别赌|免得|一律|说白了|反正" <你的文件>` 逐条看(合法用法可保留,如「场景」在「使用场景」里)。

---

## 范例(目标写法,全体实施者照此定调)

### 范例一:3.2「心跳」

> ### 心跳行
>
> 心跳用来维持连接,并让客户端判断连接是否还活着。
>
> - 谁发:服务端通过当前这条 SSE 连接发出。实时连接(`mode: "stream"` 的响应流,以及 run 未结束时的事件接口)都会发;run 已结束后的事件续传不发,因为续传一次性返回、不会挂起等待。
> - 什么时候发:连接上连续 15 秒没有任何事件时发一行;有事件时不发。
> - 内容:一行以冒号开头的注释,不带 `event:` 和 `data:`,不占序号,断线后也不会补发。
>
> ``` [事件流片段]
> : heartbeat
>
> ```
>
> 客户端的处理方式:
>
> 1. 解析时忽略以 `:` 开头的行,不要按事件处理。
> 2. 把它当作连接存活的信号。服务端没有规定「多久没有数据就算断开」;建议客户端把读超时设为 45 秒(三个心跳周期),超时后按 3.6 的方式重连。3.5 的接收器示例使用的就是这个值。

### 范例二:3.2「带 id 的事件」

> ### 带 id 的事件与断线续传
>
> 先说明两个词。`id:` 是事件的序号行(格式见下一节);「续传」指断线重连后,服务端把客户端未收到的那一段事件重新发送(操作步骤见 3.6)。
>
> 只有「run 本身发生的事」带 `id:`。服务端会记录这些事件,因此能够续传;其它事件不记录、不续传。
>
> | | 事件 | 断线后会续传 |
> |---|---|---|
> | 带 `id:` | `metadata`、`updates`、`worker`、`guard`、`compaction`、`approval`、`retry`、`error` | 会 |
> | 不带 `id:` | `token`、`end`、`gap`、`truncated` | 不会 |
>
> 不带 `id:` 的四个事件,各自的原因:
>
> - `token` 是模型逐字输出的即时预览。断线期间的 `token` 不会补发;完整内容以 `updates` 为准。
> - `end` 是一条流自己的结束标记。重连之后会收到新的一个。
> - `gap` 与 `truncated` 描述的是这条连接的状况,不是 run 的事件。

### 范例三:3.4「updates → data 字段」

> #### data 字段
>
> `data` 是一个 JSON 对象。每个键表示「这一步由谁完成」,键对应的值是这一步的产出。
>
> ```js [data 的结构]
> { "agent": { "messages": [ … ], "step_count": 1, "_duration_ms": 2140 } }
> //  ↑ 这一步由谁完成    ↑ 这一步的产出
> ```
>
> 键的取值:
>
> | 键 | 这一步由谁完成 | 什么时候出现 |
> |---|---|---|
> | `agent` | 模型:生成回答,或决定调用工具 | 每次 run 都有 |
> | `tools` | 工具:执行模型发起的调用 | 模型调用了工具时 |
> | `memory_recall` | 平台:召回相关的长期记忆 | Agent 开启了长期记忆 |
> | `workspace_ingest` | 平台:读取工作区文件 | Agent 接入了工作区 |
> | `planner` | 平台:生成或更新计划 | Agent 使用「先规划再执行」模式 |
> | `reflect` | 平台:对结果做自检 | Agent 配置了反思环节 |
> | `memory_writeback` | 平台:把值得记住的内容写回长期记忆 | Agent 开启了记忆回写 |
>
> 一个 Agent 会出现哪些键,由租户管理员在管理控制台的配置决定。平台后续可能新增键;客户端遇到不认识的键应忽略,不要报错。
>
> 三条处理规则:
>
> - 一个事件通常只有一个键;存在并行分支时会有多个。请遍历全部键,不要只取第一个。
> - 键对应的值可能是 `null`,表示这一步没有产出(平台的辅助步骤常常如此)。读取 `messages` 前先判断是否为 `null`。
> - 值里除下表三个字段之外的键是平台内部使用的,请忽略。
>
> 值的字段(值不为 `null` 时):
>
> | 字段 | 类型 | 说明 |
> |---|---|---|
> | `messages` | array | 这一步新产生的消息,可能为空数组。每一项的结构见下文「messages 里的两种消息」 |
> | `step_count` | integer | 这一步的编号,从 1 开始。只有 `agent` 这一键的值带此字段 |
> | `_duration_ms` | integer | 距上一个 `updates` 事件经过的毫秒数(第一个 `updates` 从 run 开始算起)。`token` 等其它事件不重置这个计时 |

对照旧文可以看到差别:不再有「图 / 节点 / 写入 / 落库 / 真栈实测 / 第一天就会踩到的坑」;`null` 有明确主语;内部键不再逐个列出;一段只一处加粗;标题无标点。

---

## 文件结构与任务划分

| Task | 文件 | 模型 |
|---|---|---|
| 1 | `guide/sse-events.md` | opus |
| 2 | `guide/chat.md`、`guide/quickstart.md`、`guide/best-practices.md` | opus |
| 3 | `guide/query.md`、`guide/run-control.md` | opus |
| 4 | `guide/errors.md`、`guide/conventions.md`、`guide/auth.md` | opus |
| 5 | `guide/examples.md`(只改正文与代码注释的语气、标题、词表;代码逻辑不动) | sonnet |

五个任务文件不相交,并行五个 worktree。`.vitepress/config.mts` 侧栏由控制器合并后改。

---

### Task 1: sse-events.md 整章按规范重写

**Files:** Modify `apps/admin-ui/docs-site/guide/sse-events.md`

- [ ] **Step 1** 通读规范与三个范例;把 3.2 与 `updates` 按范例落地(范例是目标,可按上下文微调,不降低标准)。
- [ ] **Step 2** 3.1 / 3.3 / 3.5 / 3.6 与 3.4 其余 11 个事件小节逐节按规范重写:四段结构(什么时候发 / data 字段 / 示例 / 客户端怎么处理——「前端怎么渲染」统一改名);字段表三列;取值写进说明;首段答「谁→谁/何时/客户端做什么」;依赖概念首次出现给一句话定义 + 链接;删内部词、内部过程用语、口语;一段一处加粗;提示框一框一话题;标题去标点。示例载荷裁剪到最小但完整;示例代码注释正式化。
- [ ] **Step 3** 事实清单勾完;标题 old→new 锚点表写进 report;本文件内链接改好。
- [ ] **Step 4** 构建 + 本文件死链 + 红线 + 禁用词 → Commit `docs(external-api): SSE 章按对外文档规范重写(第四轮)`

### Task 2: chat.md / quickstart.md / best-practices.md

**Files:** Modify 三文件

- [ ] **Step 1** 通读规范与范例。
- [ ] **Step 2** 逐节重写:接口小节按规范 §4 顺序;参数表三列;2.6 附件三步保留结构、去口语;2.8 幂等保留全部事实;quickstart 保持「四步跑通」骨架,语气正式;best-practices 每条建议改成「做什么 / 为什么」两句。
- [ ] **Step 3** 事实清单 + 锚点表 + 本文件内链接。
- [ ] **Step 4** 构建 + 死链 + 红线 + 禁用词 → Commit `docs(external-api): 对话 / 上手 / 最佳实践三章按对外文档规范重写(第四轮)`

### Task 3: query.md / run-control.md

**Files:** Modify 两文件

- [ ] **Step 1–4** 同上;重点:run status 八值表保留但表头与说明按规范;分页规则写成一段通则;审批决策与取消保留全部事实;工作区文件 / 产物 两节区分清楚;删「任务」指 run 的用法。Commit `docs(external-api): 查询 / 控制两章按对外文档规范重写(第四轮)`

### Task 4: errors.md / conventions.md / auth.md

**Files:** Modify 三文件

- [ ] **Step 1–4** 同上;重点:错误码总表保留「端点」列但表头名词化、说明每格 ≤ 2 句;8.x 各节按「什么时候会遇到 / 响应长什么样 / 怎么处理」三段;conventions 各节一句话定义在前;auth 三档权限表三列。Commit `docs(external-api): 错误码 / 约定 / 认证三章按对外文档规范重写(第四轮)`

### Task 5: examples.md 语气与词表

**Files:** Modify `guide/examples.md`

- [ ] **Step 1** 只改:章首与各场景的说明段、代码注释与 docstring 里的口语与禁用词、代码块标题;**代码逻辑与请求 / 响应形状一字不动**。
- [ ] **Step 2** 构建 + 死链 + 红线 + 禁用词 → Commit `docs(external-api): 示例章说明与注释按对外文档规范统一语气(第四轮)`

---

## 收尾

五支合回 → 控制器统一改跨文件链接与侧栏(按各 report 的锚点表)→ 全站构建 + 死链 + 红线 → 逐章「外部工程师冷读 + 企业文档编辑」评审(opus,按规范 §12 清单)→ 一波修复 → PR → 合并 → 发测试。
