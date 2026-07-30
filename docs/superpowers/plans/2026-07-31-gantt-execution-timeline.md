# 执行轨迹 Gantt 时间线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 调试台/对话详情页「执行轨迹」视图从竖列表升级为 Gantt 时间线——并发工具、并行 worker 重叠可见,慢步条形直出,嵌入/92vw 放大两档。

**Architecture:** 纯前端。服务端毫秒时戳取自 SSE `id`(`"{ms}-{seq}"`,live bridge 与 replay 同构,后端零改动);三个现有解析器各加可选 `serverMs`;新 `gantt_timeline.ts` 拼装 GanttModel;新 `GanttTimeline` 组件(div 绝对定位条形,无图表库);TurnCard 的 timeline 视图分支替换。Spec: `docs/superpowers/specs/2026-07-31-gantt-execution-timeline-design.md`。

**Tech Stack:** React/TS + antd(Tooltip/Modal)+ `--ew-*` 语义色令牌。

## Global Constraints

- 后端零改动;不引图表库;token 流帧不入 Gantt。
- 绝对时间只取 SSE `id` 的 ms 段——**禁止**用 `receivedAt` 排布条形(历史 replay 全挤一瞬)。`id` 缺失/非法 → 该行退化为「上一行结束时刻起、顺序拼接」,不崩不空。
- 颜色一律 `--ew-*` 语义色令牌(样式随双主题),不写死 hex。
- 嵌入态:标签列 176px + antd Tooltip 全名 + 时长悬停显;放大态:92vw Modal + 292px 标签 + 时长常显。同一组件 `variant: "embedded" | "expanded"`。
- 尊重 `prefers-reduced-motion`(动画禁用)。
- 「工具调用」「原始事件」「精确」三视图与 StepTimeline 组件本体不动(StepTimeline 仍被详情展开复用)。
- 测试:`npx vitest run <files>`(pnpm test -- --run 有双 run 坑)+ 收口 `pnpm -C apps/admin-ui exec tsc -b --noEmit`。
- conventional commits,无 attribution 尾行。

---

### Task 1: 数据层 — 解析器 serverMs + `buildGanttRows`

**Files:**
- Modify: `apps/admin-ui/src/api/timeline.ts`(TimelineItem 三接口加 `serverMs: number | null`;`parseTimeline` 的 `push` 带上当前 evt 的 id ms)
- Modify: `apps/admin-ui/src/api/tool_timeline.ts`(`ToolActivity` 加 `serverMs: number | null` = RESULT 帧 id ms)
- Modify: `apps/admin-ui/src/api/worker_timeline.ts`(`WorkerStepSummary` 加 `serverMs: number | null` = 该 worker 帧 id ms)
- Create: `apps/admin-ui/src/api/gantt_timeline.ts`
- Test: `apps/admin-ui/src/api/__tests__/gantt_timeline.test.ts`

**Interfaces:**
- Consumes: `parseTimeline`/`parseToolCalls`/`parseWorkerFrames` 现有输出 + 各 payload `durationMs`。
- Produces(Task 2/3 逐字消费):

```ts
export function serverMsOf(id: string | null): number | null; // "1722400000123-7" → 1722400000123;非法 → null
export interface GanttRow {
  key: string;
  label: string;
  model?: string;
  kind: "agent" | "aux" | "tool" | "worker" | "final";
  depth: 0 | 1 | 2;
  startMs: number;            // 相对 t0,毫秒
  durationMs: number | null;  // null = 进行中(生长条)
  detail:
    | { type: "item"; item: TimelineItem }        // agent/aux 行 → 整卡复用
    | { type: "parentStep"; item: TimelineItem }; // tool/worker 行 → 所属步整卡
}
export interface GanttMarker { atMs: number; kind: MarkerItem["kind"]; text: string; }
export interface GanttModel {
  rows: GanttRow[];
  markers: GanttMarker[];
  totalMs: number;
  degraded: boolean;          // 任一行走了退化拼接(id 缺失)
}
export function buildGanttRows(events: readonly SseEvent[], opts?: { settled?: boolean }): GanttModel;
```

- [ ] **Step 1: 写失败测试**(fixture 用手造 SseEvent,id 按 `"{ms}-{seq}"`;helper 仿 timeline.test.ts 既有构造):

