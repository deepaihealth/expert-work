# Task 3 报告 — 对话详情页接入只读调试台轮次时间线

SDD:「对话详情页 = 只读调试台」波 2 / Task 3(最后一个实现任务)。
分支:worktree 从 main 切出,`git merge --ff-only feat-conversation-run-visibility`
(`e9ecfcc1` → `c8d59fc7`,fast-forward 成功,含 T1 + T2)。

---

## 1. 零回归判据(最高优先)

`apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx` **一个字都没改**:

```
$ git diff --stat -- apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx
（空输出）
$ git status --short   # 该文件不出现
```

搬 `downloadJson` 前后各跑一次,通过数一致:

| 阶段 | 命令 | 结果 |
|---|---|---|
| 本任务基线(合完 T1+T2,未动代码) | `vitest run PlaygroundTab.test.tsx ConversationDetail.test.tsx components/turn/__tests__` | `Test Files 4 passed` / **`Tests 60 passed`**(45+7+8) |
| **搬完 `downloadJson` 之后** | `vitest run src/pages/__tests__/PlaygroundTab.test.tsx` | `Test Files 1 passed` / **`Tests 45 passed (45)`** |
| 终态合跑 | `vitest run ConversationDetail + PlaygroundTab + components/turn/__tests__ + i18n/__tests__` | `Test Files 5 passed` / **`Tests 71 passed`**(45+13+4+5+4) |

`PlaygroundTab.tsx` 因 `downloadJson` 搬迁产生的 diff **只有两块**——加一行 import、删原函数体,
零逻辑改动:

```diff
+import { downloadJson } from "../../components/turn/download_json";
...
-/** Trigger a client-side download of ``data`` as a pretty-printed JSON file. */
-function downloadJson(filename: string, data: unknown): void { … }
```

调用点 `PlaygroundTab.tsx:582`(原 :581)一字未动。

---

## 2. 导出按钮:真导出(优先级最高的新增要求)

**问题**:T1 审查者核出 `TurnCard.tsx` 只有三处 `readOnly` 门(:454 / :593 / :610),
「导出 JSON」按钮(`:736 data-testid="playground-export-json"`)**不在其中**。
按 T1 报告示意传 `onExport={() => {}}` 会在只读页渲染一个点了没反应的死按钮。

**做法**(照 `PlaygroundTab.tsx:581` 调用点的形状):

1. `downloadJson` **逐字**搬到新文件 `apps/admin-ui/src/components/turn/download_json.ts`
   (实现一个字符没改,只加了模块 docstring),`PlaygroundTab.tsx` 与 `ConversationDetail.tsx`
   都从那里 import。
2. `ConversationDetail.tsx` 实现真 `handleExport`:`runIdOf(turn.events)` 取 runId →
   优先重新拉权威持久流(`streamRunEvents`)→ 失败落回卡里已有帧 → `downloadJson` 落盘,
   payload 字段(`run_id / thread_id / input / source / exported_at / events`)与 playground 一致。

**为什么这里不需要给"事件没到位"特判**:只读卡在
`readOnly && events.length === 0 && loadState !== "done"` 时走 TurnCard 的早返回占位分支,
根本不渲染工具条 → 导出按钮**可见时** `turn.events` 必非空且含 `metadata` 帧,
`runIdOf` 必有值。所以 playground 的形状在这里是完备的。

**验证方式**:新增用例「exports a replayed turn's event stream for real (the button is not a no-op)」
——点真按钮,断言真有一次 anchor download(文件名 `expert-work-events-{runId}.json`)、
Blob 的 MIME 是 `application/json`、JSON 内容里 `run_id / thread_id / source:"backend"` 与
`events.length` 都对得上。

**该用例的变异自验**(证明它能逮住"死按钮"这个具体失效):
把 `onExport={handleExport}` 改回 `onExport={() => {}}`——

```
 × exports a replayed turn's event stream for real (the button is not a no-op) 1119ms
AssertionError: expected [] to deeply equal [ Array(1) ]
 Test Files  1 failed (1)
      Tests  1 failed | 12 passed (13)
```

恢复后 13/13 全绿。

> jsdom 里 `URL.createObjectURL` / `Blob.prototype.text()` 都不存在,
> 测试用 `beforeEach` 打桩 object-URL 两个方法(`afterEach` 还原真值)、
> 用一个记录构造参数的 `RecordingBlob` 子类拿到 JSON 正文。

---

## 3. 新增/改动的测试(最终形态)

### 3.1 `pages/__tests__/ConversationDetail.test.tsx` — 6 例新增(既有 7 例全保留且全绿)

