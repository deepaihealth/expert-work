# 内容 JSON 参考（content-schema）

这是渲染器唯一认识的输入格式。字段名、必填/可选、取值范围与脚本代码（`hpr/content.py` 的
`KIND_SCHEMAS`）逐一对应；渲染器只呈现你写进这里的文字，不会替你增删或改写。完整虚构样例见
`sample/sample-plan.json`。

## 顶层字段

| 字段 | 必填/可选 | 类型 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | 必填 | 固定字符串 | 目前固定写 `"1"` |
| `title` | 必填 | 非空文字 | 方案名称，出现在封面主标题 |
| `subtitle` | 可选 | 非空文字 | 封面副标题 |
| `period` | 可选 | 对象 `{label, weeks}` | `label` 必填、`weeks` 可选数字；`label` 会出现在封面「阶段」一栏 |
| `generated_at` | 必填 | 日期 `YYYY-MM-DD` | 生成日期 |
| `data_basis` | 可选 | 非空文字 | 封面「数据依据」一栏 |
| `client` | 必填 | 对象 `{name, facts[]}` | `name` 必填；`facts` 每项 `{label, value}`，出现在封面客户信息区 |
| `manager` | 可选 | 对象 `{name, title}` | `name` 必填；`title` 可选，缺省时封面显示为「负责人」 |
| `brand` | 可选 | 对象 `{org_name, logo_path, footer_signature, disclaimer}` | 四个字段都可选；只从这里读取，任何样式层都改不了（见 `style-options.md`） |
| `sections` | 必填，至少 1 项 | 数组 | 见下「章节与积木」 |

封面信息挤不下或互相重叠会渲染失败，报错位置是这四者之一：`title`（标题）、`subtitle`（副标题）、
`client.facts`（客户信息卡）、`brand`（LOGO/机构名称行），遇到时精简对应字段。`brand.disclaimer` 太长会把
PDF 页脚顶出页面底边，同样会报错（路径 `brand.disclaimer`），页脚最多约 4 行，请把免责声明控制在合理长度内。

## 章节与积木

每个章节：`{id, title, blocks: [...]}`。

- `id`：`^[a-z][a-z0-9_-]{0,31}$`（小写字母开头，只含小写字母/数字/`-`/`_`，≤32 位），同一份内容 JSON 内不能重复；
  用来在「样式与参数」里精确指定某个章节的版式。
- `title`：非空文字，出现在该章节每一页的页眉/页面标题（内容放不下要拆成多页时，续页沿用同一个标题，不会加「（续）」之类的后缀）。
- `blocks`：至少 1 个积木，每个积木 `{kind, id?, ...该 kind 的字段}`。
  `id` 可选，写了要在章节内唯一、格式同上；不写就按积木在章节里的序号（从 1 开始）定位。

每个积木必须是下面 24 种 `kind` 之一。除非另有说明，字段里的“文字”一律指非空字符串；“数字”不能是字符串。

### `summary` 方案摘要

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项 `{label, text}`（均为文字） |

示例：`{"kind": "summary", "items": [{"label": "核心问题", "text": "空腹血糖略高于参考范围。"}]}`

可选版式：`columns`（默认，卡片分栏）/ `list`（键值列表）。呈现说明：把要点摊平展示，不做归纳或增删。

### `profile` 健康画像

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项见下 |

每项：`name`（文字，必填）、`value`（数字或文字，必填）、`unit`（文字，可选）、`ref_low`/`ref_high`（数字，可选）、
`ref_text`（文字，可选，优先于 `ref_low`/`ref_high` 拼出的参考范围文案）、`position`（可选，枚举
`within`/`above`/`below`/`none`）。

示例：`{"kind": "profile", "items": [{"name": "空腹血糖", "value": 6.4, "unit": "mmol/L", "ref_low": 3.9, "ref_high": 6.1, "position": "above"}]}`

