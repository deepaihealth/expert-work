---
name: docx
description: 新建、修改或转换 Word 文档（.docx）时使用：方案、报告、合同、按机构模板出文档、转 PDF。只读取内容用 read_document，不用本技能。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 通用
---

新建、修改已有 .docx、按模板填值、转 PDF 时用本技能；只想读取已有文档内容用 `read_document` /
`read_page`，不用本技能。修订痕迹、批注等做不到的事见文末「做不到」一节。

## 环境

- 已预装、直接用、不要装：python-docx、python-pptx、openpyxl、pypdf、pdfplumber、pdf2image、weasyprint、matplotlib、Pillow、pandas；命令行 soffice（LibreOffice）、pdftoppm；中文字体 Noto Sans CJK。
- 需要别的 Python 包时可以 pip install（走国内镜像，很快），但沙箱空闲后会回收、装过的包会丢；本技能不依赖任何需要现装的包。
- 没有 npm：不要走任何 JavaScript 路线。
- 工作目录 /workspace；本技能脚本在 $EXPERT_WORK_SKILLS_DIR/docx/scripts/。
- 成品必须用 save_artifact 登记，否则用户拿不到。
- 只读取已有文档内容用 read_document / read_page。

## 新建

用 python-docx。中文字体必须**同时**设西文名与 `w:eastAsia`（只设 `font.name` 结果还是宋体）；
用样式（Normal / Heading 1~3）而不是逐段手设字号，保证标题层级统一。下面的骨架代码可以原样执行，
生成一份含一级标题、正文、表格、图片、页脚页码的示例：

```python title=skeleton
import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

CN_FONT = "微软雅黑"
EN_FONT = "Arial"


def set_cn_font(style_or_run, cn: str = CN_FONT, en: str = EN_FONT) -> None:
    """给样式或某个 run 设中西文字体：西文名走 font.name，中文名写 w:eastAsia。"""
    style_or_run.font.name = en
    rfonts = style_or_run.element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), cn)


def add_page_number(paragraph) -> None:
    """在段落里插入 Word 的 PAGE 域（自动页码，而不是写死数字）。"""
    run = paragraph.add_run()
    for tag, attrs, text in (
        ("w:fldChar", {"w:fldCharType": "begin"}, None),
        ("w:instrText", {"xml:space": "preserve"}, "PAGE"),
        ("w:fldChar", {"w:fldCharType": "separate"}, None),
        ("w:fldChar", {"w:fldCharType": "end"}, None),
    ):
        el = OxmlElement(tag)
        for k, v in attrs.items():
            el.set(qn(k), v)
        if text is not None:
            el.text = text
        run.element.append(el)


document = docx.Document()
for style_name in ("Normal", "Heading 1", "Heading 2", "Heading 3"):
    set_cn_font(document.styles[style_name])

document.add_heading("示例方案", level=1)
document.add_paragraph("本文档由骨架代码生成，演示中文标题、正文、表格与插图的基本写法。")

table = document.add_table(rows=2, cols=3)
table.style = "Table Grid"
for cell, text in zip(table.rows[0].cells, ("项目", "负责人", "进度"), strict=True):
    run = cell.paragraphs[0].add_run(text)
    run.bold = True
for cell, text in zip(table.rows[1].cells, ("示例任务", "张三", "进行中"), strict=True):
    cell.paragraphs[0].add_run(text)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(4, 2.4))
ax.bar(["A", "B", "C"], [3, 5, 2])
fig.tight_layout()
fig.savefig("_skeleton_chart.png", dpi=150)
plt.close(fig)

section = document.sections[0]
avail_width = section.page_width - section.left_margin - section.right_margin
document.add_picture("_skeleton_chart.png", width=int(avail_width * 0.8))

footer_p = section.footer.paragraphs[0]
footer_p.alignment = 1
add_page_number(footer_p)

document.save("骨架 示例.docx")
```

要点：表格用 `table.style = "Table Grid"` 才有边框、表头行单独加粗；图片按 `section` 的可用宽度
（页宽减左右边距）等比缩放，不要写死像素；页码用 PAGE 域而不是手写数字，翻页顺序改变时才不会错。

## 修改已有文件

