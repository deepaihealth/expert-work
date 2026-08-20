# 剩余工作总清单(全项目待办唯一入口)

> 原为本机 git-ignored 文件 `.superpowers/sdd/ROADMAP-2026-08-13.md`,2026-08-17 搬进仓库,此后以本文件为唯一入口。
> 各 program 的完整执行历史仍在各自的(本机)`.superpowers/sdd/<plan>/progress.md`;入仓的设计与计划在
> `docs/superpowers/specs/` 与 `docs/superpowers/plans/`。

## 状态(2026-08-20)

- **阶段 0/1/2/3 全部交付并上线**(见下文各节的 ✅ 标记)。
- **收官后的文档反馈波全部交付并上线**:
  - #1189 线 B 文档可读性(SSE 章重写、代码块标题插件、「帧」→「事件」)
  - #1191 文档站宽屏版式
  - #1193 线 A 附件模型统一(`upl_` 统一 id、`files:[{upload_id}]`、附件下载端点;设计 `docs/superpowers/specs/2026-08-17-external-upload-unification-design.md`)
  - #1195 第三轮可读性(审计驱动,枚举值取代码真值;审计 `docs/superpowers/specs/2026-08-17-external-docs-readability-w3-audit.md`)
  - #1197 第四轮整站重写(第三方工程师视角 + 企业级语气;**写作规范 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`,以后改对外文档先按它自检**)
- **调试台 / 对话记录 / Run 详情交互重设计 program(2026-08-17 立项)—— ✅ 全收官(2026-08-20)**:
  PR0 #1200(Jinja 两 bug)→ PR1 #1202(对外 `plan` SSE 事件 + 文档)→ PR-A #1207(三栏壳 + 轨迹合一)→
  PR-A.1 #1214(六条界面反馈)→ PR-A.2 #1216(轨迹对标 deepseek-harness 重写)→
  PR-A.3 #1218 + follow-up #1219(Schema tab / SYSTEM 行 / TTFT 补数据)→
  **PR-B #1221(对话记录页 + Run 详情页切 console 组件,退役 TurnCard 集群 −9.6k 行)**。
  测试环境 `afe00ebb`,两页真栈验收 15/15,记录 PR #1222 已合并(`72a5421b`)。
  设计 `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md`;遗留见下文「D · 调试台 program follow-up」。
- **仍然有效的剩余项** = 「待产品拍板」P-1~P-5、「其它 program 挂起项」X-1 与 X-3~X-10、
  「小 backlog」B-1~B-7 与 B-9~B-19(B-19 值得先做:直接影响对外 429 行为)、
  ~~「调试台 follow-up」D-1~D-4~~(✅ 2026-08-20 全清)。X-3 起与 D 节为 2026-08-20 从各 program 记忆汇集补录,
  **开工前先按行内注明的来源核实现状**(记忆反映的是记录时点)。

---

## 阶段 0 · 当前分支收尾(阻塞后面一切)

### 0.1 真栈验收 —— ✅ **全过(2026-08-14)**

发布 `6bb7dff6` 到测试集群(smoke 9/9,两个迁移落库并核实过 `message_count`
可空无 server_default、部分唯一索引 WHERE 条件),用一把临时铸的 `read+write`
key 打对外面跑完 **18/18 PASS**(用完已撤销并验证 401)。

**四项"静态审无法定论"的全部落地**:

| | 结论 |
|---|---|
| ① `files[]` 文档投递(头号) | **PASS,且拿到了直接证据** —— agent 的 reasoning 里明写 “The file is at `uploads/acceptance.txt`. Let me read it.”,证明模型确实能从 `[file attached: …]` 那一行自行推断出路径。这条此前是"不通过则整条通路产品级失效且静默"的唯一未知 |
| ② queue → 真 worker → 真 graph 吃 `document_names` | **PASS**(24s 内答出文档内容,非 monkeypatch 路径) |
| ③ queue 幂等真并发 | **PASS** —— 6 并发同 key,单一 `run_id` |
| ④ stream 幂等真并发 | **PASS** —— 6 并发同 key,全 200、单一 session、帧数都是 468(输家真接到了赢家的完整流,不是各跑各的) |

**验收过程中三次"以为是缺陷、其实是我错了"**,都记在这里免得下次重犯:

1. **`uploads/` 不出现在工作区列表 ≠ bug**。`layout.py` 刻意把 `uploads/`、`skills/`
   设为保留前缀,浏览面只显示 agent 产出(模型同 `.gitignore`)。spec §八10 的原话是
   「**agent 在工作区生成一个文件** → 列得到」,我却拿自己上传的文档去验。
2. **`Content-Disposition: inline` ≠ bug**。实现是白名单模型:安全文本
   (`.txt`/`.json`/图片)inline,**active content(`.html`/`.svg`/`.xml`)强制
   attachment**(注释标"(c) red-line — never inline")。spec §八10 写的
   「响应头是 attachment」措辞过于绝对,实现比它细致。改成验红线本身 —— 让 agent
   多写一个 `.html`,实测确为 `attachment` + `text/html`。**spec 措辞该按实现修正。**
