# Task 3 报告 — TurnCard 接线 + 详情整卡复用 + 生长条 + 放大 Modal + 收口

> 本文件覆盖同名旧 `task-3-report.md`(PR5 成员页清除入口报告，属另一
> 分支/另一 PR 遗留，与本 gantt-execution-timeline 系列无关）；旧版完整
> 保留在 git 历史。

## STATUS: DONE（TDD 先红后绿，全量收口全绿）

Commit: `cc4ad279` feat(ui): 执行轨迹升级 Gantt——TurnCard 接线+92vw 放大+流式生长条

## 交付内容

### 1. `TurnCard.tsx` — eventView "timeline" 分支换用 `GanttTimeline`

- `const gantt = useMemo(() => buildGanttRows(turn.events, { settled: turn.status !== "running" }), [turn.events, turn.status]);`（逐字照 brief）。
- **详情整卡复用**：`renderGanttDetail(row) => <StepTimeline items={[row.detail.item]} liveByStep ttftMs finalized onFireResult />`。`detail.type` 的两种变体（`"item"` 给 agent/aux 行、`"parentStep"` 给 tool/worker 行）都携带同一个 `TimelineItem`，所以单一薄适配器覆盖全部行类型——tool/worker 行点开渲染的是它们所属步的整卡（含内嵌工具/worker 子时间线），符合 brief"tool/worker 行的 parentStep 同样渲所属步整卡"的定义。
- **生长条**：`turn.status === "running"` 时 1s `setInterval` 驱动 `nowTick`；`ganttModel` = `gantt`（未 running 时原样）或按 `lastKnownFrame(turn.events)` 校准后推进的 `totalMs`：
  `nowServerMs = lastFrame.serverMs + (Date.now() - lastFrame.receivedAtMs)`，
  `grownTotalMs = gantt.totalMs + max(0, nowServerMs - lastFrame.serverMs)`。
  `lastKnownFrame` 是新的模块级 helper（复用 `serverMsOf`），从 `turn.events` 倒序找最近一条带合法 server-ms id 的帧。settle 瞬间 `useEffect` cleanup 清 interval。这条路径完全没碰 `gantt_timeline.ts`/`GanttTimeline.tsx`——遵守 Task2 report 记录的边界（"GanttModel 是快照，无 now 输入，Task3 的活是给定 model 做每秒重算"），只在 TurnCard 侧推进 `totalMs` 后整体替换传给组件的 `model`。
- **放大 Modal**：头部（events 面板 header，仅 `eventView === "timeline"` 时渲染）新增 `Maximize2` 图标 `Button`（`data-testid="playground-gantt-expand"`）→ 独立 `<Modal open={ganttExpanded} width="min(92vw, 1680px)" destroyOnHidden>` 内渲 `variant="expanded"` 的同一 `ganttModel`/`renderGanttDetail`。Modal 声明在组件底部（`FullTextModal` 旁），不嵌在 `eventView` 三元分支内——切视图不会连带把它卸载。
- **degraded 提示**：`gantt.degraded && <Text type="secondary" data-testid="playground-gantt-degraded">{t("playground.gantt_degraded")}</Text>`。

### 2. 顺带移除的失效路径（StepTimeline → GanttTimeline 直接导致）

- `TimelineFilterBar` / `tlType` / `tlQuery` / `visibleTimeline` / `timelineToolCount` / `timelineFailCount` / `timelineCount`：brief Step3 的实现片段明确用 `turn.events` 直喂 `buildGanttRows`（不是 `visibleTimeline`），GanttTimeline 也没有过滤 hook——这条状态在改动后必然是死代码，按"移除因你的改动产生的孤儿"原则删除（`api/timeline_filter.ts`/`TimelineFilterBar.tsx` 两个文件本身未删，仅去掉了 TurnCard 里的唯一消费点，见下方 Concerns）。
- `RunStatusBanner` 的 `onJump`（timeline 分支这处）：原实现 `document.querySelector('[data-testid="step-timeline"] [data-error="true"]')`，StepTimeline 撤下后该选择器永远查不到东西。GanttTimeline 没有等价的按行 `data-error` 锚点，直接去掉 `onJump`（banner 文案/状态本身不受影响，只是"跳转到出错步骤"这个按钮不再出现）。"精确"（exact）视图自己的 `traceBanner.onJump` 未动。

### 3. i18n（en.ts + zh-CN.ts，各自类型块+值块）

- `playground.gantt_expand`：en `"Expand"` / zh `"放大查看"`。
- `playground.gantt_degraded`：en `"Timeline approximated by event order (server timestamps unavailable)"` / zh `"时间轴按事件顺序近似(缺服务端时戳)"`。
- 先 grep 确认两 locale 均未占用这两个键名。

### 4. Task 2 自审遗留断言补齐

`GanttTimeline.test.tsx` 第一条测试的 `expanded` 渲染块里追加一行：
`expect(screen.getByText("glm-5.2")).toBeInTheDocument();`（放大态直接显示 model 文本，不只是靠 tooltip），未改动任何既有断言。

### 5. 下游回归修复（StepTimeline 直渲测试全数改走 Gantt 路径）

- `TurnCard.test.tsx`：
  - 既有 "renders the replayed run's step timeline…" 测试更名 + 改为
    `getByTestId("gantt-timeline")` + 先点 `gantt-row-item-0` 开详情、
    再点 `step-head` 展开、才能摸到 `tool-call-card`（原来一步到位，
    现在整卡默认折叠，多一层点击）。
  - 新增 5 条 Task 3 测试（brief Step1 逐条）：Gantt 渲染取代 StepTimeline
    直渲 / 放大按钮开 92vw Modal 且 expanded variant 与 embedded 共存 /
    点击行渲染出 `step-card`（AgentStepCard 稳定 testid）/ degraded 提示
    文案 / running 时 `durationMs===null` 行带 `ew-gantt-bar--running`
    class（用一个真实"工具已派发、RESULT 未到"的 pending 场景造夹具，
    而不是虚构"进行中 LLM 步"——数据层压根不为后者建行，见下方
    Concerns #1）。
- `PlaygroundTab.test.tsx`：
  - "…jump scrolls the errored step-card into view" 测试改名去掉 jump 部分，
    断言 `run-status-jump` 不再渲染（前提已随 `onJump` 移除而不存在）。
  - "keeps the error banner even when a timeline filter hides…" 整条删除
    （断言的正是 `TimelineFilterBar` 的 `timeline-filter-query` 输入框，
    该 UI 在 timeline 分支里已不存在），原地留注释说明删除原因；
    banner 派生自未过滤 `timeline` 这条不变式仍由上一条测试覆盖。
  - `fireFromManageTaskCard` 共享 helper（喂 6 条 "fire-now result card"
    测试）补一次 `gantt-row-item-0` 点击再到 `step-head`。
- `ConversationDetail.test.tsx`：同 TurnCard.test.tsx 的模式，`step-timeline`
  → `gantt-timeline`，`step-head` 前补 `gantt-row-item-0` 点击。

## TDD 过程

- **Step1（红）**：先写 TurnCard.test.tsx 的 5 条新测试 + GanttTimeline.test.tsx
  的补充断言。跑 `npx vitest run src/components/turn`：5/26 新断言失败，
  失败原因均为 `gantt-timeline`/`playground-gantt-expand` 等 testid 不存在
  ——红得符合预期（未误红既有测试）。
- **Step3（实现）**：如上"交付内容" 1-3。
- **Step4（绿 + 全量收口）**：
  1. `npx vitest run src/components/turn` → 先出现 2 处失败（旧
     "renders the replayed run's step timeline…" 测试断言 `step-timeline`；
     degraded 测试断言了中文文案，而测试环境 i18n 默认语言是英文——
     全文件其它既有断言早已全部走英文字符串，跟随该惯例改成英文）。
     修完 → 3 files / 26 tests 全绿。
  2. `npx vitest run`（全量）→ 出现 9 处失败，集中在
     `PlaygroundTab.test.tsx`（7 处）+ `ConversationDetail.test.tsx`（1 处，
     另 1 处是同一 describe 下的级联）——根因统一：这些测试直接摸
     `step-head`/`step-timeline`，绕过了新插入的 Gantt 行点击层。逐条按
     "交付内容 5" 修复；其中 1 条（timeline filter）因前提结构性消失而
     整条删除，不是缝合。
  3. 复跑全量 → **158 files / 1368 tests 全绿**（1369 − 1 条删除 = 1368，
     数字对得上，没有测试被静默跳过）。
  4. `pnpm -C apps/admin-ui exec tsc -b --noEmit` → 无输出（通过）。

## 验证摘要

```
npx vitest run src/components/turn   → 3 files / 26 tests passed
npx vitest run                        → 158 files / 1368 tests passed
pnpm -C apps/admin-ui exec tsc -b --noEmit → 0 errors
```

## Concerns

1. **"进行中 LLM 步" 的生长条实际不存在**——设计文档写"进行中的 LLM 步
   （收到 step 帧前）渲染生长条"，但 Task1 已交付的 `buildGanttRows` 只为
   **已派发未完成的 tool/worker 调用**（`durationMs===null`）造行；一个
   还没发出 `updates` 帧的 LLM 步根本不在 `parseTimeline` 输出里，因而
   在 Gantt 里完全不可见（这一段的"进行中"反馈仍然只靠独立的
   `liveByStep`/`StreamingStepCard` 打字机机制，与 Gantt 无关——这正是
   design doc"不做"清单里"token 流帧不入 Gantt"的直接后果）。Task2 report
   的实现备注 #3 已经点破这个边界，我在 Task3 用"真实 pending 工具调用"
   而不是"虚构进行中 LLM 步"来写生长条测试，是照实现现状写的，不是我
   引入的缺口——只是想再显式记一遍，避免后续任务凭设计文档字面意思
   误以为 Gantt 能看到"当前正在生成的这一步"。
2. **TimelineFilterBar / api/timeline_filter.ts 两文件成了全局死代码**——
   移除 TurnCard 里的唯一消费点后，`grep -rl` 确认仓库里再没有别处引用
   这两个模块（连同 `TimelineFilterBar.test.tsx` 自身的独立单测还在，
   那个测试测的是组件本体，不受影响，继续绿）。按 brief 的 Files 清单
   （未列出要删除这两个文件）以及"不删无关死代码，只提醒"的原则，我
   **没有**删除它们，仅在这里标记为后续可清理项。
3. **"跳转到出错步骤" 功能在 timeline 视图里消失**——`RunStatusBanner` 的
   `onJump` 在旧 StepTimeline 分支里能把视图滚动到第一个 `data-error`
   步骤；GanttTimeline 没有等价的逐行错误锚点（行本身不带
   `data-error`），brief/design doc 均未要求补一个，我按现状移除了这颗
   按钮（PlaygroundTab.test.tsx 那条测试也相应改为断言按钮不出现）。
   "精确"视图自己的 jump（读 span 级 `data-error`）不受影响。若产品后续
   要恢复这个功能，最小实现是给 GanttTimeline 的错误行加
   `data-error="true"` 并把 key 通过某种方式暴露给调用方——不在本任务
   范围内。
4. 未做 CI 里的 `pnpm build`（vite build）——brief 验收命令只列了
   vitest + tsc，未列 build；本地已确认 tsc 全绿，构建面本身（无新依赖/
   无新 vite 配置项）风险很低，交给 CI 兜底。
