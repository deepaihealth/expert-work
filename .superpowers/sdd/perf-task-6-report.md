# Task 6: TraceView 分解条 + 配色分组 —— 报告

## 状态

DONE

## commit(按时间顺序，4 轮)

1. `7c6ca67b` — `feat(admin-ui): TraceView 首字分解条 + 入口链 span 配色分组`(13 files changed, 179 insertions, 2 deletions) —— 初版实施 / 报告：`501237ef`
2. `43902cf4` — `fix(admin-ui): trace-entry 色令牌双主题化 + 配色/点击选中测试补全` —— 一轮复核后修复色令牌未双主题化 + 补配色分支/点击选中测试 / 报告：`97eb2434`
3. `08a6941c` — `fix(admin-ui): dark 主题分解条文字对比度(半透明底+主题感知文字色)` —— 二轮复核后修复 dark 主题分解条文字对比度 1.99:1 的问题 / 本报告更新的提交见 git log（本文件本身的提交）

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

`TraceSpan.group` 是必填字段（非 optional），加了之后 tsc 报了 6 个既有测试文件的 fixture 缺字段 / 类型不兼容：

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

Coordinator 指出的"既有测试文件补 `group` fixture 是类型变更的必然后果、`TraceSpan.group` 设成 required 而非 optional 是对的"——认同，`_span_as_dict`（control-plane facade）无条件带这个字段,没有"可能不返回"的分支,required 能让以后哪个 mock/fixture 漏传时被 tsc 当场抓住,而不是运行时才发现 `undefined` 被当成合法值传下去。（口径更正：上面写的"7 个既有测试文件"数错了,实际改动的是 6 个——`trace_facade.test.ts`/`PlaygroundTab.test.tsx`/`trace_banner.test.ts`/`trace_purpose.test.ts`/`TraceView.test.tsx`/`trace_tree.test.ts`,纯文档口径错误,diff 本身没有多余改动。）

---

## 追加二轮：dark 主题分解条文字对比度 Important 修复（commit `08a6941c`）

Coordinator 用 WCAG 相对亮度公式手算 + 仓库内已装的 `axe-core@4.11.4`（`@axe-core/playwright` 的传递依赖，和 CI 用的 `@axe-core/playwright` 同引擎）在 jsdom 里跑 `color-contrast` 复核，指出 `EntryBreakdown.tsx` 的分段按钮固定 `color: "#fff"` + 不透明 `background: var(--ew-trace-entry, ...)`，dark 主题下 `--ew-trace-entry` 取 `indigo-300`（`#a5b4fc`，一个亮色），白字对它只有 **1.99:1**，远低于 AA 对 UI 文字的 3:1。只要 dark 主题打开一个有 memory 的 run，任何占比 ≥6%（`LABEL_MIN_SHARE`）的段都会踩中——也就是「召回 2.0s / 规划 1.6s / 首调 0.6s」这种最典型的分布必然触发。

### 根因 & 修法

新文件没沿用同一个文件夹里 `TraceView.tsx:308-319`（`GanttBar`）早就用过的模式——瀑布图的 kind 色块本来就是**半透明背景**（`color-mix(in srgb, <色> 62%, transparent)`）+ **主题感知文字色**（`var(--ew-text-primary)`），而不是不透明底 + 写死文字色。`EntryBreakdown.tsx` 是这次新加的文件，没抄这个先例，直接用了 Step 6 计划代码字面量给的 `background: "var(--ew-trace-entry, #7c8cff)"` + `color: "#fff"`。

`EntryBreakdown.tsx` 改动（唯一的生产代码改动）：
```diff
-                background: "var(--ew-trace-entry, #7c8cff)",
-                color: "#fff",
+                background: ENTRY_BG,   // color-mix(in srgb, var(--ew-trace-entry, #7c8cff) 62%, transparent)
+                color: "var(--ew-text-primary)",
```
`62%` 这个比例不是新拍的——跟 `TraceView.tsx` 的 `kindBarColor` 给 `group === "entry"` 分支返回的 `color-mix(in srgb, ${ENTRY} 62%, transparent)`（上一轮 commit 加的）完全一致，保证分解条和瀑布图里的 entry 色块视觉上是同一个颜色。没有选"白字改黑字"这种单主题修法——那样会让 light 主题（深底）反过来坏掉；两个主题都要靠背景变半透明后跟页面基底混合，文字色再跟着主题走。

