# 健康方案交付件平台技能（health-plan-report）设计

- 日期：2026-09-28
- 状态：设计已逐块口头确认，待审阅书面稿
- 关联：`platform-skills/`（B-116 office 技能同构）、ai-health-plan 提示词（rev38）

## 0. 背景与问题

ai-health-plan 目前的成品链路是：模型先写方案 JSON，再**由模型现场手写一份约 3 万字符的渲染脚本**
`style/render_plan.py`（每位员工首次生成时写，此后作为「风格锚」锁定复用），用它出 PPT / PDF。

2026-09-27 测试环境一份实际产物（run `88a94ad6`，9 页 PPT）暴露的问题：

1. **硬 bug**：脚本把英寸换算了两次（`_box(slide, 0.75, Inches(0.48), Inches(10.5), …)`），
   8 页内页标题框位置约 43 万英寸、宽约 960 万英寸，标题在 LibreOffice 竖排、在 PowerPoint 大概率不在页面上。
2. **没有看图核验**：该 run 只抽文字比对，没调用 read_page / ask_image；同时段另一 run 做了核验——执行不稳定。
3. **又挤又空**：正文 11pt、页脚 8pt；第 3–5 页下方约 40% 空白。
4. **字体写死微软雅黑**，沙箱与 Mac 客户端都没有该字体。
5. 提示词「默认视觉基线」20 余条审美规则明令禁止的装饰竖条、阴影仍然出现——**规则写得再细，模型手写代码也执行不到位**。
6. **坏脚本被锁成风格锚**，该员工以后每次都复用；生产上每位员工都有同样的抽签风险。
7. 缺少专业方案应有的元素：趋势图、目标对比、一页摘要、随访安排。

外部参考（他人编写的 health-plan-delivery skill）的可取之处是**分工**：模型只写结构化内容 JSON，
版式由固定、经过测试的代码决定，并自动核对内容无丢失、自动分页。但其视觉仅达「干净的模板」，
且依赖 Node + Chromium + PPT Master（沙箱无 Node）。本设计**借鉴其分工思路，不复制其代码**
（该 skill 未附许可证；与 `platform-skills/README.md` 的 clean-room 规则一致）。

## 1. 目标与非目标

### 技能边界（首要原则）

本技能是**健康方案交付件的设计规范及其执行程序**，只负责「把已定稿的内容呈现成专业的 PPT / PDF」，
**不参与健康方案的制定**：

- 不拉数、不做任何健康判断、不生成 / 改写 / 补充 / 删减 / 重排任何健康内容；
- 不内置任何健康业务规则（红线、阈值、措辞要求、板块取舍都属于调用方 Agent）；
- 内容 JSON 的每个字、每个章节与积木的顺序都按调用方给的原样呈现；技能能改变的只有「长什么样」。

下文任何条目与本原则冲突时，以本原则为准。

### 目标

- 新增一个**平台技能** `health-plan-report`：把「已确定的健康管理方案内容」渲染成企业级的 PPT（16:9，可编辑）
  与 PDF（A4，字体嵌入），视觉质量由平台保证，不依赖模型写排版代码。
- **健康方案垂直领域**：渲染器理解健康方案的语义积木（健康画像、趋势、营养处方、餐单、运动处方……），
  每种积木有专门设计的专业版式。其它垂直领域的报告将来各自另建技能。
- **不与 ai-health-plan 耦合**：任何健康类 Agent 都能挂载使用；技能正文与脚本不出现任何具体机构、
  客户、员工或 Agent 的名字，示例一律虚构。
- **视觉可随时调整**：用户对任何一次成品不满意，可在对话里调整视觉（换色、字号、某板块版式……），
  用同一份内容重新出图，内容一字不变；可选择「只改这一份」或「以后都这样」。
- **参数多样化可接入**：调用方 Agent 的输入参数（名称、写法各异，如自由文本的员工样式配置）都能映射到
  技能的可调项并生效；每个参数都有「生效 / 未生效及原因」的交代，不静默丢弃。

### 非目标

- 不做诊断、用药相关内容的承载与呈现（健康管理机构无权诊断与用药指导）。
- 不做医学判断：状态、参考范围、目标值、阈值全部由调用方 Agent 给出，渲染器只呈现。
- 不承载方案制定逻辑：技能正文与脚本中不出现健康红线、阈值、措辞规范等业务规则。
- 不生成 HTML / H5 交付件；不生成二维码。
- 不支持模型或用户改渲染代码；超出可调范围的诉求如实告知做不到。
- 不迁移其它 Agent；本期只迁移 ai-health-plan。

