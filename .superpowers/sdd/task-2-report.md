# Task 2 Report — `GanttTimeline` 组件(行/条/轴/两档/tooltip)

## STATUS

DONE — TDD 先红后绿,5 测全绿,已提交。

## Commit

- `a7d7703c` feat(ui): GanttTimeline 组件——两档形态+标签 tooltip+并发条形

## 变更文件

- `apps/admin-ui/src/components/turn/GanttTimeline.tsx`(新)— 组件本体。
- `apps/admin-ui/src/components/turn/GanttTimeline.css`(新)— 组件专属样式(见下方「实现备注」的必要性说明)。
- `apps/admin-ui/src/components/turn/__tests__/GanttTimeline.test.tsx`(新)— brief Step 1 五条测试,逐字对齐。

## 接口(按 brief 定稿,未偏离)

```tsx
export interface GanttTimelineProps {
  model: GanttModel;
  variant: "embedded" | "expanded";
  running?: boolean;
  renderDetail: (row: GanttRow) => ReactNode;
}
export function GanttTimeline(props: GanttTimelineProps): JSX.Element;
```

消费 Task 1 `api/gantt_timeline.ts` 的真实导出(`GanttModel.rows/markers/totalMs/degraded`;`GanttRow.label/model/kind/depth/startMs/durationMs/detail`)——已核对该文件字段,组件不读取 `detail` 内部结构,只原样转发给调用方注入的 `renderDetail`。

## 实现要点

- **两档共用一套 class**:标签列宽度(176px/292px)、model 名显隐、时长常显/悬停显、marker 覆盖层的 `left` 偏移,全部靠根节点 `data-variant="embedded"|"expanded"` 属性 + CSS 后代选择器切换(仿原型 `body.compact` 的做法),JSX 里不做双份宽度分支,只有 model 文案本身按 `!embedded &&` 条件不渲染(嵌入态压根不产出该 DOM,不只是隐藏)。
- **条形几何**:`left = startMs/totalMs*100%`,`width = max(durationMs/totalMs*100%, 0.35%)`;`durationMs === null` 行按「延伸到轴右边界」处理(`width = max((totalMs-startMs)/totalMs*100%, 0.35%)`),再叠加 `running` 决定的 `ew-gantt-bar--running`(生长/脉冲动画)或 `ew-gantt-bar--interrupted`(斜纹中断态)修饰类。`totalMs<=0` 时用 `safeTotal=1` 兜底避免除零。
- **时间轴刻度**:`totalMs<10s→1s`、`<60s→10s`、否则 `30s`,三档都整除到整秒,tick 文案直接 `${ms/1000}s`。
- **色 class → `--ew-*` 令牌**(先 grep 了 `theme/tokens.css` 全表,未新增令牌):
  - agent → `var(--ew-text-info)`(brand 蓝,StepTimeline 的 `INFO` 同源)
  - aux → `var(--ew-accent-violet)`(brief 要的紫,表里已有,非新增)
  - tool → `var(--ew-text-success)`
  - worker → `var(--ew-text-warning)`
  - final → `var(--ew-color-success-500)` raw 色阶(比语义层 `--ew-text-success` 更饱和的"强绿",brief 明确要求 final 与 tool 视觉可区分;不是新令牌,是既有色阶里挑更浓的一档)+ `color-mix` 出的 box-shadow 光晕,避免硬编码 rgba 表达"颜色"。
  - marker 六种 kind 复用同一张令牌(warn/danger/violet/success 四色分派),不新增。
- **缩进**:`paddingLeft = depth*18+10`,`depth>0` 前缀 `└`(`aria-hidden` 装饰性字符)。
- **点击展开**:单个 `openKey` state,一次一行;行同时是 `role="button" tabIndex=0` 支持 Enter/Space。
- **Tooltip**:antd `Tooltip` 包裹标签列,`title = model ? \`${label} · ${model}\` : label`——嵌入态标签列本身不渲染 model span,全靠 tooltip 补全;放大态 model 常显 + tooltip 仍可用(不冲突)。

## 5 条测试(brief Step 1,逐条落地)

