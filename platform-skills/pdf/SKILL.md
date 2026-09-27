---
name: pdf
description: 新建 PDF，或对已有 PDF 做合并、拆分、旋转、加水印、盖章时使用。只读取 PDF 内容用 read_document，不用本技能；PDF 里的文字不能直接改，要回到源文件改后重新生成。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 通用
---

新建 PDF、或对已有 PDF 做合并 / 拆分 / 旋转 / 加水印 / 盖章时用本技能。读取 PDF 文字 / 表格：用
`read_document`；需要精确表格结构（跨行合并单元格等）时用 `pdfplumber`，两种情况都不用本技能。把
Word / PPT / Excel 转成 PDF，用 docx / pptx / xlsx 任一技能里的 `convert.py --to pdf`（三份是同一个
脚本，每份都能转全部格式，手边加载了哪个就用哪个），本技能只处理输入已经是 PDF 的场景。做不到的事见文末「做不到」一节。

## 环境

- 已预装、直接用、不要装：python-docx、python-pptx、openpyxl、pypdf、pdfplumber、pdf2image、weasyprint、matplotlib、Pillow、pandas；命令行 soffice（LibreOffice）、pdftoppm；中文字体 Noto Sans CJK。
- 需要别的 Python 包时可以 pip install（走国内镜像，很快），但沙箱空闲后会回收、装过的包会丢；本技能不依赖任何需要现装的包。
- 没有 npm：不要走任何 JavaScript 路线。
- 工作目录 /workspace；本技能脚本在 $EXPERT_WORK_SKILLS_DIR/pdf/scripts/。
- 成品必须用 save_artifact 登记，否则用户拿不到。
- 只读取已有文档内容用 read_document / read_page。

## 新建

用 HTML + CSS 拼字符串，交给 weasyprint 渲染成 PDF；中文字体只要在 CSS 里写对 `font-family`
（沙箱里的字体名见「字体」一节），weasyprint 会自动把用到的字形嵌入 PDF 文件。下面的骨架代码可以
原样执行，生成一份含 A4 页面设置、页眉、页脚页码、中文标题、表格、图表的两页示例：

```python title=skeleton
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from weasyprint import HTML

CN_FONT = "Noto Sans CJK SC"

fig, ax = plt.subplots(figsize=(4, 2.4))
ax.bar(["一月", "二月", "三月"], [3, 5, 2])
fig.tight_layout()
chart_path = Path("_skeleton_chart.png").resolve()
fig.savefig(chart_path, dpi=150)
plt.close(fig)

rows = "".join(f"<tr><td>示例任务 {i}</td><td>张三</td><td>进行中</td></tr>" for i in range(1, 4))

html = f"""<html><head><style>
@page {{
  size: A4;
  margin: 2.5cm 2cm 2cm 2cm;
  @top-center {{ content: "示例页眉"; font-family: "{CN_FONT}"; font-size: 9pt; color: #666; }}
  @bottom-center {{ content: counter(page) " / " counter(pages); font-family: "{CN_FONT}"; font-size: 9pt; color: #666; }}
}}
body {{ font-family: "{CN_FONT}"; }}
h1 {{ font-size: 20pt; }}
table {{ width: 100%; border-collapse: collapse; break-inside: avoid; }}
th, td {{ border: 1px solid #999; padding: 6px 8px; font-size: 10.5pt; }}
thead {{ display: table-header-group; }}
.card {{ break-inside: avoid; margin-top: 16pt; }}
</style></head>
<body>
<h1>示例方案</h1>
<p>本文档由骨架代码生成，演示中文标题、分页、表格与图表的基本写法。</p>
<div class="card">
<table>
<thead><tr><th>项目</th><th>负责人</th><th>进度</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
<div style="break-before: page">
<h1>第二页：数据图表</h1>
<img src="{chart_path}" style="width:80%">
</div>
</body></html>"""

HTML(string=html).write_pdf("骨架 示例.pdf")
```

要点：分页用 `@page` 设纸张与页边距，`@top-center` / `@bottom-center` 等写页眉页脚，页码用
`counter(page)` / `counter(pages)`（不是手写数字，页数变化时自动对）；不想让表格行 / 卡片被硬切到
下一页用 `break-inside: avoid`；想强制另起一页用 `break-before: page`；字体名集中成一个常量
（`CN_FONT`），改一处即全局生效。