## 2. 已拍板的决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 通用程度 | 健康方案垂直技能（2026-09-28 由通用改为垂直） |
| D2 | 调整的持久化范围 | 默认只改本次；用户明说「以后都这样」才写入个人默认 |
| D3 | 个人默认的归属 | 每位用户一份（按 用户 × Agent 隔离） |
| D4 | 品牌项 | LOGO、机构名称、页脚署名、免责声明由调用方带入，锁定，任何样式层不可覆盖 |
| D5 | PPT 可编辑性 | 文字、表格、图表均为 PPT 原生对象，可在 PowerPoint / WPS 编辑 |
| D6 | 目标设备 | 不确定（Windows / Mac / 手机均可能），格式也不确定：PPT = 可编辑版，PDF = 保真版 |
| D7 | 实现路线 | 纯 Python，跑在现有沙箱镜像：python-pptx 出 PPT，HTML + weasyprint 出 PDF |
| D8 | 视觉方向 | 方向 A「临床专业」（浏览器设计稿 v2 选定） |
| D9 | 参数接入 | 技能只定义「能调什么」，不认参数名；样式层数与优先级由调用方传入 |
| D10 | 方案 JSON 格式变更 | 可改；对接方不解析 JSON 产物，无需对外协调 |
| D11 | 用药积木 | 不做 |
| D12 | 用药数据 | Agent 可读取作内部安全判断依据，成品中不出现任何用药字样 |

## 3. 总体架构

```
调用方 Agent（如 ai-health-plan）
  │  1. 拉数、做健康判断、定稿内容（红线在 Agent 提示词）
  ▼
content.json ──┐
               │   2. validate.py  —— 结构校验，指出错误位置
style 层 1..N ─┤   3. resolve_style.py —— 叠加样式层 + 品牌锁定 + 大白话换算 → resolved style + 参数落实清单
（高 → 低）     │
               ▼
          render.py  —— 版式引擎（测量、分页、合并短章节）→ PPT / PDF
               │
               ▼
   成品文件 + qa.json（丢字 / 溢出 / 对比度 / 越界）+ params-report.json
               │
               ▼
   4. Agent 看图核验（read_page 渲染成品页 → ask_image）→ 5. save_artifact 登记 → 报告未生效参数与降级
```

技能目录（与 docx/pptx/xlsx/pdf 同构）：

```
platform-skills/health-plan-report/
  SKILL.md                 技能正文（何时用、流程、命令、可调项、大白话对照表、边界声明：只呈现不制定）
  skill.yaml               shared: [_cli.py]
  reference/               懒加载参考文档
    content-schema.md      内容 JSON 全量字段说明 + 虚构完整样例
    style-options.md       视觉可调项全表 + 取值 + 大白话对照表
    migration.md           旧 style/PLAN_STYLE.md 偏好迁移指引
  scripts/
    validate.py            内容 JSON 校验
    resolve_style.py       样式层叠加与换算
    render.py              入口：--content --style… --format pptx|pdf|both --out-dir
    hpr/                   渲染器包
      schema.py            内容与样式的数据模型（dataclass + 校验）
      theme.py             主题 token（方向 A）、配色派生、对比度
      measure.py           字体测量（Pillow + 沙箱内 Noto Sans CJK 字体文件）
      layout.py            版式引擎：积木 → 页面框；分页、续页、合并
      blocks/              每种积木一个模块（ppt 与 pdf 两种输出）
      pptx_writer.py       PPT 输出（原生文本框/表格/图表/形状）
      pdf_writer.py        PDF 输出（HTML + CSS → weasyprint）
      charts.py            图表：PPT 原生图表；PDF 用同主题矢量图
      icons/               内置线性图标（开源许可允许商用、允许改作，许可证文件随包）
      qa.py                丢字、溢出、对比度检查
  sample/                  虚构完整样例内容 JSON（测试与自检用）
```

依赖全部为沙箱镜像已预装（2026-09-28 实测镜像 7ac31957：Python 3.12、python-pptx 1.0.2、weasyprint 70.0、
matplotlib 3.11、Pillow 12.3、pypdf 6.18；字体 `/usr/share/fonts/opentype/noto/NotoSansCJK-{Regular,Bold}.ttc`；
镜像无 pydantic / PyYAML，渲染器只用标准库 + 上述包）。**不需要改沙箱镜像，不需要 Node。**

