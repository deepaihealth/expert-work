# B-262 P1 + P3:上传 / 输入 / 工具结果按对话分开,老对话自动迁移

日期:2026-11-02 · 状态:已拍板(11-02:跨 agent 附件复制、预拉缓存按对话、老对话按本文迁移)

## 要解决什么

对接方在一个平台用户下面跑很多终端客户(一段对话一个客户)。B-236 已经把 Agent 自己写的
文件按对话分开,但三个平台目录仍挂在 agent 根上、所有对话共用:

| 目录 | 里面是什么 | 今天谁看得到 |
| --- | --- | --- |
| `uploads/` | 用户上传的文档 | 同一 agent 的所有对话(`/workspace/uploads` 与 `/agent/uploads` 是同一个 bind) |
| `inputs/` | 每轮注入变量 `inputs/<run>/inputs.json`、预拉缓存 `inputs/cache/<sha>` | 同上;缓存目录列得出别的客户的 URL 下载件 |
| `.tool_results/` | 超长工具结果、`read_page` 渲染页 | 同上;`ask_image` 手写 ref 可读别的对话的渲染页 |

P0(#2001)只堵了两处已经会串的(同名覆盖、按页查图扫全 agent)。本文把三个目录整体搬进对话。

## 一句话方案

**对话视图里不再把 agent 根的三个目录 bind 进来,它们就是 `threads/<conv>/work/` 下的普通
子目录**;`/agent` 下同名的三个目录在对话视图里盖住(看不到别的对话的)。模型看到的路径
(`/workspace/uploads/x.pdf`、`.tool_results/<run>/…`、`$EXPERT_WORK_INPUTS`)**一个字不变**。
老视图的对话下次运行时迁到对话视图(P3)。

为什么放 `work/` 里面而不是 `threads/<conv>/uploads`:`layout.py` 的浏览隐藏 / 删除保护已经会剥
`threads/<uuid>/work/` 再看首段,放 `work/` 下这两条规矩自动成立;放 `threads/<conv>/` 下则
首段是 `threads`,上传会出现在产物列表里、客户端还能删(调研风险 1)。

## P1:新布局

### 1. 执行视图(`exec_view.py` 与 `runner.py` 两份脚本逐字相同)

- 对话视图:删掉 `PLATFORM_DIRS` 的 bind 循环;`/workspace` 下的三个目录就是 `work/` 里的。
- 对话视图:`/agent/uploads`、`/agent/inputs`、`/agent/.tool_results` 用空 tmpfs 盖住(与
  `hide_threads` 同一手法)。老视图不变。
- 新的沙箱布局版本号(`SANDBOX_LAYOUT_*`),让热沙箱按新脚本重建(同 B-242)。

### 2. 宿主侧写入点改指对话

| 写入点 | 今天 | 改成 |
| --- | --- | --- |
| 控制台上传 `POST /v1/sessions/{id}/uploads` | `agents/<k>/uploads/` | 对话视图的会话:`agents/<k>/threads/<conv>/work/uploads/` |
| 对外上传 `POST /v1/agents/{code}/uploads` | 同上 | 同上(这个接口总有 session) |
| 注入变量 `inputs_node` + 预拉 exec | `ToolContext` 不带布局,落 agent 根 | 带上布局与对话,落本对话 |
| 超长结果 `_externalize_tool_overflow` | `ws=/agent` | 对话视图写 `/workspace`(子代写根对话的) |
| `read_page` 渲染 | `ws=AGENT_VIEW` | 同上 |

`workspace_scope.scoped_path` / `workspace_paths.resolve_workspace_path` 里「对话视图 + 平台
目录 → 改指 agent 根」那一档删掉,平台目录就按普通对话路径走。老视图不受影响。

`user_upload.ref` 仍是 `uploads/<名字>`(相对**对话**的 `/workspace`),对接方缓存的 id、
`upl_` 形状都不变。下载接口按 `row.thread_id` 的布局拼路径。

### 3. 引用别的会话的上传:复制进本对话(你 10-31 已同意)

对外 run 的 `files[{upload_id}]`:

- 上传行的会话 == 本会话:照旧。
- 不同会话(同一 agent 或另一个 agent,用户 11-02 拍板两种都复制):把文件从上传行所在会话的
  目录复制进本对话 `uploads/`(沿用 P0 的同名规则:同内容复用、不同内容加 `-<sha8>`),本轮
  `[file attached: …]` 写实际落地名。源会话按 `row.thread_id → thread_meta` 定 agent 与布局,
  不再按本 agent 的目录找同名文件(今天跨 agent 会找到别的文件或找不到)。
- 文件属于同一用户、由对接方显式挂上,复制不越权;用户校验(`row.user_id`)照旧。

控制台的附件本来就上传到当前会话,不涉及复制。

### 4. 读取点

- `ask_image` 手写 ref:指向 agent 根 `.tool_results/<run>/` 的,只认本对话自己的 run
  (`figure_run_ids`,含父链);其余一律拒。修掉调研风险 10。
- 渲染页 ref 解析(`multimodal.is_cacheable_image_ref`、`parse_rendered_figure_ref`)按分段数剥
  前缀,新增认 `agents/<k>/threads/<uuid>/work/` 这一种前缀;`figure_run_ids` 从尾部数段,不受影响。
- 按页查图(`figure_lookup`)先找本对话目录,再找 agent 根下**本对话自己的** run(迁移过来的
  老对话要用)。
- `doc_sha` 由路径派生,对话路径不同 → 同名文档在不同对话里不再共用渲染页(本来就是要的)。

### 5. 预拉缓存 `inputs/cache` 改为按对话

今天按 agent 共享、按 URL 内容寻址,目录列得出别的客户下载的件。改为放本对话
`work/inputs/cache/`。代价:不同对话拉同一个 URL 不再命中缓存,多下载一次。

### 6. 清理

- 会话删除:已经整棵删 `threads/<conv>/`,三个目录随之删掉(今天删会话不删上传,这次顺带对了)。
- 工作区 janitor(`inputs` 7 天 / 30 天 TTL)与留存作业的 `.tool_results` 孤儿清扫:新增遍历
  `agents/<k>/threads/*/work/` 下的同名目录。长期对话(如 ai-health-plan 每个员工一个会话)不清的话
  会一直涨。
- agent 根下的老三目录:老视图对话还在用,不动;等迁移完、留存期过了再说(不在本次范围)。

## P3:老对话自动迁移

### 哪些对话要迁

1. `thread_meta.workspace_layout = 'agent_root'` 的老对话(B-236 之前开的;**生产目前全是这种**)。
2. 已是对话视图、但平台文件还在 agent 根的(B-236 上测试后开的对话)。

### 什么时候、做什么

在对话下一次 run 开始时(图入口,与 `conversation_seed` 同一位置、同一把按对话的锁,
标记 `threads/<conv>/.platform-migrated`,做过不再做;委派子代不做;失败不让 run 失败,
下次再试):

1. **本对话自己的上传**复制进 `work/uploads/`:来源是 `user_upload` 行(`thread_id` = 本对话)
   与审计里的 `DOCUMENT_UPLOAD`(控制台上传没有登记行,只有审计)。同名已存在就不覆盖。
2. **本对话自己的 run 的 `.tool_results/<run>/`** 复制进 `work/.tool_results/`(run 来自
   `agent_run` 行),老消息里「已保存到 `.tool_results/<run>/…`」的说明继续指得到。
3. 情形 1 的对话:再走一次开场复制(agent 根下一级目录,20 MB 上限,规则同 B-236),然后把
   `workspace_layout` 改成 `conversation`。
4. 情形 1 的对话:本轮平台段里加一句一次性说明:「工作区已改为每个对话独立;之前在 `/workspace`
   下的文件现在在 `/agent/` 下同样的位置;上传、注入变量、工具结果仍在 `/workspace/` 下」。`/agent`
   就是原来的 agent 根,老文件一个不少;三个平台目录在 `/agent` 下看不到(P1 盖住了),本对话自己的
   那部分已由第 1、2 步复制回 `/workspace`。

`inputs/<run>` 不迁:每轮重写。迁移那一刻不会有挂起的审批(一个会话同时只有一个 run)。

### 迁不了的

- P0 之前已经被别的客户同名覆盖掉的上传:复制过来的是覆盖后的内容,无法还原。
- 子代 run 的 `.tool_results`:要核 `agent_run` 有没有子 run 的行;没有就从检查点的
  `viewed_figures` 补 run id。开工第一步核。

### 实现与上文的出入(P3 实现时定,11-02)

- **在哪做**:不在图入口节点里,而在起**新** run 的两个入口(`api/runs._start_run`、
  `run_queue_worker._execute`)把布局写进 `configurable` 之前(`control_plane.platform_migration`)。
  图开跑之前 `configurable` 就定了,节点里翻布局,这一轮的工具仍是老视图;放在入口,迁移的
  那一轮从头到尾跑新视图(`conversation_seed` 节点照常做开场复制,一次性说明也在这一轮)。审批
  续跑、孤儿复活不迁(接着跑已有的 run);触发器每次新建对话,不迁。锁仍是开场复制那一把
  (同 classid、同键 `{tenant}:{conv}`)。
- **顺序与锁**(review 3):要查库的(会话、登记行、审计、`agent_run`、检查点)都在拿锁之前读完;
  持锁(一个池连接上的事务级 advisory lock)只做「情形 2 再看一次标记 + 复制」;放锁之后再比较
  再交换翻布局(只有库里还是 `agent_root` 才翻,两份并发、哪份翻成都一样,所以不必在锁里),
  最后写标记。开场复制由同一轮的 `conversation_seed` 节点做,不在迁移里另做一遍。翻完、写标记
  之前崩了,下一轮按情形 2 补跑(复制幂等)。
- **并发上限**:每个进程同时迁移的对话最多 4 个;等名额超过 2 秒,这一轮不迁(不翻、不写标记,
  照老视图跑),下一轮再试。
- **标记只管情形 2**:情形 1 只看库里的布局(`agent_root` 就迁),不看标记 —— 模型写得到
  `threads/<conv>/` 下的文件,不能让它把对话一直钉在老视图。
- **上传件核对内容**:只按审计记下的 sha256 核对(只凭大小,同样大小的别人的件也对得上);
  对不上**不复制**,没有 sha256 记「确认不了」也不复制,两者都不挡迁移。「迁不了的」第一条
  因此变成「缺这一份」,而不是「复制过来的是别的客户覆盖后的内容」。读不了(I/O 错,不是源
  不在、不是内容不对)算这一轮失败:不翻、不写标记,下一轮再试。审计按会话查(`details->>
  'thread_id'`、`created_at` 往前 5 分钟起),不翻整个租户。情形 1 没接审计来源(无库部署)
  就不翻:控制台上传只有审计,翻了会悄悄落下它们。
- **上限**:只限工具结果 —— 一个对话合计复制 100 MiB(另有条目数 / 深度上限),放不下的那一个
  run 整个跳过、记日志。上传件不计入上限:是这个对话自己的东西,不能因为大就留在 agent 根。
- **子代 run**:子代没有 `agent_run` 行(已核);run 另取检查点 `viewed_figures` 里的,其中在
  **别的**会话有 `agent_run` 行的丢掉。只写过超长结果、没渲染过页的子代 run,其 `.tool_results`
  找不回来。run 取自落库的 run 表(不是进程内登记);无库部署不搬 `.tool_results`。

**已知缺口(review 3 定为不修,记在这里):**

- **迁移与同时进行的上传有竞态**:迁移在锁外读完登记行 / 审计,复制时 agent 根下又来了一份
  同名上传(老对话在老视图里上传、或别的对话覆盖同名),可能漏搬或搬到「对不上 sha256」而不搬。
  只影响 P0 之前老视图里同名上传的极少数对话;后果是那一份缺(模型看到「没有这个文件」),
  不会搬错内容。
- **RLS**:迁移走的是现有 store,与其它读写一样以超级用户连接执行,租户隔离只靠查询里的
  `tenant_id` 条件,RLS 兜底不生效(见 RLS 项目,Phase 0 后搁置)。这次新加的查询都带显式
  `tenant_id` 条件;RLS 真开启时要和其它 store 一起核一遍。
- **审计查询没有专门索引**:`audit_log_action_idx (tenant_id, action, occurred_at)` 把扫描限在
  「本租户、上传动作、对话建立之后」,`details->>'thread_id'` 在这些行上过滤。上传多的租户
  若慢,加表达式索引 `(tenant_id, action, (details->>'thread_id'))`;这次不加迁移。

## 开工前先数(不花钱,只读)

- 测试与生产:`agent_root` 布局、近 30 天有 run 的会话数;它们 `uploads/`、`.tool_results/`
  的总量(估迁移复制量与耗时)。生产要你用 `!` 跑我给的只读脚本。
- 近 30 天对外 run 里「附件来自别的会话」「来自别的 agent」的次数(复制量与更新记录措辞)。

## PR 切分

| PR | 内容 | 依赖 |
| --- | --- | --- |
| P1 | 第 1–6 节:脚本、写入点、复制附件、读取点、缓存、清理;新沙箱布局版本 | — |
| P3 | 迁移节点、标记、一次性说明 | P1 |

**P1 不能单独发**(审查 11-02:B-236 之后开的对话视图会话会突然看不到 agent 根下自己的上传 / 工具结果,
渲染页的 `doc_sha` 也变了),两个一起发测试,真栈用例写进手册新的一节。上生产前通知深护智康:附件来自别的会话 / 别的 agent
时平台会复制进本会话,本轮附件名可能带 `-<sha8>`。

## 不做

- B-262 P2(`/agent` 只读、平台工具写记忆)—— 下一步单独出方案。
- 留存作业「上传到期删除」今天按用户根拼路径,实际删不到(B-50 起就是);P0 的复用让多行共用
  一个文件,修它要先定引用计数。登记为新编号,不在本次修。
- 删 agent 根下的老三目录。