| # | 断言 |
|---|------|
| 1 | 嵌入态:mouseEnter 标签列 → `role="tooltip"` 文案含 model 名;时长 `<span>` 带 `gantt-dur-hover`;放大态同一行不带该 class |
| 2 | 三行(1 个 agent + 2 个并发 tool,同 startMs)→ `style.left`/`style.width` 精确百分比;两并发行 `left` 相等(重叠断言) |
| 3 | 点击行 1 渲染 `renderDetail`;点击行 2 → 行 1 详情消失、行 2 详情出现(`getAllByTestId` 长度恒 ≤1);再点行 2 → 收起 |
| 4 | `kind="final"` 行 bar class 含 `ew-gantt-bar--final`;marker 元素带 `title` 属性且值等于 marker.text |
| 5 | `durationMs=null` 行:`running` 时 class 含 `ew-gantt-bar--running`;`running=false` 时含 `ew-gantt-bar--interrupted` |

**红**:实现前跑测试 → `Failed to resolve import "../GanttTimeline"`(组件文件不存在,失败原因正确)。
**绿**:实现后 5/5 通过。中途一处返工——`screen.getByText("glm-5.2")` 在 antd Tooltip 把 `label · model` 渲染成单个文本节点时匹配不到(getByText 默认精确匹配整节点文本),改用 `screen.getByRole("tooltip")` + `toHaveTextContent` 断言子串,更贴合 antd 的渲染形态。

## 验证摘要

- `npx vitest run src/components/turn/__tests__/GanttTimeline.test.tsx` → 5 passed。
- `npx vitest run`(全量) → **158 files / 1364 tests all passed**(零回归)。
- `npx tsc -b --noEmit` → 无输出(通过)。
- `npx vite build` → 构建成功,新 CSS 正常打进 `dist/assets/index-*.css`(13.00 kB,较改动前增量正常);唯一警告是存量的 chunk-size/dynamic-import 提示,与本次改动无关。

## 实现备注 / concerns

1. **本仓首个组件级 `.css` 文件**:grep 全 `apps/admin-ui/src`,此前每个组件(含 StepTimeline/ToolTimeline/FullTextModal 等)都是纯内联 `style={{...}}` + `var(--ew-*, #hex)`,零 `<style>` 标签、零 `.css` 文件先例。但 brief 明确要三样东西——① 时长标签「行 hover 才显」、② 入场 `scaleX` 动画、③ `prefers-reduced-motion` 关闭动画——这三样都要 `:hover` 伪类 / `@keyframes` / `@media`,内联 style 对象表达不了。权衡后新增同目录 `GanttTimeline.css`(Vite 原生支持,`vitest.config.ts` 的 `css:false` 让测试环境把它当空模块处理,不影响断言;真实 `vite build` 已验证能正常打包)。这是本任务唯一偏离"纯内联"既有惯例的地方,做了显式记录以便后续任务/评审知情。
2. **`prefers-reduced-motion` 没有走独立的 `@media` 覆盖动画名**,而是让入场/脉冲动画的 `animation-duration` 直接引用 `var(--ew-duration-slow)`,复用 `tokens.css` 里已存在的「reduced-motion 把 `--ew-duration-*` 归零」规则(见该文件第 338-344 行)——归零后 `animation-duration:0ms` 等价于关闭动画,不需要再写一份 `.ew-gantt-bar{animation:none}` 的媒体查询去重复表达同一件事;`GanttTimeline.css` 里仍保留了一份显式 `@media(prefers-reduced-motion:reduce){.ew-gantt-bar{animation:none}}` 作为双保险(两条规则同时生效,互不冲突)。
3. **`durationMs===null` 行的宽度语义是本任务自行定义的**(brief 只要求"渲染生长条 class 或中断态 class",没钉死具体宽度公式):选择「从 `startMs` 延伸到轴右边界」而不是零宽度,因为 Task 1 对这类行的原始 placement 就是零宽度(`start===end===prevEnd`),原样渲染在视觉上等于看不见。这个选择只影响 CSS/像素表现,不影响 5 条测试断言(测试只查 class 名)。Task 3 接线 `running` prop 时如果需要更精确的"当前时刻"宽度(设计稿提到的『每秒 tick 重算』),那是运行时数据层的活(GanttModel 目前没有 `t0Ms`/"now" 输入),本组件的职责边界到"给定一个 model 快照,按 `running` 布尔选样式"为止,与 brief 的 Task 2 范围一致。
4. 未做的东西(有意,YAGNI 对齐设计文档"不做"清单):不引图表库、不做时间轴缩放/拖拽、组件本身不含图例(legend)——brief 的 props 签名和 5 条测试都没提,归为 Task 3(TurnCard 接线)或压根不需要的范围,避免抢建。