可选版式：`cards`（默认，指标卡+区间条）/ `table`（表格）。呈现说明：渲染器只呈现 `position`
对应的固定文案（在参考范围内/高于参考范围/低于参考范围），状态判断由你给出，不做计算或推断。

### `issues` 核心问题

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项 `{title, evidence?, level?}` |

`title` 文字必填；`evidence` 文字可选；`level` 可选枚举 `focus`/`watch`/`info`。

示例：`{"kind": "issues", "items": [{"title": "血糖偏高", "evidence": "近 30 天有 9 天超标", "level": "focus"}]}`

可选版式：`cards`（默认，编号卡片）/ `list`（编号列表）。

### `trend` 指标趋势

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `metric` | 必填 | 文字 |
| `unit` | 可选 | 文字 |
| `points` | 必填，≥2 项 | 数组，每项 `{date, value}` |
| `target_low` / `target_high` | 可选 | 数字 |
| `ref_text` | 可选 | 文字 |

`points[].date` 是文字标签（用作图表横轴，不校验日期格式），`points[].value` **必须是数字**。

示例：`{"kind": "trend", "metric": "空腹血糖", "unit": "mmol/L", "points": [{"date": "09-01", "value": 6.8}, {"date": "09-15", "value": 6.3}]}`

可选版式：`line`（默认，折线图，`target_low`/`target_high` 画作目标区间）/ `bar`（柱状图）。

### `goals` 阶段目标

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项见下 |

每项：`name`（文字，必填）、`current`（数字或文字，可选）、`target`（数字或文字，必填）、`unit`（文字，可选）、
`due`（文字，可选）、`note`（文字，可选）。

示例：`{"kind": "goals", "items": [{"name": "体重", "current": 77.0, "target": "75.5–76.0", "unit": "kg", "due": "2 周"}]}`

可选版式：`cards`（默认）/ `table`。

### `phases` 阶段计划

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项 `{label, focus}` |

`label` 文字必填；`focus` 必填数组（≥1 项），每项 `{area, text}`（均为文字）。

示例：`{"kind": "phases", "items": [{"label": "第 1 周", "focus": [{"area": "饮食", "text": "按 A 模板执行"}]}]}`

可选版式：`timeline`（默认，时间轴）/ `columns`（分栏）/ `table`（表格，各阶段共同的 `area` 会拆成列）。

### `nutrition` 营养处方

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `energy_kcal` | 可选 | 数字或文字 |
| `macros` | 可选，≥1 项 | 数组，每项 `{name, grams?, percent?}` |
| `meals` | 可选，≥1 项 | 数组，每项 `{name, percent?, note?}` |

`energy_kcal` / `macros` / `meals` 至少要有一个。`macros[].grams`/`percent` 是数字或文字均可，但用环形图版式时
**`percent` 必须是数字**，否则改用表格版式（`blocks.nutrition.variant` = `table`）。

示例：`{"kind": "nutrition", "energy_kcal": 1650, "macros": [{"name": "碳水化合物", "grams": 185, "percent": 45}]}`

可选版式：`donut`（默认，环形图+图例）/ `table`（表格）。

### `meal_plan` 餐单

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `templates` | 必填，≥1 项 | 数组，每项见下 |

每个模板：`name`（文字，可选）、`applies_to`（文字，可选，如「周一/三/五」）、`meals`（必填，≥1 项）。
每餐：`name`（文字，必填）、`time`（文字，可选）、`foods`（必填，≥1 项，每项 `{name, amount?}`）、`kcal`（数字或文字，可选）。

示例：`{"kind": "meal_plan", "templates": [{"name": "A 模板", "meals": [{"name": "早餐", "time": "7:30", "foods": [{"name": "杂粮馒头", "amount": "50 g"}], "kcal": 420}]}]}`

可选版式：`timeline`（默认，按餐次的时间轴）/ `table`（表格）/ `cards`（卡片）。