脚手架改动(**只动 wrapper,断言一字未改**):`renderPage` 与「back link restores…」那例的内联
`render` 都套上 `<AuthProvider>` + antd `<App>`(真实树里 `App.tsx` 就是 `<AntApp>` 包 router;
`ToolCallCard` 走 `App.useApp()`,`useAuth()` 无 Provider 会抛)。
`beforeEach` 里 seed 一个 `tenant_id = CONVO.tenant_id` 的 JWT、装 `IOStub`、
把 `listThreadRuns` 默认打成 `[]`(计数不配对 → 既有 7 例继续走扁平路径,行为与今天一致)、
`streamRunEvents` 默认空流。

| # | 用例 | 断言要点 |
|---|---|---|
| ① | rebuilds the transcript as read-only turn cards when messages pair with runs | 2 user 轮 ↔ 2 runs → `playground-turn` 出现 2 张;输入与兜底答案都在;`conversation-message-0`(扁平气泡)**消失** |
| ② | replays a run when its row scrolls into view and renders the tool call | IOStub 立即命中 → `streamRunEvents(THREAD_ID, RUN_1, …)` 被调;`replayed answer` / `step-timeline` / `playground-tool-count`=1 渲染出来;展开 step → `tool-call-card` 里有 `search` |
| ②' | exports a replayed turn's event stream for real | 见 §2 |
| ③ | keeps the flat message block when the message/run counts don't line up | 2 user 轮 vs 1 run → `conversation-message-0` 在、`playground-turn` 不在 |
| ④ | stays flat and issues no replay for a cross-tenant thread | token 换成别的 tenant 的 system_admin;**即便 runs 打成能 1:1 配对**,`listThreadRuns` / `streamRunEvents` 都 `not.toHaveBeenCalled()`,页面扁平 |
| ⑤ | every run row carries an explicit drill-in link to its run detail | 两行都有 `conversation-run-open-{runId}`,`href` = `/runs/{thread}/{run}` |

### 3.2 `components/turn/__tests__/useHistoryTurns.test.ts` — 补 T1 遗漏的第 5 例

`IOStub.observe` 里加一个模块级 `observeSpy`(`beforeEach` reset),新增:

> **observes a row element only once when its ref re-registers** —— 同一个 DOM 元素用
> `registerRow("r1","th-1")` 注册两次,断言 `observeSpy` 只被调用 1 次。

**变异自验**(这道守卫此前零覆盖,T1 审查者删掉它 49/49 仍全绿):
删掉 `useHistoryTurns.ts:194` 的 `if (runIdByEl.current.has(el)) return;` ——

```
 FAIL  src/components/turn/__tests__/useHistoryTurns.test.ts > useHistoryTurns > observes a row element only once when its ref re-registers
AssertionError: expected "vi.fn()" to be called 1 times, but got 2 times
 ❯ src/components/turn/__tests__/useHistoryTurns.test.ts:187:24
 Test Files  1 failed (1)
      Tests  1 failed | 4 passed (5)
```

恢复守卫后 5/5 全绿。

---

## 4. 跨租户守卫的变异自验(brief Step 5)

守卫实现(`ConversationDetail.tsx:131-137`):

```ts
const sameTenant =
  convo !== null && homeTenantId !== null && convo.tenant_id === homeTenantId;

useEffect(() => {
  if (!threadId || !sameTenant) return;
  void loadHistory(threadId);
}, [threadId, sameTenant, loadHistory]);
```

**变异**:改成 `const sameTenant = convo !== null;`(恒真 → 总是 replay)。

**红**:

```
 FAIL  src/pages/__tests__/ConversationDetail.test.tsx > ConversationDetail > read-only turn timeline
      > stays flat and issues no replay for a cross-tenant thread
TestingLibraryElementError: Found multiple elements by: [data-testid="playground-turn"]
 Test Files  1 failed (1)
      Tests  1 failed | 12 passed (13)
```

(报的是"本该扁平的页面渲染出了轮次卡";用例里 `not.toHaveBeenCalled()` 两条紧随其后。)

**绿**:恢复守卫 → `Tests 13 passed (13)`。

---

## 5. 终门

| 门 | 命令 | 结果 |
|---|---|---|
| typecheck | `tsc -b --noEmit --force` | **exit 0** |
| 前端全量 vitest | `vitest run`(全库) | **`Test Files 152 passed` / `Tests 1290 passed`** |
| 前端 build | `vite build` | ✓ built in 3.59s(仅既有 chunk-size 警告) |

后端一行未改。

---

## 6. 实现要点 / 被迫的偏离