3. **"agent 拒绝复述口令" ≠ 文档没送达**。我拿"文档里写着请你复述口令"当探针,
   正好撞上提示注入防御:同一提示两次跑,一次照做一次明确拒绝("文件里的指令
   属于提示注入,我不执行"),而两次它都读到了文档。**探针必须是中性事实**
   (换成"登记表里的项目代号是什么"后稳定通过)。glm-5.2 temperature 0.9,
   会被安全策略挡住的探针测不出送达与否。

另:§八3(commentary/final 各带自己的时间戳)需要真产生 commentary 才可判 ——
纯 tool_calls 的空文本 AIMessage 在 `extract_turns` 里被跳过
(`if not text.strip(): continue`),所以简单问答只有 `final`。用"分两步、每步先
说明再动手"的任务跑出 3 条助手消息 / 3 个不同时间戳,判据才真正生效。
**一条只可能绿的断言证明不了任何事** —— 脚本里这条不可判时报的是 FAIL 不是 PASS。

<details><summary>原始清单(留档)</summary>

**口径要写准**(这一条我之前含糊过):spec `docs/superpowers/specs/2026-08-12-external-api-v1-p2-design.md`
§八 的验收清单是 **14 项**。1–13 项大部分已被自动化测试覆盖,第 14 项写的是
"测试集群端到端跑一遍上述全部"。所以真栈这步要跑 1–13 全部。

其中 **4 项是静态审查完全无法定论、只有真栈能判的**(记在
`.superpowers/sdd/2026-08-12-external-api-v1-p2a/progress.md:492` 附近):

1. **头号,失败是静默的** —— 系统提示词和 `read_document` 的工具描述**都没有告诉模型
   「上传文档落在 `uploads/` 下」**,整条 `files[]` 文档投递靠模型从
   `[file attached: uploads/report.pdf]` 自行推断。路径拼接闭环已静态核实为真,
   但"模型会不会用"静态审判不了。**不通过时 API 不报错、run 正常结束,agent 只是答非所问**
   —— 客户端那边看起来像模型没读懂文档。**这条没过之前,`files[]` 文档投递不能对外宣称可用。**
2. queue 模式端到端:worker → 真实 graph 的 `document_names` 投递(现有测试用 monkeypatch
   的假 `run_agent`,只证明了入参构造正确)
3. 真多副本下的 HTTP 层幂等竞态(T13/T14)
4. 第四项在 P2-a ledger 同一段,派发前拉出来核对

</details>

### 0.2 P1 的发布收尾 —— ✅ **已完成(2026-08-14 核实,此前本文件记错了)**

**订正**:我曾照抄 `HANDOFF-2026-08-12.md` 写成"一直没做"。核实结论相反:

- 测试集群 **2026-08-12 12:28(UTC+8)已发 `e26b86ed`**,双镜像
  (control-plane `e26b86ed` / admin-ui `e26b86ed-test`),迁移 Job 完成,全部 Deployment 就绪
- **smoke 9/9 PASS**(首轮旧 pod Terminating、次轮 prometheus 2/3 均已知瞬态,第三轮 3/3 全绿)
- **发布记录 PR #1157 已合**(`7bfd7622`),即本分支的 merge-base
- 发布后在运行镜像内复核过 P1 载荷:`EXTERNAL_SUBJECT_PREFIX == 'ext:'`,5 个 `external_*` 模块 import 通过
- **孤儿 `tenant_user` 已定位并裁定**:`w3-acc-synthetic-1`(id `99b186c6-…`,租户 `dd068302-…`,
  2026-08-10 W3 验收期建),持 0 个会话,唯一引用是一条已归档软删的 `user_workspace` ——
  **没有活数据被搁浅,可留可清**

**教训(同教训 #1)**:"一直没做"是否定性断言,我从交接单转抄未核实。
`kubectl get deploy -o wide` 一条命令就能证伪。**凡写进计划的"未做/从未/没有",落笔前查现状。**

### 0.3 开 PR —— ✅ **已开(2026-08-14):PR #1158**

79 commit(77 + 开 PR 前全量套件逮到的 2 条修复)。**后面的工作不要再往这个分支堆。**

**开 PR 前全量套件逮到的两条**(每个 task 只跑点名文件,只有全量才照得出来):

1. **8 条治具失败**(`24f32f84`)—— `/v1/agents` 挂 `console_only()` 后,若干测试仍用
   API key / 裸 app 打 console 路由。其中 `test_admin_api` 拿 `/v1/agents/schema` 当
   authn canary 是**第二次**踩同一个坑:P1 关掉 `GET /v1/agents` 后 canary 才挪到 schema,
   当时注上"deliberately left open to API keys",P2 又关了它。
   **根治**:判据不再依赖"某路由保持对 API key 开放"——`console_only()` 的 403 只在
   `_principal` 解析出身份后才抛,故 **403 本身即认证成功的证明**,401 是认证失败。
2. **`integration` 标记加在 fixture 体内无效**(`6bb7dff6`)—— `-m` 在**收集期**
   deselect,fixture 到**运行期**才执行,`request.node.add_marker` 永远赶不上。
   `[sql]` 三条因此仍在 unit job 里执行并拉 testcontainers 镜像。

**这两条本身就是 1.3(分区自审)要防的那类东西**:一个"我说它有闸就当它有闸"的自证结构,
一个"我说它是 integration 就当它是"的无效声明。写 1.3 的测试时记住:**声明必须跟实际
生效的机制比对**。

---

### 0.4 正式发布 + 记录 PR —— ✅ **已随阶段 1/2/3 多次发布(最近 `b0abffdb`,记录 PR #1188,2026-08-17)**

**当前测试集群跑的是预发布验收版本 `6bb7dff6`(分支 commit),而仓库里
`infra/k8s/overlays/test/kustomization.yaml` 仍写 `e26b86ed`** —— 这个不一致是**故意**的:
`6bb7dff6` 不在 main 上,把它提交进 overlay 会让 main 指向一个分支 tag。#1154/#1157 的
流程都是「合并 → 用 main 的 squash tag 发布 → 开记录 PR」,这次是为验收提前发的。

**用户拍板:不在 #1158 合并后单独发一次,等阶段 1 的安全收口做完一起发。**
所以这个不一致会持续整个阶段 1 —— 期间测试集群跑的是 P2 预发布验收版本,
功能上等价于 #1158,只是 sha 对不上仓库 overlay。**任何人看集群版本时记得这一点。**

到时候(阶段 1 收口合并后)一次做完:

1. `tools/deploy/release.sh test`(会自己取 main 的新 squash sha 当 tag)
2. smoke 9/9
3. 提交 overlay 的 newTag 编辑,开 `chore(deploy)` 记录 PR,照 #1157 格式
4. **归位 `kustomize edit` 挪走的注释** —— 它每次都会把
   `# Sandbox migration W1 Task 2 …` 从 credential-proxy 条目上挪到 `images:` 前面。
   #1154/#1157 都记过同一件事,是稳定复现的工具行为

---

## 阶段 1 · 安全收口剩余

### 1.1 owner 维度重盘 —— ✅ **已交付 PR #1160**(main `df51b2ad`)

**结论比原记录轻一档,也比原记录重一档,两边都要改**:

- **轻**:`conversations.py` 的跨用户可见**有明文设计**(模块 docstring:
  "an **operations** surface … shows every user's conversations in the tenant,
  so an operator can answer 'what happened in user X's conversation' without
  owning the thread")。不是漏洞。
- **重**:实际缺的不是 owner 过滤,是**连员工 RBAC 都没有** —— viewer 能读任意
  终端用户的完整轨迹原文并 promote/dismiss。

**用户拍板**:读保持全员 / 写收紧 operator+ / 加审计 / 前端不动。已实现:
conversations 2 条 + curation candidates 读 2 条 → `require("session","read")`,
promote/dismiss → `require("session","write")`,curation 详情补 `SESSION_READ` 审计
(原来完整轨迹出平台**零留痕** —— `audit` 形参只喂给了跨租户判定)。

**测试判据 = role-less JWT**:viewer/operator/admin 都持 `session:read`,拿它们
证不了闸真在。8 条新用例,5 条先红后绿。

**⏳ 留给产品的一条**:`curation` 详情交出 `trajectory.messages` **原文**,而
`sessions.py`/`runs.py`/`plan.py` 的 `caller_owns_thread` 对非 admin 员工恒为假(拒)。
curation 的 docstring 对这件事**无明文**。要不要对齐是产品判断,改动量一行。
详见 `.superpowers/sdd/owner-audit-2026-08-14.md`。

<details><summary>原始盘点记录</summary>


**这轮最大的方法论发现**:172 条路由的盘点,分组依据是"哪些文件**没挂** `console_only`"
—— 于是所有**已挂闸**的文件从没被按 owner 维度看过。
`console_only`(平面隔离,挡第三方 key)和 owner 校验(用户间隔离,挡跨终端用户)
**是两个正交维度**。那份 172 条表对第二个维度**无效**。

待盘(全部涉及终端用户数据、全在"已挂闸"那侧):

| 文件 | 已知线索 |
|---|---|
| `curation.py` | **`GET /v1/curation/candidates/{id}` 返回完整会话轨迹 `messages`,对任意终端用户不做 owner 过滤** —— C 组盘点明确记录 |
| `memory.py` | 终端用户长期记忆;现靠 `resolve_caller_user_id` 对 service_account 返 `None` **意外**挡住,非设计闸 |
| `artifacts.py` | per-(tenant, user) 产物 |
| `conversations.py` | 会话 |
| `agent_users.py` | 终端用户本身 |

**预算提醒**:`skills` 那一族按这个维度扫出来 **七轮、四条 Critical**,而**只有第一条是原始盘点
找到的**,最后一条甚至不在 skills 代码里(是"投递 worker"+"权限模型缺口"的组合)。
**这批必须按"每族两三轮"做预算**,靠的是"改一轮 → 独立复审 → 再改"的循环,不是盘一次就完。

</details>

**1.1 留给产品拍板的最后一条(curation 详情交出完整对话原文)—— ✅ 用户 2026-08-17 拍板收到 operator+,PR #1187(`b0abffdb`),已上线。列表(元数据)仍 `session:read` 全员,详情挂 `session:write`。**

### 1.5 员工轴:内容面 RBAC —— ✅ **已交付 PR #1162**(main `fe670a97`)

拍板:复用 `manifest` 资源,读全员 / 写 operator / 删 admin,零改 `rbac.py`。
三处刻意不按动词映射(subscribe 的 DELETE、supporting-files 的 DELETE、
`POST .../test`)。**skills 那 7 条红不是回归** —— 那些用例的被测对象是高风险闸,
`viewer` 只是顺手的非 admin;模块 docstring 声称的 "all skill mutations are
admin-only" 在路由层从来没真过。改角色不改判据。

<details><summary>原始记录</summary>

`skills` / `knowledge` / `eval_runs` / `quality` / `curation` **全部零 `require()`**。
`skills.py` 的注释明写「inline role gate follows the skills.py convention
(no require() / rbac.py matrix)」—— 是这一族的既有惯例,不是遗漏。

P2 那轮给这 42 条加的 `console_only` **只挡第三方 key**,与员工角色无关。所以现状:
**任何员工(含 viewer)可对整个内容面任意读写** —— 建/删知识库、起评测、改质量配置。

**与 1.2 是不同的轴**:1.2 是"API key 能到哪",这条是"员工角色能干什么"。
规模 42 条,风险低于 1.1(不含终端用户对话原文)。**动手前要先定"内容面各操作
分别要什么角色"——那是产品判断**。细节见 `.superpowers/sdd/owner-audit-2026-08-14.md`。

</details>

### 1.2 API key 轴剩余 —— ✅ **已交付 PR #1161**(main `13cf8693`)

**实际是 49 条,不是这里记的 13 条。** 原表只列了 audit/quota/sandbox_egress/memory/
usage/tenant_config/tenant_quotas 这几族;真扫出来还有 `api_keys` 6 + `members` 6 +
`service_accounts` 3 + `role_bindings` 1 + `mcp_servers` 10 + `mcp_oauth` 4。
一把 admin scope 的 key 能**铸造/回显/轮换同租户其它 key、重置员工密码、purge 员工**。
已全部补 `console_only`(只留 `/v1/me`),文档站 auth.md 同步改掉旧说法。

<details><summary>原始记录</summary>

已完成:webhook 5 条、`/v1/agents` 9 条、内容面 42 条(skills/knowledge/curation/eval_runs/quality)。

| 目标 | 问题 | 风险 |
|---|---|---|
| `audit.py` 2 条 | 零 authz,零 scope key 直读租户审计流水 | 中 |
| `quota.py` 4 条 | write key 可达 check/reserve/commit/release,**可能连着计费**;docstring 称 mTLS 服务间平面但 admin-ui 零调用 | 中 |
| `sandbox_egress_audit.py` 1 条 | 只有 `require("audit","read")`,admin/write key 可读沙箱出网日志 | 中 |
| `memory.py` 4 条 | 见 1.1,补显式闸 | 低(但脆) |
| `usage` / `tenant_config` / `tenant_quotas` 的 GET | write key 可达 | 低 |
| `me.py` 1 条 | 零 authz,自身身份 | 很低 |

**关键事实**:零 scope key 落到**空角色集**(`rbac.py:185-192` 的 if/elif 三分支全不匹配),
所以它只能到**完全没有 RBAC 闸**的路由。全平台就 `audit.py` 2 条 + `me.py` 1 条(内容面
那 42 条已修)。

</details>

### 1.3 地基:全 app 路由分区自审 —— ✅ **已交付 PR #1161**

`test_route_plane_partition.py`,261 条路由**零条无法解释**。分类是**推导**的
(依赖图 qualname + 同模块源码),不是查表;推导器本身被正控制钉住。
判据写错了四轮,后两轮都是「没闸的看起来有闸」——详见 PR 正文。

<details><summary>原始记录</summary>

每条路由必须归属 console / external / platform / public 之一,**无第三类**。
**先写测试让它红,红的部分就是施工清单本身。**

已有可抄的范本:`test_console_lockdown.py` 的
`test_every_console_route_carries_the_lockdown_dependency`(真查 `route.dependant.dependencies`)
和 `test_agents_prefix_is_partitioned_exactly`。

**一个必须避开的坑**:旧的 `_SELF_GATED_AGENT_ROUTES` / `_OPEN_AGENT_ROUTES` 两张容忍表,
**除了拼进并集检查外没有任何测试引用** —— 是"我说它有闸就当它有闸"的自证黑箱,
新路由零授权也能靠塞进表让分区测试永绿。全 app 版**必须让分类表跟实际挂的闸比对**,不能自说自话。
(那两张表已在 `85abdb39` 删除。)

</details>

### 1.4 共享平台闸依赖 —— ✅ **已交付 PR #1163**(2026-08-14 合)

`platform_only(message)` 进 `_authz.py`,15 个文件转完;`role_bindings.py` 的判定
是**条件式**(`payload.platform_scope and not ...`)不能转,全仓就这两处。
文案 per-router 保留(19 条各点名被保护资源,11 个测试文件钉着)。
**解锁了新的网**:闸移到依赖层后在 body 校验之前跑,于是能对 40+ 条平台路由做行为扫描
——以前内联在 handler 里,POST 会先 422 拿不到 403。

<details><summary>原始记录</summary>

47 条平台路由各自**内联手抄**一份 `_require_system_admin`,没有共享 `Depends`;
`tenants.py` 甚至连辅助函数都没有,`is_system_admin` 内联写了三遍。

**放最后的理由**:这 47 条**当前没有漏**(A 组核实过),所以是纯预防性重构 ——
收益在未来,风险在当下。**必须等 1.3 的自审测试做完再动**,否则改错了没有网。

</details>

---

## 阶段 2 · P3 —— ✅ **全交付并上线**(波 1 #1169 SSE 契约四修 + 文档站骨架 → `53046a9b`;波 2 #1170 帧文档补全 + #1175 四语言示例 → `5fee6c5e`;计划归档 #1180)

来源:记忆 `external-api-v1-program.md` + spec §九。

| # | 内容 | 备注 |
|---|---|---|
| 2.1 | **SSE seq 修复 + `updates` 帧解析文档 + 三帧补录 + SSE 三个 bug** | spec 原话:「**「看得见 agent」的真正阻塞项,能力已在流里,缺文档**」;记忆标注这是**用户最早的痛点** —— 第三方没法在自己界面还原 agent 交互过程。工程量确定,不是设计题 |
| 2.2 | 文档站 8 章重构 | |
| 2.3 | 四语言示例 | |

---

## 阶段 3 · 补能力(产品功能,与安全收口分开做)

| # | 接口 | 状态 |
|---|---|---|
| 3.1 | ✅ **PR-A #1181**(`df5ab443`)`GET /v1/agent-catalog` | 用户已确认要。**不是**现在这个吐完整 manifest(系统提示词/工具清单/模型配置)的 `GET /v1/agents`,那条已在 `85abdb39` 对第三方关死 |
| 3.2 | ✅ **PR-A #1181** `GET /v1/agents/{code}/runs` | 用户已确认要。现在只能按 `run_id` 拿事件,没有"列出这个会话/用户跑过哪些 run" |
| 3.3 | ✅ **PR-B #1185**(`7e743c2c`)`GET/DELETE …/artifacts` + `/artifacts/download` | 范围已定并交付。原「范围待定」:是 spec §九 第 4 项(文件删除)的**超集** |

**3.3 的背景**(免得下次重新盘):工作区文件分两种,区别在于 agent 有没有主动登记
(`save_artifact` 工具;`Mini-ADR J-11 — explicit registration, never an auto-scan`)。

| | 第三方能用的 `.../workspace/files` | 控制台面的 `/v1/artifacts` |
|---|---|---|
| 看到什么 | 工作区**全部**文件 | 只有 agent **登记过**的 |
| 字段 | 路径 + 大小 | 名字、分类、大小、sha256、版本数、时间 |
| 分类 | 无 | `document`/`code`/`data`/`other` |
| 版本历史 | 无 | 有 |
| 删除 | **没有接口** | 软删 |

**客户端最要紧的缺口是分不出主次**(一次 run 留十几个中间文件,只有一两个是给用户看的成果),
其次才是删不掉。所以建议开**产物视图**,而不是给裸文件列表加个删除按钮。

---

## 待产品拍板(不是工程题)

| # | 事项 | 性质 |
|---|---|---|
| P-1 | **重新生成 / 编辑重发** | spec §九 第 2 项。会话是 append-only checkpoint,**没有"回退到某条消息重跑"的概念**。要做是一道真设计题,不是加端点 |
| P-2 | 消息级点赞 / 点踩 | 内部已有 feedback 表,对外零暴露。薄,但要先定"反馈给谁看、进不进评测回路" |
| P-3 | **admin scope = 完整租户控制台权限** | P1 遗留。文档已明写"永不发 admin 给第三方",但实际范围比措辞暗示的更宽 |
| P-4 | `api_keys` 是**凭据横向扩散路径** | 一把 admin key 能枚举 / 回显 / 新铸本租户其它 key。与 P-3 同源(`rbac._collect_roles` 把 admin scope 直接映射成 `Role.ADMIN`) |
| P-5 | `approve_promote`/`reject_promote` 对 `tenant` 可见性技能**零角色限制** | 任何 viewer 能批准他人对公开技能的 promote-request。需定治理审批要什么角色 |

---

## 其它 program 的挂起项

| # | 事项 | 备注 |
|---|---|---|
| X-1 | **沙箱迁移波 4(收尾波)** | W1/W2/W3 全交付,波 4 因第三方对接插队而挂起。内容:**死字段裁决**(`sandbox_instance.node` + `spec.sandbox` 13 个死字段,15 个里只有 network 三键 + `persistent_workspace` 是活的)、沙箱指标接 Prometheus+Grafana、契约测试补全、文档(本地开发 / 发布 runbook / supervisor 冻结声明)。**另含 W1 遗留清单**(头号=egress token 24h 到期后出网全挂且全仓无 407 日志/指标/告警;`pip install` 装进 user site 而 `python -I` 读不到;调试台不显示沙箱 stdout/退出码;明文 HTTP 走 proxy 恒 407 —— 空密码不发认证头) |
| ~~X-2~~ | ~~CI pytest-xdist `-n auto`~~ | ✅ **已交付 PR #1159**(main `edbd8c96`)。实测 **29m17s → 11m32s(2.5x)**。三条教训:①「需先审 fixture 并行安全性」**是个不存在的前置** —— 零个 fixture 要改;真正挡路的是两个模块把 `uuid4()` 插进 parametrize id,导致各 worker 收集集不同。②**别拿本地数外推** —— 本地 `-n 4` 是 3.7x,runner 只有 2.5x(每 worker 都要 import 整个 app,固定开销被本地掩盖)。③基线自己在涨:P2 合入后 main 已经 **29m17s**,不是记录里的 18min |
| X-3 | **触发器 program Spec2/Spec3**(来源:triggers-user-dimension program) | Spec1「对话核心」四 PR + 加固已全交付(#1039~#1043)。剩:**Spec2 通知**(2a webhook / 2b 长连接)、**Spec3 后台管理面 A + manifest triggers 弃用 C**。DEFER 池头号=**投递无 CAS/幂等**(多副本下双 reconcile 会重复投递,迁移多副本前必须按 `expert_work_source_run_id` 去重或 CAS claim;单副本今日无风险);次=TranscriptMirrorSweep 纯注入不重扫(投递消息不即入全文搜索)/ DEAD_LETTER→TRIGGER_FAILED 无专测 |
| X-4 | **用户维度运维页 BACKLOG**(用户 2026-07-15 定暂缓) | ① 成员页员工清除 —— 唯一未做的删除入口,需新后端端点(绕 member 闸 + 删 Keycloak 账号)+ 前端;② Phase 3b 90 天物理硬删(`UserWorkspaceStore.hard_delete` + retention-job 扫 `deleted_at`+`archived_object_key` + `WORKSPACE_HARD_DELETE` 审计) |
| X-5 | **MCP allowlist 残留名不可移出**(来源:MCP 界面重设计 2026-07) | 目录条目被删后,租户 allowlist 残留名在 UI 无法移出,后端缺 **disable-by-name** 端点;现降级为「禁用 + 提示」的诚实态 |
| X-6 | **平台技能导出/同步收尾**(来源:skill-export-sync) | ① 52 个导出包待推测试环境(`POST /v1/platform/skills/import` 幂等可重跑,PLATFORM_ADMIN 凭证在金库);② admin-ui 技能导出按钮(platform + tenant);③ tenant export 丢 `supporting_files` bug(`skills.py:1390` 没传,platform 侧 `platform_skills.py:1333` 是无损姿势可照抄) |
| X-7 | **观测栈残留**(来源:W2-PR3 observability) | ① alertmanager receivers 仍 placeholder(P0 告警投递到空气);② compose 侧 prometheus 同源坑(promtool 单测文件混进 rules 整目录挂载,`--profile observability` 起不来;修法=单测挪 tests/ 子目录 + 改两处文档 promtool 路径);③ Langfuse org/project 显示名(只设 INIT_*_ID 没设 NAME,界面显示 "Provisioned Org/Project");④ 探针 trace 噪音(middleware 对 `/healthz*` 也开 span,2K 条/天淹没真 run) |
| X-8 | **Docker Hub 依赖镜像统一 mirror**(来源:测试环境镜像逃生舱 + 2026-08-20 CI 突刺) | ① node:22-alpine 进 ACR(照 keycloak/searxng 先例);② **CI testcontainers 镜像(postgres/minio/pgbouncer)也要脱离 Docker Hub**(mirror 或 GHCR pull-through cache)—— 2026-08-20 一小时 ~8 条并发 run 撞限速,integration 从基线 7-15 分钟被拖到 24:36+ 集体撞 25m 超时假红(#1232 临时把预算提到 40) |
| X-9 | **Agent 延迟 perf follow-up 池**(来源:perf 一期+二期;明细在本机 perf program 台账 progress.md) | oauth put 半成功窄漏清窗口(pre-existing)/ 双键空间 × per-tenant 键 LRU 有效容量随租户数下滑 / first_output SLI 缺面板 / resolve_embedder+resolve_reranker 死代码 / admin-ui 分解条 total 并行后夸大 |
| X-10 | **生产多副本整体实施方案**(来源:production-distributed-premise,2026-07-28 拍板) | 五道选型题已收口(ACS 双集群杭州 / AgentSandbox+E2B / OSS+NAS 工作区 / RDS PG16 / Redis 社区版 7.0 noeviction),四波行动清单在 `docs/research/2026-07-28-multi-replica-readiness-audit.md`;**整体实施方案还没出**。期间新特性默认按多副本语义设计 |
| X-11 | **mcp SDK 1.x→2.x + httpx2 迁移**(2026-08-20 立项,源头 dependabot #1229) | mcp 2.x 换 `httpx2` 依赖,`streamable_http_client` 签名变更(3 元组 + `httpx2.AsyncClient`),`orchestrator/tools/mcp.py:606-609` 起的整条 MCP 客户端层要迁;dependabot 已加 major ignore(做完把 `.github/dependabot.yml` 里那条一起删)。迁移要真栈测 MCP 工具(streamable-http + OAuth 路径) |
| X-12 | **fastapi ≥0.137 迁移:路由自审适配懒挂载**(2026-08-20 立项,源头 dependabot #1233) | fastapi 0.137 起 `include_router` 懒挂载:`app.routes` 里是 `_IncludedRouter`,APIRoute 不再摊平(实测 0.136.3 摊平 / 0.137.2 起不摊,启动也不展开),控制面整族路由自审(console lockdown / external gate / NUL guard / reachability / app_factory)集体失明。迁移=自审改用 `fastapi.routing.iter_route_contexts` 遍历并核实 router 级依赖的合成语义(**这批是安全闸,依赖合成改错比不改危险,要变异自证**);生产代码零 `app.routes` 枚举已核实。两个 service 的 pyproject 钉了 `<0.137` + dependabot ignore,做完一起放开 |

---

## 小 backlog

| # | 事项 |
|---|---|
| B-1 | `webhook_endpoint` **不记录创建者**(`user_id` 硬编码 `None`,全仓仅一个写路径)。现在无痛(**用户确认测试和生产都没配 webhook,表是空的**),一旦开始配就无法追溯 |
| B-2 | 前端 `WebhooksList` 无角色 gate(后端已收紧写操作,前端没有对应入口控制) |
| B-3 | `console_only` 的 403 文案在 4 处测试 + 1 处源码硬编码,改文案要动 5 处 |
| B-4 | promote-request 分页在 `created_at` **相同**时 tie-break 方向两后端不一致(in-memory id 降序 / SQL id 升序)。**预先存在,非本轮引入**;生产用 SQL 风险低,但别把那两条新测试当"两后端完全等价"的证明 |
| B-5 | `test_viewer_reads_are_unaffected` 的治具复用 POST,改用 store 直接建行会让它在任何变异下只对 GET 负责 |
| B-6 | **同一资源的两个写操作,`user_id` 位置不一致**:`PATCH .../sessions/{id}` 在 **body**(`ExternalRenameRequest`),`DELETE .../sessions/{id}` 在 **query**。各自都说得通(PATCH 本来就有 body,DELETE 通常没有),但对接方要分别记。**文档必须写清楚**,真栈验收时我就按 query 传 PATCH 吃了 422 |
| B-7 | **spec §八10 措辞与实现不符**(实现更对):spec 写「响应头是 attachment」,实现是白名单 —— 安全文本 inline、active content 强制 attachment。**改 spec,别改代码** |
| ~~B-8~~ | ✅ **PR #1164**。`tools/deploy/smoke.sh` 用 `items[0]` 选 control-plane pod,不过滤 `Running`。发布刚结束时会选中正在 Terminating / 已 Succeeded 的旧 pod,报 `cannot exec into a container in a completed pod`。**光加 field-selector 不够** —— Terminating 的 pod phase 仍是 Running,还要 deletionTimestamp 为空 + Ready=True;且 kubectl jsonpath **没有否定**(`!` 直接报错),要在 awk 里筛 |
| B-9 | **(PR-A #1181 复审发现,非其引入)** `/v1/agents/` 前缀下 **12 条 `console_only()` 路由**的 422 也套第三方信封(生产异常处理器判据=前缀 OR tag),而 `admin-ui/src/api/client.ts:89` 读 `data.detail` 读不到 → 降级成 `HTTP_422` + axios 通用文案。修改前后一致,未变坏。修法二选一:①判据改成「挂了 `external_only()` 依赖」驱动(复审给出的真定义,更贴);②admin-ui 兼容信封 |
| B-10 | **(同上)缺一道 CI 闸,不是现存 bug**:今天 8 个 `external_*.py` router 都带 `tags=["external"]`,但**将来谁漏打**,信封静默丢失且**没有任何自审能逮** —— 三个 external 发现器 PR-A Task 4 后纯 tag 驱动,同样瞎;`test_route_plane_partition` 只钉 `tenant`(无闸)集合、不钉 `external`。廉价补法:加一条自审「凡挂 `external_only()` 必带 `external` tag,例外(`agents.py` 的 `bind_session`/`run_agent_for_user`)走白名单」 |
| B-11 | **(PR-B #1185 终审 triage 留)** `control_plane/audit.py:111` 的 `ResourceType` Literal **不含 `"artifact"`**(protocol 侧有)。`external_artifacts.py` 与控制台 `artifacts.py:293` 同款 `resource_type="artifact"` 都是类型缺口;CI mypy 不扫 control-plane 所以今天不红,**范围一扩到 control-plane 立刻红**。修它要碰共享 Literal,顺手把两处一起对齐 |
| B-12 | **(同上)** 对外产物下载的配额(`artifact_download`,cost=1)在 `workspace_store is None` / 权限失败 / 内容缺失三条失败分支**之前**就扣了,失败也白扣一次。镜像的是 console 侧既有顺序,非 PR-B 引入;改的话两侧一起改 |
| B-13 | **(同上)** `_artifact_mime.infer_content_type` 的 `kind` 参数是摆设(fallback 分支 `del kind`),反查 `artifact.kind` 目前用不上。要么接上(按 kind 收窄 MIME 推断),要么删参数;别留死参 |
| B-14 | **(附件统一 2026-08-17 终审 triage 留,pre-existing)** `GET …/sessions/{id}/messages` 在「非 vision 模型 + Agent 声明了 `vision:` 块」这一组合下,消息文本里会带 `[image attached: expert_work://image/…]`(`api/runs.py:427` 附近)—— 是最后一处内部 URI 能越过对外边界的地方,与本次「对外不再暴露 `expert_work://`」目标相悖。修法:对外消息投影时把这类标记改成 `upl_` 形态或直接剥掉 |
| B-15 | **(同上)** 同名文档覆盖:`_safe_workspace_name` 确定性映射,同一用户重复上传 `report.pdf` 得到两个 `upload_id` 指向同一路径,后者覆盖前者字节,先前那行的 `size`/`mime` 过期。终审只让文档写明(chat.md §2.6),代码级修法(uuid 后缀 stem)是行为变更且 run 内按名读文档(`read_document`),要单独立项 |
| B-16 | **(同上,小项打包)** ① run 循环 `agents.py` 对 `kind="image"` 行缺 `parse_image_ref` 守卫(下载端点有,run 端点没有;今天只有 upload_for_user 写图片行所以不可达)② `_validate_image_refs` 内部冗余 thread 检查抛裸 `HTTPException`(不可达,靠 T2 两处 `thread_id` 同源的不变式,终审修复波已加断言钉住)③ `TOO_MANY_IMAGE_REFS` 兜底在 files 上限 == `MAX_RUN_IMAGE_REFS` 时不可达(保留,测试靠调低常量证明)④ `INVALID_FILE_REF` 仍由 `_safe_document_name_or_422` 发出但文档已删(只有手工塞坏行才触发)⑤ 迁移 `0146` 的 `ix_user_upload_tenant_thread` 目前无查询用(spec 定的,若不做按会话列附件可删)⑥ 端点级跨租户下载测试缺(store 级有)⑦ T2 文档分支 `uploads.insert` 失败在工作区已落盘之后 → 裸 500 + 孤儿文件 + 图片分支多扣一次配额(镜像 image_upload 既有模式)⑧ T1 `test_get_does_not_filter_user` 末句 `!= uuid4()` 空断言 |
| B-17 | **(线 B 文档可读性 #1189 顺带发现的代码侧小项)** ① `external_agent_catalog.py:95` 注释写 30s,真实 TTL 是 `settings.kill_switch_cache_ttl_s`(5s)② `_run_event_stream.py:24` 从 run store 借 `MAX_LIST_LIMIT`,语义上应有自己的常量 ③ `approval` 事件把 `binding_digest` 原样放到对外线路上,对接方用不上、且暴露内部摘要形态,考虑从对外投影里剥掉 |
| B-18 | **(附件统一真栈验收顺带发现,pre-existing,全 app)** `/v1/agents/...` 前缀下**匹配不到路由**的请求(例:路径参数里带 `%2F`/`%3A`,Starlette 解码后 `{upload_id}` 段吃不下 `/`)返回框架裸 404 `{"detail":"Not Found"}`,不套第三方信封;对外信封处理器只接 `RequestValidationError`。conventions.md 已泛泛写「不是所有错误都有 error.code」,可加一句「路径拼错是裸 404」;或给对外前缀加 404 信封 |
| B-19 | **(第四轮文档终审 C-1 核代码顺带发现,pre-existing)** `check_admission` → `QuotaService.check` 对租户配置的**全部** bucket 维度逐个扣费,不按 `resource_kind` 过滤:发起对话 / 产物下载各扣 `image_upload_count_30d` 1 次、`image_storage_bytes` 1 字节(如配置了这两行);图片存储写满后发起对话也会 429 `dimension=image_storage_bytes`。另:redis Lua 对 `refill_rate=0` 的黏性维度算 `retry_ms = need*1000/0`(除零 → inf → 整数化不可控),`Retry-After` 值无意义;in-memory 用 `max(rate,1e-9)` 得到天文数字。文档已按「按字节配额重试无效、先读 dimension」措辞绕开;代码侧应按 resource_kind 选维度 + 黏性维度不给 Retry-After |

---

## D · 调试台 program follow-up —— ✅ **全清(2026-08-20 收官当天一波做掉)**

| # | 事项 | 结果 |
|---|---|---|
| D-1 | Run 详情页轨迹「真配对」e2e 用例 | ✅ `e2e/run_detail.spec.ts` 增真配对用例(配对 messages/runs + 真 SSE 回放体 → 账本行断言)。**顺带逮到既有假象**:该文件的 messages/runs 桩用裸 glob,匹配不到带 `?tenant_id=` 的真请求,页面一直靠「穿透→报错→降级空态」活着 —— 改 URL 谓词匹配(exact pathname),桩第一次真正生效 |
| D-2 | RunDetail 切线程竞态守卫(Ruling 4) | ✅ `convoTenant` 状态改为 `{threadId, tenantId}` 打上来源线程标签,history 效果拒绝错线程的 tenant;单测用 `useNavigate` 同页切线程驱动潜伏帧,变异自证(拆守卫→红) |
| D-3 | `runs_page.summary_resume` 死 i18n 键 | ✅ 三处(en 类型/en 值/zh-CN)全删,全仓零残留 |
| D-4 | `RecordDetails.tsx:241` 既有占位行空 `<dt>` | ✅ **已被 PR-B 顺手修掉**(占位行已是 `<dl>` 外的散文 `<p>`,注释明写避免空 `<dt>`),本波仅核实 |

---

## 建议顺序(2026-08-20 更新;原阶段 0→1→2→3 顺序已全部走完)

1. **B-19**(配额 check 不按 resource_kind 过滤维度)—— 直接影响对外 429 行为,工程可直接开工
2. **P-1~P-5 拍板** —— 产品题,不拍板工程动不了;P-4(api_keys 凭据横向扩散)风险最高建议先议
3. **X-1 沙箱波 4** 或 **X-10 多副本整体实施方案** —— 两个大体量项,按业务优先级择一开
4. 其余 X / B 项零散,可捎带或攒波做;X-3 的「投递无 CAS/幂等」在动多副本(X-10)前必修

## 这一轮攒下的教训(派发时带上)

1. **否定性断言必须穷尽**。"一道闸都没有"/"零 authz"/"从未" —— 窗口有限的 grep/sed 的
   "没找到"只支撑得起**肯定**断言。本轮因此错判 `fork_template` 为零授权(它有
   `ensure_resource_access`,在我 40 行窗口外的第 978 行),写进 brief 后**直接造成一个未修的
   Critical**(implementer 照我说的跳过了 `POST /v1/skills/import`)。
2. **数字必须带口径**。本轮三次同类错误:external 路由"2 条"(是文件内 vs 前缀下)、
   基线"93 passed"(是双文件合计 vs 单文件)、报告"351 用例"(把被测文件自己的数折回去了)。
   凡写进 brief 的数字,连口径一起写。
3. **单独看低危的缺口,组合起来是 Critical**。我在 Task 3 brief 里亲手把"员工 viewer 能注册
   webhook"标成"独立的低优先级问题" —— 它后来成了绕过整条 `agent_private` 隐私修复链的通道。
4. **凡"这种数据不会存在,所以不用过滤"的推理,先找出所有能产生这种数据的路径**,
   而不是只想到攻击路径。上一轮漏判 `list_promote_requests` 就是漏了"admin 合法开
   promote-request 是正常工作流"。
5. **让复审独立判断而非采信报告叙述** —— 它防的不只是过度自信,也防过度自责:本轮有一次
   实现者自报"这条测试无法独立证明 GET 未受影响",复审实测反驳,证明它**能**独立捕获,
   省了一轮白改。
6. **加闸时不光看闸挂没挂,还要确认目标路径没被上游认证豁免吃掉**
   (`auth_exempt_path_prefixes` 里的 `/v1/webhooks` 差一点就吃掉 `/v1/webhook-endpoints`,
   靠的是豁免判断写的是 `prefix + "/"` 而非裸 `startswith`)。
