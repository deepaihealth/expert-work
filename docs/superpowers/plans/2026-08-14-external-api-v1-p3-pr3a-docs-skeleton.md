# P3 PR-3a:文档站 8 章骨架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把文档站从 6 篇散页重排成 spec §七 的 8 章结构,补齐两个完全没有文档的端点(取消 run / 审批决策),并新增「通用约定」章和可查的错误码总表。

**Architecture:** **保留现有 6 个文件名当章节载体**,只新增 2 个文件。这样零个 URL 失效,不用上重定向基建。章节编号手写(VitePress 不自动编号 —— 手写让锚点、搜索、跨篇引用都对得上),侧栏展开到二级。

**Tech Stack:** VitePress / Markdown。

## Global Constraints

- **spec 出处**:`docs/superpowers/specs/2026-08-11-external-api-v1-design.md` §七。
- **本 PR 与 PR-1(SSE 契约修正)并行进行,两者文件集必须不相交。** 本 PR **不得**修改 `guide/sse-events.md`、`guide/run-agent.md`、`guide/quickstart.md` —— 那三个文件归 PR-1 / PR-2。
- **不得复述 SSE 帧形状。** 涉及 SSE 的地方一律链接到「SSE 事件格式」章。原因:PR-1 正在改帧契约,任何在本 PR 里写下的帧样例都会在几天内变成假话。
- **机密红线**(沿用 #1151):公开文档不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径。写完逐文件自查。
- **文档里的每一条事实都要有代码出处。** 从 `services/control-plane/src/control_plane/api/external_*.py` 和 `api/_external.py` 读,不要从别的文档转抄 —— 转抄正是 `metadata` 帧那条 `trace_id` 假描述扩散到两个文件的原因。
- 每个 Task 结束跑 `cd apps/admin-ui/docs-site && pnpm build`。
- 提交信息用中文,遵循 `<type>: <description>`。

## 章节 → 文件映射(实现时照这张表,不要自创)

| spec 章节 | 文件 | 本 PR 动它吗 |
|---|---|---|
| 1 概述与对接流程 | `guide/quickstart.md` | ❌ 归 PR-2 |
| **2 通用约定** | **新增 `guide/conventions.md`** | ✅ Task 1 |
| 3 认证 | `guide/auth.md` | ✅ Task 4(只加编号 + 交叉链接) |
| 4 接口详情 | `guide/run-agent.md` | ❌ 归 PR-2 |
| **4.2 / 4.7 两个缺失端点** | **新增 `guide/run-control.md`** | ✅ Task 2 |
| 5 SSE 事件格式 | `guide/sse-events.md` | ❌ 归 PR-1 / PR-2 |
| 6 错误码总表 | `guide/errors.md` | ✅ Task 3 |
| 7 对接注意事项与 FAQ | `guide/best-practices.md` | ✅ Task 4 |
| **8 附录:示例 + 自测清单** | 新增 `guide/examples.md` | ❌ 归 PR-3b(四语言示例) |

> 4.2/4.7 之所以单开 `run-control.md` 而不是塞进 `run-agent.md`:`run-agent.md` 已经 463 行,而且它归 PR-2 改 —— 塞进去就是和并行分支抢同一个文件。

---

### Task 1:新增「2 通用约定」章

**Files:**
- Create: `apps/admin-ui/docs-site/guide/conventions.md`
- Modify: `apps/admin-ui/docs-site/.vitepress/config.ts`

**Interfaces:**
- Produces:`/guide/conventions` 页面,承载 spec §七 的 2.1–2.6 六节

**内容来源(逐节的代码出处,实现者按这个去读,不要凭印象写)**:

| 节 | 写什么 | 去哪读 |
|---|---|---|
| 2.1 环境地址 | 测试环境公网地址 + `/v1` 前缀;生产环境标"未开放" | `tools/deploy/smoke.sh` 里的 `PUBLIC_BASE`(**只取域名,不要把脚本路径或集群信息写进文档**) |
| 2.2 协议约定 | HTTPS、UTF-8、JSON、时间戳格式(ISO-8601 带时区)、UUID 形态 | `api/external_sessions.py` 的响应模型 |
| 2.3 公共请求头 | `Authorization: Bearer <key>`、`Content-Type`、`Idempotency-Key`;以及**响应**头 `X-Expert-Work-Run-Id` / `X-Expert-Work-Session-Id` / `X-Expert-Work-Stream-Mode` | `api/external_events.py:113-119`、`api/agents.py` 的 `extra_headers` |
| 2.4 统一响应格式 | 成功信封 `{"success": true, "data": …}`;错误信封 `{"detail": {"code": …, "message": …}}`。**errors.md:5 已经指出"错误响应的信封形状不统一"——把这个事实原样搬过来,不要粉饰** | `api/_external.py` 的 `external_error` |
| 2.5 限流与配额 | 两者是不同的东西,都会 429 但含义不同 | `errors.md:121-165` 已有内容,搬过来并留一条链接回 errors.md 的详细版 |
| 2.6 幂等性 | `Idempotency-Key` 的作用域(agent + 请求体)、重放语义、两个错误码 | `api/_idempotency.py`;`run-agent.md:181-216` 有现成描述,**转述而不是复制整段**(那一整段归 PR-2 重排) |

**顺带清一条 backlog(B-6)**:同一资源的两个写操作 `user_id` 位置不一致 —— `PATCH /v1/agents/{code}/sessions/{id}` 在 **body**,`DELETE` 在 **query**。这条在 2.3 节写死(带一个"最容易踩"的提示框)。真栈验收时按 query 传 PATCH 吃过 422,这不是假想的坑。

- [ ] **Step 1:读代码,记下每条事实的出处**

- [ ] **Step 2:写 `conventions.md`**

页首用 `# 2 通用约定`,二级标题用 `## 2.1 环境地址` 这样的手写编号。

- [ ] **Step 3:挂进侧栏**

`config.ts` 的 `sidebar` 里,把「对接指南」这一组的 items 按 8 章顺序重排,text 带上章号(`"2 通用约定"`)。本 Task 只加 `conventions.md` 这一项,其余项只改 text 加章号、不改 link。

- [ ] **Step 4:构建 + 自查**

Run: `cd apps/admin-ui/docs-site && pnpm build`
逐行自查机密红线。

- [ ] **Step 5:提交**

```bash
git add -A && git commit -m "docs: 新增「2 通用约定」章——环境/协议/请求头/信封/限流/幂等"
```

---

### Task 2:补两个零文档端点 —— 取消 run 与审批决策

**Files:**
- Create: `apps/admin-ui/docs-site/guide/run-control.md`
- Modify: `apps/admin-ui/docs-site/.vitepress/config.ts`

**Interfaces:**
- Consumes:Task 1 的侧栏结构
- Produces:`/guide/run-control` 页面(spec §七 的 4.2 + 4.7)

**为什么是个真缺口**:全文档站 grep 不到 `cancel`,也 grep 不到 `decide`。这两个端点 P1 就交付了(spec §四 第 2、7 项),第三方要用只能靠猜。

- [ ] **Step 1:读实现**

`services/control-plane/src/control_plane/api/external_runs.py`(`:cancel`)和 `external_approvals.py`(`:decide`)。读全,包括:路径与方法、必填 query / body、成功响应体、每一种失败的状态码与 `code`、以及归属校验失败时返回什么(是 404 不是 403 —— 这一点必须写清楚,否则对接方会把"不是你的 run"误判成"run 不存在"而去重试)。

- [ ] **Step 2:写 `run-control.md`**

两节:`## 4.2 取消 run` / `## 4.7 审批决策`。每节给 curl 样例 + 请求参数表 + 响应样例 + 失败码表。

**SSE 侧一律不写。** "取消之后流上会看到什么"链接到「SSE 事件格式」章 —— PR-1 正在改这部分契约。

- [ ] **Step 3:挂侧栏 + 从 `errors.md` 加交叉链接**

(`run-agent.md` 里的交叉链接归 PR-2 加 —— 本 PR 不碰那个文件。)

- [ ] **Step 4:构建 + 自查**

Run: `cd apps/admin-ui/docs-site && pnpm build`

- [ ] **Step 5:提交**

```bash
git add -A && git commit -m "docs: 补取消 run 与审批决策两个端点的文档"
```

---

### Task 3:「6 错误码总表」—— 加一张能查的表

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/errors.md`

**背景**:现在这篇按 HTTP 状态码分节讲得挺细,但**没有一张可查的码表** —— 对接方线上收到一个 `code`,要靠翻六节找它在哪。spec §七 第 6 章要的就是这张表。

- [ ] **Step 1:穷尽收集错误码**

`rg -n '"code":|code="' services/control-plane/src/control_plane/api/_external.py services/control-plane/src/control_plane/api/external_*.py` 收集对外面会返回的全部 `code`。

**这是一个否定性断言场景**:"这就是全部的码"这句话,窗口有限的 grep 的"没找到"支撑不起来。要么读到文件真实边界,要么找出码的**单一定义处**(如果有枚举/常量表)并以它为准。收集完在报告里写出你用的是哪种判据。

- [ ] **Step 2:在文件顶部加总表**

`## 6.1 错误码总表`,列 `code` / HTTP 状态 / 含义 / 建议处理(重试?换参数?联系管理员?)。现有的按状态码分节内容保留,作为 `## 6.2` 起的详解,总表的每一行链接到对应小节。

**建议处理这一列是这张表的价值所在**,别只翻译含义 —— "429 限流:退避重试" 和 "429 配额耗尽:重试没用,去后台加额度" 是两件事,现有文档已经指出了这个区别,总表要把它带进来。

- [ ] **Step 3:构建**

Run: `cd apps/admin-ui/docs-site && pnpm build`

- [ ] **Step 4:提交**

```bash
git add -A && git commit -m "docs: 错误码总表——按 code 可查 + 每条给处理建议"
```

---

### Task 4:「7 对接注意事项与 FAQ」+ 联调自测清单 + 页脚版本号

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/best-practices.md`
- Modify: `apps/admin-ui/docs-site/guide/auth.md`(只加章号 + 交叉链接)
- Modify: `apps/admin-ui/docs-site/.vitepress/config.ts`

- [ ] **Step 1:`best-practices.md` 重排为「7 对接注意事项与 FAQ」**

现有四节(只在服务端调用 / `user_id` 怎么取 / key 保管轮换 / Stream 断线)保留,加章号。**「Stream 断线怎么处理」这一节只留一句话 + 链接到「SSE 事件格式」章** —— 它现在复述了帧语义,而 PR-1 正在改那部分。

新增 FAQ 小节,每条是真问题不是凑数。候选(实现者按代码核实后择优,不要全写):
- 会话 id 从哪来、不传会怎样
- 同一个 `user_id` 能并存多少段会话
- key 轮换期间旧 key 还能用多久
- `agent_code` 从哪拿(**注意**:`GET /v1/agents` 已对第三方关死,别在文档里指向它)
- 拿不到 `trace_id` 怎么排查问题

- [ ] **Step 2:加「联调自测清单」**

勾选式,覆盖对接方上线前该验一遍的动作:拿 key → 发起 run(两种 mode)→ 读 SSE → 上传文件 → 续会话 → 断线重连 → 取消 → 审批决策 → 会话列表/历史消息 → 工作区文件列表/下载。每项一行,链接到对应章节。

**这一节的形式很重要**:是让人照着勾的清单,不是散文。

- [ ] **Step 3:页脚版本号**

`config.ts` 的 `themeConfig.footer` 加 `message` / `copyright`,带 API 版本(`v1`)和文档更新日期。查 VitePress 当前版本的 footer 配置字段名再写,别凭记忆。

- [ ] **Step 4:侧栏展开到二级**

spec §七 要求"侧栏展开到二级"。`config.ts` 里给每个章节项加 `items`(指向页内二级锚点),或者开 `sidebar` 的 `collapsed: false`。以实际渲染效果为准 —— 跑 `pnpm dev` 看一眼,别只看配置对不对。

- [ ] **Step 5:构建 + 全站链接自查**

Run: `cd apps/admin-ui/docs-site && pnpm build`
构建通过后,grep 全站相对链接,确认没有指向不存在的文件或锚点(VitePress 的死链检查默认不报错)。

- [ ] **Step 6:提交**

```bash
git add -A && git commit -m "docs: FAQ + 联调自测清单 + 页脚版本号 + 侧栏二级"
```

---

### Task 5:收尾自检

- [ ] **Step 1:确认没碰 PR-1 的文件**

Run: `git diff --name-only main...HEAD`
必须**不包含** `guide/sse-events.md`、`guide/run-agent.md`、`guide/quickstart.md`。包含了就是和并行分支抢文件,退回去改。

- [ ] **Step 2:全站构建**

Run: `cd apps/admin-ui/docs-site && pnpm build`

- [ ] **Step 3:机密红线终检**

对本 PR 全部改动跑一遍:凭据 / 密钥名 / 金库路径 / 内网地址 / 集群串 / 内部服务名 / 内部模块路径,一个都不能有。

- [ ] **Step 4:开 PR**

PR 描述列出:8 章映射表、新增的两个页面、错误码总表的收集判据(见 Task 3 Step 1)、以及"没碰 PR-1 文件"的验证结果。