实现结构：内容积木先映射为一小组**版式原语**（卡片网格、表格、键值、要点、提醒框、图表、时间线、分栏、素材、图片），
PPT 与 PDF 各自只实现原语的绘制；版式引擎（测量、分页、合并）只用于 PPT，PDF 交给 weasyprint 的流式分页
（CSS `break-inside: avoid`、表头重复）。技能分类（frontmatter `expert_work.category`）取「健康」。

## 4. 内容 JSON（Agent 写什么）

### 4.1 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 固定 `"1"`；格式升级时递增 |
| `title` | 是 | 方案名称 |
| `subtitle` | 否 | 如「第 1 阶段 · 2 周 ｜ 控糖与体重管理」 |
| `period` | 否 | `{label, weeks}` 方案阶段与周期 |
| `generated_at` | 是 | 生成日期 `YYYY-MM-DD` |
| `data_basis` | 否 | 数据依据说明，如「近 30 天记录」 |
| `client` | 是 | `{name, facts:[{label,value}]}` 称呼 + 基础信息（性别年龄、身高体重等），缺失即不写 |
| `manager` | 否 | `{name, title}` 负责健康管理师 |
| `brand` | 否 | `{org_name, logo_path, footer_signature, disclaimer}`，锁定项，见 §5.4 |
| `sections` | 是 | 有序章节数组，非空 |

### 4.2 章节

`{id, title, blocks:[…]}`。`id` 为短标识（`^[a-z][a-z0-9_-]{0,31}$`，文档内唯一），用于样式定位
（如「`diet` 的餐单改成时间轴」）；`title` 按调用方原文呈现；顺序按数组顺序。

### 4.3 积木

每个积木都有 `kind` 与可选 `id`（章节内唯一，定位写作 `章节id.积木id`，省略时按序号 `diet.2`）。

健康专用积木：

| kind | 主要字段 | 默认呈现（方向 A） | 可选版式 |
|---|---|---|---|
| `summary` 方案摘要 | `items:[{label, text}]`（由调用方提供的「一页看懂」要点） | 摘要页（分栏要点） | 分栏、列表 |
| `profile` 健康画像 | `items:[{name, value, unit, ref_low, ref_high, ref_text, position}]`，`position` ∈ `within/above/below/none`（由 Agent 给出） | 指标卡 + 参考范围条 + 中性对照标签（「在参考范围内 / 高于参考范围 / 低于参考范围」） | 卡片、表格 |
| `issues` 核心问题 | `items:[{title, evidence, level}]`，`level` ∈ `focus/watch/info` | 编号问题卡，按内容顺序，等级只影响标签颜色 | 卡片、列表 |
| `trend` 指标趋势 | `metric, unit, points:[{date,value}], target_low, target_high, ref_text` | 折线 + 目标区间（PDF 为色带；PPT 原生图表不支持折线与面积组合，改为上下限虚线系列）+ 末值标注 | 折线、柱状 |
| `goals` 阶段目标 | `items:[{name, current, target, unit, due, note}]` | 「现在 → 目标」对比卡 | 卡片、表格 |
| `phases` 阶段计划 | `items:[{label, focus:[{area, text}]}]` | 周时间线 | 时间线、分栏卡片、表格 |
| `nutrition` 营养处方 | `energy_kcal, macros:[{name, grams, percent}], meals:[{name, percent, note}]` | 环形图 + 数值卡 + 餐次分配 | 环形图 + 表、纯表 |
| `meal_plan` 餐单 | `templates:[{name, applies_to, meals:[{name, time, foods:[{name, amount}], kcal}]}]` | 一日餐次时间轴（每模板一组） | 时间轴、表格、按天卡片 |
| `diet_rules` 饮食原则 | `recommend[], limit[], avoid[], swaps:[{from,to}]` | 宜 / 慎 / 忌 三栏 | 三栏、列表 |
| `exercise` 运动处方 | `fitt:{frequency, intensity, time, type}, schedule:[{day, items}], progression, cautions[]` | FITT 四格 + 周安排 | 四格 + 表、卡片 |
| `sleep` 睡眠管理 | `current:{bed, wake}, target:{bed, wake}, tips[]` | 作息时间条（当前 vs 目标）+ 要点 | 时间条、列表 |
| `stress` 压力与情绪 | `status, methods:[{name, how, frequency}]` | 方法卡 | 卡片、列表 |
| `habits` 生活习惯 | `items:[{name, current, target, how}]`（吸烟、饮酒、久坐等） | 现状 → 目标对比卡 | 卡片、表格 |
| `material` 动作/产品素材 | `name, description, url, media_path?` | 素材卡；PPT 嵌 MP4（失败降级链接），PDF 可点击链接；`description` 原文呈现 | — |
| `monitoring` 监测计划 | `items:[{item, frequency, timing, alert}]` | 监测表，`alert` 阈值高亮 | 表格、卡片 |
| `referral` 就医提醒 | `text` | 红色警示框（醒目样式）；位置与文字均按内容 JSON 原样 | — |
| `shopping` 采购清单 | `groups:[{name, items[]}]` | 分类清单 | 分栏、列表 |
| `follow_up` 随访安排 | `items:[{label, value}]`（复评日期、联系人、反馈方式） | 键值卡 | 卡片、表格 |