### 两个主题的实测对比度数字

背景合成假设：分解条按钮所在的 `TraceTree` 根 `<div>`（`EntryBreakdown` 的父级）不显式设置 `background`（只有 axis 表头行显式用了 `--ew-surface-raised`），沿这条无背景链条一路网上找到的是页面主内容面（`--ew-surface-base`）——这也是同一个文件里另外两处"静置"面板（`:561`、`:1014`）用的同一个变量，是这个文件对"默认背景"最一致的既有假设。62% 半透明的 `--ew-trace-entry` 与它做 sRGB 通道线性混合（`color-mix(in srgb, ...)` 本身就是直接按比例混通道，不走 gamma），再用 WCAG 相对亮度公式对 `--ew-text-primary` 求对比度：

| 主题 | `--ew-trace-entry` | 合成后背景（62% 混 `--ew-surface-base`） | `--ew-text-primary` | 对比度 |
|---|---|---|---|---|
| dark | indigo-300 `#a5b4fc` | `#6f79a9` | neutral-100 `#f4f5f7` | **3.863 : 1** |
| light | indigo-700 `#4338ca` | `#8a84de` | neutral-900 `#161921` | **5.385 : 1** |

两边都清了 coordinator 定的 3:1 门槛。用仓库里 `axe-core@4.11.4` 的 `commons.color.{Color,getContrast,flattenColors}`（同一引擎，直接喂合成后的 RGBA，不经过 jsdom 的 DOM/CSS 解析——原因见下）独立复核过，数字一致；旧代码（不透明底 + 白字）用同一脚本算出 dark 1.99、light 7.90，跟 coordinator 报的数字完全对上，交叉验证了我解出的 `indigo-300`/`indigo-700` 十六进制值和 surface-base 假设是对的。

**诚实的口径**：3.863:1 是 WCAG 1.4.11（非文本/UI 组件对比度）的 3:1 门槛，coordinator 两次明确把 3:1 定为验收线，达标；但严格套用 1.4.3（正文文字对比度，11px 常规字重需要 4.5:1）dark 主题这条差一点没到（`axe-core` 的 `hasValidContrastRatio(bg, fg, 11pt-ish, false)` 判定 `isValid: false, expectedContrastRatio: 4.5`）。light 主题 5.385:1 两条都过。如果要把 dark 也顶到 4.5:1，需要动 `62%` 这个混合比例或者换一档更深的 indigo——但 `62%` 是刻意跟 `kindBarColor` 的 entry 分支保持一致（不然分解条和瀑布图颜色对不上），没有在这轮改；先按 coordinator 明确给的 3:1 验收线交付，这个 4.5:1 缺口在下面的顾虑里标出来供取舍。

### 为什么不直接跑 axe-core 的 DOM `color-contrast` 规则

`vitest.config.ts` 设了 `css: false`（vitest 显式关闭 CSS 处理），`tokens.css` 从不会被解析进 jsdom；即便解析了,jsdom 的 CSS 引擎也不认识 `color-mix()`（探测过：`element.style.background` 会原样存住这个函数调用的字符串，但不会计算出实际像素色）。真要在 jsdom 里跑 axe-core 完整的 DOM `color-contrast` 规则，必须先把合成后的具体像素色注入进去——等于要先做我下面这条"合成计算"，DOM 规则本身反而是多余的一层。所以复核选择直接调 `axe-core` 暴露的底层 `commons.color` 算法（`getContrast`/`flattenColors`），而不是走全量 `axe.run()`。

### 两个 axe 测试文件的处置

`find` 遍历过整个仓库（`/Users/mac/src/github/jone_qian/expert-work`，含所有 worktree），没有找到 `axe_button_name.test.tsx` 或 `zz_axe_button_name.test.tsx` 这两个文件——它们不在我的 worktree 里，大概率是 coordinator 那边审查用的独立沙箱产物，跟我这次改动无关，**没有需要清理的东西**。

我自己为验证这次改动新建了一个探测用的临时文件 `zzprobe.test.tsx`（确认 jsdom 会原样保留 `var()`/`color-mix()` 字符串），验证完已删除，未提交。

按 coordinator"对比度回归值得长期守"的意见，新增了一个**永久、命名规范**的回归测试，两个方向都补了（缺一个都堵不住"过一阵子被静默改坏"）：