```ts
it("start = 帧 id 时刻 − durationMs;并发工具条形重叠", () => {
  // 两个工具 RESULT 帧 id 同毫秒、各带 _duration_ms 5100/4400 → 两行 start 差 700ms、末端对齐
  const m = buildGanttRows(fx.concurrentTools);
  const [a, b] = m.rows.filter((r) => r.kind === "tool");
  expect(a.startMs + (a.durationMs ?? 0)).toBe(b.startMs + (b.durationMs ?? 0));
  expect(Math.abs(a.startMs - b.startMs)).toBe(700);
});
it("id 缺失 → 退化顺序拼接且 degraded=true", () => {
  const m = buildGanttRows(fx.noIds);
  expect(m.degraded).toBe(true);
  const [r0, r1] = m.rows;
  expect(r1.startMs).toBe(r0.startMs + (r0.durationMs ?? 0));
});
it("worker 行挂在 sub_agent 工具行下,depth=2,detail 指向所属步", () => { /* fx.delegation */ });
it("settled turn 的末条 agent 行 kind=final;running 中最新无 duration 行 durationMs=null", () => { /* fx.settledRun / fx.runningRun */ });
it("marker(retry/error/end)不占行,进 markers[]", () => { /* fx.withMarkers */ });
```

- [ ] **Step 2: 跑红** `npx vitest run src/api/__tests__/gantt_timeline.test.ts` → FAIL(模块不存在)
- [ ] **Step 3: 实现**
  - `serverMsOf`:`/^(\d{10,})-\d+$/` 提取,否则 null。
  - 三解析器:在各自迭代 evt 处补 `serverMs: serverMsOf(evt.id)`(工具取 RESULT 帧、worker 取该帧、timeline push 取当前 evt);字段可选带 null 默认,现有消费者零改动。
  - `buildGanttRows`:以 `parseTimeline` 输出为骨架:agent/aux → 顶层行(`end = serverMs`,`start = end − durationMs`;durationMs null 且未 settled → 进行中行);该步的工具(`parseToolCalls` 按 stepCount 关联,现有关联字段沿用 tool_timeline 的 step 归属)→ depth 1 行;工具的 worker(`parseWorkerFrames` 的 `parentToolCallId`)→ depth 2 行。marker kind → markers[]。t0 = 全行最小 start;各 start 减 t0;totalMs = max(end) − t0(进行中行按"最后已知 end"参与)。`kind: "final"` = `opts.settled === true` 时最后一个 agent 行(与 #1072 final 语义对齐:settle 后末步即终结步)。任一行 serverMs null → 该行 `start = prevEnd`(退化拼接)并置 degraded。
- [ ] **Step 4: 跑绿 + 回归** `npx vitest run src/api/__tests__/gantt_timeline.test.ts src/api/__tests__/timeline.test.ts src/api/__tests__/tool_timeline.test.ts src/api/__tests__/worker_timeline.test.ts` → 全 PASS
- [ ] **Step 5: Commit** `feat(ui): gantt 数据层——解析器 serverMs(SSE id 毫秒段)+ buildGanttRows`

---

### Task 2: `GanttTimeline` 组件(行/条/轴/两档/tooltip)

**Files:**
- Create: `apps/admin-ui/src/components/turn/GanttTimeline.tsx`
- Test: `apps/admin-ui/src/components/turn/__tests__/GanttTimeline.test.tsx`

**Interfaces:**
- Consumes: Task 1 全部导出。
- Produces(Task 3 消费):

```tsx
export interface GanttTimelineProps {
  model: GanttModel;
  variant: "embedded" | "expanded";
  running?: boolean;                       // 生长条 tick 开关(Task 3 接)
  renderDetail: (row: GanttRow) => ReactNode; // 行展开内容(Task 3 注入整卡)
}
export function GanttTimeline(props: GanttTimelineProps): JSX.Element;
```

- [ ] **Step 1: 写失败测试**(RTL;断言按原型定稿行为):

```tsx
it("嵌入态:标签列 Tooltip 包裹、时长标签带 hover-only class;放大态:时长常显", () => { ... });
it("条形 left/width 按 startMs/durationMs 百分比;并发两行条形区间重叠", () => { ... });
it("点击行渲染 renderDetail 内容,再点收起;一次仅一行展开", () => { ... });
it("kind=final 行携带 final 语义色 class;marker 渲染为轴上刻度并带 title", () => { ... });
it("durationMs=null 行渲染生长条 class(running)或中断态(!running)", () => { ... });
```

- [ ] **Step 2: 跑红**
- [ ] **Step 3: 实现**——布局照原型翻译成 React:左标签列(embedded 176px/expanded 292px,`Tooltip title={label + model}`)、右 track(gridline 刻度自适应:总长 <10s→1s、<60s→10s、否则 30s)、bar 绝对定位 `left/width %`(width 下限 0.35%)、时长 `<span>`(embedded 加 `.gantt-dur-hover` CSS,行 hover 显)、色 class → `--ew-*` 令牌映射(agent=info/aux=purple 令牌/tool=success/worker=warning/final=success-strong,对照 #979 令牌表取既有名,缺的复用最近义者,不新增令牌)、marker 竖线 + title、行点击 toggle 展开区渲 `renderDetail(row)`、入场 scaleX 动画 + `@media (prefers-reduced-motion: reduce)` 关闭、整体 `overflow-x: auto` 下限 560px。深度缩进 `paddingLeft: depth * 18 + 10`。
- [ ] **Step 4: 跑绿** `npx vitest run src/components/turn/__tests__/GanttTimeline.test.tsx`
- [ ] **Step 5: Commit** `feat(ui): GanttTimeline 组件——两档形态+标签 tooltip+并发条形`

---

### Task 3: TurnCard 接线 + 详情整卡复用 + 生长条 + 放大 Modal + 收口

**Files:**
- Modify: `apps/admin-ui/src/components/turn/TurnCard.tsx`(eventView "timeline" 分支:`StepTimeline` → `GanttTimeline`;头部放大按钮 + 92vw Modal)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts` / `zh-CN.ts`(新键:`playground.gantt_expand`「放大查看」/ `playground.gantt_degraded`「时间轴按事件顺序近似(缺服务端时戳)」——先 grep 两 locale 确认不撞)
- Test: `apps/admin-ui/src/components/turn/__tests__/TurnCard.test.tsx`(timeline 视图断言更新)

**Interfaces:**
- Consumes: Task 1 `buildGanttRows(turn.events, { settled: turn.status !== "running" })`、Task 2 `GanttTimeline`;现有 `StepTimeline`(整卡渲染源)。
- Produces: 终态页面行为;调试台+对话详情页共享生效。

- [ ] **Step 1: 写失败测试**:timeline 视图渲染 `GanttTimeline`(testid);放大按钮开 Modal(width 92vw)内含 expanded variant;`renderDetail` 对 agent 行渲出现有 AgentStepCard 内容(以其稳定 testid 断言);degraded 时显示提示 Text;running 时生长条存在。
- [ ] **Step 2: 跑红**
- [ ] **Step 3: 实现**
  - `const gantt = useMemo(() => buildGanttRows(turn.events, { settled: turn.status !== "running" }), [turn.events, turn.status]);`
  - `renderDetail`:`row.detail` 的 item 交给一个薄渲染函数——agent 项渲 `<StepTimeline items={[item]} …/>`(单元素数组即现有整卡,含内嵌工具/worker 子时间线;aux 项同理)。tool/worker 行的 `parentStep` 同样渲所属步整卡。
  - 生长条:`running` 时 1s interval `setNowTick`,进行中行宽度 = `(nowServerMs − start)`,`nowServerMs = lastFrameServerMs + (Date.now() − lastFrameReceivedAtMs)`(客户端漂移校准);settle 即清 interval。
  - 放大:头部 icon Button(`Maximize2`,lucide 已用)→ `Modal width="min(92vw, 1680px)"`,`destroyOnHidden`,内渲 expanded variant(同一 model/renderDetail)。
  - degraded → `Text type="secondary"` 提示行。
- [ ] **Step 4: 跑绿 + 全量收口** `npx vitest run src/components/turn && npx vitest run && pnpm -C apps/admin-ui exec tsc -b --noEmit` → 全 PASS/零错
- [ ] **Step 5: Commit** `feat(ui): 执行轨迹升级 Gantt——TurnCard 接线+92vw 放大+流式生长条`

---

## Self-Review 记录

- Spec 覆盖:数据层(T1)/组件两档+tooltip+marker(T2)/接线+详情整卡+生长条+放大+degraded(T3);「不做」清单无对应任务 ✓;后端零改动 ✓。
- 类型一致:`GanttModel/GanttRow/GanttMarker/serverMsOf`(T1→T2/T3)、`GanttTimelineProps/renderDetail`(T2→T3)逐字对齐。
- 无占位符;代码步骤给了关键实现;组件 JSX 细节以原型(scratchpad/gantt-mockup.html)为视觉基准。
