# 第三方对接 API v1 设计

> 2026-08-11 定稿。范围:给第三方 app 的对外契约(7 个端点)+ 请求/响应侧改造 + 文档站重构。
> 起因:文档站 #1151 上线后用户逐页验收,发现对外只有 run 一个接口、附件能力表达不了、
> SSE 响应侧无法还原交互过程、会话管理接口是控制台形状。

## 一、背景

第三方 app 明日对接。现状盘点(逐条核过代码):

- **对外只有 `POST /v1/agents/{agent_code}/runs` 一个端点**。会话管理、取消、事件回放走的是
  `/v1/sessions/*` 控制台 API——对机器身份既不好用(无 `user_id` 维度)也不安全(归属校验对
  机器主体直接放行整租户)。
- **附件能力表达不了**:请求体只有 `image_refs`,文档走"落工作区、Agent 自己读"的旁路,
  run 请求上看不见;且文档上传对 API Key 恒 400(落盘要求"调用者本人的工作区",机器身份没有)。
- **响应侧能力是全的但文档是空的**:对外流与调试台**同一条流零裁剪**(共用 `spawn_run`),
  但承载 90% 信息的 `updates` 帧文档只有一行,`worker`/`guard`/`compaction` 三帧未记录。
- **三个断线重连的真 bug**(见 §六),其中两个静默丢数据。
- **没有 run 级取消**:`POST /v1/sessions/{id}:cancel` 只改会话状态,执行引擎全程不读会话状态,
  正在跑的 run 继续烧 token 直到自然结束;`queue` 模式完全无法中止。

## 二、范围

### 对外契约(全部挂 `/v1/agents/{agent_code}/` 命名空间)

| # | 端点 | 状态 |
|---|---|---|
| 1 | `POST /v1/agents/{code}/runs` | 已有,改造 |
| 2 | `POST /v1/agents/{code}/runs/{run_id}:cancel` | 新建 |
| 3 | `GET /v1/agents/{code}/runs/{run_id}/events` | 新建(对外回放/断线重连) |
| 4 | `GET /v1/agents/{code}/sessions` | 新建(按 `user_id` 列会话) |
| 5 | `GET /v1/agents/{code}/sessions/{session_id}/messages` | 新建 |
| 6 | `POST /v1/agents/{code}/uploads` | 新建(文件上传) |
| 7 | `POST /v1/agents/{code}/runs/{run_id}:decide` | 新建(审批决策) |

### 不做什么

- **不做会话级 cancel / 归档 / 彻底删除的对外暴露**——那是不可逆关闭(会话作废,后续 run 一律 409),
  属管理后台职能。终端用户的「停止」按钮映射到 run 级取消(停这次执行、对话仍可续)。
