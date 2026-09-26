---
name: xlsx
description: 新建、修改或转换 Excel 表格（.xlsx）时使用：数据表、预算、清单、带公式的报表、转 PDF。只读取内容用 read_document，不用本技能。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 通用
---

新建、修改已有 .xlsx、重算公式、转 PDF 时用本技能；只想读取已有表格内容用 `read_document` /
`read_page`，不用本技能。数据透视表、宏、保留原有图表等做不到的事见文末「做不到」一节。

## 环境

- 已预装、直接用、不要装：python-docx、python-pptx、openpyxl、pypdf、pdfplumber、pdf2image、weasyprint、matplotlib、Pillow、pandas；命令行 soffice（LibreOffice）、pdftoppm；中文字体 Noto Sans CJK。
- 需要别的 Python 包时可以 pip install（走国内镜像，很快），但沙箱空闲后会回收、装过的包会丢；本技能不依赖任何需要现装的包。
- 没有 npm：不要走任何 JavaScript 路线。
- 工作目录 /workspace；本技能脚本在 $EXPERT_WORK_SKILLS_DIR/xlsx/scripts/。
- 成品必须用 save_artifact 登记，否则用户拿不到。
- 只读取已有文档内容用 read_document / read_page。

## 新建

用 openpyxl。**公式写成公式**（`=SUM(B2:B9)`），不要在 Python 里自己算好写死结果——写死的数字
改不了、也骗不过下面的「检查成品」。数字/百分比/日期都要设 `number_format`，否则 Excel 只按普通数字
显示。下面的骨架代码可以原样执行，生成一份含表头样式、冻结首行、数字格式、SUM 公式、插图的示例：

```python title=skeleton
import openpyxl
from openpyxl.styles import Font, PatternFill

CN_FONT = "微软雅黑"

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "预算"

headers = ["项目", "预算", "占比"]
for col, text in enumerate(headers, start=1):
    cell = ws.cell(row=1, column=col, value=text)
    cell.font = Font(name=CN_FONT, bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor="4472C4")

rows = [("场地", 12000, 0.4), ("物料", 9000, 0.3), ("人力", 9000, 0.3)]
for r, (name, amount, pct) in enumerate(rows, start=2):
    ws.cell(row=r, column=1, value=name).font = Font(name=CN_FONT)
    ws.cell(row=r, column=2, value=amount).number_format = "#,##0"
    ws.cell(row=r, column=3, value=pct).number_format = "0%"

total_row = len(rows) + 2
ws.cell(row=total_row, column=1, value="合计").font = Font(name=CN_FONT, bold=True)
ws.cell(row=total_row, column=2, value=f"=SUM(B2:B{total_row - 1})")

for col, width in zip("ABC", (14, 12, 10), strict=True):
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A2"  # 冻结首行，往下滚表头仍可见

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(4, 2.4))
ax.bar([name for name, _, _ in rows], [amount for _, amount, _ in rows])
fig.tight_layout()
fig.savefig("_skeleton_chart.png", dpi=150)
plt.close(fig)
img = openpyxl.drawing.image.Image("_skeleton_chart.png")
img.anchor = "E2"
ws.add_image(img)

wb.save("骨架 示例.xlsx")
```

要点：表头单独设字体加粗和填充色区分正文；列宽按内容估手动设 `column_dimensions[列].width`；
条件格式用 `openpyxl.formatting.rule`，数据校验用 `openpyxl.worksheet.datavalidation.DataValidation`，
两者都直接对 `ws` 操作，用法见 openpyxl 官方文档；图表同样推荐 matplotlib 出图插入（原生图表见
「做不到」）。openpyxl 写入的公式**没有缓存值**（Python 不会算），必须走下面的 `recalc.py` 才能
在其它软件里看到结果。

## 修改已有文件