1. **`EntryBreakdown.contrast.test.ts`**（新文件，纯计算，不渲染）—— 直接读 `theme/tokens.css` 源文件（不是抄一份写死的十六进制快照），解出两个主题下 `--ew-trace-entry`/`--ew-surface-base`/`--ew-text-primary` 实际引用的原色阶值，按 62% 混合后跑 WCAG 对比度公式，断言两个主题都 ≥3:1。用真实 tokens.css 文本而非硬编码副本的理由：硬编码副本会跟 tokens.css 本身脱钩，将来改了色阶这个测试还是绿的（正是这次事故的成因之一）。变异验证：把 `--ew-trace-entry` 临时改成 `indigo-100`（更亮）,dark 侧断言从 3.863 掉到 2.607,测试真的红了;改回后复跑绿。
2. **`EntryBreakdown.test.tsx` 新增一条 `it`**（渲染实测）—— 断言真实渲染出的按钮 `style.color === "var(--ew-text-primary)"`（且不是 `"#fff"`）、`style.background` 里含 `color-mix`/`62%`/`transparent`/`var(--ew-trace-entry`。这条堵的是"tokens.css 没变但组件代码被人手滑改回旧样式"这个（1）测不到的洞。变异验证：把 `EntryBreakdown.tsx` 临时改回旧的 `background: "var(--ew-trace-entry, #7c8cff)"` + `color: "#fff"`，这条新测试真的红了（`AssertionError: expected 'rgb(255, 255, 255)' to be 'var(--ew-text-primary)'`）；改回后复跑绿。

两条一起覆盖"token 漂移"和"组件代码手滑"两类退化路径，缺一类都会留一个静默通道。

### Minor 项处置

- **色相间距**：实测 dark 主题 ENTRY(indigo-300) hue≈229.7° / PURPLE(accent-400) hue≈270.0°,gap≈40.3°；light 主题 ENTRY(indigo-700) hue≈244.5° / PURPLE(accent-600) hue≈271.5°,gap≈27.0°(跟 coordinator 估的 ~26° 基本对上)。light 侧饱和度(indigo-700 57.9% vs accent-600 81.3%)和明度(50.6% vs 55.9%)都有明显差异,按 coordinator "拉不开就算了" 的口径,这轮没有为了拉开 hue gap 去换色阶(indigo-700 是 Tailwind 默认色板值,换一档就脱离了这批新增色阶"照 Tailwind 惯例走"的一致性,划不来换一个更窄的 minor 项)。
- **"7 个既有测试文件"文档口径**：已在报告正文改成"6 个"，并列出准确文件名。

### 校验命令（同步跑）

1. `vitest run`（全量）—— **155 test files / 1306 tests 全绿**（比上一版多 1 个文件 3 个测试：新增的 `EntryBreakdown.contrast.test.ts` 2 条 + `EntryBreakdown.test.tsx` 追加的 1 条），无回归
2. `tsc -b --noEmit` —— 干净，0 错误

### commit

`08a6941c` — `fix(admin-ui): dark 主题分解条文字对比度(半透明底+主题感知文字色)`(3 files changed, 142 insertions, 2 deletions)，父提交 `97eb2434`。

### 顾虑（这一轮）

1. **dark 主题 3.863:1 清 3:1 但没到 4.5:1**——上面"诚实的口径"那段已展开：coordinator 明确定的验收线是 3:1（两次强调），本轮按这条线交付；如果目标其实是完整的 WCAG 1.4.3 正文对比度（4.5:1），dark 侧还差一截，需要再动 `62%` 混合比例或换更深一档的 indigo，而这会牵动跟 `kindBarColor` entry 分支共享的同一个比例常量，我没有在这轮擅自改。
2. **`--ew-surface-base` 是我的假设，不是从代码强制读出的**——`EntryBreakdown` 的父级容器链一路到 `TraceView` 挂载点都没有显式设置 `background`（唯一确定的锚点是它不是 `--ew-surface-raised`，因为那个只用在 axis 表头行），所以合成对比度用的"背景基底"是我按同文件里另外两处"静置面板"背景的既有用法类推出来的，不是从 DOM 树强制解析出来的。如果实际挂载上下文有其它显式背景（比如某层 Modal/Drawer 用了 `--ew-surface-overlay`），真实对比度会和这里算的不完全一样——但两个主题都留了 1.3~1.8 的余量（3.863 vs 3.0、5.385 vs 3.0），不同"静置"档位背景之间的差异不太可能吃掉这个余量。