两条硬规则：① 输出到**新文件**，绝不覆盖原件（脚本层面强制：输入输出同路径直接报错退出）；
② 改完必须走下面的「检查成品」三步。

- 保留格式替换文字：
  `python $EXPERT_WORK_SKILLS_DIR/docx/scripts/replace_text.py IN.docx OUT.docx --rules RULES.json`
  `RULES.json` 是 `[{"find": "旧文字", "replace": "新文字"}]`；覆盖正文、表格（含嵌套）、页眉页脚，
  一句话被拆成多个 run 也能正确替换并保留第一个 run 的格式。输出 JSON：`rules`（每条规则的命中
  次数）、`unmatched`（0 命中的 find 列表，自己判断是不是没找对词）。
- `{{占位符}}` 模板填值：
  `python $EXPERT_WORK_SKILLS_DIR/docx/scripts/fill_template.py TEMPLATE.docx --list` 先列出模板
  里所有占位符：`{"placeholders": [...]}`。
  `python $EXPERT_WORK_SKILLS_DIR/docx/scripts/fill_template.py TEMPLATE.docx OUT.docx --data DATA.json`
  按 `DATA.json`（`{"占位符名": "值"}`）填值，输出 `filled`（已填）、`missing`（模板有但没给值，
  原样留在文档里）、`unused`（给了值但模板里没有这个占位符）。
  占位符只做标量替换（不做循环 / 条件）；表格多行数据按正文里的 python-docx 表格写法做。
  `DATA.json` 的值只能是字符串或数字，`null` / 列表 / 字典 / 布尔值会直接报错退出。
- 增删段落 / 插图 / 页眉页脚，直接用 python-docx：
  ```python
  document.add_paragraph("新增的一段")                    # 末尾加一段
  p._element.getparent().remove(p._element)               # 删除段落 p
  document.add_picture("图.png", width=int(avail_width * 0.8))  # 插图，宽度算法见「新建」
  document.sections[0].header.paragraphs[0].text = "页眉文字"     # 页脚同理用 .footer
  ```

## 转换

`python $EXPERT_WORK_SKILLS_DIR/docx/scripts/convert.py IN.docx --to pdf --out-dir DIR`；
老格式 `.doc` 转成 `.docx` 用 `--to docx`。输出 JSON：`{"output": "生成文件的路径"}`。

## 检查成品

1. 用 python-docx 重新打开成品，确认没坏：`docx.Document("成品.docx")` 不报错即可。
2. `python $EXPERT_WORK_SKILLS_DIR/docx/scripts/preview.py 成品.docx` 出图。
3. 用 `ask_image(path=...)` 看首页和信息最密的一页：中文是否方块、文字是否溢出 / 重叠、版式是否错乱。Agent 没配看图能力时跳过这一步，并在回复里写明「未做视觉检查」。

## 字体

Word 只记字体名，客户打开时用他自己电脑上的字体，没装就被 Office 自动换掉；沙箱里的字体只影响
预览图。默认中文用 `微软雅黑`、西文用 `Arial`（Windows / Mac 版 Office 普遍自带）。员工指定了某个
字体时照写该字体名，并在回复里提醒一句：「客户电脑没装这个字体时，Word 会自动替换成别的字体。」
品牌字体要求严格（VI 指定字体、要求还原度高）时，建议改交付 PDF（字体嵌入文件，谁打开都一样）。

## 常见坑

- 只设 `font.name`，中文仍是宋体 → 必须同时设 `w:eastAsia`（见「新建」的 `set_cn_font`）。
- 直接 `paragraph.text = "新文字"` 会把原有的加粗、颜色等格式全部冲掉 → 改某个 `run.text`，或用
  `replace_text.py`。
- 表格列宽只设在 `table.columns[i].width` 不生效，要设在**每个单元格**的 `cell.width` 上。
- 图片按原始像素插入容易溢出页面 → 按「新建」里的可用宽度算法等比缩放。
- 超链接（`w:hyperlink`）里的锚文字也在 `replace_text.py` / `fill_template.py` 的替换范围内，不会漏改。

## 做不到

修订痕迹、批注、改内嵌图表 / SmartArt 数据、宏——如实告诉用户「这个做不到」，不要假装做了。