通用兜底积木（承载自定义章节，如「家庭支持」）：`paragraph{text}`、`bullets{items[]}`、
`table{columns[], rows[][]}`、`kv{items:[{label,value}]}`、`image{path, caption}`、
`callout{level: info|warn, title, text}`。

### 4.4 内容规则

- 渲染器**不改、不补、不编**任何文字；缺失字段不显示对应元素。
- 唯一例外是空白规整，校验通过后在 `render.py` 里对全部文字统一做一次、两种格式同一规则：`\r\n` 与单独的 `\r` 换成换行 `\n`，其余 C0 控制字符（`\n`、`\t` 除外，如 `\v`、`\f`、`\x00`）换成空格（否则 python-pptx 会把它们存成字面的 `_x000D_`、weasyprint 会把 PDF 文字层弄乱）；文件路径字段不动。PDF 另对 C1 控制字符与行 / 段分隔符（U+2028 / U+2029）换空格作兜底。
- 结构错误（未知 kind、缺必填、列数不一致、类型不符、id 重复）→ `validate.py` 与 `render.py` 均报错退出，
  错误信息给出 JSON 路径（如 `sections[3].blocks[1].rows[4]`）。
- 内容 JSON 中**不出现任何视觉信息**（颜色、字号、位置、版式），这些只在样式层。
- 数值字段接受数字或字符串（字符串原样呈现，如「约 400」）；图表类积木（`trend`、`nutrition` 环形图）
  的绘图数据必须是数字，否则报错并提示改用表格版式。

## 5. 样式体系

### 5.1 可调项（第一版）

| 维度 | 键 | 取值 |
|---|---|---|
| 主色 | `color.primary` | 任意色值或中文色名（「深蓝」「墨绿」）；派生辅助色自动生成 |
| 背景 | `color.background` | `white` / `light-gray` / `tint`（主色极浅调）/ 色值或中文色名（仅浅色） |
| 强调色 | `color.accent` | 同上；默认方向 A 暖橙 |
| 字号档位 | `type.scale` | `compact` / `standard` / `large`（整体放大一级，适合老年客户） |
| 密度 | `layout.density` | `airy` / `standard` / `compact` |
| LOGO 位置 | `brand.logo_position` | 左上 / 右上 / 左下 / 右下 / 居中（接受中文或英文） |
| 机构名称位置 | `brand.org_position` | 同上，默认左上 |
| 页脚对齐 | `footer.align` | 左 / 居中 |
| 页码 | `footer.page_number` | 开 / 关（PDF 默认开，PPT 默认开） |
| 封面样式 | `cover.variant` | `band`（整版色块 + 数据曲线图形，默认）/ `split`（左右分栏）/ `minimal`（白底极简） |
| 目录 | `toc` | `auto`（PPT ≥ 8 个章节、PDF ≥ 6 页自动加）/ 开 / 关 |
| 章节图标 | `section.icons` | 开 / 关 |
| 积木版式 | `blocks.<kind>.variant`（按类型全局）/ `sections.<章节id>.variant` / `sections.<章节id>.<积木id或1起序号>.variant`（精确定位） | 见 §4.3 各积木可选版式；优先级：积木精确 > 章节 > 类型 > 默认 |
| 格式与纸张 | `output.formats` | `pptx` / `pdf` / 两者；PPT 固定 16:9，PDF 固定 A4 竖版 |

