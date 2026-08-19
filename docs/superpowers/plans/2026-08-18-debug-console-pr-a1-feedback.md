# 调试台 PR-A.1 —— 测试环境反馈修订 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec §八(2026-08-18 修订)把 PR-A 上线后的六条反馈落地:壳与左栏排版修正、会话统计芯片行上移、过程条折叠、一行式脚注、输入区收紧、右栏泳道 / 行表按 deepseek-harness 交互重做。

**Architecture:** 全部在 `apps/admin-ui`,只动 `components/console/**`、`api/timeline.ts`(可选字段)、`api/trajectory_rows.ts`(可选字段)、`pages/agent_detail/PlaygroundTab.tsx`(接线)、两个 e2e spec、i18n。新增组件 `ProcessStrip.tsx`;`StatsBar` 改芯片布局;`LaneStrip` + `lane_strip_model` 重写成双投影;`TrajectoryRows` 表格化;`TurnFooter` / `Composer` / `TurnMeta` 用法收敛。后端零改动。

**Tech Stack:** React 18 + antd 5.29 + lucide-react + react-i18next(单 `console.*` 命名空间)+ vitest/RTL + Playwright(e2e)。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` **§八**(修订节,与 §二.1 冲突处以 §八为准)。设计稿(已确认)= §八 八条 + 「过程条三态」+ 「泳道交互样例 A–F」。

## Global Constraints

- **既有 `it` 一条都不静默删除**:本计划碰到的测试文件与当前 `it` 数(在 `78ddaefd` 上 `grep -c "^\s*it("` 数出来的):`ConsoleShell.test.tsx` 2 / `SessionSidebar.test.tsx` 16 / `StatsBar.test.tsx` 7 / `TurnBlock.test.tsx` 9 / `TurnFooter.test.tsx` 2 / `Composer.test.tsx` 5 / `LaneStrip.test.tsx` 3 / `lane_strip_model.test.ts` 3 / `TrajectoryRows.test.tsx` 6 / `TrajectoryPanel.test.tsx` 9 / `RowDetail.test.tsx` 5 / `CompactRow.test.tsx` 7 / `PlaygroundTab.test.tsx` 46 / `api/__tests__/timeline.test.ts` 12 / `api/__tests__/trajectory_rows.test.ts` 13。行为**有意改变**的条目改断言并在报告里逐条列「旧断言 → 新断言 → 依据(§八第几条)」;行为没变的条目原样保留。控制器合并前按上表核数。
- **e2e 也是行为清单**(PR-A 的教训):改 testid / 删按钮 / 换文案前 `grep -rn '<testid>' apps/admin-ui/e2e/`,命中的 spec 一起改;Task 7 本地跑受影响 spec(`pnpm exec playwright test e2e/session-history.spec.ts e2e/playground-upload.spec.ts`,浏览器已装)。
- **testid**:既有 `console-*` / `playground-*` 在同一控件仍存在时保留;新增一律 `console-*`。本计划明确改名的只有:`console-turn-inspect`(保留 testid,文案换成「查看轨迹」)。
- **i18n**:新键只加 `console.*`,三处同步(`en.ts` 接口块 + `en.ts` 值块 + `zh-CN.ts` 值块),先 grep 是否撞既有;本计划**删除**的键只有 StatsBar 旧文案(`console.stats_turns / stats_llm_tools / stats_ttft / stats_tps / stats_cache / stats_tokens / stats_cost`,Task 2 内确认无其它消费者后三处删)与 `console.footer_inspect`(Task 4)。中文正文全角标点;代码注释跟随所在文件既有习惯。
- **纯函数进 `api/` 或 `components/console/*.ts`**;组件进 `components/console/`;单文件 ≤ 400 行,超了拆。
- **不动**:`TurnCard.tsx`、`TraceView.tsx`、`GanttTimeline.tsx`、`StepTimeline.tsx`、`EventCard.tsx`、`ConversationDetail.tsx`、`RunDetail.tsx`、`api/gantt_timeline.ts`、`components/turn/*`;`api/timeline.ts` 只允许加**可选**字段(`reasoningTokens?` / `cacheReadTokens?`);`api/trajectory_rows.ts` 只允许给 `ThinkRow` 加可选字段。
- **测试渲染**:用到 `App.useApp()` 的组件测试包 antd `<App>`;`Tooltip` / `Popconfirm` 在 jsdom 下用 `mouseover` + `findByRole("tooltip")`;antd `Splitter` 子节点在 jsdom 仍在 DOM,只断 DOM / 文案 / 回调不断尺寸。
- **前端命令**(在 `apps/admin-ui`):`pnpm exec vitest run <file>`、`pnpm typecheck`(必须用它,裸 `tsc --noEmit` 恒绿)、`pnpm build`、`pnpm build-storybook`、`pnpm exec playwright test <spec>`。**无 eslint**。
- **特性分支** `feat/debug-console-pr-a1`,worktree `.worktrees/debug-console-pr-a1`,基于 `origin/main`(`78ddaefd`,PR-A 已合)。SDD 台账 `.superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/progress.md`。
- **并行波次**(每个 task 一个 worktree `.worktrees/pr-a1-t<N>`、分支 `feat/pr-a1-t<N>`,从特性分支当前 HEAD 切;波末控制器按 task 号 `git merge --no-ff`,合完跑 `pnpm typecheck && pnpm exec vitest run && pnpm build`):

| 波 | 并行 task | 依赖 |
|---|---|---|
| 1 | 1 · 2 · 3 · 4 · 5 | 互不相依(文件不相交:T1 壳/侧栏,T2 StatsBar,T3 ProcessStrip+TurnBlock,T4 TurnFooter+Composer,T5 泳道模型+LaneStrip) |
| 2 | 6 | 需 5(LaneStrip v2 props) |
| 3 | 7 | 全部(PlaygroundTab 接线 + e2e + 全门) |
| — | 8 | 合并后 |

  并行规矩同 PR-A 计划:task 内只跑定点测试 + `pnpm typecheck`;i18n 漏键在 `console` 块**末尾**追加,同波冲突控制器合并时两边都留;评审包 `<切出时特性分支 HEAD>..<task 分支 HEAD>`。

---

## 文件结构

| 文件 | 责任 | Task |
|---|---|---|
| `components/console/ConsoleShell.tsx` + `console.css` | 高度按实际 top 铺满(CSS 变量) | 1 |
| `components/console/SessionSidebarItem.tsx` + `SessionSidebar.css` + `SessionSidebar.tsx` | 标题占满、hover 浮层操作、搜索行两行 | 1 |
| `components/console/StatsBar.tsx` | 芯片行(flex-wrap),不截断 | 2 |
| `components/console/process_summary.ts`(新) | 过程摘要纯函数 | 3 |
| `components/console/ProcessStrip.tsx`(新)+ `process_strip.css`(新) | 过程条(折叠 / 展开 / 运行中) | 3 |
| `components/console/TurnBlock.tsx` | 紧凑行 → ProcessStrip | 3 |
| `components/console/TurnFooter.tsx` + `turn_footer.css`(新) | 一行式脚注 + 「查看轨迹」 | 4 |
| `components/console/Composer.tsx` | 停止归位、单行工具条、2 行起 | 4 |
| `components/console/lane_strip_model.ts` | 双投影 `laneProjection` | 5 |
| `components/console/LaneStrip.tsx` + `lane_strip.css` | 细泳道 / 提示 / hover 联动 / 拖选 / 刻度 / 呼吸 | 5 |
| `api/timeline.ts` / `api/trajectory_rows.ts` | 可选 `reasoningTokens` / `cacheReadTokens` | 6 |
| `components/console/TrajectoryRows.tsx` + `trajectory_rows.css` | 表格列 + hover 联动 + 筛选芯片 | 6 |
| `components/console/RowDetail.tsx` | 头部改「#序号 · 类型 · 摘要 · 耗时」+ 内边距 | 6 |
| `components/console/TrajectoryPanel.tsx` | 头部(状态 · 工具 · 耗时 · Run 详情 · Langfuse · 顺序/时长)+ 联动状态 | 6 |
| `pages/agent_detail/PlaygroundTab.tsx` | 芯片行上移、底部 StatsBar 去掉 | 7 |
| `pages/__tests__/PlaygroundTab.test.tsx`、`e2e/*.spec.ts` | 断言更新 | 7 |
| `i18n/locales/en.ts` / `zh-CN.ts` | `console.*` 新键三处 | 各 task |

---

### Task 1: 壳高度自适应 + 左栏条目排版

**Files:**
- Modify: `apps/admin-ui/src/components/console/ConsoleShell.tsx`、`console.css`
- Modify: `apps/admin-ui/src/components/console/SessionSidebarItem.tsx`、`SessionSidebar.css`、`SessionSidebar.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/ConsoleShell.test.tsx`(2 → ≥3)、`SessionSidebar.test.tsx`(16 保留)

**Interfaces:**
- Consumes: 无。
- Produces: `ConsoleShell` 对外 props 不变;导出常量 `CONSOLE_HEIGHT_OFFSET_PX` **删除**(唯一消费者是它自己的 CSS 注释;grep 确认后删,`PlaygroundTab` 未引用)。

**行为(§八.1)**:
- 壳根元素挂 `ref`,`useLayoutEffect` 在 mount + `window resize` 时量 `getBoundingClientRect().top`,写到根元素 style `--ew-console-top: <top>px`;CSS 改 `height: calc(100dvh - var(--ew-console-top, 0px) - 24px); min-height: 480px`。jsdom 里 `top` 为 0 → 变量 `0px`,测试只断变量被写入。
- `SessionSidebarItem`:三个操作按钮的容器 `.ew-session-item__acts` 改成 `position: absolute; right: 6px; top: 6px; display: none`,`.ew-session-item:hover .ew-session-item__acts, .ew-session-item:focus-within .ew-session-item__acts { display: inline-flex }`;不再用 antd `List.Item` 的 `actions` prop(它是 flex 兄弟节点会占宽)——改成 `List.Item` 内部自己排:`<div class="ew-session-item__body">`(标题 + 时间)+ `<div class="ew-session-item__acts">`(三个按钮原样,含 `data-testid` / `aria-label` / `Popconfirm`)。标题 `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`。
- `SessionSidebar` 搜索行:`Input`(search)独占一行,`Segmented`(活跃 / 已归档)在下一行,两行都 `padding: 0 10px`。

- [ ] **Step 1: 写失败测试**(`ConsoleShell.test.tsx` 追加):

```tsx
it("writes its own top offset into --ew-console-top so the CSS can fill to the viewport bottom", () => {
  render(<ConsoleShell sidebarLabel="会话" sidebar={<div>s</div>} main={<div>m</div>} inspect={<div>i</div>} />);
  const root = screen.getByTestId("playground-tab");
  // jsdom: getBoundingClientRect().top === 0 → "0px"; the point is that the
  // variable is set at all (the CSS reads it), not its value.
  expect(root.style.getPropertyValue("--ew-console-top")).toBe("0px");
});
```

`SessionSidebar.test.tsx` 追加一条:渲染两条会话后,`screen.getByTestId("console-session-rename-<id>")` 存在且其最近的 `.ew-session-item__acts` 祖先在 DOM 中(hover 显隐是 CSS,jsdom 不算样式,只断结构 + 标题节点有 `ew-session-item__title` 类)。

- [ ] **Step 2: 跑红** — `pnpm exec vitest run src/components/console/__tests__/ConsoleShell.test.tsx src/components/console/__tests__/SessionSidebar.test.tsx`。
- [ ] **Step 3: 实现**:

`ConsoleShell.tsx` 增:
```tsx
const rootRef = useRef<HTMLDivElement>(null);
useLayoutEffect(() => {
  const el = rootRef.current;
  if (!el) return;
  const apply = () => {
    el.style.setProperty("--ew-console-top", `${Math.max(0, Math.round(el.getBoundingClientRect().top))}px`);
  };
  apply();
  window.addEventListener("resize", apply);
  return () => window.removeEventListener("resize", apply);
}, []);
// <div ref={rootRef} className="ew-console" data-testid="playground-tab">
```
`console.css`:`.ew-console { height: calc(100dvh - var(--ew-console-top, 0px) - 24px); min-height: 480px; }`(删掉 `calc(100vh - 360px)` 与 `CONSOLE_HEIGHT_OFFSET_PX`)。

`SessionSidebarItem.tsx`:去掉 `actions={[...]}`,`List.Item` children 改为:
```tsx
<div className="ew-session-item__body">
  <div className="ew-session-item__title" title={title}>{showDot && <span className="ew-session-running-dot" …/>}{title}</div>
  <div className="ew-session-item__meta">{relativeTime(session.updated_at, t)}</div>
</div>
<div className="ew-session-item__acts" onClick={(e) => e.stopPropagation()}>
  {/* 三个按钮原样搬进来(rename / archive / purge),testid / aria-label / Popconfirm 不动 */}
