"""``document_figures`` —— 来源侧图清单。

两层,与 test_read_document 同构:
  1. 沙箱内探测片段,拿本地 temp 工作区直接跑;
  2. 信封 → 清单文本的渲染。

docx/pdf 的结构性不变式测试**手搭字节,不依赖第三方解析库**:
``python-docx`` 不在 ``uv.lock`` 里(这个仓库的文档解析走
``markitdown[docx,pdf,pptx,xlsx]``,它的 docx extra 拉的是
``mammoth``/``lxml``,不是 ``python-docx``),``reportlab``/``pypdf`` 也不在。
用 ``pytest.importorskip`` 包一层会在 CI 里**静默跳过**,而这正是 B-64 要
消灭的那类失效在测试里又出现了一次。docx 用 ``zipfile`` 手写最小 OOXML
(与 ``_docx_inventory`` 读的是同一套 XML 形状);pdf 用手拼字节(带正确
xref 偏移量),``pdfplumber`` 本身在 ``uv.lock`` 里,不需要额外的库来写。
"""

from __future__ import annotations

import ast
import io
import json
import zipfile
from pathlib import Path

import pytest

from orchestrator.tools.document_figures import (
    _FIGURE_INVENTORY_MAIN,
    MIN_FIGURE_EDGE_PT,
    SUPPORTED_EXTENSIONS,
    build_figure_inventory_wrapper,
    render_figure_map,
)


def _run(tmp_path: Path, rel: str) -> dict:
    """本地执行沙箱片段(与 test_read_document 的做法一致)。"""
    code = build_figure_inventory_wrapper(rel, ws=str(tmp_path), max_bytes=25 * 1024 * 1024)
    ns: dict = {}
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<snippet>", "exec"), ns)  # noqa: S102
    return json.loads(buf.getvalue())


def _png(w: int, h: int) -> io.BytesIO:
    Image = pytest.importorskip("PIL.Image")  # noqa: N806
    b = io.BytesIO()
    Image.new("RGB", (w, h), "#cde").save(b, "PNG")
    b.seek(0)
    return b


def test_pptx_picture_slide_is_listed(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches

    prs = pptx.Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "封面"
    s1.shapes.add_picture(_png(800, 500), Inches(1), Inches(2), width=Inches(5))
    prs.save(tmp_path / "d.pptx")

    env = _run(tmp_path, "d.pptx")
    assert env["ok"] is True
    assert env["state"] == "figures"
    assert env["format"] == "pptx"
    assert [f["unit"] for f in env["figures"]] == [1]
    assert env["figures"][0]["kind"] == "picture"
    assert env["figures"][0]["title"] == "封面"


def test_pptx_native_chart_becomes_data_not_a_figure(tmp_path: Path) -> None:
    """图表是数据,不该逼模型去看图。"""
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "趋势"
    cd = CategoryChartData()
    cd.categories = ["7月", "8月", "9月"]
    cd.add_series("血糖", (6.1, 5.8, 5.5))
    s.shapes.add_chart(XL_CHART_TYPE.LINE, Inches(1), Inches(1.5), Inches(6), Inches(4), cd)
    prs.save(tmp_path / "c.pptx")

    env = _run(tmp_path, "c.pptx")
    chart = next(f for f in env["figures"] if f["kind"] == "chart")
    assert chart["chart_data"]["cats"] == ["7月", "8月", "9月"]
    assert chart["chart_data"]["series"] == {"血糖": [6.1, 5.8, 5.5]}


# ---------------------------------------------------------------------------
# docx —— 手搭最小 OOXML,不经过 python-docx(不在 uv.lock 里,CI 会跳过)。
# ---------------------------------------------------------------------------

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml"
  ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
  Target="word/document.xml"/>
</Relationships>"""

_DOCX_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def _docx_text(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx_drawing(rid: str, name: str, cx: int, cy: int, descr: str | None = None) -> str:
    """一个含 ``w:drawing`` 的段落 —— 与 ``_docx_inventory`` 读的 XML 形状
    (``wp:inline`` / ``wp:docPr`` / ``wp:extent`` / ``a:graphicData`` /
    ``a:blip``)一致。"""
    descr_attr = f' descr="{descr}"' if descr else ""
    return (
        "<w:p><w:r><w:drawing>"
        f'<wp:inline><wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="1" name="{name}"{descr_attr}/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>"
    )


def _build_docx(path: Path, body: str, *, rels: dict[str, str] | None = None) -> None:
    """拿 ``zipfile`` 手搭一份最小合法 docx。``rels`` 是 ``{关系 id: 媒体文件名}``,
    只有引用了图片关系的 fixture 才需要传。"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_ROOT_RELS)
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {_DOCX_NS}><w:body>{body}"
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
            "</w:body></w:document>"
        )
        zf.writestr("word/document.xml", document_xml)
        if rels:
            entries = "".join(
                f'<Relationship Id="{rid}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/{name}"/>'
                for rid, name in rels.items()
            )
            zf.writestr(
                "word/_rels/document.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f"{entries}</Relationships>",
            )
            for name in set(rels.values()):
                zf.writestr(f"word/media/{name}", _png(4, 4).read())


