# 样式参数参考（style-options）

样式只管「长什么样」，不碰内容文字。所有样式来源（本次对话要求、系统注入的样式配置、个人默认文件……）
都先各自写成一个 JSON 小文件，再按优先级从高到低传给 `render.py --style`（可传多个 `--style`）。

## 1. 叠加规则

- 多个 `--style` 文件按传入顺序从高到低叠加；同一个键，取排在最前面（优先级最高）的那一层里**生效**的值。
- 某一层某个键的值无法识别、或超出取值范围，该层该键就不生效，自动落到下一层，直到平台默认值；这条记录会出现
  在渲染输出的 `not_applied` 里（状态见第 4 节）。
- 品牌四项 `brand.org_name` / `brand.logo_path` / `brand.footer_signature` / `brand.disclaimer`
  **只能写在内容 JSON 的 `brand` 里**；样式层里出现同名键一律不生效（状态 `brand_locked`）。

## 2. 可调项全表

| 键 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `color.primary` | `#RRGGBB` 或颜色名（如「深蓝」） | `#0B4F5C` | 主色；与背景对比不够时会被自动加深（色相不变）。状态色（正常/超出/警示）的色条、色块不变；用作文字时若在浅底上不够清楚，会自动取同色相的深一点的颜色 |
| `color.accent` | 同上 | `#E8A33D` | 强调色（封面装饰、小标题左侧竖线等） |
| `color.background` | 同上，或「浅色调」 | `#FFFFFF` | 背景色；只支持浅色，太深或不够亮会被自动调浅 |
| `type.scale` | `compact` / `standard` / `large` | `standard` | 字号档位 |
| `layout.density` | `compact` / `standard` / `airy` | `standard` | 版面密度（行距/间距） |
| `brand.logo_position` | `top-left` / `top-right` / `bottom-left` / `bottom-right` / `center` | `top-left` | 封面 LOGO 位置；LOGO 会自动裁掉透明边距，落在深色底上时自动加白色底板，不用预先处理 LOGO 图片 |
| `brand.org_position` | 同上五选一 | `top-left` | 封面机构名称位置 |
| `footer.align` | `left` / `center` | `left` | 页脚（署名/免责声明）对齐方式，PPTX 与 PDF 都生效 |
| `footer.page_number` | 开 / 关 | 开 | 是否显示页码 |
| `cover.variant` | `band` / `split` / `minimal` | `band` | 封面版式 |
| `toc` | `auto` / `on` / `off` | `auto` | 目录；`auto` 时 PDF 章节数 ≥ 6、PPTX 章节数 ≥ 8 自动加目录（两种格式阈值不同） |
| `section.icons` | 开 / 关 | 开 | 章节标题旁是否显示图标 |
| `output.formats` | `pptx` / `pdf` / `both` | `pptx`（`render.py --format` 会覆盖这一项） | 输出格式 |

## 3. 版式键（每类积木的展示方式）

三种写法，按优先级从高到低：

1. `sections.<章节id>.<积木id或序号>.variant` —— 只改某个章节里的某一个积木（有 `id` 用 `id`，没有就用它
   在章节里的序号，从 1 开始）
2. `sections.<章节id>.variant` —— 改某个章节里所有积木的版式
3. `blocks.<kind>.variant` —— 改某一类积木在全篇的默认版式

三层都没设时，用该积木类型的第一个版式（即默认版式）；每种 `kind` 支持哪些版式见
`reference/content-schema.md`。

## 4. 大白话对照表

以下是常见口语和它对应的键、取值，脚本会自动识别（还认识很多同义词，不限于表内这些）：

| 用户说的 | 对应键 | 取值 |
| --- | --- | --- |
| 「字太小 / 大一点 / 适合老年人」 | `type.scale` | `大一点`（相对当前档升一档）或直接 `large` |
| 「太挤了 / 留白多一点 / 宽松一点」 | `layout.density` | `airy` |
| 「换成蓝色 / 用深蓝」 | `color.primary` | `深蓝`（或任意 `#RRGGBB`） |
| 「背景浅一点 / 浅灰底」 | `color.background` | `浅灰`（背景只支持浅色，深色会被自动调浅） |
| 「LOGO 放右上」 | `brand.logo_position` | `右上角` |
| 「饮食那页改成表格」 | `sections.<饮食章节id>.variant` | `表格` |
| 「餐单都用时间轴」 | `blocks.meal_plan.variant` | `时间轴` |
| 「不要页码」 | `footer.page_number` | `关` |
| 「不要目录」 | `toc` | `不要` |
| 「封面简洁一点」 | `cover.variant` | `极简` |
| 「只要 PDF / 两个都要」 | `output.formats` | `pdf` / `都要` |

颜色除了中文色名（如「深青」「暖橙」「浅灰」），也接受 `#RRGGBB`、`#RGB`、`rgb(r, g, b)`。版式词（如
「卡片」「表格」「时间轴」「分栏」）在 `sections.*.variant` / `blocks.*.variant` 里同样通用。

## 5. 参数落实清单（渲染输出 `not_applied` / `params-report.json`）

每条参数处理结果有以下状态之一，回复用户时按这份表来取舍：

| 状态 | 含义 | 要不要告诉用户 |
| --- | --- | --- |
| `applied` | 按给的值原样生效 | 不必说 |
| `adjusted` | 为满足可读性/展示要求被自动改了（如背景调浅、主色加深到对比度达标） | 必须说，并说明改成了什么值、为什么 |
| `out_of_range` | 值无法识别或超出取值范围，未生效，已回退到下一层/默认值 | 必须说，并说明原因 |
| `brand_locked` | 写在样式层里的品牌项不会生效，只认内容 JSON 里的 `brand` | 必须说 |
| `overridden` | 被优先级更高的同名键覆盖 | 只在用户问起时说 |

## 6. 个人默认样式

用户明确说「以后都这样」，才把某个键写进个人默认文件 `report-style/personal.json`（只存这些明确要求长期
生效的键），维护方式：

```
python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/resolve_style.py \
  --merge-into report-style/personal.json --set 键=值 --unset 不要的键
```

文件里保存的是用户当时说的原话值（如「深蓝」），不是换算后的十六进制色号；渲染时仍会照常规做值校验与换算。
一次性的临时要求不要写进这个文件，只放进本次渲染的样式层。
