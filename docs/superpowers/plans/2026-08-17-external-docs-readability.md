# 对外文档可读性整改 —— 实施计划(线 B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按用户 2026-08-17 的八条反馈把对外文档站(`apps/admin-ui/docs-site/guide/`)改到「对接方一遍能读懂」:代码块分清请求/响应、长段拆开、术语统一、SSE 整章重写成前端实施指南、参数表补全。

**Architecture:** 纯文档 + 一个 VitePress 渲染小插件(代码块标题)。**不碰 `chat.md`、`errors.md`**——它们随接口改动(线 A)一起重写,避免两条线冲突。

**Tech Stack:** VitePress 1.6(mermaid 插件已在)、markdown-it fence 包装。

**分支:** `docs/external-api-readability`(已有 1 commit `27bc175a`:删两处「破坏性变更」块)。

## Global Constraints

- **术语**:全站「帧」一律改「事件」(SSE 协议 `event:` 字段本就叫事件;「帧」是自造词)。唯一例外:引用 SSE 规范原文的地方可写「事件(SSE event)」。`token` 这一种可称「`token` 事件」,不叫「token 帧」。
- **代码块标题**:请求块用 ```` ```bash [请求] ````,响应块用 ```` ```json [响应 200] ````(状态码按真实值),SSE 片段用 ```` ``` [事件流片段] ````。同一场景多个变体(文档 / 图片、stream / queue)用 `::: code-group`,每个 tab 里请求与响应**成对**出现。**永远不出现连着两个无标题代码块。**
- **可读性四条**(每个 bullet / 段落对照检查):① 一条 bullet 只说一件事;② 一个列表只装一类事(参数 / 权限 / 超时 / 分页各自成节或成表);③ 占位符统一花括号风格 `{user_id}` `{agent_code}` `{run_id}`,**行内代码里不夹中文**(`?user_id=<同一个 user_id>` 这种改成 `?user_id={user_id}` + 正文说明「与发起 run 时相同」);④ 「**加粗:**」开头的伪标题升成 `###` 真标题。
- **示例载荷完整**:不用 `"..."` 省略;id 用固定示例值贯穿(`user_id: "u-123"`、session `sess_…`、run 用文首约定的一个 UUID)。**载荷字段必须与代码一致**——SSE 事件的 `data` 形状以 `services/orchestrator/src/orchestrator/sse.py`(`metadata_payload` ≈L509、`retry_payload` ≈L595、`approval_payload` ≈L670、`token` ≈L476、`worker` ≈L484、`guard` ≈L490、`compaction` ≈L450、`end_frame_data`)与 `services/control-plane/src/control_plane/api/_run_event_stream.py`(`truncated` L180、`gap` L240/L279/L358)为准,**逐字段核对后再写**。
- **事实不丢**:重写前先把当前章节的每一条规范性陈述(「必须 / 一律 / 不会 / 只有 / 上限 …」)抄进 report 文件做「事实清单」,重写后逐条勾选落点;reviewer 按清单验收。
- **公开文档红线**:不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径(含 `expert_work://`、`control_plane.`、`orchestrator.`、`packages/`、`services/`)。
- 每个 task 结束:`cd apps/admin-ui/docs-site && pnpm exec vitepress build .` 通过 + 跑死链/死锚点脚本(见 `docs/superpowers/plans/2026-08-16-phase3-pr-b-artifacts.md` 末尾那段 python,含同页锚点)零 dead + 红线 `rg` 为空。
- 章节编号(3.x / 4.x / 5.x)与侧栏 `.vitepress/config.mts` 里的条目**同步**;改标题就改侧栏,改锚点就全站 `rg` 引用处一起改。

---

## 文件结构

| 动作 | 路径 | 职责 |
|---|---|---|
| Modify | `apps/admin-ui/docs-site/.vitepress/config.mts` | Task 0 fence 标题插件;Task 1/2/3 侧栏条目同步 |
| Create | `apps/admin-ui/docs-site/.vitepress/theme/index.ts` + `custom.css` | 代码块标题样式(扩展默认主题) |
| Rewrite | `guide/sse-events.md` | Task 1 |
| Modify | `guide/run-control.md` | Task 2 |
| Modify | `guide/query.md` | Task 3 |
| Modify | `guide/quickstart.md`、`auth.md`、`conventions.md`、`best-practices.md`、`examples.md`、`index.md` | Task 4 |
| **不碰** | `guide/chat.md`、`guide/errors.md` | 线 A Task 5 |

