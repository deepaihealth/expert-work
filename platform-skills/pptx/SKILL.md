---
name: pptx
description: 新建、修改或转换 PowerPoint 演示文稿（.pptx）时使用：汇报、方案展示、按机构模板出 PPT、转 PDF。只读取内容用 read_document，不用本技能。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 通用
---

新建、修改已有 .pptx、套用模板、转 PDF 时用本技能；只想读取已有演示文稿内容用 `read_document` /
`read_page`，不用本技能。动画、改内嵌图表数据等做不到的事见文末「做不到」一节。

## 环境

- 已预装、直接用、不要装：python-docx、python-pptx、openpyxl、pypdf、pdfplumber、pdf2image、weasyprint、matplotlib、Pillow、pandas；命令行 soffice（LibreOffice）、pdftoppm；中文字体 Noto Sans CJK。
- 需要别的 Python 包时可以 pip install（走国内镜像，很快），但沙箱空闲后会回收、装过的包会丢；本技能不依赖任何需要现装的包。
- 没有 npm：不要走任何 JavaScript 路线。
- 工作目录 /workspace；本技能脚本在 $EXPERT_WORK_SKILLS_DIR/pptx/scripts/。
- 成品必须用 save_artifact 登记，否则用户拿不到。
- 只读取已有文档内容用 read_document / read_page。

## 新建

用 python-pptx。16:9（宽 12192000 EMU、高 6858000 EMU，等于 13.333×7.5 英寸）。中文字体必须**同时**
设西文名（`a:latin`）与东亚名（`a:ea`）——python-pptx 没有公开 API，要直接操作 run 的 `rPr`。字号
只用四级常量。骨架代码可原样执行，生成封面、表格页、图表页三页示例：

```python title=skeleton
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pptx
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Inches, Pt

CN_FONT = "微软雅黑"
EN_FONT = "Arial"

SIZE_TITLE = Pt(36)  # 1 级：封面/页标题
SIZE_HEADING = Pt(24)  # 2 级：页内小标题
SIZE_BODY = Pt(18)  # 3 级：正文、表格
SIZE_CAPTION = Pt(12)  # 4 级：图注、脚注


def set_cn_font(run, cn: str = CN_FONT, en: str = EN_FONT) -> None:
    """西文写 a:latin（font.name），中文写 a:ea——python-pptx 无公开 API，直接改 rPr。"""
    run.font.name = en
    rpr = run.font._element
    ea = rpr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}ea")
    if ea is None:
        ea = OxmlElement("a:ea")
        rpr.insert_element_before(
            ea, "a:cs", "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst"
        )
    ea.set("typeface", cn)


def set_run(run, text: str, size, *, bold: bool = False) -> None:
    run.text = text
    run.font.size = size
    run.font.bold = bold
    set_cn_font(run)


prs = pptx.Presentation()
prs.slide_width = Emu(12192000)
prs.slide_height = Emu(6858000)

# 封面
cover = prs.slides.add_slide(prs.slide_layouts[0])
set_run(cover.shapes.title.text_frame.paragraphs[0].add_run(), "示例汇报", SIZE_TITLE, bold=True)
set_run(cover.placeholders[1].text_frame.paragraphs[0].add_run(), "骨架代码生成", SIZE_BODY)

# 表格页
table_slide = prs.slides.add_slide(prs.slide_layouts[5])
set_run(table_slide.shapes.title.text_frame.paragraphs[0].add_run(), "进度一览", SIZE_HEADING, bold=True)
left, top = Inches(1), Inches(2)
width = prs.slide_width - Inches(2)
table = table_slide.shapes.add_table(2, 3, left, top, width, Inches(1.5)).table
for c, text in enumerate(("项目", "负责人", "进度")):
    set_run(table.cell(0, c).text_frame.paragraphs[0].add_run(), text, SIZE_BODY, bold=True)
for c, text in enumerate(("示例任务", "张三", "进行中")):
    set_run(table.cell(1, c).text_frame.paragraphs[0].add_run(), text, SIZE_BODY)

# 图表页（matplotlib 出图再插入）
fig, ax = plt.subplots(figsize=(6, 3.2))
ax.bar(["A", "B", "C"], [3, 5, 2])
fig.tight_layout()
fig.savefig("_skeleton_chart.png", dpi=150)
plt.close(fig)

chart_slide = prs.slides.add_slide(prs.slide_layouts[5])
set_run(chart_slide.shapes.title.text_frame.paragraphs[0].add_run(), "数据示意", SIZE_HEADING, bold=True)
avail_width = prs.slide_width - Inches(2)
chart_slide.shapes.add_picture("_skeleton_chart.png", Inches(1), Inches(1.8), width=int(avail_width))

prs.save("骨架 示例.pptx")
```

要点：`slide_width` / `slide_height` 直接赋 EMU 值，不要用 `Inches(13.333)` 之类的浮点换算（累计误差
会让尺寸对不上标准 16:9）；图片按可用宽度（页宽减左右各 1 英寸）等比缩放，不要写死像素。