章节的增删、改名、调序属于内容，由调用方改内容 JSON，不设样式键。

### 5.2 样式层与优先级

- 调用方按**自己的**优先级传入任意多个样式层，由高到低：
  `render.py --style 本次.json --style 员工配置.json --style 个人默认.json …`；平台默认主题恒为最底层。
- 同一键以最高层为准；层内未出现的键落到下一层。
- 品牌锁定项（§5.4）不参与叠加。
- 以 ai-health-plan 为例：本次对话指令 > 员工方案样式配置（`plan_style`）> 个人默认 > 平台默认。

### 5.3 值的换算（大白话 → 标准值）

`resolve_style.py` 负责，Agent 不必自算色值（避免模型手抄出错）：

- 颜色：内置中文色名表（含「浅蓝」「藏青」「墨绿」「米白」等常用健康机构配色说法）+ `#RRGGBB`；
  背景只接受浅色，给出深色背景时降到最接近的浅色并记入参数落实清单。
- 位置、对齐、开关：中英文同义词表。
- 档位类：接受「大一点 / 小一点」这类相对说法（相对于下一层的生效值调一级）。
- **可读性守门**：主色与背景、正文与背景对比度不足（正文 < 7:1、主色标题 < 4.5:1）时，自动调深主色到可读的最近色，
  并记入清单「已调整」。
- 换算不了的值：报错并列出可选范围（Agent 应向用户说明）。

### 5.4 品牌锁定项

`brand.org_name`、`brand.logo_path`、`brand.footer_signature`、`brand.disclaimer` 只从内容 JSON 的
`brand` 读取（由调用方从自身配置带入），任何样式层中出现这些键一律忽略并记入清单「与品牌锁定冲突」。
LOGO 文件缺失或无法解码：保留机构名称文字，记入降级警告。

### 5.5 个人默认的持久化

- 位置：调用 Agent 私有工作区内 `report-style/personal.json`（工作区按 租户 × 用户 隔离，
  Agent 私有目录互不可见——满足 D3）。
- 写入时机：仅当用户明确表达持久意图（「以后都…」「默认用…」）时，由 Agent 把对应键写入；
  一次性要求只进本次样式层。
- 技能提供 `resolve_style.py --merge-into report-style/personal.json --set 键=值…`，避免 Agent 手写 JSON 出错。

### 5.6 参数落实清单（params-report.json）

对每个输入键列出：来源层、最终生效值、状态（`applied` / `overridden`（被更高层覆盖）/
`adjusted`（为可读性调整）/ `out_of_range` / `brand_locked`）。Agent 在最终回复中必须如实告知
所有非 `applied` 且非 `overridden` 的项。

### 5.7 参数接入（多样化参数如何生效）

- 技能**不认参数名**，只公布可调项（§5.1）与内容字段（§4）。
- 调用方拿到任何参数（注入变量、自由文本样式配置、对话指令），由调用方 Agent 按 SKILL.md 中的
  **映射示范表**翻译：影响内容的写进内容 JSON（增删板块、周期、称呼、素材），影响视觉的写进某一样式层。
- 表里没有、且无法映射到任何可调项的参数 → Agent 告诉用户「当前不支持」，不得私改代码；
  平台据反馈扩充可调项（新增键 + 测试），属于技能版本迭代。

## 6. 视觉设计（方向 A「临床专业」）

### 6.1 设计语言

- 基调：高端健康机构报告——数据清晰、克制、可信；有分量但不花哨。
- 网格：PPT 12 栏（页边距 0.5in，栏间距 0.2in）；PDF A4 12 栏（边距 18mm/18mm/22mm/18mm）。间距只用 4 档。
- 颜色：
  - 品牌角色：主色（默认深青 `#0B4F5C`）用于封面底色、标题图标底、表头线、卡片顶线；强调色（默认暖橙 `#E8A33D`）
    用于封面数据曲线、顶部品牌条尾段、营养环第三段；中性色为主色偏色的灰阶。
  - **状态色固定、独立于品牌色**：在参考范围内 `#2E7D5B`、高于/低于参考范围 `#B86A0C`、警示（就医提醒）`#B42318`。
- 字体：PPT 中文 `a:ea`=微软雅黑、西文 `a:latin`=Arial；PDF 嵌入 Noto Sans CJK SC（思源黑体开源版）。
  数字等宽（PDF `font-variant-numeric: tabular-nums`）。
