# Task 1 报告 — 数据层:解析器 serverMs + `buildGanttRows`

**分支**: gantt-execution-timeline
**Commit**: `4981e70539634121e6faeb842058a5714fdcec60`
**状态**: 完成,TDD 全绿(红→绿已验证),全量回归通过。

## 做了什么

### 1. 三解析器加 `serverMs`(可选,`number | null` 默认,向后兼容)

- `apps/admin-ui/src/api/timeline.ts`:`AgentStep`/`AuxNodeItem`/`MarkerItem`
  三接口加 `serverMs?: number | null`。`parseTimeline` 内部用一个
  per-iteration 的 `evtServerMs`(在 `for (const evt of events)` 顶部
  `serverMsOf(evt.id)` 赋值),`push()` 统一带上,零改动各 push 调用点。
- `apps/admin-ui/src/api/tool_timeline.ts`:`ToolCallEntry` 加
  `serverMs?: number | null`,两处 `ensure()` 初始化为 `null`,RESULT 分支
  (`m.type === "tool"`)里 `entry.serverMs = serverMsOf(evt.id)`。
- `apps/admin-ui/src/api/worker_timeline.ts`:`WorkerStepSummary` 加
  `serverMs?: number | null`,`update` 帧 push 时
  `serverMs: serverMsOf(evt.id)`。

**关键修正(TDD 中发现)**:brief 给的接口签名写的是
`serverMs: number | null`(必填),但仓库里已有 5 个既存测试文件
(`timeline_filter.test.ts` / `ToolTimeline.test.tsx` / `StepTimeline.test.tsx`
/ `StepTimeline.worker.test.tsx` / `timeline_banner.test.ts`)手造这三个接口
的对象字面量、完全不带 `serverMs` 字段。必填会让这 5 个文件 `tsc -b` 报错,
直接违反 brief 自己写的硬约束「现有消费者零改动」。改成**可选**
(`serverMs?: number | null`)后 `tsc -b --force` 全绿,这 5 个文件一行未动。

### 2. 新建 `apps/admin-ui/src/api/gantt_timeline.ts`

- `serverMsOf(id)`:`/^(\d{10,})-\d+$/` 提取 ms 段,非法/null → `null`。
- `buildGanttRows(events, opts?)`:以 `parseTimeline(events)` 的输出为骨架
  (不重复调用 `parseToolCalls`/`parseWorkerFrames`——直接复用每个
  `AgentStep.tools`/`ToolCallEntry.workers` 上已挂好的关联,这就是 brief
  说的「现有关联字段沿用」)。
  - agent/aux → depth 0 行,`end = serverMs`,`start = end − durationMs`;
    `durationMs` 为 null 时(不管是否 settled)以 `prevEnd` 作为 start、
    `end = serverMs`(design doc「从上一事件结束时刻起」)。
  - 该步 `.tools` → depth 1 行,`detail = {type:"parentStep", item: 该步}`。
    **一个我在实现中加的修正**:tool 若 `status === "pending"` /
    `"pending_approval"`(RESULT 还没到,自然没有 `serverMs`),不算
    「退化」——否则任何还在跑的工具调用都会把整条 run 错误标成
    `degraded=true`,这个 false positive 会侵蚀 `degraded` 字段本该表达
    的「id 解析异常」语义。
  - tool 的 `.workers`(递归拍平 `children`,depth 封顶 2)→ 每个
    `WorkerStepSummary` 一行,depth 2,`detail` 同样指向该步整卡。
  - marker kind(compaction/retry/error/approval/guard/end)→ 不占行,进
    `markers[]`。
  - `t0 = 全行(仅 GanttRow,不含 marker)最小 start`;`totalMs = max(end) − t0`
    (进行中行的 `end` 就是它自己的 `serverMs`,即「最后已知 end」)。
  - `serverMs` 缺失/非法 → 该行 `start = prevEnd`(链式拼接,duration 若有
    照样保留、只是起点变成拼接值)并置 `GanttModel.degraded = true`。
  - `opts.settled === true` → 从后往前找最后一个 `kind === "agent"` 的行
    改成 `kind: "final"`。

### 3. TDD 流程(严格红→绿,已实测验证)

1. 先写 5 个测试(`gantt_timeline.test.ts`),brief 给了前两个的完整断言体,
   逐字采用;后三个(worker 挂树 / settled-final+running-null-duration /
   marker 不占行)只有注释占位,自己补的断言体。
2. **验证红**:临时把刚写好的 `gantt_timeline.ts` 挪开,跑
   `npx vitest run src/api/__tests__/gantt_timeline.test.ts` →
   `Failed to resolve import "../gantt_timeline"`(模块不存在),确认红。
3. 恢复实现,再跑 → 前两遍红(2 个断言失败):测试 fixture 里我用的 ms 是
   小数字(1000/5000…),但 `serverMsOf` 要求 `\d{10,}`(真实 epoch ms,
   10 位以上)——这是 fixture bug 不是实现 bug,加了 `BASE_MS =
   1_700_000_000_000` 偏移后 5/5 绿。
4. 回归:`gantt_timeline.test.ts` + `timeline.test.ts` + `tool_timeline.test.ts`
   + `worker_timeline.test.ts` = 4 files / 60 tests 全绿。
5. `npx tsc -b --force`(admin-ui 全量,含所有既存测试文件)零错误。
6. `npx vitest run`(admin-ui 全量)= 157 files / 1357 tests 全绿。

## 关于「循环 import」的设计取舍