def test_decorative_logo_is_skipped(tmp_path: Path) -> None:
    """页脚 logo 28.8x28.8pt 占页 0.17%,不进清单;同页的 288x192pt 图进。"""
    body = (
        _docx_text("A paragraph before the chart mentions the glucose trend chart below.")
        + _docx_drawing("rId1", "Picture 1", 3657600, 2438400)
        + _docx_text("A small logo follows below.")
        + _docx_drawing("rId2", "Picture 2", 365760, 365760)
    )
    _build_docx(tmp_path / "d.docx", body, rels={"rId1": "image1.png", "rId2": "image2.png"})

    env = _run(tmp_path, "d.docx")
    assert len(env["figures"]) == 1
    assert env["skipped_decorative"] == 1
    assert env["figures"][0]["w_pt"] == pytest.approx(288.0, abs=1.0)
    assert min(env["figures"][0]["w_pt"], env["figures"][0]["h_pt"]) >= MIN_FIGURE_EDGE_PT


def test_thin_strip_is_skipped_by_the_edge_clause(tmp_path: Path) -> None:
    """``test_decorative_logo_is_skipped`` 的 28.8x28.8pt logo 同时撞了 edge
    (< 40pt)与 area(0.17% < 1%)两条阈值,单独改 ``MIN_FIGURE_EDGE_PT`` 不会让
    它露出来(见 M1 变异自证)。这里造一个只会撞 edge、撞不到 area 阈值的窄条
    (30x170pt,面积占页约 1.05% >= ``MIN_FIGURE_AREA_RATIO``),专门钉住
    ``MIN_FIGURE_EDGE_PT`` 这一条独立起作用。"""
    body = _docx_drawing("rId1", "Picture 1", 381000, 2159000)
    _build_docx(tmp_path / "thin.docx", body, rels={"rId1": "image1.png"})

    env = _run(tmp_path, "thin.docx")
    assert env["figures"] == []
    assert env["skipped_decorative"] == 1


def test_docx_anchor_is_the_sentence_before_the_figure(tmp_path: Path) -> None:
    body = _docx_text(
        "Section three tail sentence about the glucose trend chart follows below."
    ) + _docx_drawing("rId1", "Picture 1", 3657600, 2438400)
    _build_docx(tmp_path / "a.docx", body, rels={"rId1": "image1.png"})

    env = _run(tmp_path, "a.docx")
    assert "glucose trend chart" in env["figures"][0]["anchor"]


def test_docx_picture_inside_table_cell_is_reported(tmp_path: Path) -> None:
    """图片放进表格单元格是最普通的 Word 排版之一 —— ``body.findall(w:p))``
    只看 ``w:body`` 的直接子节点,单元格里的段落(``w:tbl/w:tr/w:tc/w:p``)
    永远不会被访问到,于是这份文档会被判成 ``state="none"``,正是本功能要
    消灭的那类静默降级。"""
    drawing = _docx_drawing("rId1", "Picture 1", 3657600, 2438400)
    body = (
        _docx_text("Intro paragraph before the table.")
        + "<w:tbl><w:tr><w:tc>"
        + "<w:p><w:r><w:t>Cell caption mentions the glucose trend chart.</w:t></w:r></w:p>"
        + drawing
        + "</w:tc></w:tr></w:tbl>"
    )
    _build_docx(tmp_path / "table_pic.docx", body, rels={"rId1": "image1.png"})

    env = _run(tmp_path, "table_pic.docx")
    assert env["state"] == "figures"
    assert len(env["figures"]) == 1
    assert env["figures"][0]["kind"] == "picture"