### `diet_rules` 饮食原则

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `recommend` / `limit` / `avoid` | 可选，≥1 项 | 文字数组 |
| `swaps` | 可选，≥1 项 | 数组，每项 `{from, to}`（均为文字） |

`recommend`/`limit`/`avoid`/`swaps` 至少要有一个。

示例：`{"kind": "diet_rules", "recommend": ["粗细搭配的主食"], "avoid": ["含糖饮料"], "swaps": [{"from": "白米饭", "to": "杂粮饭"}]}`

可选版式：`columns`（默认，推荐/限制/避免三栏+替换表）/ `list`（分组列表+替换表）。

### `exercise` 运动处方

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `fitt` | 可选 | 对象 `{frequency?, intensity?, time?, type?}`（四选一起步） |
| `schedule` | 可选，≥1 项 | 数组，每项 `{day, items}`（`items` 为文字数组，≥1 项） |
| `progression` | 可选 | 文字 |
| `cautions` | 可选，≥1 项 | 文字数组 |

`fitt`/`schedule`/`progression`/`cautions` 至少要有一个；`fitt` 内部四个字段也至少要有一个。

示例：`{"kind": "exercise", "fitt": {"frequency": "每周 5 次", "intensity": "中等"}, "cautions": ["运动中头晕立即停止"]}`

可选版式：`fitt`（默认，FITT 四格+安排+注意事项）/ `cards`（安排改为卡片）。

### `sleep` 睡眠

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `current` / `target` | 可选 | 对象 `{bed, wake}`（均为 `HH:MM`，必填） |
| `tips` | 可选，≥1 项 | 文字数组 |

`current`/`target`/`tips` 至少要有一个。

示例：`{"kind": "sleep", "current": {"bed": "00:30", "wake": "07:00"}, "target": {"bed": "23:00", "wake": "06:30"}}`

可选版式：`timebar`（默认，作息条对比）/ `list`（键值列表）。

### `stress` 压力与情绪

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `status` | 可选 | 文字 |
| `methods` | 可选，≥1 项 | 数组，每项 `{name, how?, frequency?}` |

`status`/`methods` 至少要有一个。

示例：`{"kind": "stress", "status": "工作压力较大。", "methods": [{"name": "腹式呼吸", "how": "吸 4 秒、呼 6 秒", "frequency": "每天 2 次"}]}`

可选版式：`cards`（默认）/ `list`。

### `habits` 生活习惯

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项见下 |

每项：`name`（文字，必填）、`current`（文字，可选）、`target`（文字，可选）、`how`（文字，可选）。

示例：`{"kind": "habits", "items": [{"name": "久坐", "current": "连续坐 3 小时", "target": "每小时起身 5 分钟"}]}`

可选版式：`cards`（默认）/ `table`。

### `material` 动作/产品素材

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `name` | 必填 | 文字 |
| `description` | 必填 | 文字 |
| `url` | 必填 | `http(s)` 链接 |
| `media_path` | 可选 | 文件路径 |

`media_path` 只用于 PPTX：指向一个存在、可读的 `.mp4` 文件，且版面留有足够空间时，会把视频真正嵌入为可播放
对象；文件不是 `.mp4`、文件不存在、空间不够，或者嵌入本身失败，都会降级为可点击链接并给出警告，不会中断渲染。
`url` 始终会同时渲染成一行可点击链接文字。PDF 不使用 `media_path`，只把 `name`/`description`/`url` 渲染成
可点击链接。

示例：`{"kind": "material", "name": "快走动作示范", "description": "示范视频说明快走姿势。", "url": "https://example.com/materials/brisk-walk"}`

唯一版式：`card`。

### `monitoring` 监测计划

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项见下 |

每项：`item`（文字，必填）、`frequency`（文字，可选）、`timing`（文字，可选）、`alert`（文字，可选，提醒阈值）。

示例：`{"kind": "monitoring", "items": [{"item": "空腹血糖", "frequency": "每周 4 次", "alert": "≥ 7.0 连续 2 次请联系管理师"}]}`