## 版式网格与留白原则

- 统一边距、同级元素对齐：同一份演示文稿页边距固定，不要每页各设一套；并列的卡片、文字块要上下
  左右对齐，不要靠肉眼估。
- 一页一个视觉焦点：多个要点用项目符号纵向排列，不要平铺塞满。
- 内容多就拆页，不缩字号：字号只在四级常量里选，塞不下就拆成两页，不靠缩字号硬塞。

## 套用机构模板

先看模板有哪些版式与占位符：
`python $EXPERT_WORK_SKILLS_DIR/pptx/scripts/inspect_template.py 模板.pptx`
输出 `layouts`（版式的 `index`/`name`/`placeholders`，占位符含 `idx`/`type`/`name`/位置）与
`slides`（已有页用的版式与形状概况）。选定版式号后加页、按 `idx` 填值：

```python
slide = prs.slides.add_slide(prs.slide_layouts[1])  # index 取自 inspect_template.py 的输出
slide.placeholders[0].text_frame.text = "标题文字"
slide.placeholders[1].text_frame.text = "正文内容"
# 图片占位符：slide.placeholders[idx].insert_picture("图.png")
```

## 删页 / 调序

python-pptx 没有 `delete_slide` 方法，直接操作幻灯片 ID 列表 `prs.slides._sldIdLst`：

```python
ids = prs.slides._sldIdLst
ids.remove(ids[2])  # 删除第 3 页（下标从 0 开始）
ids.insert(0, ids[-1])  # 把最后一页挪到最前
```

删页 / 调序后必须**另存为新文件**，不要覆盖原件。

## 复制一页

`python $EXPERT_WORK_SKILLS_DIR/pptx/scripts/duplicate_slide.py IN.pptx OUT.pptx --index N [--after M]`
（页码从 1 开始；默认紧跟原页，`--after 0` = 放最前）。输出 `new_slide_number`、
`shared_parts_warning`：图表 / SmartArt / 嵌入对象 / 嵌入文件这几类关系复制后**共享同一个数据部件**，
改一页的这类数据会连带改到另一页，出现在告警里就要提醒用户；图片、视频、超链接各自独立，不受影响；
演讲者备注各自拷贝一份，互不影响、也不出现在告警里；`notes_format_lost` 为 `true` 说明只复制了备注
纯文本、格式没保留（极少见）。

## 嵌视频

```python
slide.shapes.add_movie("演示.mp4", left, top, width, height, poster_frame_image="封面.png")
```

必须给封面图（`poster_frame_image`），拿不到视频首帧时用一张说明性截图代替；尺寸不自动缩放，
宽高要自己算好。

## 转换

`python $EXPERT_WORK_SKILLS_DIR/pptx/scripts/convert.py IN.pptx --to pdf --out-dir DIR`；
老格式 `.ppt` 转成 `.pptx` 用 `--to pptx`。输出 JSON：`{"output": "生成文件的路径"}`。

## 检查成品

1. 用 python-pptx 重新打开成品，确认没坏：`pptx.Presentation("成品.pptx")` 不报错即可。
2. `python $EXPERT_WORK_SKILLS_DIR/pptx/scripts/preview.py 成品.pptx` 出图。
3. 用 `ask_image(path=...)` 看首页和信息最密的一页：中文是否方块、文字是否溢出 / 重叠、版式是否
   错乱。Agent 没配看图能力时跳过这一步，并在回复里写明「未做视觉检查」。

## 字体

PowerPoint 只记字体名，客户打开时用他自己电脑上的字体，没装就被 Office 自动换掉；沙箱字体只影响
预览图。默认中文用 `微软雅黑`、西文用 `Arial`（Windows / Mac 版 Office 普遍自带）。员工指定了某个
字体时照写该字体名，并提醒一句：「客户电脑没装这个字体时，Office 会自动替换成别的字体。」品牌
字体要求严格时，建议改交付 PDF（字体嵌入文件，谁打开都一样）。

## 常见坑

- `text_frame.text = "新文字"` 会冲掉原有字号、颜色等格式 → 改某个 `run.text`，或先记下格式再重设。
- 原生图表在 LibreOffice 预览里的渲染效果可能和 PowerPoint 不一致 → 推荐 matplotlib 出图再
  `add_picture` 插入。
- 图片不按「新建」里的可用宽度等比缩放，写死像素会拉伸变形。
- 只设 `font.name`，中文仍是默认字体 → 必须同时设 `a:ea`（见 `set_cn_font`）。
- `inspect_template.py` 里某个占位符的 left/top/width/height 是 `null` → 版式和母版都没单独设位置
  （继承链到头了），按“没有固定位置”处理，不要当成 0。

## 做不到

动画效果、修改内嵌图表 / SmartArt 的底层数据、宏——如实告诉用户「这个做不到」，不要假装做了。