def test_plain_table_is_not_a_figure(tmp_path: Path) -> None:
    """纯表格零误报,加一个「非图片」的 ``wp:inline``(``graphicData`` 的
    ``uri`` 是文本框而不是图片)—— 这是唯一能让这条测试在拿掉 uri 门时变红
    的构造:纯表格本身没有 ``graphicData``,拿掉 uri 门也测不出区别;
    这个非图片 drawing 的 ``docPr``+``extent`` 形状和真图片一模一样,只有
    uri 门才挡得住它。"""
    non_picture_drawing = (
        "<w:p><w:r><w:drawing>"
        '<wp:inline><wp:extent cx="3657600" cy="2438400"/>'
        '<wp:docPr id="9" name="TextBox 1"/>'
        "<a:graphic>"
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"/>'
        "</a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>"
    )
    body = (
        _docx_text("Table page")
        + "<w:tbl>"
        + "<w:tr><w:tc><w:p><w:r><w:t>0-0</w:t></w:r></w:p></w:tc>"
        + "<w:tc><w:p><w:r><w:t>0-1</w:t></w:r></w:p></w:tc></w:tr>"
        + "<w:tr><w:tc><w:p><w:r><w:t>1-0</w:t></w:r></w:p></w:tc>"
        + "<w:tc><w:p><w:r><w:t>1-1</w:t></w:r></w:p></w:tc></w:tr>"
        + "</w:tbl>"
        + non_picture_drawing
    )
    _build_docx(tmp_path / "t.docx", body)

    env = _run(tmp_path, "t.docx")
    assert env["state"] == "none"
    assert env["figures"] == []


def test_docx_real_word_shaped_output_also_parses(tmp_path: Path) -> None:
    """交叉验证,不是结构性不变式的唯一承载者 —— 那些都在上面的手搭测试里,
    不依赖任何 docx 库,CI 永远会跑。``python-docx`` 不在 ``uv.lock`` 里,
    本地装了才跑;这里只确认真实 python-docx 产出的 XML(cNvGraphicFramePr
    等手搭版本没有的细节)一样能被解析,不重复钉阈值精确值。"""
    docx = pytest.importorskip("docx")
    from docx.shared import Inches

    d = docx.Document()
    d.add_paragraph("A paragraph mentions the glucose trend chart below.")
    d.add_picture(_png(600, 400), width=Inches(4))
    d.add_picture(_png(40, 40), width=Inches(0.4))
    d.save(tmp_path / "real.docx")

    env = _run(tmp_path, "real.docx")
    assert env["state"] == "figures"
    assert len(env["figures"]) == 1
    assert env["figures"][0]["kind"] == "picture"


def test_xlsx_never_asks_for_pixels(tmp_path: Path) -> None:
    """xlsx 的图表是单元格引用;不渲染,只报引用。"""
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.chart import LineChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "体检"
    for row in (["月", "血糖"], ["7月", 6.1], ["8月", 5.8], ["9月", 5.5]):
        ws.append(row)
    ch = LineChart()
    ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=4), titles_from_data=True)
    ws.add_chart(ch, "D2")
    wb.save(tmp_path / "s.xlsx")

    env = _run(tmp_path, "s.xlsx")
    assert all(f["kind"] != "picture" for f in env["figures"])
    chart = next(f for f in env["figures"] if f["kind"] == "chart")
    assert "体检" in json.dumps(chart["chart_data"], ensure_ascii=False)


def test_corrupt_file_is_undetermined_not_none(tmp_path: Path) -> None:
    """「测不了」必须与「没有图」分开 —— 三态的核心。"""
    (tmp_path / "bad.pptx").write_bytes(b"not a zip at all")
    env = _run(tmp_path, "bad.pptx")
    assert env["state"] == "undetermined"
    assert env["state"] != "none"