- 字号四级（`standard` 档，PPT pt）：页标题 24 / 小标题 16 / 正文 14 / 辅助 11；`large` 档各 +2，`compact` 各 −1（正文下限 12）。
  PDF：20 / 13 / 10.5 / 8.5（`large` +1.5）。
- 图标：内置线性图标（饮食、运动、睡眠、监测、目标、画像、趋势、提醒、采购、随访、情绪、习惯），
  来源为允许商用与改作的开源图标库，许可证文件随包分发；仅用于章节识别。

### 6.2 页面类型

- PPT：封面、目录（按 §5.1 规则）、摘要（仅当内容含 `summary` 积木时，以摘要页版式呈现；技能不自动从其它章节提炼摘要）、
  章节内容页、结束页（随访安排 + 联系方式 + 免责声明）。内容页顶部 3px 品牌条（主色 60% + 强调色 40%），
  左上章节图标 + 页标题，右上「页码 / 总页数」，底部两行页脚（机构名称 ｜ 页脚署名；免责声明）。
- PDF：封面、目录（自动）、摘要、连续正文（章节标题带图标与细线）、每页页眉（机构名 + 方案名）与页脚（署名、免责声明、页码）。
  PDF 不是 PPT 的缩印，按 A4 连续阅读重新排版。

### 6.3 版式引擎规则

- 用 Pillow 按沙箱内真实字体文件测量文字宽度与行数；每个文字框在测量结果上再预留 15% 宽度余量
  （应对客户端字体替换，D6）。
- 放不下就拆页（续页沿用原章节标题，不加「（续）」字样），表格拆页时重复表头；**不缩字号、不压行距、不侵占页脚**。
- 单个不可拆元素（一张卡片 / 一行表格）在一页放不下：报错并指出位置（内容需拆分），不静默截断。
- 章节接续（避免稀疏页）：上一章节结束后，若本页剩余高度 ≥ 正文区高度的 35%，且「章节小标题 + 间距 + 下一章节首个元素的最小可放部分」放得下，下一章节就接在本页（章节标题降为页内小标题），之后照常流动 / 拆页；否则另起新页。

### 6.4 设计稿先行

实现 PPT / PDF 渲染代码前，先用 `sample/` 虚构完整样例出一版全积木设计稿（PPT 与 PDF 各一份），
由产品确认后再定型主题 token；此后视觉改动都走主题 token 与积木版式，不散落在代码里。

## 7. 生成流程与质检

### 7.1 Agent 使用流程（写进 SKILL.md）

1. 写内容 JSON → `validate.py` 校验 → `save_artifact(kind="data")` 登记。
2. 组装样式层（按调用方优先级；个人默认从 `report-style/personal.json` 读取，不存在则略过）。
3. `render.py` 出成品，同时产出 `qa.json`、`params-report.json`。
4. **看图核验（必做）**：`read_page(path=成品, units=[…])` 渲染封面与内容最密的两页，再对同页 `ask_image`（平台 R14：ask_image 只接受 read_page 渲染过的页，不接受 PNG 路径）；发现问题只能调整样式参数重新渲染，不得改代码。
5. `save_artifact(kind="document")` 登记成品；回复中报告未生效参数（§5.6）与降级警告（LOGO、视频）。

用户后续说「改一下（视觉）」：新增一层本次样式，**同一份内容 JSON** 重新渲染，不重新拉数、不重写内容；
用户说「以后都这样」时同时写入个人默认。

### 7.2 质检（硬门槛）

`qa.json` 任一项不通过，`render.py` 以非零退出并说明原因：

- **丢字**：内容 JSON 中每个文字字段（去空白后）必须出现在成品抽取文本中（PPT：python-pptx 逐形状 / 表格 / 图表标题抽取；
  PDF：pypdf 抽取）；计数不足即失败。
- **溢出**：版式引擎记录每个文字框的「测量所需高度 ≤ 分配高度」；任一违反即失败。
- **对比度**：所有前景 / 背景组合满足 §5.3 阈值。
- **结构**：PPT 页数、每页形状均在页面范围内（坐标与尺寸落在 0..页宽 / 页高，防 2026-09-27 双重换算类 bug）。

视觉核验由 Agent 走 read_page + ask_image 完成，不作为自动判据；技能不单独产出预览 PNG。

## 8. ai-health-plan 迁移（调用方侧改动，不属于技能本身）