## 依赖与并行

```
Task 0(渲染插件,30 分钟)
  ├─→ Task 1(SSE 整章)                 [worktree W1]
  └─→ Task 2 → Task 3 → Task 4        [worktree W2,顺序:互不相干但都小,一个人接着做省上下文]
```
Task 1 与 2/3/4 文件不相交,并行。

---

### Task 0: 代码块标题渲染

**Files:**
- Modify: `apps/admin-ui/docs-site/.vitepress/config.mts`
- Create: `apps/admin-ui/docs-site/.vitepress/theme/index.ts`、`apps/admin-ui/docs-site/.vitepress/theme/custom.css`
- Test: 临时页 `guide/_probe.md`(构建后删)

**背景(已实测)**:VitePress 1.6 单独代码块的 ```` ```bash [请求] ```` **静默丢弃**标题(`[...]` 只在 `::: code-group` 内当 tab 名)。要自己在 fence 渲染后插一个标题栏。

- [ ] **Step 1: 写探针页**(与本计划同目录的 `_probe.md`,内容:一个 `[请求]` bash 块 + 一个 `[响应 200]` json 块 + 一个 `::: code-group`(两个 tab)),构建,确认现在**没有**标题元素:

```bash
cd apps/admin-ui/docs-site && pnpm exec vitepress build . --outDir /tmp/vp-probe && \
python3 -c "import re;h=open('/tmp/vp-probe/guide/_probe.html').read();print('title-bar:', 'ew-code-title' in h)"
```
预期 `title-bar: False`。

- [ ] **Step 2: 插件**——`config.mts` 的 `defineConfig({...})` 里加(与既有 `markdown` 键合并,若已有 `config(md)` 则在里面追加):

```ts
markdown: {
  config(md) {
    const fence = md.renderer.rules.fence!
    md.renderer.rules.fence = (tokens, idx, options, env, self) => {
      const html = fence(tokens, idx, options, env, self)
      const token = tokens[idx]
      const m = /\[([^\]]+)\]/.exec(token.info)
      if (!m) return html
      // 在 ::: code-group 里,[title] 已经是 tab 名,不再重复画标题栏
      for (let i = idx - 1; i >= 0; i--) {
        const t = tokens[i]
        if (t.type === 'container_code-group_open') return html
        if (t.type === 'container_code-group_close') break
      }
      const title = md.utils.escapeHtml(m[1])
      return html.replace(
        /^(<div class="language-[^"]*"[^>]*>)/,
        `$1<div class="ew-code-title">${title}</div>`
      )
    }
  },
},
```

- [ ] **Step 3: 样式**——`theme/index.ts`:

```ts
import DefaultTheme from 'vitepress/theme'
import './custom.css'
export default DefaultTheme
```

`theme/custom.css`(与默认主题的 `.lang` 角标、copy 按钮共存;标题栏放在块顶部左侧):

```css
.vp-doc div[class*='language-'] .ew-code-title {
  font-family: var(--vp-font-family-base);
  font-size: 12px; font-weight: 600; letter-spacing: .02em;
  color: var(--vp-c-text-2);
  background: var(--vp-code-block-bg);
  border-bottom: 1px solid var(--vp-c-divider);
  padding: 8px 20px; margin: 0;
  border-radius: 8px 8px 0 0;
}
.vp-doc div[class*='language-']:has(.ew-code-title) > pre { margin-top: 0; }
.vp-doc div[class*='language-']:has(.ew-code-title) > .lang { top: 8px; }
.vp-doc div[class*='language-']:has(.ew-code-title) > .copy { top: 44px; }
```
(`:has` Chrome/Safari/Firefox 现代版均支持;不支持时只是标题栏与角标略挤,不影响阅读。)

- [ ] **Step 4: 重建探针**,断言:`ew-code-title` 出现 **恰好 2 次**(两个单块),code-group 那两块**没有**标题栏(tab 仍是「文档 / 图片」)。用 `Read` 工具打开 `/tmp/vp-probe/guide/_probe.html` 对应截图不可行,就 grep 结构;必要时 `pnpm exec vitepress preview` 用浏览器看一眼。
- [ ] **Step 5: 删 `_probe.md`**;`pnpm exec vitepress build .` 全站构建通过;Commit `docs(site): 代码块支持 [标题] 标题栏——请求/响应一眼可分`

---

### Task 1: 第 3 章「读懂 SSE 流」整章重写

**Files:**
- Rewrite: `apps/admin-ui/docs-site/guide/sse-events.md`
- Modify: `.vitepress/config.mts` 侧栏(3.x 条目按新结构)
- 全站 `rg -n "sse-events#" guide/*.md` 引用锚点同步(**chat.md / errors.md 除外——把它们需要改的锚点列进 report,交线 A**)

**用户原话(必须逐条对应)**:帧格式表述累;事件总表不清楚;「帧」改「事件」;每个事件解释不清、示例不完整、**缺前端渲染示例**;顺序乱——`end` 应在最后。

**新结构**(编号与侧栏同步):

```
3 读懂 SSE 流
3.1 先看一眼:一次 run 的事件流长什么样        —— 一段真实的完整事件流(从 metadata 到 end,可用现有 B 场景素材裁剪),
                                              每行右侧用注释标「这是什么」;读者 30 秒建立整体印象
3.2 事件的格式                                 —— SSE 三行(id / event / data)+ id 只用 seq + 心跳注释行;
                                              一张小表:哪些事件有 id、能否回放
3.3 事件一览(按出现顺序)                       —— 表:事件名 | 什么时候出现 | data 里有什么(一句话)| 前端该做什么(一句话)| 有 id
                                              顺序:metadata → token → updates → worker → guard → compaction → approval → retry → error → end;
                                              gap / truncated 单列在表尾并注明「只描述这条连接,不是 run 的事件」
3.4 每个事件怎么处理(逐个,同一模板)          —— 每个事件一个 ### 小节,顺序同 3.3。模板:
     ### metadata
       **什么时候发** 一句
       **data 字段**  表(字段 | 类型 | 说明)
       **完整示例**   ``` [事件流片段] ```(真实载荷,不省略)
       **前端怎么渲染** ```js [渲染示例]``` 10–25 行 vanilla JS:拿到 event.data 后界面怎么变
     updates 小节保留现有全部事实(节点写入可能为 null / 只读三字段 / 节点名非枚举 / messages 两种形状 /
       tool_call 配对 / UNTRUSTED 还原 / 别拿 token 重建状态),但拆成清晰小标题;
       前端渲染示例要展示「按 tool_call_id 配对、把 ai/tool 消息渲染成对话气泡 + 工具卡」
     approval / retry / error / metadata / worker / guard / compaction 的 data 字段**从 sse.py 读出来核对**,现有文档缺的补齐
     end:status 四值表 + 每值前端动作(现有内容),放本节最后一个
3.5 建议的接收器骨架                            —— 一段 40–60 行 JS:fetch + ReadableStream 解析 SSE(不能用 EventSource——它带不了
                                              Authorization 头,要写明原因)、按 event 分发到 3.4 的处理函数、维护 maxSeq、
                                              读超时与重连(带 since_seq)、遇到 truncated 拉下一页、遇到不认识的事件忽略
3.6 断线重连与回放分页                          —— 现有 3.6 全部事实(live/replay 两分支、maxSeq 游标、gap 语义、truncated + next_seq、
                                              since_seq 只能来自服务端发过的值、长连接自设读超时)重排:一段流程 + 一张时序图 + 坑列表
```

- [ ] **Step 1: 事实清单**——把现 `sse-events.md` 每条规范性陈述抄进 `<workspace>/task-1-report.md` 的「事实清单」表(编号 F1…Fn,每条一句原文 + 落点小节留空)。**这一步没做完不许动正文。**
- [ ] **Step 2: 从代码核对各事件 data**——`services/orchestrator/src/orchestrator/sse.py` 与 `_run_event_stream.py`(位置见 Global Constraints),把 metadata / token / worker / guard / compaction / approval / retry / error / end / gap / truncated 十一种 data 的字段名与类型抄成表(进 report),**现有文档没写的字段一律补,现有文档写了但代码里没有的字段一律删并在 report 里点名**。
- [ ] **Step 3: 按新结构重写全文**(遵守 Global Constraints 全部四条可读性规则 + 术语 + 代码块标题)。
- [ ] **Step 4: 逐条回填事实清单落点**——每条 F 必须有落点小节号;删掉的要写理由(只能是「与代码不符」或「重复」)。
- [ ] **Step 5: 侧栏 + 全站锚点**——`config.mts` 3.x 条目改成新标题;`rg -n "sse-events" guide/*.md` 找到所有引用,除 chat.md / errors.md 外全改;chat.md / errors.md 里需要改的锚点写进 report「交线 A」段。
- [ ] **Step 6: 构建 + 死链脚本 + 红线扫描**(三样都要贴输出进 report);`rg -n "帧" guide/sse-events.md` 为空。
- [ ] **Step 7: Commit** `docs(external-api): 第 3 章 SSE 整章重写——按出现顺序逐事件讲解 + 前端渲染示例 + 接收器骨架;「帧」统一为「事件」`

---

### Task 2: 第 4 章「对话过程中的控制」—— 参数表补全 + 可读性

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/run-control.md`
- 参数事实源:`services/control-plane/src/control_plane/api/external_approvals.py`(决策端点)、`external_runs.py`(取消端点)——**每个字段的类型、必填、取值、默认值、校验规则从 pydantic 模型抄**,不猜。

**用户原话**:审批决策里部分参数缺少解释。现表 `agent_code` / `decision` / `reason` 三行说明为空;`modified_args` 没说形状;`mode` 没说两种模式响应差异在哪。

- [ ] **Step 1: 事实清单**(同 Task 1 Step 1 做法,进 `task-2-report.md`)。
- [ ] **Step 2: 从两个端点的请求模型抄全参数**(含 4.1 取消的参数表)。
- [ ] **Step 3: 改写**——参数表每行「类型 | 必填 | 取值/默认 | 说明」齐全;`decision` 三值各一句「什么时候用、之后 run 怎样」;`modified_args` 写清是「覆盖审批节点原参数的对象,键与工具参数同名」并给一个真实示例;`reason` 写清用途(进审计与事件);响应一节按 stream / queue 分两个 code-group tab,各自请求+响应成对;失败情况列表改成表(码 | 何时 | 你该怎么办)。4.1 取消同标准。可读性四条全过。
- [ ] **Step 4: 构建 + 死链 + 红线;Commit** `docs(external-api): 第 4 章审批/取消参数表补全 + 请求响应成对`

---

### Task 3: 第 5 章「查询与管理」—— 表述与格式

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/query.md`
- 参数事实源:`external_agent_catalog.py`、`external_sessions.py`、`external_runs.py`、`external_workspace.py`、`external_artifacts.py`。

- [ ] **Step 1: 事实清单**进 `task-3-report.md`。
- [ ] **Step 2: 每个端点统一小节模板**:一行端点签名(方法 + 路径 + 权限)→ 参数表(路径 / 查询 / 请求体三种「位置」列清)→ `[请求]` + `[响应 200]` 成对 → 响应字段表 → 「注意」列表(每条一事)。5.7 产物 / 5.6 工作区的下载端点响应是裸字节的要写明「不套信封」+ 响应头。
- [ ] **Step 3: 可读性四条全过**;`5.6 工作区文件` 若提到 `uploads/` 可下载,**保留但加一句**「附件建议改用附件下载端点(见 2.6)」——线 A 会把 2.6 写好,这里只留指针。
- [ ] **Step 4: 构建 + 死链 + 红线;Commit** `docs(external-api): 第 5 章统一端点小节模板 + 请求响应成对`

---

### Task 4: 其余章节可读性 + 术语 + 代码块标题

**Files:**
- Modify: `guide/quickstart.md`、`guide/auth.md`、`guide/conventions.md`、`guide/best-practices.md`、`guide/examples.md`、`index.md`

- [ ] **Step 1**:每章过可读性四条 + 代码块标题;`examples.md` 主要是「帧」→「事件」(53 处)与注释里的术语,代码逻辑不动——**四语言示例都真跑过,别改语义**。
- [ ] **Step 2**:`rg -n "帧" guide/*.md` 只剩 chat.md(交线 A);`rg -n '```(bash|json|http)$' guide/*.md`(无标题代码块)只剩 chat.md / errors.md。
- [ ] **Step 3**:构建 + 死链 + 红线;Commit `docs(external-api): 其余章节可读性整改 + 术语统一 + 代码块标题`
