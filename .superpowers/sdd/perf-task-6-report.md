# Task 6: TraceView 分解条 + 配色分组 —— 报告

## 状态

DONE

## commit

`7c6ca67b` — `feat(admin-ui): TraceView 首字分解条 + 入口链 span 配色分组`(13 files changed, 179 insertions, 2 deletions)

## 做了什么

按 Step 1-9 逐字实施：

1. `entry_breakdown.test.ts`(新)—— 命门测试原样照抄：只取顶层 entry span（`parentId` 不在本组内的）、以首个 llm span 收口、无 entry span 时返回空数组。
2. `TraceSpan.group: "entry" | null` 加进 `api/trace_facade.ts`。
3. `entry_breakdown.ts`(新)—— `buildBreakdown` 纯函数，原样照抄。
4. `EntryBreakdown.tsx`(新)—— `<button>` 分段条组件，原样照抄；点击复用 `TraceTree` 既有的 `selectedId`/`setSelectedId`，未新增状态。
5. i18n：grep `breakdown` 确认零撞键后，两个 locale 各加了一个新的顶层命名空间 `trace: { breakdown_title }`（zh-CN "首字 {{d}}"，en "First output {{d}}"）。选用新命名空间而非塞进 `playground`（该文件里 `TraceView.tsx` 现有 key 全在 `playground.tr_*` 下），是因为 Step 6 组件代码里写的是 `t("trace.breakdown_title", ...)`，逐字照用；确认了这不撞 `zh-CN.ts:1257` 的 `playground.ttft`（那个在 `turn` 语境下，两者语义分开演化，未复用）。
6. `TraceView.tsx`：`<EntryBreakdown>` 挂进 `TraceTree` 返回的最外层 `<div>` 顶部（网格表头之前）；新增 `ENTRY` 颜色常量 `var(--ew-trace-entry, #7c8cff)`，与 `EntryBreakdown.tsx` 按钮背景色同一个 var+fallback，保证分解条和瀑布图配色一致；`kindDotColor`/`kindBarColor` 在 `kind === "tool"` 分支之后加 `group === "entry"` 分支，返回 `ENTRY`。

## 一处主动决定：颜色 token 没有写进 tokens.css

Step 7 说 ENTRY 要"过双主题（照文件里既有色值常量的定义方式加）"。`ACCENT`/`SUCCESS`/`WARNING`/`DANGER`/`PURPLE` 这几个既有常量背后的 CSS 变量（`--ew-text-info` 等）确实在 `theme/tokens.css` 的 dark/light 两个块里都有定义。但 Task 6 的 Files 清单没有列 `tokens.css`，按"只碰清单内文件"的原则没有新增 token 定义，只是让 `ENTRY` 复用 `EntryBreakdown.tsx` 里已经写死的同一个 fallback 十六进制（`#7c8cff`，靛蓝色，冷暖/色相上跟蓝色 llm、紫色 tool 都拉得开），两处引用同一个未定义的 CSS 变量名，视觉上分解条和瀑布行是同一个颜色。这个色值在两个主题背景下都可读，但严格说不是"随主题切换而变化的两套色阶"，是同一个静态色。如果这不符合预期，需要的改动是在 `tokens.css` 的 `:root,[data-theme="dark"]` 和 `[data-theme="light"]` 两块里补 `--ew-trace-entry` 的具体阶数。

## 命门测试的验证

第一个测试（顶层 vs 子 span）是这个 task 的核心断言。手动验证过反向：如果 `buildBreakdown` 不做 `parentId` 过滤，直接把所有 `group === "entry"` 的 span 都当顶层，这个测试会因为 `out` 包含 `["记忆召回", "向量化"]`（2 项，而非期望的 1 项）而失败——即测试确实卡住了这条命门,不是摆设。

## 三条校验命令

1. `vitest run .../entry_breakdown.test.ts` —— PASS（3/3，Step 2 先跑确认 FAIL：模块不存在；Step 4 写完实现后复跑 PASS）
2. `tsc -b --noEmit` —— 干净，0 错误
3. `vitest run`（全量）—— 153 test files / 1300 tests 全绿，无回归

## 因新增必填字段引发的连带修复（范围外但被迫改）

`TraceSpan.group` 是必填字段（非 optional），加了之后 tsc 报了 7 个既有测试文件的 fixture 缺字段 / 类型不兼容：

- `api/__tests__/trace_facade.test.ts`（2 处字面量 span，补 `group: null`）
- `pages/__tests__/PlaygroundTab.test.tsx`（4 处字面量 span，补 `group: null`）
- `playground/__tests__/trace_banner.test.ts`、`trace_purpose.test.ts`、`TraceView.test.tsx`（`makeSpan` 工厂函数，补 `group: over.group ?? null`）
- `playground/__tests__/trace_tree.test.ts`（`span()` 工厂函数，补 `group: null`）

这些都不在 Task 6 Files 清单里，但不改就是全新的 tsc 报错（不是既有的 stale 诊断噪音，是本次改动直接引发的真实编译错误），按"清理自己改动造成的孤儿/破坏"处理，全部补 `group: null`，没有动这些文件的其它内容。

## 顾虑

1. 上面提到的 `--ew-trace-entry` 未落 `tokens.css` 的决定——如果 coordinator 认为"过双主题"是字面要求要有真正随主题变化的两套色阶，需要补一次小改动。
2. 没有为 `EntryBreakdown` 组件本身、`TraceView.tsx` 的配色分支新增单独的渲染测试（Task 6 的 Files 清单里 Test 只列了 `entry_breakdown.test.ts`）——全量 vitest 跑过，既有 `TraceView.test.tsx` 没有因为新增的顶部分解条渲染而失败（说明 DOM 结构没有破坏既有断言的选择器），但没有专门断言"入口链 span 确实渲染成 ENTRY 颜色"或"分解条点击会联动选中"这类新行为的正面用例，纯粹按计划字面执行。