- 提示词：
  - 删除：「渲染锚 style/render_plan.py」全部规则（首次写脚本、语法自检、修复 2 轮、降级）、「默认视觉基线」
    全节、生成步骤 3 的渲染实现要求。
  - 改写：生成步骤改为调用 `health-plan-report` 技能；方案 JSON 改为技能内容格式；
    `plan_style` / 对话指令 / 个人默认映射为样式层（优先级保持：当次指令 > 员工样式配置 > 个人默认 > 默认）；
    `org_name` / `org_logo` / `disclaimer` 写入内容 `brand`；`output_format` 映射 `output.formats`；
    `materials` 映射 `material` 积木（素材硬规则保留）。
  - 保留：拉数流程与纪律、健康红线、素材使用硬规则、改版规则（新生成时间命名、不覆盖旧文件）。
  - 修正（D12）：保留 `medication_query_plans` 读取，仅作 Agent 内部安全判断依据（如服降糖药者不安排空腹运动）；
    删除「在注意事项写明当前用药仅作参考」等写入要求——成品中不出现任何用药字样。
- 员工旧偏好：已有 `style/PLAN_STYLE.md` 的员工，首次使用新技能时由 Agent 按 `reference/migration.md`
  把其中视觉偏好翻译写入 `report-style/personal.json`；旧 `style/render_plan.py` 不再使用、不删除。
- Agent 配置：为 ai-health-plan 挂载 `health-plan-report` 技能（与现有 docx/pptx/pdf 技能并存）。
- 对接方：不解析 JSON 产物（D10），无需协调；产物命名规则不变。

## 9. 测试

1. **第一层（单元，秒级）**：`validate.py` 全部错误分支；`resolve_style.py` 叠加、换算、品牌锁定、对比度调整、
   参数落实清单；版式引擎分页 / 续页 / 合并 / 不可拆元素报错；丢字检测能咬住（删一个字段必红）。
   每条新断言按 break → red → restore → green 自证。
2. **全组合矩阵**：主色（含极浅、极深、中文色名）× 字号档位 × 密度 × 各积木全部版式 × 两种格式，
   跑 `sample/` 完整样例，断言丢字 0、溢出 0、对比度达标、形状不越界。
3. **异常输入**：超长标题 / 单元格、空章节、100 行表格、缺 LOGO、坏 LOGO、深色背景、未知版式名、重复 id。
4. **第二层（沙箱镜像内）**：沿用 `platform-skills/tests/in_image` 机制，在真实沙箱镜像跑 `render.py` 两种格式，
   断言字体实际嵌入（PDF 字体表含 Noto Sans CJK）、PPT 可被 soffice 打开并出预览。
5. **第三层（测试环境真栈）**：用 ai-health-plan 重放 2026-09-27 张女士会话（thread `acca106c`），
   新旧成品并排给产品；在 WPS（Windows）、PowerPoint（Windows / Mac）、手机（微信内预览）打开核对。

## 10. 发布与拆分

- 4 个 PR，依次合入：
  1. 内容与样式模型、`validate.py`、`resolve_style.py`、参数落实清单（含第一层测试）。
  2. 主题 token、测量、版式引擎、PPT 输出（含设计稿确认与全组合矩阵）。
  3. PDF 输出、质检硬门槛、第二层镜像测试。
  4. SKILL.md 与 reference、ai-health-plan 提示词迁移、测试环境真栈验收。
- 生产：测试环境真栈通过且产品确认视觉后，走现有平台技能导入流程（`import_in_pod.py`）上生产，
  同时发布 ai-health-plan 新提示词。沙箱镜像无改动。

## 11. 风险

| 风险 | 缓解 |
|---|---|
| 客户端字体替换导致 PPT 文字折行 / 溢出 | 测量 + 15% 余量；多端实测；PDF 作保真版 |
| weasyprint CSS 能力弱于浏览器 | PDF 版式按其支持范围设计（flex / grid 基础能力），设计稿阶段在沙箱镜像里实测 |
| 可调项组合爆炸导致某些组合难看 | 全组合矩阵自动测；可调项取有限枚举，不开放任意数值 |
| 调用方参数无法映射 | 参数落实清单强制交代；平台按反馈扩充可调项 |
| 模型仍想改渲染代码 | SKILL.md 明令禁止；技能种子每次 exec 从数据库重新落盘（`skill_seed.py`），沙箱内的改动不持久；质检硬门槛兜底 |