# ---------------------------------------------------------------------------
# pdf —— 手拼最小合法 PDF 字节(带正确 xref 偏移量)。pdfplumber 在 uv.lock
# 里;reportlab/pypdf 都不在,不引入新依赖。
# ---------------------------------------------------------------------------


def _build_pdf(page_texts: list[str | None]) -> bytes:
    """``page_texts[i]`` 非空则该页有一段显示该文字的内容流,为 ``None`` 则
    内容流为空(``pdfplumber`` 的 ``extract_text()`` 判定为空页)。"""
    n_pages = len(page_texts)
    catalog_num, pages_num = 1, 2
    font_num = 3 + n_pages * 2
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(n_pages))

    objs: dict[int, str] = {
        catalog_num: f"<< /Type /Catalog /Pages {pages_num} 0 R >>",
        pages_num: f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>",
    }
    for i, text in enumerate(page_texts):
        page_obj, content_obj = 3 + i * 2, 4 + i * 2
        objs[page_obj] = (
            f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_obj} 0 R >>"
        )
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET" if text else ""
        objs[content_obj] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objs[font_num] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    buf = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(buf)
        buf += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("latin-1")
    xref_offset = len(buf)
    max_num = max(objs)
    buf += f"xref\n0 {max_num + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for num in range(1, max_num + 1):
        buf += f"{offsets.get(num, 0):010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Size {max_num + 1} /Root {catalog_num} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()
    return bytes(buf)


def test_pdf_ratio_threshold_triggers_scanned_page(tmp_path: Path) -> None:
    """空页占比 >= 20% 且绝对数 >= 2 才触发;连续空页压成一个区间,锚点取
    区间前最近一个有字页的尾部。"""
    pytest.importorskip("pdfplumber")
    texts = [
        "Page one has a reasonably long body paragraph of text content.",
        "Page two continues with a fairly long paragraph of body text.",
        None,
        None,
        None,
        "Page six resumes with another long paragraph of body content.",
        "Page seven body text paragraph continues normally here too.",
        "Page eight final body text paragraph wraps up the document.",
    ]
    (tmp_path / "ratio.pdf").write_bytes(_build_pdf(texts))

    env = _run(tmp_path, "ratio.pdf")
    assert env["state"] == "figures"
    assert len(env["figures"]) == 1
    fig = env["figures"][0]
    assert fig["kind"] == "scanned_page"
    assert fig["unit"] == 3
    assert fig["count"] == 3
    assert "body text" in fig["anchor"]


def test_pdf_absolute_threshold_triggers_regardless_of_ratio(tmp_path: Path) -> None:
    """空页绝对数 >= 10 时即使占比 < 20% 也要触发 —— 三重阈值的第二支。"""
    pytest.importorskip("pdfplumber")
    n = 60
    blanks = set(range(1, n, 6))
    assert len(blanks) == 10
    texts = [
        None
        if i in blanks
        else f"Page {i} has a reasonably long paragraph of body text content here."
        for i in range(1, n + 1)
    ]
    (tmp_path / "absolute.pdf").write_bytes(_build_pdf(texts))

    env = _run(tmp_path, "absolute.pdf")
    assert env["state"] == "figures"
    assert len(env["figures"]) == 10  # 10 个孤立空页,互不相邻 -> 10 个独立区间
    assert sum(f["count"] for f in env["figures"]) == 10


def test_pdf_below_both_thresholds_stays_none(tmp_path: Path) -> None:
    """占比 16.7% < 20% 且绝对数 2 < 10 —— 两条阈值都不到,必须保持 none。"""
    pytest.importorskip("pdfplumber")
    texts = [
        None
        if i in (3, 9)
        else f"Page {i} has a reasonably long paragraph of body text content here."
        for i in range(1, 13)
    ]
    (tmp_path / "below.pdf").write_bytes(_build_pdf(texts))

    env = _run(tmp_path, "below.pdf")
    assert env["state"] == "none"
    assert env["figures"] == []