- 不做 audio / video 附件类型(只做 image / document)。
- 不做 webhook 回调(第三方走 SSE + 回放)。
- 不做 OpenAPI 自动生成(手写文档更准,沿用 #1151 的判断)。
- 不做文档站侧栏内联搜索框(VitePress 自带顶部全站搜索弹窗,功能等价;做成截图那样要写自定义主题
  组件,收益不抵成本)。

## 三、架构决策

### A. 命名空间统一

对外端点全部挂 `/v1/agents/{agent_code}/` 下,理由:与现有 run 端点一致;`agent_code` 在路径上
让每个请求天然带上 Agent 维度,可复用现成的 kill-switch 闸(Agent 下线 → 403)与归属校验。

### B. 控制台平面对 API Key 收口

`/v1/sessions/*`、`/v1/approvals*`、`/v1/runs`、`/v1/uploads/{image_id}` 对
`subject_type == "service_account"` 主体一律 403。

理由:攻击面缩到「§二 那 7 个端点 + 既有的 `POST /v1/agents/{code}/sessions`(会话绑定)」
这 8 条路由;第三方不会依赖上内部形状,日后改控制台 API 不打断对接。
参照 Dify「API 无法访问 WebApp 创建的会话」的隔离原则。

**实现注意**:#1153 已给这些端点装了 `require_key_scope`,收口后那批测试的期望要改
(从「read key 通过读闸」变成「任何 key 403」)。这是有意收紧,不是回归。

### C. 归属校验复用现成样板

`agents.py:_resolve_session` 已经在做三重校验:会话存在 + `meta.user_id == end_user.id` +
`meta.agent_name == agent_code`,任一不满足 404(隐藏存在性)。与 Dify 的 `user` 校验同构。

**所有新端点复用这条通路**:必收 `user_id`,解析成 `tenant_user`,校验目标资源属于
(tenant, user, agent)。run 级端点先 run → thread 再套同一校验。

### D. 外部 `user_id` 命名空间隔离

**问题**:第三方传的 `user_id` 字符串与员工的 Keycloak `sub` 存在**同一命名空间**
(`agents.py:329` 用 `subject_type="user"` + 裸 `user_id` 解析)。第三方若传入某员工的
Keycloak UUID,会解析到该员工,进而列出其调试台会话。风险不高(需猜中 UUID)但契约期该封死。

**方案对比**:

| 方案 | 做法 | 代价 |
|---|---|---|
| 1. 新 `subject_type="end_user"` | 外部铸造改用新类型 | ❌ **有连带故障**:`agent_users.py:377`(用户维度运维页)与 `purge/user_purge.py` 都按 `subject_type="user"` 取数——外部用户会从运维页消失,且**变得无法删除**。需同步改 ~10 个消费点 |
| 2. 拒绝像内部身份的 `user_id` | 外部层拒收 UUID 格式 | ❌ 对第三方是不自然约束(不能用 UUID 当用户 ID) |
| 3. **subject_id 加前缀**(推荐) | 外部铸造存 `ext:{user_id}` | ✅ `subject_type` 不变,运维页/删用户链路全部照常;`ext:` 前缀永不与 UUID 碰撞 |

**取方案 3**。存量处理:实施前先查测试集群有多少 API 铸造的 `tenant_user` 行——若可忽略则直接切,
否则出一次性迁移(识别依据:该 user 名下 thread 的创建来源)。生产尚无第三方数据。

副作用:运维页上外部用户显示为 `ext:abc-123`。可接受(反而更清楚它是外部身份)。

## 四、逐端点契约

通用:`Authorization: Bearer <key>`;统一信封 `{success, data, error}`;`user_id` 为第三方自有
标识字符串(1–255 字符);所有 404 一律隐藏存在性(不区分"不存在"与"不属于你")。

### 1. `POST /v1/agents/{code}/runs`(改造)

```jsonc
{
  "user_id": "abc-123",              // 必填
  "session_id": "uuid",              // 可选,省略=开新会话
  "input": "帮我看下这份合同",
  "inputs": {"lang": "zh"},          // 新增:提示词模板变量(对齐内部接口能力)
  "mode": "stream",                  // stream | queue
  "files": [                         // 新增:统一附件数组,替代 image_refs
    {"type": "document", "transfer_method": "upload_id", "upload_id": "..."},
    {"type": "image",    "transfer_method": "url", "url": "https://.../a.png"}
  ],
  "untrusted_content": ["邮件正文…"],  // 已有:防注入隔离通道
  "idempotency_key": "order-8899"    // 新增,见 §五-D
}
```

`image_refs` 保留兼容(内部前端在用);与 `files` **同时出现报 400**,不做隐式合并。

### 2. `POST /v1/agents/{code}/runs/{run_id}:cancel`(新建)

Body `{user_id}`(必填,归属校验)。**stream 与 queue 两种模式都必须能停**——这是我们相对
Dify(「仅支持流式模式」)明确胜出的一点,且 queue 才是最需要取消的场景。

复用现成的两级取消原语(生产在用,支撑「租户停用」与「Agent 下线」):

- run 在本副本 → `run_manager.cancel(run_id)` 置 `abort_event`,**立即停**;
- run 在其他副本 → `run_store.request_cancel(...)` 写库标志,持有方下次租约心跳 CAS 失败 →
  `_renew_lease` 置其 `abort_event`,**一个心跳周期内停**。

响应:`{success: true, data: {run_id, stopped: true|false}}`。`stopped=false` 表示 run 已终态。
幂等(重复取消返回同样结果,不报错)。

### 3. `GET /v1/agents/{code}/runs/{run_id}/events`(新建)

对外回放/断线重连。`?user_id=&since_seq=&limit=`。语义见 §六(三个 bug 在此一并修)。

### 4. `GET /v1/agents/{code}/sessions`(新建)

`?user_id=`(**必填**)`&limit=&offset=`。返回该用户在该 Agent 下的会话列表。

每项:`{session_id, title, created_at, updated_at, message_count, running}`。
`running` = 该会话当前是否有 run 在执行(底层 `has_inflight` 现成),让第三方列表能直接标注
"执行中",不必逐个查。

> **⚠️ P1 未交付,已挪 P2(用户裁定 2026-08-12):`message_count`。**
> P1 实际返回 `{session_id, title, created_at, updated_at, running}`,少一个 `message_count`。
> 根因与下面 §四-5 的两个字段同源(消息本体只在 LangGraph 检查点里,SQL 侧没有可直接
> 聚合的权威行):要么每列一行会话就展开一次检查点(列表页 N 次反序列化),要么读
> `thread_message_sync.message_count` —— 那是 `TranscriptMirrorSweep` 的水位行,**扫到才有值**
> (刚建的会话根本没有这一行),且只计入文本轮次,对外当权威计数会给出滞后/为 0 的答案。
> 两条路都要真做,不是漏写一行。加字段向后兼容,晚补不破坏已对接的第三方。



### 5. `GET /v1/agents/{code}/sessions/{session_id}/messages`(新建)

`?user_id=`(必填)`&limit=&offset=`。现有内部端点返回 `{role, content, channel}`,已足够干净,
但**缺时间戳、缺所属 run、缺分页**——对外版本补上:
`{role, content, channel, created_at, run_id}` + 分页信封。

> **⚠️ P1 未交付,已挪 P2(用户裁定 2026-08-12):`created_at` 与 `run_id`。**
> P1 交付的是 `{role, content, channel}` + 分页信封;分页做了,两个新字段没做。
>
> **根因**:LangGraph 的检查点里**压根没存**逐条消息的时间戳与所属 run —— `MessageTurn`
> 只有 `seq/role/content/channel`,没有任何可以推出"这条消息什么时候产生、属于哪次 run"的
> 信息。所以这不是映射时漏写一个字段,而是数据源里没有这份数据。
>
> **补齐要二选一**:
> (a) 改**写入**路径,把时间戳 / `run_id` 塞进消息元数据 —— 影响所有 agent 执行路径,
>     且只对改动之后写入的消息生效(历史消息补不出来);
> (b) 走**消息镜像表**。注意镜像表已经存在(`thread_message` / `thread_message_sync`,
>     迁移 0106),但它是给会话浏览器做内容检索的**异步索引**,不是对外服务路径:
>     `thread_message.created_at` 是 sweep **写入镜像的时刻**,不是消息产生的时刻;没有
>     `run_id` 列,连 `channel` 都没有;而且 sweep 没扫到的会话在表里根本不存在。
>     所以这条路是"扩列 + 改由写入侧同步 + 回填历史",不是"读现成的表"。
>
> P1 的目标是契约地基 + 安全收口,这三个字段(连同 §四-4 的 `message_count`)是展示增强,
> 不阻碍第三方跑通;P2 本来就要重做请求/响应体,一并做。加字段向后兼容,晚补不破坏已对接方。

### 6. `POST /v1/agents/{code}/uploads`(新建)

`multipart/form-data`:`file` + `user_id` + **可选 `session_id`**。

**约束(实现前已核实)**:图片的引用 URI 与对象存储键里**硬编码了会话 ID**
(`expert_work://image/{tenant_id}/{thread_id}/{image_id}{ext}`,存储键
`{tenant_id}/uploads/{thread_id}/{image_id}{ext}`),且 `image_upload.thread_id` 非空。
所以图片上传**无法真正脱离会话**——彻底解耦要改 URI 格式 + 存储布局 + 表约束,代价远超收益。

**取法**:`session_id` 可选,省略时**上传端点顺带建会话并把 `session_id` 一并返回**。
对第三方而言仍是"直接传文件"(不必先单独调建会话接口),两步走完;拿到的 `session_id`
正好是下一步发起 run 要用的。同一会话的后续上传显式带上它,避免每传一个文件建一个会话。

返回 `{upload_id, session_id, type: "image"|"document", mime, size}`,`upload_id` 填进 `files[]`。

文档类落用户持久工作区(与会话无关),但为形状统一同样回传 `session_id`。

**顺带修掉文档上传对 API Key 的 400**:落盘目标从"调用者本人工作区"改为**请求声明的终端用户
的工作区**(`user_id` 解析出的 `tenant_user`)。这是那条 400 的根因。

支持类型沿用现状:图片 png/jpeg/webp/gif;文档 pdf/docx/xlsx/pptx/txt/md/csv。

### 7. `POST /v1/agents/{code}/runs/{run_id}:decide`(新建)

Body `{user_id, decision: "approve"|"reject"|"modify", modified_args?, reason?, idempotency_key?}`。

沿用内部 resume 的语义:`stream` 模式返回**续跑的 SSE 流**(第三方可继续接);`queue` 模式返回
202 + 续跑 `run_id`。两种模式下续跑都用**新的 `run_id`**(响应头 `X-Expert-Work-Run-Id`),
文档必须写明——否则第三方会拿旧 run_id 去回放,收到的是审批前那一段。

## 五、请求侧改造

### A. 统一 `files` 数组

`type ∈ {image, document}`;`transfer_method ∈ {upload_id, url}`。下游分发到现有两条路
(image → 对象存储引用;document → 用户工作区),**不新增第三条存储路径**。

### B. `transfer_method: url` 远程拉取

拉到的字节**当场落成与上传完全相同的产物**(不做懒加载/按引用),这样配额扣减、审计、EXIF 清洗、
ZIP 炸弹检查全部复用现有通路;对方 URL 过期或文件被改也不影响已开始的会话。

**五道防护(缺一不可)**:

1. **禁跳转** —— `follow_redirects=False`。合法域名 302 到云元数据地址(`169.254.169.254`)是
   最容易漏的 SSRF 路径。跳转一律 400 并给明确错误。
2. **DNS 钉住** —— `validate_remote_url`(静态)+ `resolve_and_pin_host`(解析后校验全部结果、
   连接钉住的 IP)。**标准要高于 MCP 探测**:MCP URL 是租户管理员在控制台配的(低频半可信),
   文件 URL 是第三方每请求带的(高频不可信)。
3. **流式限长** —— 不信 `Content-Length`,边下边计数,超限当场掐断。
4. **嗅探真实类型** —— 不信 URL 后缀与响应头,按字节判定后再过白名单。
5. **紧超时 + 预算** —— 单文件约 10s;多文件并发拉取(上限 4)且有总预算;远程文件条数上限
   建议 8(低于 `upload_id` 的 64)。

配额:拉取完成后按**实际字节数**走既有 `check_admission`。

**文档必须写明**:URL 必须公开可拉取——签名过期链接、需认证头的链接都不行(Dify 的用户
大量栽在传了预览页链接而非直链)。

### C. `inputs` 字段

对齐内部 `RunRequest` 已有的提示词模板变量能力,沿用其上限校验。

### D. 幂等键

`agent_run` 加 `idempotency_key` 列 + `(tenant_id, idempotency_key)` 部分唯一索引。

命中已存在的键时:
- `queue` 模式 → 返回原 run 的 `run_id`(202),不新建;
- `stream` 模式 → **接到原 run 的事件流上**(等价于调回放端点),而不是 409。

不设独立 TTL(随 run 行的既有留存策略)。

## 六、响应侧修复

### A. `updates` 帧写透(文档)

这是承载 90% 信息的帧,现文档只有一行。要逐项给出解析方法 + 真实样例:
工具调用(`tool_calls[] = {id, name, args}`)、工具结果(ToolMessage:`tool_call_id`/`content`/
`status`/`additional_kwargs.duration_ms`/`artifact`)、思考过程(`additional_kwargs.reasoning_content`)、
计划(`plan.{goal, steps[]}`)、逐步用量(`usage_metadata`)、工具失败(`tool_failures[]`)。

### B. 补 `worker` / `guard` / `compaction` 三帧文档

实际会发但未记录。`worker` 尤其可惜——那是子任务/委托的完整子时间线,第三方按"忽略未知
event"实现就白丢了。注意 `worker` 帧内文本是**摘要截断**(content 500 / args 200 / result 500 字符),
文档要标明。

### C. 修 `seq` 错位(**静默丢数据**)

**根因**:bridge 的帧序号对每个发布帧递增(**含 token 帧**),落库序号跳过 token 帧——两个计数器
错位。而文档教客户端"从 SSE 帧 id 里取 seq 当 `since_seq`"→ 回放时**跳过真实存在的帧**。

**修法**:落库帧的 `id` 一律使用**落库 seq**;**token 帧不带 `id:` 行**(它本就不可回放,给 id 是
误导)。这样任何 id 里的 seq 都是合法的 `since_seq`。

副作用:文档"除 `end` 外每帧都有 id"要改成"除 `end` / `token` 外";前端 Gantt 用帧 id 的毫秒段,
而 token 帧本就不进 `turn.events`,不受影响(需在实现时复验)。

### D. 修 live 分支忽略 `since_seq`

现状:run 还在跑时重连,`_stream_live` 既不传 `since_seq` 也不传 `last_event_id`,参数被静默丢弃。

**修法**:重连一律先从库里补 `seq > since_seq` 的帧,再挂上实时流,按 seq 去重接合。
**接合点是本项的主要风险**(补库与实时流的重叠窗口),实现时必须有针对性测试。

### E. 回放分页

现状:一次最多 500 帧、无游标,且**每次都在末尾补 `end`**——客户端按"见到 end 即结束"处理,
超长 run 只拿到前半截还以为拿全了。

**修法**:截断时**不发 `end`**,响应头给 `X-Expert-Work-Next-Seq`;客户端循环拉到收到 `end` 为止。

### F. 取消态明确信号

现状:run 被取消只发 `end`(`data: null`),第三方分不清"正常答完"与"被取消",得额外查 REST。

**修法**:`end` 帧带 `{status: "success"|"interrupted"|"error", run_id}`。
**注意**:这改动了既有契约(`data: null`),前端消费点需同步核查。

### G. 修 `metadata` 帧的 trace_id 描述

两份文档都写 `metadata` 含 trace id,实际 payload 只有 `run_id`/`thread_id`。删掉该描述。

## 七、文档站重构

按 8 章重排(参照用户提供的对接文档范式),手写章节编号(VitePress 不自动编号;手写让锚点、
搜索、跨篇引用都对得上),侧栏展开到二级:

```
1 概述与对接流程      1.1 核心概念  1.2 对接流程
2 通用约定            2.1 环境地址  2.2 协议约定  2.3 公共请求头
                      2.4 统一响应格式  2.5 限流与配额  2.6 幂等性
3 认证                3.1 服务账号与 Key  3.2 Scope  3.3 创建/轮换/回显
4 接口详情            4.1 发起 run  4.2 取消 run  4.3 会话列表
                      4.4 会话消息  4.5 事件回放  4.6 文件上传  4.7 审批决策
5 SSE 事件格式        5.1 帧格式  5.2 事件总表  5.3 updates 详解
                      5.4 token  5.5 worker/guard/compaction  5.6 断线重连
6 错误码总表
7 对接注意事项与 FAQ
8 附录:多语言示例代码 + 联调自测清单
```

**四语言代码示例**(curl / Node.js / Python / Java),用 VitePress 原生 `::: code-group`
渲染为 tab(复制按钮默认主题自带,无需插件)。覆盖:拿 key → 发起 run(stream)→ **解析 SSE 流** →
queue + 轮询 → 上传文件 → 续会话 → 断线重连 → 取消 → 审批决策。

**解析 SSE 那段是重点**:四种语言差异最大(Java 尤其——分帧、心跳注释行、`id:` 提取),
第三方卡在这一步的概率最高。

其余:错误码总表(现文档按 HTTP 状态码分节讲,缺一张可查的码表)、FAQ、联调自测清单
(勾选式)、页脚版本号。

**机密红线**(沿用 #1151):公开文档不得出现凭据、密钥名、金库路径、内网地址、集群串、
内部服务名、内部模块路径。

## 八、兼容与迁移

- `image_refs` 保留(内部前端在用),与 `files` 互斥。
- 控制台平面收口 → #1153 的相关测试期望需更新(有意收紧)。
- `end` 帧带 status → 前端消费点同步核查。
- 外部 `user_id` 加 `ext:` 前缀 → 实施前先查存量规模,决定直切还是出迁移。
- 对外契约标 **v1**,文档页脚带版本号。

## 九、分期

范围偏大,按**三期顺序推进**(后一期依赖前一期的产出,不能并行):

| 期 | 内容 | 产出 |
|---|---|---|
| **P1 契约地基** | §三(命名空间/收口/归属校验/`ext:` 前缀)+ §四 的 7 个端点(先不含 `files`/`url`) | 第三方可跑通"发起→取消→列会话→读消息→回放→审批"整链 |
| **P2 附件与请求侧** | §五 全部(`files` 数组 / 远程 URL 五道防护 / `inputs` / 幂等键) | 附件能力可用 |
| **P3 响应侧与文档站** | §六(SSE 三个 bug + 帧文档)+ §七(8 章重构 + 四语言示例) | 对外文档达到可交付状态 |

P1 交付后第三方即可开始对接主流程;P2/P3 增量补齐。每期各出一份实施计划。

## 十、验收

- 后端:各端点契约测试(含归属校验的**否定用例**:换个 `user_id` 必须 404);
  远程拉取五道防护逐条测试(重定向到内网地址、超长、类型伪装、超时、私网直连);
  seq 错位与断线重连接合点必须有针对性测试(这两条是静默丢数据类,最需要变异自证)。
- 文档站:`pnpm build` 通过;四语言示例**逐条真机跑通**(不是照抄能编译就算)。
- 真栈:测试集群跑通"发起 → 取消 → 列会话 → 读消息 → 断线重连 → 传文件(两种 transfer_method)"
  整链。