`gantt_timeline.ts` 需要真调用 `timeline.ts` 的 `parseTimeline`(值 import);
`timeline.ts`/`tool_timeline.ts`/`worker_timeline.ts` 需要真调用
`gantt_timeline.ts` 的 `serverMsOf`(值 import)。这构成一个双向依赖。

实测确认无害,原因:
- `tsconfig.app.json` 开了 `verbatimModuleSyntax: true`,`gantt_timeline.ts`
  从三个解析器只 `import type`(类型,零运行时 import 语句,erasure 后无
  边)——真正的运行时环只剩 `timeline.ts ↔ gantt_timeline.ts` 这一条。
- 双方交叉用到的符号(`parseTimeline`、`serverMsOf`)都是 `export function`
  声明(hoisted),且只在其他函数体内部被调用(从不在模块顶层执行时机调用)。
  ES module 的函数声明在模块自身求值阶段一开始就绑定,循环双方互相拿到的
  都是「已初始化」的函数引用,不是 TDZ 例外。
- `npx tsc -b --force` 与 `npx vitest run`(157 files 全绿)均已实测确认,
  非纸面推导。

## 文件

- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/gantt_timeline.ts`(新建)
- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/__tests__/gantt_timeline.test.ts`(新建)
- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/timeline.ts`(改)
- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/tool_timeline.ts`(改)
- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/worker_timeline.ts`(改)

## Concerns / 留给 Task 2/3 注意

1. **`serverMs` 是可选字段,不是必填**——brief 接口签名写的是必填,但
   实测发现必填会破坏「现有消费者零改动」硬约束,已改成可选并验证。
   Task 2/3 读 `item.serverMs`/`tool.serverMs`/`step.serverMs` 时会拿到
   `number | null | undefined`(不只是 `number | null`),`buildGanttRows`
   内部已经把 `undefined` 归一到跟 `null` 一样处理(退化路径),Task 2/3
   自己如果要直接读这三个字段(而非只读 `GanttModel` 的产出),记得同样按
   `?? null` 处理。
2. **行 `label`/`model`/`key` 格式未被 brief 的测试锁定**,是我按 design
   doc「行名(工具名+摘要 / 步骤 N / 节点名)」的精神自行拟定的(agent=
   `步骤 N`、aux=复用既有 `summary`、tool=`toolName`、worker=
   `${worker.label} · ${step.node}`)。如果 Task 2/3 对具体文案有既定预期,
   这里可能要对一下。
3. **totalMs/t0 只统计 GanttRow,不含 marker**——严格按 brief 字面「全行」
   实现;如果某个 marker(比如 `end`)在时间上比最后一行还晚,轴长不会把
   它算进去。brief 没要求覆盖这个,未测试。

## Follow-up 修复(commit 4981e705 审查 findings,2026-07-30)

**范围**:仅 `gantt_timeline.ts` + `gantt_timeline.test.ts`,三解析器与既有
5 条测试断言均未改动。

### Medium — final 判定漏 `tool_calls` 条件

`buildGanttRows` 结尾的 settle-relabel 块之前是「settled 时无条件把最后一个
`kind === "agent"` 的行标成 `"final"`」。但 `#1072` 定的权威频道语义
(`api/turn_summary.ts:192-197`)是「末条**且不带 `tool_calls`**」才是
final——settled 但末步带 `tool_calls`(guard/error/max_steps 在工具调用后
中断)的 run 会被这段逻辑误标成绿色「终结步」,误导成假成功。

修法:找到最后一个 `kind === "agent"` 的行后,读它的
`detail`(`{type:"item", item}`,`item.kind === "agent"` 时 `item` 就是
`AgentStep`),只有 `item.tools.length === 0` 才升级成 `"final"`;否则维持
`"agent"`,不再往前找替代候选(与 `turn_summary.ts` 一致——只有最后一条有
资格,资格不满足就没有 final,不做二次选拔)。

测试:新增 fixture `settledRunEndsWithTool`(两步,第二步带 1 个
`tool_calls` 且工具已 `success` 返回)+ 用例「settled 但末条 agent 步带
tool_calls → kind 仍 agent(非 final)」,断言 `agentLike[1].kind === "agent"`。

### Low — pending 工具不计 degraded 的分支补测试锁定

原实现里 `pending`/`pending_approval` 状态的工具调用已经被正确排除在
`degraded` 判定之外(`place()` 只在 `serverMs` 为 `null`/`undefined` 时才标
`degraded`,pending 分支走的是自造的 `{startMs: prevEnd, endMs: prevEnd,
durationMs: null}` 短路,不经过 `place()`),但此前没有测试锁定这个行为,
容易被将来的重构无意破坏。

新增 fixture `pendingTool`(一个 agent 步派发 `tool_a` 调用,不给对应的
`toolResult` 帧,模拟 RESULT 未到达)+ 用例「pending 工具(RESULT 未到)
不计入 degraded,其行 durationMs=null」,断言 `degraded === false` 且该工具
行 `durationMs` 为 `null`。

### 验证

- `npx vitest run src/api/__tests__/gantt_timeline.test.ts` → 7 passed(5 条
  原有断言逐字未改,2 条新增全绿)
- `pnpm exec tsc -b --noEmit` → 零错误

### 文件

- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/gantt_timeline.ts`(改)
- `/Users/mac/src/github/jone_qian/expert-work/apps/admin-ui/src/api/__tests__/gantt_timeline.test.ts`(改)
