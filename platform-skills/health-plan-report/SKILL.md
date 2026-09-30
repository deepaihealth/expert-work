---
name: health-plan-report
description: 把已定稿的健康管理方案内容渲染成企业级的可编辑 PPT（16:9）与 A4 PDF，并做内容与版式质检。只负责呈现：取数、健康判断与方案内容由你（调用方）决定。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 健康
---

健康方案定稿后，用本技能出客户交付件（PPT / PDF）。版式、配色、字号、分页都由本技能按统一设计规范完成，
**不要自己写排版代码，也不要修改本技能的脚本**。

## 边界

- 本技能只呈现：你给的每个字原样出现在成品里，章节与内容顺序照你给的来；它不补写、不删减、不重排、不做健康判断。
- 取数、健康判断、红线、措辞、板块取舍都由你按自己的规则决定，写进内容 JSON。
- 超出可调范围的视觉要求（如加背景图、改成其它字体）如实告诉用户做不到。

## 环境

- 依赖已预装（python-pptx、weasyprint、Pillow、pypdf、中文字体 Noto Sans CJK），不要安装任何包。
- 工作目录 /workspace；脚本在 $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/。
- 成品必须用 save_artifact 登记，否则用户拿不到。

## 改稿速查（已经出过稿、用户要改时用）

不必重读全文，也不要为了确认用法先列目录、翻参考文件或不带参数试跑脚本。按下面四步走，除看图分两次回复外，每一步用一次回复完成：

1. **改**：只改视觉 → 写一个新的本次样式层 JSON；改内容 → 改 `{FN}.json` 并在同一次回复里跑 `validate.py {FN}.json`。
   字段拿不准才去看 `reference/content-schema.md`。
2. **渲染**：用下面「流程」第 3 步的完整命令，换一个新的 basename。输出 `ok` 为 false 时按「流程」第 4 步处理后重新渲染。
3. **看图**：按输出的 `page_map` 找刚改的章节页。一次回复里 `read_page`（最多 3 页）；下一次回复里把这几页的
   `ask_image` **一起发出**（每页一个调用，同一次回复里并行）。没有看图能力时跳过，并在回复里写明「未做视觉检查」。
4. **保存并回复**：要交付的文件在**同一次回复里一起** `save_artifact`（kind=document，每个文件一个调用，并行），然后回复用户，
   并把渲染输出里的 `not_applied` 与 `warnings` 如实告知。

## 流程

1. 按 `reference/content-schema.md` 写内容 JSON（例：`{FN}.json`），校验：
   `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/validate.py {FN}.json`
   报错会给出位置（如 `sections[3].blocks[1].rows[4]`），改内容后重跑。
2. 准备样式层（可以没有）：把每一类参数各写成一个 JSON 小文件，**按优先级从高到低**传给渲染命令。
   常见顺序：本次对话要求 > 系统注入的样式配置 > 个人默认 `report-style/personal.json`（存在才传）。
3. 渲染：
   `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/render.py --content {FN}.json --style 本次.json --style 配置.json --style report-style/personal.json --format pptx --out-dir . --basename {FN}`
   `--format` 取 `pptx` / `pdf` / `both`。`{FN}.pptx`/`.pdf`、`.qa.json`、`.params-report.json` 里只要有一个已存在就会拒绝渲染——换新的 basename 再试。
4. 读输出 JSON：
   - `ok` 为 true：`files` 是产物路径，看第 5 步。
   - `ok` 为 false：`errors` 非空是内容/参数错误（按提示改后**可以用原 basename 重跑**，失败时本次产生的文件已被清理）；
     `errors` 为空而 `qa` 里某项 `status` 不是 `passed`，是质检没过（缺字、溢出、越界、对比度不够），产物已改名为 `{FN}.qa-failed.pptx/pdf` 供你排查，改内容或参数、**换新 basename** 重新渲染。
5. 按下面「检查成品」看图；没问题再 save_artifact（kind=document），登记的是 `files` 里的路径；要交付多个文件时在同一次回复里一起登记。
6. 回复用户时，把输出里的 `not_applied`（没生效的参数及原因）与 `warnings`（LOGO 缺失、视频改链接等）如实告知。

## 内容 JSON

顶层写方案名称、生成日期、客户、可选的阶段/负责人与机构品牌（`brand`：机构名称、LOGO 路径、页脚署名、免责声明——
这些只从内容读取，样式改不了）。正文是有序章节，每章由积木组成：健康画像、核心问题、指标趋势、阶段目标、
阶段计划、营养处方、餐单、饮食原则、运动处方、睡眠、压力与情绪、生活习惯、动作/产品素材、监测计划、
就医提醒、采购清单、随访安排、方案摘要，以及段落、要点、表格、键值、图片、提示框。字段见
`reference/content-schema.md`，完整虚构样例在 `sample/sample-plan.json`。图片路径找不到会报错并指出是哪个
积木；LOGO 缺失或无法读取只降级为不显示并警告，不会中断渲染。
封面只放 LOGO/机构名称、方案名称、副标题、客户称呼和「生成日期 · 健康管理师」一行；`client.facts`（写简短的身份信息，
如性别年龄、身高体重、管理方向）、阶段与数据依据由脚本放在正文第一页的「客户信息」栏，更长的背景写进健康画像或键值积木。

## 样式与参数

- 可调项与取值见 `reference/style-options.md`：主色 `color.primary`、背景 `color.background`、字号档
  `type.scale`、密度 `layout.density`、LOGO 位置 `brand.logo_position`、封面样式 `cover.variant`、
  各积木版式（如 `blocks.meal_plan.variant`、`sections.diet.variant`）等。
- 值可以写大白话（「深蓝」「右上角」「大一点」「表格」），脚本负责换算；换算不了会出现在 `not_applied`。
- 你拿到的任何参数（注入变量、员工设置文本、对话要求）都由你翻译成这些键；影响内容的（增删板块、改周期）改内容 JSON。
- 用户说「以后都这样」才写个人默认：
  `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/resolve_style.py --merge-into report-style/personal.json --set 键=值`
  一次性要求只放进本次样式层，不写文件。
- 想先看某组参数的生效结果：`python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/resolve_style.py --style a.json --content {FN}.json`

## 检查成品

1. 渲染输出 `ok: true` 表示机器质检通过（字全在、不溢出、不越界、对比度够）。
2. 再看图：输出里的 `page_map` 列出封面、客户信息和每个章节在哪几页，按它挑页，不要猜页号——首次出稿看封面和
   内容最多的章节，改稿后看刚改的章节。`read_page(path={FN}.pptx, units=[页号…])`（一次最多 3 页），然后
   `ask_image(path={FN}.pptx, unit=同一页号, question=...)` 看中文是否正常、有无重叠、是否美观；要看几页就在同一次回复里并行发几个 `ask_image`。
   必须先 read_page 再 ask_image。Agent 没有看图能力时跳过，并在回复里写明「未做视觉检查」。
3. 发现问题只调样式参数或内容后重新渲染（用新的 basename），不要改脚本。

## 用户要改

- 只改视觉（颜色、字号、某页版式、LOGO 位置）：同一份内容 JSON + 新的本次样式层，重新渲染。
- 改内容（换食谱、改目标、增删板块）：改内容 JSON 再渲染。
- 旧版本方案的视觉偏好迁移见 `reference/migration.md`。

## 做不到

背景图片、自定义字体、动画、二维码、HTML/H5 成品、改变渲染代码。PPT 用微软雅黑，客户电脑没有时 Office
会自动替换；需要绝对一致时建议交付 PDF（字体嵌入文件）。
