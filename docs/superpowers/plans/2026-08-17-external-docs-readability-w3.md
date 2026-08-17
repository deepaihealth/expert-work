# 对外文档站第三轮可读性整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按第三轮审计(`docs/superpowers/specs/2026-08-17-external-docs-readability-w3-audit.md`,下称「审计」)把 `apps/admin-ui/docs-site/guide/` 十章改到「字段有取值枚举 + 每个值的含义、表头自解释、概念句答谁→谁/何时/读者干嘛」的标准。

**Architecture:** 纯文档改动,不动代码。三个任务按文件切分、互不相交、可并行;每个任务只改自己名下的文件;审计里每条条目都指派给恰好一个任务。审计条目里**所有取值、行号、`file:line` 都已在代码里核过**——实施者照抄即可,但写进公开文档时**只写取值和含义,不写代码路径**。

**Tech Stack:** VitePress 1.6(`::: code-group` 一 tab 一 fence;标题式代码块 ```` ```json [响应 200] ````;标题里不能有 `[` `]`)。

## Global Constraints

- **公开文档红线**:不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径(含 `expert_work://`、`expert_work.`、`control_plane.`、`orchestrator.`、`packages/`、`services/`、`.py` 文件名、`file:line`)。审计里的 `file:line` 是给你核对用的,**一个都不能抄进 guide/**。
- **不改任何标题文字**(`##`/`###`/`####` 都不改)——锚点全站有引用;需要新小节只能**新增**,不能重命名。侧栏 `.vitepress/config.mts` 不动。
- **字段表规则**:每行 = 字段 / 类型 / 含义 / 取值(代码里是有限集的**全部列出并逐个给含义**;真开集的写「开集,今天有 …,遇到没见过的值 → 忽略/照常展示」)。禁止「取值不在这里穷举」「不穷举」这类句子;禁止表头里出现「除 X / Y 外还有」这种条件语。
- **表规则**:一张表只回答一个问题;表头是自解释名词短语;按条目逐行,不按类别归并;`kind`/`channel` 决定形状的,用「按 X 分」的 `####` 小节或加一列「哪些 X 有」,不用散句。
- **概念句规则**:每个概念(心跳、审批、worker、压缩、护栏、归档、覆盖…)首段必须答:谁发起 → 发给谁 / 什么时候 / 读者该做什么。
- **术语统一**(审计 E-5/E-8):run 一律叫「run」;后台清理叫「后台清理作业」;worker 的叫「子任务」;用户传上去的叫「附件」,Agent 写出来的叫「工作区文件」,登记成成果的叫「产物」;`session_id`/`thread_id` 同一值,凡出现 `thread_id` 的字段行都补「下一轮对话把它填进 `session_id`」。
- **示例值必须真**:审计点出的三个编造值(`worker.outcome:"completed"`、`retry.error_class:"ReadTimeout"`、worker 静态/动态混搭示例)必须换成代码里可能出现的值。
- **可读性四条**沿用:一条 bullet 一件事 / 一个列表一类事 / 占位符 `{user_id}` 花括号且行内代码不夹中文 / 「**加粗:**」伪标题升 `###`(升级会产生新锚点,不影响既有锚点)。长段(>5 行)拆 bullet;`::: warning`/`::: danger` 框一框一话题。
- **审计里被否决的条目(别做)**:A-24 的 `TOO_MANY_IMAGE_REFS`(files 上限 64 == 图片上限,真实请求撞不到)与 `INVALID_FILE_REF`(只有手工塞坏行才触发)**不加进错误码表**;E-7 缓存 TTL 文档是对的**别改**;`PLATFORM_SCOPE_FORBIDDEN` 对外到不了不加。
- 每个 task 结束:`cd apps/admin-ui/docs-site && pnpm exec vitepress build .` 通过 + 死链/死锚点脚本(`docs/superpowers/plans/2026-08-16-phase3-pr-b-artifacts.md` 1051–1081 行那段 python,含同页锚点)零 dead + 红线 `rg -n "aliyuncs|kubeconfig|127\.0\.0\.1|crpi-|expert_work[:.]|control_plane\.|orchestrator\.|packages/|services/|\.py\b|\.(md|py|ts|tsx):[0-9]+" apps/admin-ui/docs-site/guide/<你改的文件>` 为空(最后一个模式抓 `file:line` 残留;`examples.md` 里样例脚本自己的文件名如 `run_stream.py` 是合法命中,逐条看)。
- 事实清单:改每一节前把该节所有规范性陈述抄进 report 做清单,改后逐条勾落点。

---

## 文件结构

| Task | 文件 | 审计条目 |
|---|---|---|
| 1 | `guide/sse-events.md`(+ `guide/best-practices.md:40` 那一句、`guide/examples.md:44-56` 那段 docstring 的正文化) | A-1~A-16、B-1~B-7、C-1~C-6、C-11、D-1、D-2/D-3/D-4/D-5 中属于本文件的、D-8、E-6、E-9(sse 侧)、F-1/2/3/4/5/7/8/9/11/12/14 |
| 2 | `guide/query.md`、`guide/run-control.md` | A-17~A-20、A-25、B-8、C-9、C-10、D-3/D-4/D-5 中属于这两文件的、E-5(这两文件)、E-10(query 侧只留一句+链接)、F-6、F-15 |
| 3 | `guide/errors.md`、`guide/conventions.md`、`guide/chat.md`、`guide/quickstart.md`、`guide/auth.md`、`guide/best-practices.md`(除 :40 那句) | A-21~A-24(减否决项)、B-9、B-10、C-7、C-8、C-12~C-15、D-3/D-4/D-5/D-6 中属于这些文件的、E-1~E-4、E-8、E-9(quickstart 侧)、F-10、F-13 |

三个任务文件不相交,**并行三个 worktree**。

---

### Task 1: `sse-events.md` 整章 —— 3.2 心跳/id 表、3.4 十节统一模板 + 全部取值闭合

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/sse-events.md`
- Modify: `apps/admin-ui/docs-site/guide/best-practices.md`(**只改 `:40` 那一句**,让它承诺的「多久没收到数据判定连接已死」在 3.6 真有落点;别的不碰)
- Modify: `apps/admin-ui/docs-site/guide/examples.md`(**只做 D-8**:把 `:44-56` 那段 `readline()` vs `read(1024)` 的 docstring 知识点提炼成 3.5 的一段正文;examples 里的 docstring 缩成一句指向 3.5;别的不碰)

**Interfaces:** 无代码接口。审计 A-1~A-16 给了每个字段的取值与含义;B-1~B-7 给了每个概念段缺的答案;C-1~C-6/C-11 给了表的目标列集;D-1 给了 3.4 十节应统一成的模板。

- [ ] **Step 1: 3.2「心跳」段(F-1 / B-1)**:重写成「谁发(服务端的 SSE 端点)→ 发给谁(当前这条连接;只有实时连接有,回放没有)/ 什么时候(连接空闲 15 秒没有任何事件时)/ 读者做什么(跳过 `:` 开头的行、不动游标;**读超时建议 45 秒**——明写「服务端没有规定,这是建议值」)」。3.5 接收器骨架里的 `readTimeoutMs` 与 45 秒对齐;3.6 里补一段「多久没动静算断」与之呼应。`best-practices.md:40` 那句改成能兑现的措辞并链到 3.6 对应段。
- [ ] **Step 2: 3.2「哪些事件有 id」表(F-2 / C-1)**:按审计 C-1 的 12 行版本重做——每事件一行,列 = 事件 / 有 `id:` / 断线重连会重发吗 / 参与游标(`since_seq`)吗 / 为什么。删掉按类别归并的那张。
- [ ] **Step 3: 3.3 两张一览表(C-2)**:按审计 C-2 合并/重排(不改小节标题)。
- [ ] **Step 4: 3.4 十节统一模板(D-1)**:每节严格四段:`#### 什么时候发`(一句答谁→谁/何时)→ `#### data 字段`(表;形状随 `kind`/`channel` 变的用 `#### 按 kind 分` 或加「哪些 kind 有」列)→ `#### 完整示例` → `#### 前端怎么渲染`。**不改 `###` 事件名标题。**
- [ ] **Step 5: 取值闭合(A-1~A-16,F-3/4/5/7/8/9/12/14)**逐条落地,重点:`tool.status` 2 值 + 8 种 `error` 成因(用大白话列成因,不写代码位置);`worker.outcome` 3 值 + 「异常终止不发 `end`,前端要兜底」;`retry` 示例改 `AllProvidersExhaustedError`、`attempt` 恒 1、`backoff_s` 默认 10 范围 [1,120];`approval.node` 恒 `"tools"`、`reason_kind` 5 值分两类 + 「这是客户端唯一能提前区分 reject 后果的字段」+ 链到 run-control 4.2 对应段;`finish_reason` 改开集 + 已知子集 + 「别拿它判轮次结束」;`end.status` 补 `cancelled→interrupted`、`max_steps→error` 两处收敛;`guard` 与 `end` 互相点破 `max_steps` 那条矛盾(F-7);token `channel` 表按 C-3 合并;guard `detail` 表按 C-4;C-5/C-6/C-11 照审计;`updates` 的 `null` 归因改成事实描述(F-12);worker 示例二选一(F-14),`depth` 写 1–3;A-9/A-10/A-11/A-12/A-13/A-15/A-16 的补充事实逐条加。
- [ ] **Step 6: B-2~B-7 概念段**逐条补齐「谁→谁/何时/做什么」;F-11 的 approval 三句(只有这一事件能知道有待审批、必须立刻持久化、超时窗口默认 24h 可配 [60,604800] 秒、超时按 reject)。
- [ ] **Step 7: 格式**:D-2 多主题框拆开、D-3 长段拆 bullet、D-4 无标题代码块补标题、D-5 伪标题升级(本文件范围)。
- [ ] **Step 8: 构建 + 死链 + 红线 + 事实清单勾完** → Commit `docs(external-api): SSE 章第三轮整改 —— 心跳/id 表重做、3.4 十节统一模板、全部字段取值闭合`

### Task 2: `query.md` + `run-control.md` —— run status 八值含义、列表分页判据、审批/取消字段闭合

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/query.md`
- Modify: `apps/admin-ui/docs-site/guide/run-control.md`

- [ ] **Step 1: A-17 / C-9 / F-6**:5.4 run `status` 改成 8 行表(取值 / 含义 / 是否最终状态 / 客户端该怎么做),`timeout` 标「保留值,今天不会出现」;`status` 查询参数补「枚举外 → 422 `INVALID_REQUEST`」。
- [ ] **Step 2: A-18 / F-15**:三个列表(会话/消息/run)的筛选与分页:默认值、上限、无效值行为、**怎么判最后一页**(只回显 `limit`/`offset`,没有 `total`/`has_more`,靠「条目数 < limit」;消息列表是内存切片)。写进 5 章通则一段 + 各小节一句。
- [ ] **Step 3: A-25**:5.2 `running` 的判据写明三个状态名,点破 `paused` 不算 running。
- [ ] **Step 4: A-19 / A-20**:4.2 审批决策请求体 `decision`(3 值)/ `modified_args` / `mode`(2 值)/ `request_id`,4.1 取消的 `stopped` + 「已结束」定义,逐字段取值 + 含义;B-8 归档段答「谁→谁/何时/做什么」;C-10 工作区文件 `Content-Disposition` 表按条目重排;E-10 `query.md:559` 缩成一句 + 链接 `./errors#_8-11-429-——-两种情况-含义不同`(**构建后 grep 产物 HTML 核对 id**)。
- [ ] **Step 5: 格式**:D-3/D-4/D-5 本两文件范围;E-5 五处「任务」按术语统一改。
- [ ] **Step 6: 构建 + 死链 + 红线 + 事实清单** → Commit `docs(external-api): 查询与控制两章第三轮整改 —— run status 八值含义、分页判据、审批/取消字段闭合`

### Task 3: `errors.md` / `conventions.md` / `chat.md` / `quickstart.md` / `auth.md` / `best-practices.md` —— 错误码表加端点列、章名一致、附件三词对照、`POST /sessions` 补节

**Files:**
- Modify: 上述六个文件(`best-practices.md` 除 `:40` 那句外的部分)

- [ ] **Step 1: A-24 / C-12 / F-10**:错误码总表加「哪个端点」列;`UPLOAD_FAILED` 500/502 拆两行;补 `APPROVAL_ERROR` / `UPLOAD_ERROR` / `AUTH_UNAUTHENTICATED` / `AUTH_BACKEND_UNAVAILABLE` 四个兜底/低概率码(标「兜底码,一般遇不到」);**不加** `TOO_MANY_IMAGE_REFS` / `INVALID_FILE_REF` / `PLATFORM_SCOPE_FORBIDDEN`;`errors.md:181` 图片数量段保持两档(不写「三档」)。conventions 7.4 响应头表补 `X-Expert-Work-Trace-Id`(每个响应都有,报障时给它)。
- [ ] **Step 2: E-1 / E-2 / F-13**:errors.md 13 处「取消 run 与审批决策」→「4 对话过程中的控制」;短名链接改带编号全名;`conventions.md:81` 补锚点;errors.md 7 条 300+ 字符单段拆 bullet;C-13/C-14/C-15 表按审计重排(quickstart 的 `end.status` 表保持两列摘要 + 链到 3.4 `end`)。
- [ ] **Step 3: E-3**:在 `chat.md` 2.5「多轮会话」末尾**新增** `####` 小节「提前拿一个 session_id」,写 `POST /v1/agents/{agent_code}/sessions`(201,`session:write`,请求 `{user_id}`,响应 `data.session_id`,错误码同上传接口的 user/agent 校验),`[请求]`/`[响应 201]` 成对;`best-practices.md:64` 那行链到它。
- [ ] **Step 4: E-8**:`conventions.md` 新增一个 `###` 小节「附件 / 工作区文件 / 产物 —— 三个词的区别」(三行表:谁产生 / 在哪个接口 / 用什么标识 / 怎么删);`errors.md:21` `UPLOAD_NOT_FOUND` 行措辞统一成「附件」。
- [ ] **Step 5: A-21 / A-22 / A-23**:Agent 目录字段补「没有 status 字段」负空间;产物 `kind` 补「不校验」;上传响应 `type` 2 值 + MIME 白名单表 + inline/attachment 分支(chat.md 2.6);B-9 同名覆盖段答「谁→谁/何时/做什么」;B-10 `error.code` 形状段;C-7/C-8 表按审计;A-14 无关。E-4 三处「限流 vs 配额」:errors 8.11 为权威,conventions 7.6 保留表 + 一句链接,errors 8.16 缩成一句;E-9 quickstart 侧。
- [ ] **Step 6: 格式**:D-3/D-4/D-5/D-6(chat.md :102/:106 两处中文内联占位)本六文件范围。
- [ ] **Step 7: 构建 + 死链 + 红线 + 事实清单** → Commit `docs(external-api): 错误码/约定/对话/上手四章第三轮整改 —— 错误码表加端点列、章名一致、附件三词对照、预建会话补节`

---

## 收尾

三个 worktree 分支合回 `docs/external-api-readability-w3` → 全站构建 + 死链 + 红线 → 全分支终审(opus,重点:三任务之间的交叉链接与术语一致、审计每条是否有落点、公开文档红线)→ 一波修复 → PR → 合并 → 发测试。