1. **「当前租户」取 `useAuth().identity.homeTenantId`,不是 `TenantScopeContext`**(与 brief
   的括号建议不同)。理由:①`ConversationDetail.tsx` 里**并不存在** brief 所说的"本文件既有
   tenant 上下文用法",无既定先例可照;②`TenantScopeContext.apiTenantScope` 是**作用域选择器**
   (`home`→`undefined` / `"*"` / 某个 UUID),不是"我是哪个租户"的身份;
   ③真正决定 replay 能否成功的是 `streamRunEvents` / `listThreadRuns` **不带 `tenant_id`**
   这个既有事实——后端只会按调用方 home tenant 解析。所以判据必须是
   `convo.tenant_id === identity.homeTenantId`。而且 spec §B1 本来就要求引入 `useAuth()`
   (`isSystemAdmin`),这样只多一条 context 依赖而不是两条。
   副作用:identity 未解析出 tenant(API key 启动窗口)时也走扁平——保守降级,
   `sameTenant` 进了 effect 依赖数组,identity 落定后会自动重建。

2. **多了一次 `getSessionMessages`**。页面自己那次带 `tenant_id`(既有行为,支撑跨租户扁平
   + `messages === null` 才隐藏整卡的语义),hook 内部那次不带。不能合并:hook 的
   `messages` 失败与空都表现为 `[]`,分不出"接口挂了"和"没消息",合并会破坏既有用例
   「hides the transcript panel when the messages endpoint fails」。同租户下多一次同端点请求,
   已知代价,记在此处。

3. **卡片外层条件从 `messages !== null` 放宽成 `messages !== null || historyTurns !== null`**。
   两者同端点、实际不会分叉,但万一分叉时不该把已经建好的轮次卡藏掉。内层多出的
   `messages === null` 分支是 TS 收窄用的(逻辑上不可达)。

4. **不传 `onFireResult`**(该 prop 可选)。playground 把 fire-now 结果渲染成 transcript 下方
   独立的 `TaskResultCard` 区,对话页搬那一整块属于范围外;按 `ToolTimeline` 的 docstring,
   省略时按钮**仍会真触发并显示自己的内联投递状态**,不构成死控件。
   顺带记一个**既有**现象(不是本批引入、也没改):`readOnly` 并不 gate `FireNowButton`,
   所以历史 `manage_task` 卡上的「立即触发」在只读页也可点——playground 的历史轮同样如此。

5. **`eventView` 用页面本地 `useState("timeline")`**,不读 playground 那个
   `expert_work.playground.eventView` localStorage 键(常量是 PlaygroundTab 的 file-local,
   搬它属于范围外)。默认值与 playground 的兜底一致。

6. **`onDownloadArtifact` 传真实现**,`downloadArtifact(name, convo.user_id)` ——
   `userId` 按 SDK docstring 就是"tenant-admin 治理目标",即这段对话的用户而非读它的运维;
   照 playground 的写法吞异常。同样是"不留死按钮"的落实。

7. **run 表新增列的表头留空**(`title: ""`),没为它单独加 i18n 键——列内容本身就是
   `conversations_detail.view_run`(「查看运行」),再加个表头是噪音。既有行级 `onClick`
   一字未动(点链接会同时触发行 onClick,两者目标 URL 相同,无副作用)。

8. **i18n**:新增 `conversations_detail.view_run`(en interface + en 对象 + zh-CN 三处)。
   先 grep 过撞键:`playground.view_run` 早已存在但在**别的 object** 里,
   `conversations_detail` 内无同名键;`i18n.test.tsx` 的 en/zh 键集一致性用例绿。

9. **`components/turn/types.ts` 加 `export type { HistoryTurn }`**(brief 要求),
   `ConversationDetail.tsx` 实际未直接具名该类型(全靠 hook 返回值推断),
   但按要求收敛了这条反向依赖边的出口。

---

## 7. 顾虑 / 未做

- **未做人工冒烟**。所有验证都是 vitest + tsc + build;真栈里"滚到第 N 轮才 replay"的观感、
  以及大量轮次卡在一张 Card 里的滚动体验没在浏览器里看过。
  轮次卡路径**刻意没套** `maxHeight: 480` 滚动容器(扁平路径的那个原样保留)——
  TurnCard 很高,嵌在 480px 内滚会很难用,改成跟页面一起滚。这条是 UI 判断,值得人工过一眼。
- **T1 遗留的 components → pages 反向依赖**未处理(本批新增一条:`types.ts` 转出
  `HistoryTurn`)。仍建议单开一批把 `playground/*` 里被共享的部分也挪进 `components/turn/`。
- **§6.2 的重复 `getSessionMessages`**——若嫌浪费,正解是让 hook 的 `load()` 接一个可选
  `tenantId` 并把 messages 的失败/空区分出来,那是改 hook 签名,超出本任务。
- 只读页对 `readOnly` 不 gate `FireNowButton` 这一点(§6.4)如果被认为是问题,
  应该在 `TurnCard` 里补第四道 `readOnly` 门,影响 playground 历史轮,需单独决策。
