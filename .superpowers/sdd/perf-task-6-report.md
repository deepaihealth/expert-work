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

## 顾虑（初版，均已在下方追加 commit 中修掉）

1. ~~上面提到的 `--ew-trace-entry` 未落 `tokens.css` 的决定~~ —— 已修，见下。
2. ~~没有为配色分支 / 点击选中接线补测试~~ —— 已修，见下。

---

## 追加：色令牌双主题化 + 配色/点击选中测试（commit `43902cf4`）

Coordinator 复核后指出这两个 concern 都是真的，且 ①有本仓库先例（`tokens.css:224-227` 那条注释描述的正是同一类"hex fallback 不随主题变"的老毛病，之前已经修过一次），授权改 `tokens.css`（它不在 Task 6 原 Files 清单，是 brief 的疏漏）。改法：

### ① `--ew-trace-entry` 双主题令牌化

- **原色阶区**（`tokens.css` 第 81 行前）新增一组 indigo 5 阶，跟 `success`/`warning`/`danger` 现有的 100/300/500/700/900 定义方式（含数值来源——Tailwind 默认色板）保持一致：
  ```
  --ew-color-indigo-100: #e0e7ff;
  --ew-color-indigo-300: #a5b4fc;
  --ew-color-indigo-500: #6366f1;
  --ew-color-indigo-700: #4338ca;
  --ew-color-indigo-900: #312e81;
  ```
  选 indigo 而非复用 brand/accent/success/warning/danger 任何一个既有色相：跟 TraceView.tsx 里 llm(brand cyan)、tool(accent violet)都要拉得开是 Task 6 本来的判断，成功/警示/危险三色又各有既有语义（用在别处会引起混淆），只有新开一相最干净。
- **语义层**：dark 主题（`:root, html[data-theme="dark"]`）取 `--ew-color-indigo-300`（浅），light 主题（`html[data-theme="light"]`）取 `--ew-color-indigo-700`（深）。这个"浅一档给暗背景、深一档给亮背景"的两步跳（跳过中间的 500）是抄 `success`/`warning`/`danger` 自己的现有模式（它们的 `--ew-status-*-fg` 就是 dark 取 -300、light 取 -700），不是抄 `--ew-accent-violet` 的 400/600（那是另一个 12 阶色阶,阶数体系不同,硬套档位数字没意义,套"浅/深不对称"这条设计原则才是对的）。
- `TraceView.tsx` 的 `ENTRY` 常量字面量没动（还是 `var(--ew-trace-entry, #7c8cff)`），因为现在 `--ew-trace-entry` 有真实定义了，fallback 十六进制只在变量意外未加载时才会生效，保留它纯粹是防御性的，跟其它几个常量（`ACCENT`/`SUCCESS` 等）的 fallback 惯例一致。

### ② 配色分支 + 点击选中接线测试

- `TraceView.tsx` 的 `kindDotColor`/`kindBarColor` 从模块私有函数改成 `export`（唯一的生产代码改动），供测试直接调用，不用整个渲染组件去反推颜色。
- `TraceView.test.tsx` 追加一个新 `describe("kindDotColor / kindBarColor")` 块：构造 llm/tool/entry-group/plain 四种 span，断言 entry-group 的 dot 颜色和 bar 颜色都跟另外三种互不相同。
- 新建 `EntryBreakdown.test.tsx`：渲染 `<EntryBreakdown>`（一个 entry span + 一个 llm span，两个按钮），分别点击两个按钮，断言 `onSelect` 收到的是被点击那个按钮自己的 span id（先点右边的按钮断言收到 `"l"`，再点左边的断言收到 `"r"`——不是"点了就总收到同一个 id"这种会被退化实现蒙混过关的弱断言）；另加一条"无 entry span 时不渲染"的空状态用例。

### 校验命令（同步跑）

1. `vitest run`（全量）—— **154 test files / 1303 tests 全绿**（比上一版多 1 个文件、3 个测试，新增的 `EntryBreakdown.test.tsx` 2 条 + `TraceView.test.tsx` 追加的 1 条），无回归
2. `tsc -b --noEmit` —— 干净，0 错误

### 最终色阶选择

dark 主题 `--ew-trace-entry: var(--ew-color-indigo-300)`（`#a5b4fc`），light 主题 `--ew-trace-entry: var(--ew-color-indigo-700)`（`#4338ca`）。

### commit

`43902cf4` — `fix(admin-ui): trace-entry 色令牌双主题化 + 配色/点击选中测试补全`(4 files changed, 101 insertions, 3 deletions)，父提交 `501237ef`。

### 顺带确认

Coordinator 指出的"7 个既有测试文件补 `group` fixture 是类型变更的必然后果、`TraceSpan.group` 设成 required 而非 optional 是对的"——认同，`_span_as_dict`（control-plane facade）无条件带这个字段,没有"可能不返回"的分支,required 能让以后哪个 mock/fixture 漏传时被 tsc 当场抓住,而不是运行时才发现 `undefined` 被当成合法值传下去。