def test_pdf_zero_pages_stays_none_not_undetermined(tmp_path: Path) -> None:
    """总页数 < 2 直接 none —— 用 0 页(不是 1 页)来钉这条分支,是唯一能
    让它在拿掉这条分支时变红的构造:1 页时 ``empty_count`` 最多是 1,
    天然就够不到 ``_PDF_EMPTY_ABS_MIN=2``,不管这条 ``total < 2`` 分支在不
    在,1 页文档都会落回 none,分支拿掉也测不出来。0 页文档不一样:拿掉
    这条分支会让 ``empty_count / total`` 除以 0 崩掉,被上层 ``except
    Exception`` 接住变成 undetermined —— 这才是这条分支真正防住的差异。"""
    pytest.importorskip("pdfplumber")
    (tmp_path / "empty.pdf").write_bytes(_build_pdf([]))

    env = _run(tmp_path, "empty.pdf")
    assert env["state"] == "none"
    assert env["figures"] == []


# --- 清单文本渲染 -----------------------------------------------------------


def test_map_says_it_could_not_tell() -> None:
    text = render_figure_map({"ok": True, "state": "undetermined", "reason": "parse_failed"})
    assert "无法确定" in text


def test_map_is_empty_when_no_figures() -> None:
    assert render_figure_map({"ok": True, "state": "none", "figures": []}) == ""


def test_map_forbids_rendering_everything() -> None:
    env = {
        "ok": True,
        "state": "figures",
        "format": "pptx",
        "figures": [
            {
                "unit": 3,
                "kind": "picture",
                "count": 1,
                "w_pt": 288.0,
                "h_pt": 192.0,
                "anchor": "趋势",
                "alt": None,
                "title": "趋势",
            }
        ],
        "skipped_decorative": 0,
    }
    text = render_figure_map(env)
    assert "不要把所有" in text
    assert "read_page" in text
    assert "OCR" not in text.upper() or "技能" in text  # 不点名具体 skill


def test_map_caps_entries() -> None:
    env = {
        "ok": True,
        "state": "figures",
        "format": "pptx",
        "skipped_decorative": 0,
        "figures": [
            {
                "unit": i,
                "kind": "picture",
                "count": 1,
                "w_pt": 288.0,
                "h_pt": 192.0,
                "anchor": "",
                "alt": None,
                "title": None,
            }
            for i in range(1, 31)
        ],
    }
    text = render_figure_map(env)
    assert text.count("\n  ") <= 22  # 20 条 + 折叠行的余量
    assert "另有" in text


# ---------------------------------------------------------------------------
# B-64 fix round 2 —— SUPPORTED_EXTENSIONS 与沙箱内 builders 表不许悄悄分叉。
# ---------------------------------------------------------------------------


def _sandbox_dispatch_extensions() -> frozenset[str]:
    """从 ``_FIGURE_INVENTORY_MAIN`` 的源码里 ``ast`` 解析出 ``builders`` 字典
    的键集合 —— 不是手抄一份(那只会造出第三份字面量),是真的读沙箱片段里
    那段决定"这个格式能不能分析"的代码。"""
    tree = ast.parse(_FIGURE_INVENTORY_MAIN)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "builders"
            and isinstance(node.value, ast.Dict)
        ):
            keys: list[str] = []
            for key_node in node.value.keys:
                assert isinstance(key_node, ast.Constant), key_node
                assert isinstance(key_node.value, str), key_node.value
                keys.append(key_node.value)
            return frozenset(keys)
    raise AssertionError("builders dict 没在 _FIGURE_INVENTORY_MAIN 里找到")


def test_supported_extensions_matches_sandbox_dispatch_table() -> None:
    """``read_document.py`` 的控制器侧早退闸门(``SUPPORTED_EXTENSIONS``)必须
    与沙箱片段里真正的 dispatch 表(``builders``)一字不差 —— 单靠注释拴住两处
    字面量,分叉是迟早的事,而且分叉方向恰好是这个功能最怕的那种:沙箱那边
    多认一个格式而控制器不知道,有图的文档会在探测都没跑的情况下被判成
    "没有图",还是静默的。"""
    assert SUPPORTED_EXTENSIONS == _sandbox_dispatch_extensions()