`openpyxl.load_workbook` 默认保留公式与样式；改完必须走「检查成品」三步（含额外的 recalc 步骤）。
两条硬规则：① 输出到新文件，绝不覆盖原件（脚本层面强制：输入输出同路径直接报错退出）；② **原文件
里的图表 / 图片保存后会丢失**——改动前先检查 `ws._charts`、`ws._images`，非空就先告诉用户「这份表
有图表/图片，改完会丢，要继续吗」，征得同意再改。

## 重算公式 `recalc.py`

`python $EXPERT_WORK_SKILLS_DIR/xlsx/scripts/recalc.py IN.xlsx OUT.xlsx [--timeout 120]`：用
LibreOffice 打开并重算全部公式后另存，同时扫描错误值。输出一行 JSON：
`{"formulas": 公式单元格数, "errors": [{"sheet","cell","value"}]}`。
退出码：**0** = 重算成功且没有错误单元格；**2** = 重算成功但有错误单元格（`#DIV/0!` `#REF!`
`#VALUE!` `#NAME?` `#N/A` `#NUM!` `#NULL!` 之一），照 `errors` 里列出的 sheet/cell 逐个排查公式，
不要当成功忽略；**1** = 重算本身失败（文件损坏、超时等），看 stderr 的中文提示。
`--timeout` 默认 120 秒，表很大或公式很多时调大。**任何新建或改完的表格，只要含公式，都要跑一遍
`recalc.py` 再交付**——不重算的话，Excel 以外的软件（甚至部分 Excel 设置）读到的是空值。

## 转换

`python $EXPERT_WORK_SKILLS_DIR/xlsx/scripts/convert.py IN.xlsx --to pdf --out-dir DIR`；
老格式 `.xls` / 带宏的 `.xlsm` 先用 `recalc.py` 转成普通 `.xlsx`（顺带完成重算）。输出 JSON：
`{"output": "生成文件的路径"}`。

## 检查成品

1. 用 openpyxl 重新打开成品，确认没坏：`openpyxl.load_workbook("成品.xlsx")` 不报错即可。
2. 含公式就跑 `recalc.py`：退出码非 0 时按上面「重算公式」一节处理，别把有错误单元格的表直接交付。
3. `python $EXPERT_WORK_SKILLS_DIR/xlsx/scripts/preview.py 成品.xlsx` 出图，用 `ask_image(path=...)`
   看版式：数字是否溢出列宽、中文是否方块、表头是否还在。Agent 没配看图能力时跳过这一步，并在回复里
   写明「未做视觉检查」。

## 字体

Excel 只记字体名，客户打开时用他自己电脑上的字体，没装就被 Office 自动换掉；沙箱里的字体只影响
预览图。默认中文用单元格 `Font(name="微软雅黑")`、西文默认 `Arial`。员工指定了某个字体时照写该
字体名，并在回复里提醒一句：「客户电脑没装这个字体时，Excel 会自动替换成别的字体。」品牌字体要求
严格时，建议改交付 PDF（字体嵌入文件，谁打开都一样）。

## 常见坑

- openpyxl 写的公式单元格 `.value` 是公式文本、没有缓存值 → 用 `load_workbook(..., data_only=True)`
  读到的是 `None`；必须先跑 `recalc.py` 再读，或者交付前就跑一遍。
- 合并单元格（`ws.merge_cells(...)`）只有左上角那个单元格能写值，其余单元格读写都会报错或忽略。
- 日期不设 `number_format`（如 `"yyyy-mm-dd"`）会被存成序列数字，Excel 里显示一串数字而不是日期。
- 数字格式只设了 `cell.number_format` 却整列宽度没跟着调，长数字会显示成 `###`——按内容估宽度或
  用「新建」里的列宽设置。
- 转换 / 重算前不检查 `ws._charts` / `ws._images` 直接改表，原有图表和图片会在保存后消失且没有
  任何报错提示。

## 做不到

数据透视表、宏（VBA）、原生 Excel 图表的精确还原（改用 matplotlib 出图插入）、改动后保留原文件里
已有的图表 / 图片——如实告诉用户「这个做不到」，不要假装做了。