可选版式：`table`（默认）/ `cards`。

### `referral` 就医提醒

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `text` | 必填 | 文字 |

示例：`{"kind": "referral", "text": "如出现持续口渴多尿，建议先就医确认。"}`

唯一版式：`box`（警示提示框）。

### `shopping` 采购清单

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `groups` | 必填，≥1 项 | 数组，每项 `{name, items}`（`items` 为文字数组，≥1 项） |

示例：`{"kind": "shopping", "groups": [{"name": "主食", "items": ["燕麦", "荞麦面"]}]}`

可选版式：`columns`（默认，按组分栏）/ `list`（分组列表）。

### `follow_up` 随访安排

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项 `{label, value}`（均为文字） |

示例：`{"kind": "follow_up", "items": [{"label": "复评日期", "value": "2026-10-12"}]}`

可选版式：`cards`（默认）/ `table`（键值列表）。

### `paragraph` 段落

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `text` | 必填 | 文字（可含换行） |

示例：`{"kind": "paragraph", "text": "家人可协助准备清淡餐食。"}`

可选版式：`plain`（默认）/ `boxed`（带底色）。

### `bullets` 要点

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 文字数组 |

示例：`{"kind": "bullets", "items": ["按时记录数据", "两周后复盘调整"]}`

可选版式：`dots`（默认，圆点）/ `numbers`（编号）/ `checks`（打勾）。

### `table` 表格

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `columns` | 必填，≥1 项 | 文字数组 |
| `rows` | 必填，≥1 项 | 数组，每行为文字数组 |

**每行的列数必须等于 `columns` 的项数**；空单元格写 `—`，不要留空字符串。

示例：`{"kind": "table", "columns": ["日期", "体重"], "rows": [["第 1 天", "—"], ["第 2 天", "—"]]}`

唯一版式：`table`。

### `kv` 键值

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `items` | 必填，≥1 项 | 数组，每项 `{label, value}`（均为文字） |

示例：`{"kind": "kv", "items": [{"label": "反馈方式", "value": "企业微信"}]}`

可选版式：`two-column`（默认）/ `one-column`。

### `image` 图片

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `path` | 必填 | 文件路径 |
| `caption` | 可选 | 文字 |

`path` 是相对内容 JSON 所在目录的路径（也可以写绝对路径）。**文件找不到或无法解码（损坏/格式不支持/像素过大）
会导致渲染失败**，错误信息会指出是哪个积木（如 `sections[2].blocks[1]`）；这与 LOGO/素材图不同，图片积木缺
图不会被静默跳过。

示例：`{"kind": "image", "path": "trend-note.png", "caption": "示例配图（虚构）"}`

唯一版式：`fit`（等比缩放）。

### `callout` 提示框

| 字段 | 必填/可选 | 类型 |
| --- | --- | --- |
| `level` | 可选 | 枚举 `info` / `warn` |
| `title` | 可选 | 文字 |
| `text` | 必填 | 文字（可含换行） |

示例：`{"kind": "callout", "level": "warn", "title": "提醒", "text": "方案执行中如有不适请暂停并联系健康管理师。"}`

唯一版式：`box`。

## 特别说明

- 渲染器只呈现：状态（`position`：`within` / `above` / `below` / `none`）、参考范围、目标值、阈值都由你给出，
  渲染器按你给的显示为「在参考范围内 / 高于参考范围 / 低于参考范围」，不做判断。
- `trend` 的 `points[].value`、`nutrition` 环形图的 `percent` 必须是数字；做不到时改用表格版式
  （`blocks.nutrition.variant` = `table`）。
- `image.path`、`material.media_path`、`brand.logo_path` 是相对内容 JSON 所在目录的路径；图片缺失或无法解码
  会报错并中断渲染，LOGO / 素材图缺失只降级并警告、不中断渲染。
- 表格每行列数必须等于 `columns`；空单元格写 `—`。