## 修改已有 PDF

两条硬规则：① 所有子命令都输出到**新文件**，绝不覆盖原件（脚本层面强制：输入输出同路径直接报错
退出，`merge` 的每一个输入都会与输出比较）；② PDF 里的文字**不能**直接改——PDF 不像 docx/pptx
那样能定位到一段文字重写，要么回到生成它的源文件（Word/HTML 等）改完重新生成 PDF，要么明确告知
用户做不到。

`python $EXPERT_WORK_SKILLS_DIR/pdf/scripts/pdf_ops.py <子命令> ...`，页码范围写法与 `preview.py`
一致（`1-3,5`、`all`）：

- `info IN.pdf` → `{"pages", "sizes_pt", "encrypted", "has_text"}`：页数、每页 `[宽, 高]`（pt）、
  是否加密、有没有文字层。加密文件也能问 `info`（如实报 `encrypted: true`），其它子命令遇到加密
  文件一律报错拒绝，不尝试破解或去除密码。
- `merge OUT.pdf IN1.pdf IN2.pdf ...` → 依次合并成一份，各输入原有的页面尺寸各自保留（不会被
  拉伸成统一大小）。
- `split IN.pdf OUT_DIR [--pages 1-3,5]`：不给 `--pages` 时每页拆成一个文件；给了就把这些页抽成
  一个新文件。
- `rotate IN.pdf OUT.pdf --degrees 90|180|270 [--pages 2,4]`：不给 `--pages` 默认整份都转。
- `watermark IN.pdf OUT.pdf --text "内部资料" [--opacity 0.15] [--angle 45] [--font "..."]`：每页
  叠加一层文字水印，水印本身也用 weasyprint 生成，中文照样嵌入字体，不会变方块。
- `stamp IN.pdf OUT.pdf --stamp 章.pdf [--pages 4]`：把另一份 PDF（例如盖章图、签名做成的一页）
  的第 1 页叠加到指定页上，以页面显示时的左下角为原点；不给 `--pages` 默认整份都盖。水印与盖章都按
  页面显示方向叠加，旋转过的页也是正的。

## 检查成品

1. 用 pypdf 重新打开成品，确认没坏：`PdfReader("成品.pdf")` 不报错即可；顺手跑一下
   `pdf_ops.py info 成品.pdf` 核对页数对不对。
2. `python $EXPERT_WORK_SKILLS_DIR/pdf/scripts/preview.py 成品.pdf` 出图。
3. 用 `ask_image(path=...)` 看首页和信息最密的一页：中文是否方块、文字是否溢出 / 重叠、版式是否
   错乱。Agent 没配看图能力时跳过这一步，并在回复里写明「未做视觉检查」。

## 字体

PDF 会把用到的字形**嵌入文件本身**，所以不管在谁的电脑上打开都长一样（这点和 Word/PPT 只记字体名、
换电脑可能换字体不同）。中文默认用沙箱里的 `Noto Sans CJK SC`；员工指定了别的字体时，先用
`fc-list :lang=zh` 查沙箱里有没有这个字体——有就照写，没有就如实告诉用户「沙箱里没有这个字体，已
用 Noto Sans CJK 代替」，不要尝试联网下载安装字体。

## 常见坑

- weasyprint 对 CSS `flex` / `grid` 支持有限，排版容易跑偏 → 需要行列对齐的内容用 `<table>` 布局。
- `HTML(string=...)` 渲染时，`<img>` 用相对路径会被**静默丢弃**（不报错，PDF 里就是没有这张图）→
  图片一律用绝对路径（如骨架代码里 `Path(...).resolve()`），或给 `HTML(..., base_url=...)`。
- 长表格跨页时表头不会自动重复 → `<thead>` 配 `display: table-header-group`（骨架代码已给出）。
- `rotate` 的 `--degrees` 只接受 90 / 180 / 270，其它角度直接拒绝。
- 水印 / 盖章是叠加在原内容之上，原页面本来就很满时可能和正文重叠，叠加前留意版面空间。

## 做不到

表单域填写、加密 / 解密、扫描件 OCR、直接编辑 PDF 里已有的文字——如实告诉用户「这个做不到」，
建议回到源文件改后用「新建」一节的方法重新生成。