</div>
```
`SessionSidebar.css`:
```css
.ew-session-item { position: relative; }
.ew-session-item__title { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ew-session-item__meta { font-size: 11px; color: var(--ew-text-tertiary); }
.ew-session-item__acts { position: absolute; right: 6px; top: 6px; display: none; gap: 2px; padding: 1px 3px; border-radius: 6px; background: var(--ew-surface-raised); border: 1px solid var(--ew-border-default); }
.ew-session-item:hover .ew-session-item__acts, .ew-session-item:focus-within .ew-session-item__acts { display: inline-flex; }
```
(删掉原 `.ew-session-item .ant-list-item-action { opacity: 0 }` 两条规则。)

`SessionSidebar.tsx` 搜索行:外层 `display:flex; flexDirection:column; gap:6px; padding:"8px 10px 0"`,`Input` 去掉 `style={{flex:1}}` 改 `style={{width:"100%"}}`,`Segmented` 单独一行 `block`。

- [ ] **Step 4: 跑绿** 同上两个文件 + `pnpm typecheck`。
- [ ] **Step 5: Commit** `fix(console): 壳高度按实际位置铺满;左栏条目标题占满、操作图标 hover 浮层;搜索行两行`。

---

### Task 2: 会话统计改芯片行

**Files:**
- Modify: `apps/admin-ui/src/components/console/StatsBar.tsx`
- Test: `apps/admin-ui/src/components/console/__tests__/StatsBar.test.tsx`(7 保留,断言按新文案改)
- i18n:新键 `console.stats_chip_turns / stats_chip_steps / stats_chip_llm / stats_chip_tools / stats_chip_ttft / stats_chip_tps / stats_chip_cache / stats_chip_in / stats_chip_out / stats_chip_cost`;删旧 `console.stats_turns / stats_llm_tools / stats_ttft / stats_tps / stats_cache / stats_tokens / stats_cost`(`stats_partial` 保留)。删前 `grep -rn "console.stats_" src` 确认只有 StatsBar 与其测试用。

**Interfaces:**
- Consumes: `SessionStats`(`api/session_stats.ts`,不改)。
- Produces: `StatsBar` props 不变(`{ stats, isSystemAdmin }`);根 testid `console-stats-bar` 不变;每枚芯片 `data-testid="console-stat-<name>"`(name 集合:`turns steps llm tools ttft tps cache in out cost partial`——比现在多拆了 `steps` / `tools` / `in` / `out`)。

**行为(§八.2)**:根节点 `display:flex; flex-wrap:wrap; gap:4px 6px`,**无** `overflow/ellipsis/title`;芯片 = `<span class="ew-stat-chip"><span class="ew-stat-chip__k">{label}</span><span class="ew-stat-chip__v">{value}</span></span>`,顺序:轮数 · 步数 · LLM · 工具 · 首 token(有则)· 速度(有则)· 缓存命中(有则)· 入 · 出 · 费用(admin 且有);`stats.partial` 末尾追加一枚 `console-stat-partial`。`stats.turns === 0` 仍返回 `null`。

- [ ] **Step 1: 改测试**:把 7 条里断 `"2 轮 · 4 步"` / `"|"` 分隔 / `title` 全文的改成断芯片:`getByTestId("console-stat-turns")` 文本含 `2`,`console-stat-steps` 含 `4`,`console-stat-in` 含 `56.4k`(`formatCompact` 不变);新增 1 条「根节点没有 title 属性且样式 flex-wrap」(`toHaveStyle({ flexWrap: "wrap" })`)。跑红。
- [ ] **Step 2: 实现**(`StatsBar.tsx` 主体):

```tsx
const chips: { name: string; label: string; value: string }[] = [
  { name: "turns", label: t("console.stats_chip_turns"), value: String(stats.turns) },
  { name: "steps", label: t("console.stats_chip_steps"), value: String(stats.steps) },
  { name: "llm", label: t("console.stats_chip_llm"), value: fmtDuration(stats.llmMs) },
  { name: "tools", label: t("console.stats_chip_tools"), value: fmtDuration(stats.toolMs) },
];
if (stats.ttftAvgMs !== null) chips.push({ name: "ttft", label: t("console.stats_chip_ttft"), value: fmtDuration(stats.ttftAvgMs) });
if (stats.tokPerSec !== null) chips.push({ name: "tps", label: t("console.stats_chip_tps"), value: `≈ ${stats.tokPerSec} tok/s` });
if (stats.cacheHitPct !== null) chips.push({ name: "cache", label: t("console.stats_chip_cache"), value: `${stats.cacheHitPct}%` });
chips.push({ name: "in", label: t("console.stats_chip_in"), value: formatCompact(stats.inputTokens) });
chips.push({ name: "out", label: t("console.stats_chip_out"), value: formatCompact(stats.outputTokens) });
if (isSystemAdmin && stats.costCny !== null) chips.push({ name: "cost", label: t("console.stats_chip_cost"), value: `¥${stats.costCny.toFixed(2)}` });
return (
  <div data-testid="console-stats-bar" style={{ display: "flex", flexWrap: "wrap", gap: "4px 6px" }}>
    {chips.map((c) => (
      <span key={c.name} className="ew-stat-chip" data-testid={`console-stat-${c.name}`}>
        <span className="ew-stat-chip__k">{c.label}</span><span className="ew-stat-chip__v">{c.value}</span>
      </span>
    ))}
    {stats.partial && <span className="ew-stat-chip" data-testid="console-stat-partial">{t("console.stats_partial")}</span>}
  </div>
);
```
芯片样式放 `console.css`(`.ew-stat-chip { display:inline-flex; gap:4px; align-items:baseline; font-size:11px; color:var(--ew-text-tertiary); background:var(--ew-surface-raised); border-radius:4px; padding:1px 6px; font-variant-numeric:tabular-nums; white-space:nowrap } .ew-stat-chip__v { color:var(--ew-text-secondary); font-weight:500 }`)。i18n 值:zh `轮数 / 步数 / LLM / 工具 / 首 token / 速度 / 缓存命中 / 入 / 出 / 费用`,en `Turns / Steps / LLM / Tools / First token / Speed / Cache hit / In / Out / Cost`。

- [ ] **Step 3: 跑绿** + `pnpm typecheck`(旧键删除会让接口块报错——三处同步删)。
- [ ] **Step 4: Commit** `feat(console): 会话统计改芯片行,自动换行不截断`。

---

### Task 3: 过程条 ProcessStrip + TurnBlock 接线

**Files:**
- Create: `apps/admin-ui/src/components/console/process_summary.ts`、`ProcessStrip.tsx`、`process_strip.css`
- Modify: `apps/admin-ui/src/components/console/TurnBlock.tsx`
- Modify: `apps/admin-ui/src/components/console/CompactRow.tsx`(只改「检查」文案键值:`console.row_inspect` zh `轨迹` / en `Trajectory`;组件不动)
- Test: 新 `__tests__/process_summary.test.ts`、`__tests__/ProcessStrip.test.tsx`;`TurnBlock.test.tsx`(9 保留,涉及紧凑行可见性的改成先展开)
- i18n 新键:`console.process_think`(zh `思考 {{n}} 次` / en `Thinking ×{{n}}`)、`process_tools`(`工具 {{n}} 次` / `Tools ×{{n}}`)、`process_other`(`其它 {{n}} 步` / `{{n}} other steps`)、`process_failed`(`{{n}} 次失败` / `{{n}} failed`)、`process_empty`(`无过程` / `No steps`)、`process_more`(`还有 {{n}} 步…` / `{{n}} more steps…`)、`process_expand`(`展开过程` / `Expand steps`)、`process_collapse`(`收起过程` / `Collapse steps`)。

**Interfaces:**
- Consumes: `CompactRow`(`api/trajectory_rows.ts` 的 `CompactRow` 类型 + `./CompactRow` 组件,props `row / expanded / onToggle / liveText / onInspect / onFireResult`)。
- Produces:

```ts
// process_summary.ts
export interface ProcessSummary {
  think: number; tools: number; other: number; failed: number;
  /** "web_search ×4 · http ×1",按次数降序;空串表示无工具。 */
  toolBreakdown: string;
  /** 全部行的 durationMs 之和(null 一律按 0);无行 → null。 */
  durationMs: number | null;
}
export function summarizeProcess(rows: readonly CompactRow[]): ProcessSummary;
export function processHeadline(s: ProcessSummary, t: TFn): string;
// "思考 3 次 · 工具 5 次(web_search ×4 · http ×1)· 1 次失败" —— 无工具省略括号,无失败省略尾段,三项皆 0 → t("console.process_empty")

// ProcessStrip.tsx
export interface ProcessStripProps {
  rows: readonly { row: CompactRow; liveText?: string }[];
  running: boolean;
  /** 已展开详情的行 id 集合 + 切换(TurnBlock 原有的 expandedIds/toggleRow 下放)。 */
  expandedRowIds: ReadonlySet<string>;
  onToggleRow: (id: string) => void;
  onInspectRow: (rowId: string) => void;
  onFireResult?: (r: FireNowResult) => void;
}
```
- 内部状态:`open: boolean | null`(`null` = 自动:`running ? true : false`);用户点头部一次 → 固定为 `!currentOpen`,之后不再跟 running 变;`running` 从 true 变 false 且 `open === null` 时自然收起(因为自动值变了)。**运行中且 open 为自动**:只渲染最后 3 行 + 前面一行「还有 N 步…」按钮(点了 = 全展开,等价手动 open=true);手动展开则全量。

**行为(§八.3)**:
- 头部一行 `data-testid="console-process-head"`:左 `▸/▾` 图标 + `processHeadline`;右 `durationMs`(`fmtDuration`)+ 运行中转圈(antd `Spin size="small"` 或 lucide `Loader2` 带 `animate-spin` 类;jsdom 只断存在)。`aria-expanded`、`aria-label`(`process_expand/collapse`)。
- 展开体 `data-testid="console-process-steps"`:逐行 `<CompactRow …/>`(原 TurnBlock 那段 map 搬过来,`onInspect={() => onInspectRow(row.id)}`)。
- `rows.length === 0` → 组件返回 `null`(用户轮没有过程时不占位)。

- [ ] **Step 1: 写失败测试**

`process_summary.test.ts`(用 `compactRowsOf` 真事件 fixture 造行,或直接手造 `CompactRow` 对象——用真 fixture:`src/components/console/__tests__/fixtures/`若无就在测试内造 `updates` 帧,形状同 `TurnBlock.test.tsx` 现有 fixture):
```ts
it("counts think / tool rows and builds the tool breakdown by count desc", () => {
  const rows = compactRowsOf(EVENTS_3THINK_5TOOLS); // 4× web_search, 1× http, 一条 tool status=error
  const s = summarizeProcess(rows);
  expect(s).toMatchObject({ think: 3, tools: 5, failed: 1, toolBreakdown: "web_search ×4 · http ×1" });
});
it("headline omits the tool parenthesis without tools and the failed tail without failures", () => {
  expect(processHeadline({ think: 2, tools: 0, other: 0, failed: 0, toolBreakdown: "", durationMs: 1200 }, t)).toBe("思考 2 次");
  expect(processHeadline({ think: 0, tools: 0, other: 0, failed: 0, toolBreakdown: "", durationMs: null }, t)).toBe("无过程");
});
```
`ProcessStrip.test.tsx`(包 `<App>` 因 CompactRow → ToolCallCard 可能用 `App.useApp()`):
```tsx
it("collapses by default when settled and shows only the headline", …);      // console-process-steps 不在 DOM
it("expands automatically while running and shows the last 3 rows + a 'more' button", …); // 传 8 行 running → 3 个 CompactRow + console-process-more 文案含 5
it("a manual toggle wins over the automatic state and is kept when running flips", …); // running=true 点头部 → 收起;rerender running=false 仍收起
it("clicking a row's 轨迹 link calls onInspectRow with that row id", …);
it("renders nothing for a turn without process rows", …);
```
`TurnBlock.test.tsx`:凡是断紧凑行文本可见的 `it`(如 think 行 / tool 行 / 「检查」链接),先 `fireEvent.click(screen.getByTestId("console-process-head"))` 再断(settled 轮默认折叠);running 轮那条(live think 行 `console.row_think_live`)不用点(自动展开)。报告里逐条列。

- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现** `process_summary.ts`:

```ts
export function summarizeProcess(rows: readonly CompactRow[]): ProcessSummary {
  let think = 0, tools = 0, other = 0, failed = 0, dur = 0, any = false;
  const byTool = new Map<string, number>();
  for (const r of rows) {
    if (r.kind === "think") think += 1;
    else if (r.kind === "tool") { tools += 1; byTool.set(r.entry.toolName, (byTool.get(r.entry.toolName) ?? 0) + 1); }
    else other += 1;
    if (r.status === "error") failed += 1;
    if (r.durationMs !== null) { dur += r.durationMs; any = true; }
  }
  const toolBreakdown = [...byTool.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([n, c]) => `${n} ×${c}`).join(" · ");
  return { think, tools, other, failed, toolBreakdown, durationMs: any ? dur : null };
}
export function processHeadline(s: ProcessSummary, t: TFn): string {
  const parts: string[] = [];
  if (s.think > 0) parts.push(t("console.process_think", { n: s.think }));
  if (s.tools > 0) parts.push(t("console.process_tools", { n: s.tools }) + (s.toolBreakdown ? `(${s.toolBreakdown})` : ""));
  if (s.other > 0) parts.push(t("console.process_other", { n: s.other }));
  if (parts.length === 0) return t("console.process_empty");
  if (s.failed > 0) parts.push(t("console.process_failed", { n: s.failed }));
  return parts.join(" · ");
}
```
`ProcessStrip.tsx`:按上面接口;`const auto = running; const isOpen = open ?? auto;` 头部 `<button type="button" className="ew-process__head" aria-expanded={isOpen} onClick={() => setOpen(!isOpen)}>`;体内 `visible = isOpen ? (open === null && running && rows.length > 3 ? rows.slice(-3) : rows) : []`;more 按钮 `data-testid="console-process-more"` 只在 `open === null && running && rows.length > 3` 时渲染在体内首位,点击 `setOpen(true)`。失败尾段单独一个 `<span className="ew-process__failed">` 红色(`var(--ew-text-danger)`)。

`TurnBlock.tsx`:把 `rows.map(CompactRow)` 那段替换成 `<ProcessStrip rows={rows} running={turn.turn.status === "running"} expandedRowIds={expandedIds} onToggleRow={toggleRow} onInspectRow={(rowId) => onInspectRow(turn.key, rowId)} onFireResult={onFireResult} />`;`expandedIds/toggleRow` 状态留在 TurnBlock。

- [ ] **Step 4: 跑绿**(三个测试文件)+ `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 过程条 —— 运行中展开、完成后折叠成一句摘要`。

---

### Task 4: 一行式脚注 + 输入区收紧

**Files:**
- Modify: `apps/admin-ui/src/components/console/TurnFooter.tsx`;Create `turn_footer.css`
- Modify: `apps/admin-ui/src/components/console/Composer.tsx`
- Test: `TurnFooter.test.tsx`(2 → ≥6)、`Composer.test.tsx`(5 保留 + 2 新)
- i18n 新键:`console.footer_view_trajectory`(zh `查看轨迹` / en `View trajectory`)、`console.footer_tokens`(`{{n}} tok`)、`console.footer_steps`(`{{n}} 步` / `{{n}} steps`)、`console.footer_export`(`导出` / `Export`);**删** `console.footer_inspect`(唯一消费者是 TurnFooter,先 grep);`console.composer_hint` 值改短:zh `Enter 发送,Shift+Enter 换行` 保留即可,但 Composer 里把「{count} / {max}」拼在它前面;`playground.input_placeholder` 值改成 zh `输入要发给智能体的提示词…` / en `Type a prompt for the agent…`(去掉 SSE 那半句;它在 `playground.*` 里,允许改值不改键)。

**Interfaces:**
- Consumes: `TurnSummary`(`usage / stepCount / latencyMs / modelName / finishReason`)、`FeedbackBar`、`runIdOf`。
- Produces: `TurnFooterProps` **去掉** `selected`(不再有主色态);其余 props 名不变。`Composer` props 不变。

**行为(§八.4 / §八.5)**:
- 脚注一行 `display:flex; align-items:center; gap:10px; flex-wrap:wrap`(窄屏允许折两行,但正常宽度一行):
  - 左:`Tag`(状态,testid `console-turn-status` 不变)+ `<span data-testid="console-footer-meta">` 紧凑摘要 = `[usage ? footer_tokens(totalTokens 千分位) ] · [stepCount ? footer_steps] · [latencyMs ? fmtDuration] · [modelName]` 用 ` · ` 连,包在 antd `Tooltip` 里,`title` 是多行拆分:输入 / 输出 / 缓存 / 思考 / 费用(≈¥,有则)/ finish_reason(非 stop 才列)——文案键复用 `playground.usage_in / usage_out / usage_cache / usage_reasoning / meta_finish`(都在 `playground.*`,是保留键;先 grep 确认它们在 zh/en 都有)。
  - 右(`margin-left:auto`,`display:inline-flex; gap:2px`):`FeedbackBar`(条件同现在:`!readOnly && status==="done" && threadId`,包 `ReadonlyTooltip`)· 重试(`Button size="small" type="text" icon RotateCcw` + 文案 `playground.retry`,条件同现在,testid `playground-turn-retry`)· 导出(`type="text"` icon Download + 文案 `console.footer_export`,testid `playground-export-json`)· **查看轨迹**(`type="text"` icon lucide `Route`,文案 `console.footer_view_trajectory`,testid `console-turn-inspect`,颜色 link)。**不再渲染** `TurnMeta`(不 import),**不再渲染**「查看运行」。
- `Composer`:`TextArea autoSize={{ minRows: 2, maxRows: 8 }}`,`showCount` 关掉;下面一行 `display:flex; align-items:center; gap:8px`:发送按钮(`running` 时**换成** `danger` 「停止」按钮,icon `Square`,`onClick={onStop}`,testid `playground-stop`;不 running 时是原「运行」按钮 testid `playground-run` —— 两个按钮**同一位置二选一**,不再并排)· 图片 · 文档 · 右侧 `<Text type="secondary" style={{marginLeft:"auto",fontSize:12}}>{value.length} / {maxLength} · {t("console.composer_hint")}</Text>`(testid `console-composer-hint`)。`readOnly` / `missingVariables` tooltip 逻辑不变。

- [ ] **Step 1: 写失败测试**

`TurnFooter.test.tsx`(现 2 条保留并调整 testid 期待):
```tsx
it("renders status, a compact meta line, and the actions on one row (no TurnMeta chips)", …); // console-footer-meta 文本匹配 /36,030 tok · 3 步 · .* · glm-5\.2/;queryByTestId("playground-usage") 为 null;console-turn-inspect 文本 = 查看轨迹
it("meta tooltip lists input / output / cache / reasoning breakdown", …);   // mouseover console-footer-meta → findByRole("tooltip") 含 "输入" 与 "34408"
it("does not render a view-run link any more", …);                           // queryByText(/查看运行|View run/) null
it("running turn: no retry, no feedback, still 查看轨迹", …);
it("hides feedback when readOnly", …);
it("retry button is danger-styled for a failed turn", …);                    // 原有,保留
```
`Composer.test.tsx` 新增:
```tsx
it("while running the run button is replaced by a stop button in the same slot", …); // running=true → getByTestId("playground-stop") 存在,queryByTestId("playground-run") null;点击调 onStop
it("shows the counter and hint on the toolbar row", …);                            // console-composer-hint 文本 /^0 \/ 65536 · /
```
既有 5 条里若有断「运行 loading 且同时有停止」的,改成二选一并列入报告。

- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现**(TurnFooter 主体):

```tsx
const meta: string[] = [];
if (summary.usage) meta.push(t("console.footer_tokens", { n: summary.usage.totalTokens.toLocaleString() }));
if (summary.stepCount !== null) meta.push(t("console.footer_steps", { n: summary.stepCount }));
if (summary.latencyMs !== null) meta.push(fmtDuration(summary.latencyMs));
if (summary.modelName) meta.push(summary.modelName);
const breakdown = summary.usage ? [
  `${t("playground.usage_in")}: ${summary.usage.inputTokens}`,
  `${t("playground.usage_out")}: ${summary.usage.outputTokens}`,
  `${t("playground.usage_cache")}: ${summary.usage.cacheReadTokens}`,
  `${t("playground.usage_reasoning")}: ${summary.usage.reasoningTokens}`,
  ...(costCny !== null ? [`≈ ¥${costCny.toFixed(4)}`] : []),
  ...(summary.finishReason && summary.finishReason !== "stop" ? [`${t("playground.meta_finish")}: ${summary.finishReason}`] : []),
] : [];
return (
  <div className="ew-turn-footer">
    <Tag color={STATUS_TAG_COLOR[status]} bordered={false} data-testid="console-turn-status">{t(`console.footer_status_${status}`)}</Tag>
    {meta.length > 0 && (
      <Tooltip title={breakdown.length ? <div style={{ whiteSpace: "pre-line" }}>{breakdown.join("\n")}</div> : undefined}>
        <span className="ew-turn-footer__meta" data-testid="console-footer-meta">{meta.join(" · ")}</span>
      </Tooltip>
    )}
    <span className="ew-turn-footer__acts">
      {!readOnly && status === "done" && threadId && (<ReadonlyTooltip on={isTenantSwitched}><FeedbackBar threadId={threadId} turnSeq={turn.seq} disabled={isTenantSwitched} /></ReadonlyTooltip>)}
      {onRetry && status !== "running" && (<Button type="text" size="small" danger={failed} icon={<RotateCcw size={13} strokeWidth={1.75} />} onClick={() => onRetry(turn.turn)} data-testid="playground-turn-retry">{t("playground.retry")}</Button>)}
      <Button type="text" size="small" icon={<Download size={13} strokeWidth={1.75} />} loading={exporting} onClick={() => onExport(turn.turn)} data-testid="playground-export-json">{t("console.footer_export")}</Button>
      <Button type="link" size="small" icon={<Route size={13} strokeWidth={1.75} />} onClick={onInspect} data-testid="console-turn-inspect">{t("console.footer_view_trajectory")}</Button>
    </span>
  </div>
);
```
`turn_footer.css`:`.ew-turn-footer { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:11.5px; color:var(--ew-text-tertiary); border-top:1px dashed var(--ew-border-subtle); padding-top:6px } .ew-turn-footer__meta { font-variant-numeric:tabular-nums } .ew-turn-footer__acts { margin-left:auto; display:inline-flex; align-items:center; gap:2px }`。`TurnBlock` 里去掉传 `selected`。

Composer:发送位 `running ? <Button danger icon={<Square/>} onClick={onStop} data-testid="playground-stop">{t("playground.stop")}</Button> : sendButtonWithMissingTooltip`(仍包 `ReadonlyTooltip`);去掉原来末尾的独立停止按钮与 `Text` 提示行;工具条右侧 hint 节点如上。

- [ ] **Step 4: 跑绿** + `pnpm typecheck`(`selected` prop 删掉后 `Transcript.tsx` / `TurnBlock.tsx` 传参处会报错 → 一并去掉;`Transcript` 若把 `selected` 传给 `TurnFooter` 是经 TurnBlock,只改 TurnBlock 那一行)。
- [ ] **Step 5: Commit** `feat(console): 脚注一行式(查看轨迹 / 去查看运行)+ 输入区收紧、停止按钮归位`。

---

### Task 5: 泳道 v2 —— 双投影模型 + LaneStrip 重绘

**Files:**
- Rewrite: `apps/admin-ui/src/components/console/lane_strip_model.ts`
- Rewrite: `apps/admin-ui/src/components/console/LaneStrip.tsx`、`lane_strip.css`
- Test: `lane_strip_model.test.ts`(3 → ≥8)、`LaneStrip.test.tsx`(3 → ≥9)
- i18n 新键:`console.lane_user`(zh `用户` / en `User`;`lane_input / lane_model / lane_tools` 保留但 `lane_input` 本组件不再用——留给 PR-B 清)、`console.lane_mode_sequence`(`顺序` / `Sequence`)、`console.lane_mode_duration`(`时长` / `Duration`)、`console.lane_tip_hint`(`点击选中 · 拖选过滤 · 双击复位` / `Click to select · drag to filter · double-click to reset`)、`console.lane_tip_range`(`{{start}} → {{end}} · {{d}}`)。

**Interfaces:**
- Consumes: `TrajectoryRow`(`api/trajectory_rows.ts`)、`laneModelOf` 的时序来源 `buildGanttRows`(`api/gantt_timeline.ts`,不动)、`resolveGanttKey`、`fmtDuration`。
- Produces:

```ts
// lane_strip_model.ts
export type Lane = "user" | "model" | "tools";
export type LaneMode = "sequence" | "duration";
export const LANE_OF_KIND: Record<TrajectoryRow["kind"], Lane> = {
  user: "user", plan: "user", memory: "user", reflect: "user",
  compaction: "user", retry: "user", error: "user", approval: "user", guard: "user", gap: "user",
  think: "model", assistant: "model",
  tool: "tools", subagent: "tools",
};
export interface LaneBlock {
  key: string;            // row.id
  rowId: string;
  rowIndex: number;       // 1-based,与行表 # 一致
  lane: Lane;
  kind: TrajectoryRow["kind"];
  /** 投影域里的起止:sequence 模式 = [i, i+1);duration 模式 = ms。 */
  start: number; end: number;
  hasError: boolean;
  live: boolean;          // 运行中且 durationMs 为 null 的尾块
}
export interface LaneTick { at: number; label: string }
export interface LaneProjection {
  mode: LaneMode;
  blocks: LaneBlock[];
  /** 域长度:sequence = rows.length;duration = totalMs(运行中随 nowMs 生长)。 */
  total: number;
  ticks: LaneTick[];
  degraded: boolean;      // duration 模式且 gantt 无时序时 true(此时 blocks 退化成 sequence 排布)
}
export function laneProjection(
  rows: readonly TrajectoryRow[], events: readonly SseEvent[],
  opts: { mode: LaneMode; running: boolean; nowMs: number },
): LaneProjection;
/** 拖选:域坐标 [a,b] → 与之相交的行 index 区间(1-based 闭区间);无相交 → null。 */
export function rangeToRowSpan(p: LaneProjection, a: number, b: number): { from: number; to: number } | null;

// LaneStrip.tsx
export interface LaneStripProps {
  rows: readonly TrajectoryRow[];
  events: readonly SseEvent[];
  running: boolean;
  mode: LaneMode;
  selectedRowId: string | null;
  hoveredRowId: string | null;
  onHoverRow: (rowId: string | null) => void;
  onSelectRow: (rowId: string) => void;
  /** 行序号闭区间筛选;null = 无筛选。 */
  range: { from: number; to: number } | null;
  onRangeChange: (range: { from: number; to: number } | null) => void;
  /** 每行的摘要(行表同一函数,提示气泡里用)。 */
  summaryOf: (row: TrajectoryRow) => string;
}
```

**行为(§八.7)**:
- **sequence**:`blocks[i] = { start: i, end: i+1 }`,`total = rows.length`,`ticks` = 每 `ceil(N/6)` 条一个 `#k`(首尾必有);`degraded=false`。
- **duration**:先 `buildGanttRows(events, { settled: !running })` 拿到带 `startMs/durationMs` 的时序块,`resolveGanttKey(rows, key)` 映回 rowId;有时序的行用它们的 ms;user 行放 `[0, 0]`、assistant 行放 `[total, total]`(点块,渲染时最小宽度);其它没时序的行(plan/memory/reflect/marker)按其 `serverMs`(`RowBase.serverMs`,相对第一帧)放点块,`serverMs` 也没有的**不画**;`total` = gantt `totalMs`,运行中按 PR-A `laneModelOf` 的 `lastKnownFrame` 算法生长(把那段代码保留);`ticks` = 0 / 25% / 50% / 75% / 100% 五个 `fmtDuration`;gantt `degraded` 或 `totalMs===0` → `degraded=true`,blocks 退成 sequence 排布(保证永远画得出)。
- **渲染**:`<div class="ew-lanes" data-testid="console-lane-strip" data-mode={mode}>` 三行 `.ew-lane`(`display:grid; grid-template-columns:34px 1fr; height:14px`),标签 `t("console.lane_user|lane_model|lane_tools")`;轨道 `.ew-lane__track { position:relative; height:8px }`;块 `<button class="ew-lane__block" data-testid="console-lane-block" data-row-id data-lane data-kind data-index data-error data-live data-selected data-hovered data-dimmed style="left:X%;width:W%">`,`W = max((end-start)/total*100, 0.6)`,块间留 1px(CSS `box-shadow: 0 0 0 1px var(--ew-surface-base)` 造缝);颜色:user 泳道 `var(--ew-color-brand-300)`,model `var(--ew-color-brand-500)`,tools `var(--ew-accent-violet)`,error `var(--ew-color-danger-500)`;`data-live` 呼吸(`@keyframes` + reduced-motion 关);`data-dimmed`(在 range 之外)`opacity:.35`;`data-hovered` 白描边 1px,`data-selected` 白描边 2px。
- **提示**:每块包 antd `Tooltip`,`title` = `${t("console.traj_kind_"+kind)} · #${rowIndex} · ${summaryOf(row)}` + 换行 + (duration 模式且有 ms:`lane_tip_range` 用 `fmtDuration(start)` / `fmtDuration(end)` / `fmtDuration(end-start)`;sequence 模式:`fmtDuration(row.durationMs)` 或空)+ 换行 + `lane_tip_hint`;`mouseenter/leave` → `onHoverRow(rowId|null)`;`click` → `onSelectRow(rowId)`。
- **拖选**:轨道容器上 `onPointerDown`(记录 `x0` 的域坐标,`setPointerCapture`)/ `onPointerMove`(更新 draft 遮罩 `.ew-lanes__draft` 的 left/width)/ `onPointerUp`(`|dx| < 4px` 视为点击不产生 range;否则 `onRangeChange(rangeToRowSpan(p, a, b))`);`onDoubleClick` → `onRangeChange(null)`。有 `range` 时画 `.ew-lanes__range` 遮罩(range 的 from/to 对应块的 start/end 域坐标)。
- **刻度**:`.ew-lanes__ticks` 一行,`ticks.map` 绝对定位 `left: at/total*100%`,mono 9.5px。
- `rows.length === 0` → 返回 `null`。

- [ ] **Step 1: 写失败测试**

`lane_strip_model.test.ts`(fixture:9 行 = user / think / tool / tool / think / tool / tool(error) / think / assistant,用 `trajectoryRowsOf(EVENTS, input, answer, "done")` 真投影;`EVENTS` 用 `TrajectoryPanel.test.tsx` 现有的那份多工具 fixture 或在测试里造带 `_duration_ms` 与 `id:` 的 `updates` 帧):
```ts
it("sequence: one block per row, equal width, lane by kind, ticks at #1/#4/#7/#9", …);
it("sequence: a tool row with status=error is hasError", …);
it("duration: blocks carry gantt ms; user is a point at 0 and assistant a point at total", …);
it("duration: with no parseable timing it degrades to sequence layout and flags degraded", …);
it("running: the unsettled tail block is live and total grows with nowMs", …);
it("rangeToRowSpan maps a domain interval to the intersecting 1-based row span, null when empty", …);
it("LANE_OF_KIND covers every TrajectoryRow kind", …); // Object.keys 与 union 的 exhaustiveness 用一个 satisfies 断言
```
`LaneStrip.test.tsx`(3 条保留:块存在 / 点击选行 / error data 属性;新增):
```tsx
it("renders three labelled lanes and one block per row with data-index", …);
it("hover on a block calls onHoverRow(id) then null on leave, and the block reflects hoveredRowId", …);
it("selected block gets data-selected", …);
it("blocks outside `range` are dimmed", …);
it("drag on the track calls onRangeChange with the row span; a <4px move does not", …); // pointerdown/move/up 用 fireEvent,轨道 getBoundingClientRect mock 成 width 300
it("double-click clears the range", …);
it("shows the tooltip with kind · #index · summary", …); // mouseover → findByRole("tooltip")
it("mode=duration renders time ticks (0s … total) and mode=sequence renders #k ticks", …);
```

- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现** `lane_strip_model.ts` / `LaneStrip.tsx` / `lane_strip.css` 按上面接口与行为;`lastKnownFrame` 与生长逻辑从现文件原样保留。
- [ ] **Step 4: 跑绿** + `pnpm typecheck`(此时 `TrajectoryPanel.tsx` 还在按旧 props 用 `LaneStrip` → **本 task 内**给 `TrajectoryPanel` 做**最小适配**让类型过:传 `mode="sequence"`、`hoveredRowId={null}`、`onHoverRow={() => {}}`、`range={null}`、`onRangeChange={() => {}}`、`summaryOf={() => ""}`,`rows/events/running/selectedRowId/onSelectRow` 照旧——真正的联动在 Task 6 接;报告里注明这段是占位)。
- [ ] **Step 5: Commit** `feat(console): 泳道 v2 —— 顺序 / 时长双投影、细泳道、提示、hover 联动、拖选、刻度`。

---

### Task 6: 行表加列 + 详情修边 + 右栏头部与联动

**Files:**
- Modify: `apps/admin-ui/src/api/timeline.ts`(`StepItem` 加 `reasoningTokens?: number; cacheReadTokens?: number`,在 `push({kind:"agent"…})` 处从 `um.output_token_details.reasoning` / `um.input_token_details.cache_read` 读)、`api/trajectory_rows.ts`(`ThinkRow` 加 `reasoningTokens?: number; cacheReadTokens?: number`,构造处透传)
- Modify: `apps/admin-ui/src/components/console/TrajectoryRows.tsx`、`trajectory_rows.css`
- Modify: `apps/admin-ui/src/components/console/RowDetail.tsx`
- Modify: `apps/admin-ui/src/components/console/TrajectoryPanel.tsx`
- Test: `api/__tests__/timeline.test.ts`(12 + 1)、`api/__tests__/trajectory_rows.test.ts`(13 + 1)、`TrajectoryRows.test.tsx`(6 → ≥10)、`RowDetail.test.tsx`(5 保留 + 1)、`TrajectoryPanel.test.tsx`(9 → ≥13)
- i18n 新键:`console.traj_col_idx`(`#`)、`traj_col_kind`(`类型` / `Kind`)、`traj_col_summary`(`摘要` / `Summary`)、`traj_col_in`(`入` / `In`)、`traj_col_out`(`出` / `Out`)、`traj_col_think`(`思考` / `Think`)、`traj_col_duration`(`耗时` / `Time`)、`console.traj_filter`(`已筛选 #{{a}}–#{{b}}({{n}} 条)` / `Filtered #{{a}}–#{{b}} ({{n}} rows)`)、`console.traj_filter_clear`(`清除筛选` / `Clear filter`)、`console.inspect_run_detail`(`Run 详情` / `Run detail`)、`console.detail_header`(`#{{idx}}`——直接拼也行,不加键)。

**Interfaces:**
- Consumes: Task 5 的 `LaneStrip` v2 props、`laneProjection`;`rowSummary`(TrajectoryRows 已导出)。
- Produces:

```ts
export interface TrajectoryRowsProps {
  rows: readonly TrajectoryRow[];
  selectedRowId: string | null;
  hoveredRowId: string | null;
  onHoverRow: (rowId: string | null) => void;
  onSelectRow: (rowId: string) => void;
  running: boolean;
  /** 1-based 闭区间;null = 全部。 */
  range: { from: number; to: number } | null;
  onClearRange: () => void;
}
export interface RowDetailProps { …原样… ; rowIndex: number; }   // 头部显示 #rowIndex
```

**行为(§八.6 / §八.8)**:
- `TrajectoryRows`:根仍是 `role="listbox"` 容器 `console-traj-rows`(键盘 ↑↓ 保留,`Esc` 由 TrajectoryPanel 处理),但每行改成 grid 列 `28px 68px 1fr 46px 46px 46px 56px`:`#`(`data-testid` 不加,文本 = 1-based index)· 类型(mono,`traj_kind_*`,running 脉冲保留)· 摘要(`rowSummary`,省略)· 入 / 出 / 思考(think 行 `formatCompact(inputTokens|outputTokens|reasoningTokens ?? "")`,其它行空)· 耗时。表头一行 `.ew-traj-rows__head`(sticky top)用 `traj_col_*`。`range` 非空时:只渲染 `index ∈ [from,to]` 的行,表头上方一枚芯片 `data-testid="console-traj-filter"` 文案 `traj_filter`,带 ✕(`aria-label=traj_filter_clear`,`onClearRange`)。行 `onMouseEnter/Leave` → `onHoverRow`;`hoveredRowId===row.id` → `data-hovered` 背景 hover 色。testid `console-traj-row` + `data-row-id / data-kind / data-status / data-index` 保留/新增。
- `RowDetail`:根 `padding: 8px 12px; height:100%; overflow:auto`;头部 `#${rowIndex}`(mono,tertiary)· `traj_kind_*`(mono)· `headerSummary`(ellipsis,flex:1)· `fmtDuration(row.durationMs)`(有则)· ✕。去掉「第 N 轮」文字(轮在面板头部已有);`console.detail_level_turn_only` 键保留给 SummaryTab 用(它还在用)。
- `TrajectoryPanel`:头部改成一行:`第 N 轮 · {status}`(`inspect_turn_header` 保留)· `playground-tool-count`(有)· `playground-tool-failed`(有)· 总耗时(`fmtDuration(summary.latencyMs)`,有)· `Run 详情 ↗`(`<Link to={`/runs/${threadId}/${turn.runId}`} data-testid="console-inspect-run-link">`,`threadId && turn.runId` 才渲染)· Langfuse 链(`playground-turn-langfuse`,admin 且有,照旧)· 右侧 `Segmented`(`console-lane-mode`,`顺序 / 时长`,值存 `localStorage["expert_work.console.lane_mode"]`,读不到默认 `sequence`)。状态:`hoveredRowId`、`range`(切轮 `turn?.key` 变化时都重置为 null);`Esc` 键在面板根 `onKeyDown` → `setSelectedRowId(null)`。`LaneStrip` 接全部新 props(`summaryOf={(r) => rowSummary(r, t)}`);`TrajectoryRows` 接 `hoveredRowId/onHoverRow/range/onClearRange`;`RowDetail` 接 `rowIndex = rows.findIndex(r => r.id === selectedRow.id) + 1`。`RunStatusBanner` 保留。

- [ ] **Step 1: 写失败测试**
  - `timeline.test.ts` +1:`usage_metadata.output_token_details.reasoning=770, input_token_details.cache_read=14336` → agent step `reasoningTokens===770, cacheReadTokens===14336`;缺省 → 字段 `undefined`。
  - `trajectory_rows.test.ts` +1:同 fixture 经 `trajectoryRowsOf` 的 think 行带两字段。
  - `TrajectoryRows.test.tsx`(6 保留;新增):表头七列文案;think 行三列 token 有值、tool 行为空;`data-index` 从 1 递增;`range={from:2,to:4}` 只剩 3 行 + `console-traj-filter` 文案含 `#2–#4` 与 `3`;点 ✕ 调 `onClearRange`;hover 行调 `onHoverRow(id)`;`hoveredRowId` 命中行 `data-hovered="true"`。
  - `RowDetail.test.tsx` +1:头部含 `#4` 与 `TOOL`,不含「第 1 轮」;根 style padding。
  - `TrajectoryPanel.test.tsx`(9 保留;新增):头部有 `console-inspect-run-link` 指向 `/runs/<thread>/<run>`;`console-lane-mode` 切到「时长」后 `console-lane-strip` 的 `data-mode="duration"` 且 localStorage 写入;hover 泳道块 → 对应 `console-traj-row` `data-hovered`;拖选后 `console-traj-filter` 出现、切轮后消失;`Esc` 关详情。

- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现**(按行为节;`TrajectoryRows` 表头 sticky 用 `position: sticky; top: 0; background: var(--ew-surface-base)`;列样式进 `trajectory_rows.css`)。
- [ ] **Step 4: 跑绿** 五个测试文件 + `pnpm typecheck`。
- [ ] **Step 5: Commit** `feat(console): 行表加 token 列 + 筛选芯片、详情头部修边、右栏头部(Run 详情 / 顺序·时长)与泳道联动`。

---

### Task 7: PlaygroundTab 接线 + 测试 / e2e 更新 + 全门

**Files:**
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx`
- Modify: `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx`(46 保留,断言按 §八 改)
- Modify: `apps/admin-ui/e2e/playground-upload.spec.ts`、`e2e/session-history.spec.ts`(视残留而定)
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.stories.tsx`(若 story 断言/mock 引用旧结构)

**行为(§八.2)**:`MAIN_HEAD_STYLE` 那行下面新增 `<div style={{ padding: "6px 16px", borderBottom: "1px solid var(--ew-border-subtle)" }} data-testid="console-stats-row"><StatsBar stats={stats} isSystemAdmin={isSystemAdmin} /></div>`(`stats.turns===0` 时 StatsBar 返回 null → 容器也别渲染:`consoleTurns.length > 0 &&`);`COMPOSER_STYLE` 块里**删掉** `<StatsBar …/>`。其它接线不变(`Transcript` 不再需要 `selected` 之外的改动)。

- [ ] **Step 1: 更新 `PlaygroundTab.test.tsx`**:逐条过 46 条 —— 涉及 `console-stats-bar` 位置 / 文案(改成芯片 testid + 位于 `console-stats-row` 内)、`console-turn-inspect` 文案(「查看轨迹」)、紧凑行可见性(先点 `console-process-head` 展开;流式轮自动展开不用点)、`playground-stop` 与 `playground-run` 二选一、`playground-usage` 不再存在(TurnMeta 退出脚注)的条目改断言并列表;其余原样。**报告里给 46 行去向表**(留 / 改 + 依据)。
- [ ] **Step 2: 接线**,跑 `pnpm exec vitest run src/pages/__tests__/PlaygroundTab.test.tsx` 绿。
- [ ] **Step 3: e2e**:`grep -rn 'playground-\|console-' e2e/*.spec.ts`,核对本计划改动的 testid / 文案(重点 `playground-run`(运行中变 `playground-stop`)、`console-turn`、`console-turn-status`、`console-session-*`),改受影响 spec;本地跑 `pnpm exec playwright test e2e/playground-upload.spec.ts e2e/session-history.spec.ts` 绿(注意 axe:新泳道块是 `<button>` 需要可访问名——`aria-label` = 提示首行;`.ew-lanes__track` 若可滚动加 `tabIndex`)。
- [ ] **Step 4: 全门** `pnpm typecheck && pnpm exec vitest run && pnpm build && pnpm build-storybook`;残留 grep:`CONSOLE_HEIGHT_OFFSET_PX`(0)、`console.footer_inspect`(0)、`console.stats_turns`(0)、`TurnMeta`(只剩 `TurnCard.tsx` / `TurnMeta.tsx` 自身 / 其测试)。
- [ ] **Step 5: Commit** `feat(console): 芯片状态栏上移接线 + 46 条测试与 e2e 更新`。

---

### Task 8: 上线与真栈冒烟(合并后)

- [ ] `export KUBECONFIG=~/.kube/expert-work-test.yaml && tools/deploy/release.sh test`(SMOKE PASS)→ 挪回注释 → 分支 `chore/deploy-test-<sha>` 记录 PR。
- [ ] 浏览器冒烟(用户或 Chrome 扩展):①左栏标题单行 + hover 出三图标;②壳铺满到底,底部无留白;③顶部芯片行完整、窄窗换行;④发一轮:过程条运行中展开(最近 3 步 + 转圈)、完成后折叠一句摘要,点开全部行,「轨迹」跳右栏;⑤脚注一行:状态 · 摘要(hover 拆分)· 👍👎 · 重试 · 导出 · 查看轨迹;⑥输入区:运行中原位「停止」;⑦右栏:头部 Run 详情链接 / 顺序·时长切换记住;泳道每行一块、并行工具分开、hover 提示 + 表行高亮、点击开详情、拖选出芯片、双击复位、运行中呼吸;⑧行表七列有 token 数;详情头部「#4 TOOL …」左边不再裁。
- [ ] 结果写进记录 PR,绿了等用户合并。

---

## Self-Review

- **Spec 覆盖(§八 八条)**:1 → T1;2 → T2 + T7;3 → T3;4 → T4;5 → T4;6 → T6;7 → T5(+T6 接线);8 → T6。「明确不做」三条计划里没有对应 task(对)。
- **占位扫描**:T5 / T6 的测试用一句话描述 + 明确断言对象;实现代码给了接口与行为规则而非全量代码(泳道渲染 / 表格布局是样式密集型,写全量反而会和实现者的 CSS 细节打架),控制器评审按「行为」节逐条对。
- **类型一致**:`LaneMode / LaneBlock / LaneProjection / rangeToRowSpan`(T5)在 T6 的 `TrajectoryPanel` 用;`TrajectoryRowsProps` 新增四字段(T6)只在 T6 内消费;`ProcessStripProps.onInspectRow(rowId)` 单参数,`TurnBlock` 里包一层补 `turn.key`;`TurnFooterProps` 去 `selected` 影响 `TurnBlock`(T4 内改)—— T3 也改 `TurnBlock`:**T3 改的是紧凑行段(ProcessStrip 替换 `rows.map`),T4 改的是 `<TurnFooter …/>` 那一处的 `selected` 行**,两处不相邻,控制器合并 T3/T4 时若冲突按两边都取。
- **数据缺口(诚实记录)**:模型块 TTFT 双色本次不做(每步 TTFT 后端没给,spec §八.7 已写明);assistant 行 token 三列留空(用量记在 think 步上)。
