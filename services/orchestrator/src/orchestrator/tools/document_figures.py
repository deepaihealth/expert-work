"""文档图清单 —— B-64。

**判定只看来源侧 OOXML。** 最初设计想用 ``pdfplumber.Page.images`` 做统一探测器,
实测证伪:LibreOffice 把 EMF/WMF 与原生图表渲成**原生 PDF 矢量算子**而不是位图
XObject,``Page.images`` 对它们一律为 0 —— 照那个设计发出去,一页全是矢量图表的
slide 会被报成「这里没有图」,正是本功能要消灭的静默失效换一层重现。

``<a:graphicData uri>`` 反过来分得干净,而且**格式无关**:PNG 图和 EMF 图的 uri
完全相同(``.../drawingml/2006/picture``),所以判「有没有图」那一步永远不去碰可能
栅格化不了的字节(Pillow 在沙箱里 ``hasattr(Image.core, "drawwmf")`` 为 False)。
纯表格没有 ``graphicData`` —— 零误报。

**装饰图按显示尺寸过滤,不按原始像素**:一张 2000 px 的 logo 缩到 0.4 英寸摆在
页脚,像素很大但人看不见。实测分离是两个数量级 —— 页脚 logo 29x29 pt(占页
0.17 %)vs 血糖图表 288x192 pt(11.4 %)。

**这个模块只判定「有没有图 / 是什么图」,不渲染像素。** 渲染(LibreOffice ->
PDF -> 裁图)是后续任务;这里只产出一份文本清单,前置在 ``read_document`` 的
``content`` 头部,给模型一个「要不要花钱去看某一处」的判断依据。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from orchestrator.tools.file_ops import _snippet

#: 显示短边下限(pt)。低于此的判为装饰图。
MIN_FIGURE_EDGE_PT: Final[float] = 40.0
#: 显示面积占页比下限。低于此的判为装饰图。
MIN_FIGURE_AREA_RATIO: Final[float] = 0.01
#: 清单条目上限 —— 交替出现的图文页会把清单撑爆。
MAX_MAP_ENTRIES: Final[int] = 20
#: pptx 演讲者备注的每页字数上限(spec 14.3:今天全环境 0 条,这是保险不是收益)。
MAX_NOTES_CHARS: Final[int] = 500
#: PDF 空页判据 —— 字符数低于此判为空页(沿用 hermes 的 PDF_EMPTY_PAGE_CHARS)。
_PDF_EMPTY_PAGE_CHARS: Final[int] = 20
#: 空页三重阈值 —— 绝对页数下限。
_PDF_EMPTY_ABS_MIN: Final[int] = 2
#: 空页三重阈值 —— 占比下限。
_PDF_EMPTY_RATIO_MIN: Final[float] = 0.20
#: 空页三重阈值 —— 绝对页数(不看占比)下限。
_PDF_EMPTY_ABS_ALWAYS: Final[int] = 10

# 沙箱内探测片段的正文。``os`` / ``json`` / ``_P`` / ``_resolve`` 来自共享的
# ``_PRELUDE``(见 file_ops)。各格式解析库都是按需、分格式惰性 import 的,
# 缺库降级成 ``state="undetermined"`` 而不是让沙箱崩掉 —— 这个任务的核心
# 就是「测不了」永远不许悄悄变成「没有图」(见下面的 _main)。
_FIGURE_INVENTORY_MAIN = """

_PICTURE_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DIAGRAM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_EMU_PER_PT = 12700
_MIN_EDGE_PT = 40.0
_MIN_AREA_RATIO = 0.01
_MAX_NOTES = 500
_PDF_EMPTY_CHARS = 20
_PDF_EMPTY_ABS_MIN = 2
_PDF_EMPTY_RATIO_MIN = 0.20
_PDF_EMPTY_ABS_ALWAYS = 10


def _pptx_inventory(full):
    import pptx
    from pptx.util import Emu

    prs = pptx.Presentation(full)
    page_area_pt = (Emu(prs.slide_width).pt) * (Emu(prs.slide_height).pt)
    figures, skipped = [], 0
    for n, slide in enumerate(prs.slides, 1):
        title = None
        if slide.shapes.title is not None and slide.shapes.title.text_frame.text.strip():
            title = slide.shapes.title.text_frame.text.strip()
        notes = ""
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()[:_MAX_NOTES]
        pics, charts = [], []
        for sh in slide.shapes:
            if getattr(sh, "has_chart", False):
                ch = sh.chart
                try:
                    cats = [str(c) for c in ch.plots[0].categories]
                    series = {s.name: [None if v is None else float(v) for v in s.values]
                              for s in ch.series}
                except Exception:
                    cats, series = [], {}
                charts.append({"cats": cats, "series": series})
                continue
            if str(sh.shape_type).startswith("PICTURE") or str(sh.shape_type).startswith("GROUP"):
                w_pt, h_pt = Emu(sh.width).pt, Emu(sh.height).pt
                if min(w_pt, h_pt) < _MIN_EDGE_PT or (w_pt * h_pt) / page_area_pt < _MIN_AREA_RATIO:
                    skipped += 1
                    continue
                pics.append((w_pt, h_pt))
        for w_pt, h_pt in pics:
            figures.append({"unit": n, "kind": "picture", "count": 1,
                            "w_pt": round(w_pt, 1), "h_pt": round(h_pt, 1),
                            "anchor": title or "", "alt": None, "title": title,
                            "notes": notes})
        for data in charts:
            figures.append({"unit": n, "kind": "chart", "count": 1,
                            "w_pt": 0.0, "h_pt": 0.0, "anchor": title or "",
                            "alt": None, "title": title, "chart_data": data,
                            "notes": notes})
    return figures, skipped


# 依文档顺序遍历正文段落,含表格单元格里的段落 -- 表格(w:tbl/w:tr/w:tc,
# 任意嵌套深度)会递归下钻找。碰到一个 w:p 就不再往它内部找更多顶层段落:
# OOXML 里只有文本框(wp:txbx/w:txbxContent)才能在一个 w:p 里面再嵌一层
# w:p,而文本框自己的段落不算独立的正文单位 -- 它的说明文字不该被锚点逻辑
# 当成前文。它里面如果真有图片,不会漏:下面处理每个段落时用的是
# p.iter(),扫的是整棵子树,不受这层"不算独立单位"限制,文本框内嵌的图片
# 照样会被宿主段落找到。
def _docx_flow_paragraphs(elem, p_tag):
    for child in elem:
        if child.tag == p_tag:
            yield child
        else:
            yield from _docx_flow_paragraphs(child, p_tag)


def _docx_inventory(full):
    import xml.etree.ElementTree as ET
    import zipfile

    _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    _WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    _A = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def w(tag):
        return "{" + _W + "}" + tag

    def wp(tag):
        return "{" + _WP + "}" + tag

    def a(tag):
        return "{" + _A + "}" + tag

    with zipfile.ZipFile(full) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)  # noqa: S314 -- 自己刚解压的 zip 部件,不是网络抓取

    body = root.find(w("body"))
    paragraphs = list(_docx_flow_paragraphs(body, w("p"))) if body is not None else []

    page_w_pt, page_h_pt = 612.0, 792.0  # 拿不到 sectPr/pgSz 时用 Letter 兜底
    sect_pr = next(root.iter(w("sectPr")), None)
    if sect_pr is not None:
        pg_sz = sect_pr.find(w("pgSz"))
        if pg_sz is not None:
            w_twip, h_twip = pg_sz.get(w("w")), pg_sz.get(w("h"))
            if w_twip and h_twip:
                page_w_pt, page_h_pt = int(w_twip) / 20.0, int(h_twip) / 20.0
    page_area_pt = page_w_pt * page_h_pt

    figures, skipped = [], 0
    last_text = ""
    for idx, p in enumerate(paragraphs, 1):
        containers = list(p.iter(wp("inline"))) + list(p.iter(wp("anchor")))
        if not containers:
            text = "".join(t.text or "" for t in p.iter(w("t"))).strip()
            if text:
                last_text = text
            continue
        for container in containers:
            graphic_data = next(container.iter(a("graphicData")), None)
            if graphic_data is None or graphic_data.get("uri") != _PICTURE_URI:
                # 文本框/OLE/其他 wp:inline 载荷用的是同一套 docPr+extent 形状,
                # 只是 uri 不一样 —— 挡掉它们,零误报是这道门存在的唯一理由;
                # 不挡的话一个文本框形状就会被错报成图片。
                continue
            doc_pr = container.find(wp("docPr"))
            extent = container.find(wp("extent"))
            if doc_pr is None or extent is None:
                continue
            cx, cy = extent.get("cx"), extent.get("cy")
            if not cx or not cy:
                continue
            w_pt, h_pt = int(cx) / _EMU_PER_PT, int(cy) / _EMU_PER_PT
            if min(w_pt, h_pt) < _MIN_EDGE_PT or (w_pt * h_pt) / page_area_pt < _MIN_AREA_RATIO:
                skipped += 1
                continue
            alt = doc_pr.get("descr") or doc_pr.get("name")
            figures.append({"unit": idx, "kind": "picture", "count": 1,
                            "w_pt": round(w_pt, 1), "h_pt": round(h_pt, 1),
                            "anchor": last_text[-60:], "alt": alt, "title": None})
    return figures, skipped


def _xlsx_ref(data_source):
    if data_source is None:
        return None
    ref = getattr(data_source, "numRef", None) or getattr(data_source, "strRef", None)
    return getattr(ref, "f", None) if ref is not None else None


def _xlsx_inventory(full):
    import openpyxl

    wb = openpyxl.load_workbook(full, data_only=True)
    try:
        figures, skipped = [], 0
        for sheet_idx, ws in enumerate(wb.worksheets, 1):
            # spec 9.4 —— xlsx 不渲染图片,所有图片一律按装饰图处理。
            skipped += len(list(getattr(ws, "_images", None) or []))
            for ch in list(getattr(ws, "_charts", None) or []):
                series = {}
                cats = None
                for i, s in enumerate(list(getattr(ch, "series", None) or [])):
                    val_ref = _xlsx_ref(getattr(s, "val", None))
                    name_ref = _xlsx_ref(getattr(s, "tx", None)) or ("series_" + str(i + 1))
                    series[name_ref] = val_ref
                    if cats is None:
                        cats = _xlsx_ref(getattr(s, "cat", None))
                figures.append({"unit": sheet_idx, "kind": "chart", "count": 1,
                                "w_pt": 0.0, "h_pt": 0.0, "anchor": ws.title,
                                "alt": None, "title": None,
                                "chart_data": {"cats": cats, "series": series}})
        return figures, skipped
    finally:
        wb.close()


def _pdf_inventory(full):
    import itertools

    import pdfplumber

    with pdfplumber.open(full) as pdf:
        pages = pdf.pages
        total = len(pages)
        if total < 2:
            return [], 0
        texts = [(page.extract_text() or "").strip() for page in pages]
        empty_idx = [i for i, t in enumerate(texts, 1) if len(t) < _PDF_EMPTY_CHARS]
        empty_count = len(empty_idx)
        ratio = empty_count / total
        noisy_enough = empty_count >= _PDF_EMPTY_ABS_ALWAYS or (
            empty_count >= _PDF_EMPTY_ABS_MIN and ratio >= _PDF_EMPTY_RATIO_MIN
        )
        if not noisy_enough:
            return [], 0
        figures = []
        for _, group in itertools.groupby(enumerate(empty_idx), lambda pair: pair[1] - pair[0]):
            run = [page_no for _, page_no in group]
            start, end = run[0], run[-1]
            anchor = ""
            for j in range(start - 1, 0, -1):
                if texts[j - 1]:
                    anchor = texts[j - 1][-60:]
                    break
            page_obj = pages[start - 1]
            figures.append({"unit": start, "kind": "scanned_page", "count": end - start + 1,
                            "w_pt": round(float(page_obj.width), 1),
                            "h_pt": round(float(page_obj.height), 1),
                            "anchor": anchor, "alt": None, "title": None})
        return figures, 0


def _envelope(state, fmt, *, figures=None, skipped=0, reason=None):
    env = {"ok": True, "state": state, "format": fmt,
           "figures": figures or [], "skipped_decorative": skipped}
    if reason is not None:
        env["reason"] = reason
    return env


def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    try:
        size = os.path.getsize(full)
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    if size > _P["max_bytes"]:
        return _envelope("undetermined", None, reason="file_too_large")
    ext = os.path.splitext(full)[1].lower().lstrip(".")
    builders = {
        "pptx": _pptx_inventory,
        "docx": _docx_inventory,
        "xlsx": _xlsx_inventory,
        "pdf": _pdf_inventory,
    }
    build = builders.get(ext)
    if build is None:
        return _envelope("none", ext)
    try:
        figures, skipped = build(full)
    except ImportError:
        return _envelope("undetermined", ext, reason="parser_unavailable")
    except Exception:
        return _envelope("undetermined", ext, reason="parse_failed")
    state = "figures" if figures else "none"
    return _envelope(state, ext, figures=figures, skipped=skipped)


print(json.dumps(_main()))
"""


def build_figure_inventory_wrapper(rel: str, *, ws: str, max_bytes: int) -> str:
    """沙箱片段:对 ``ws/rel`` 生成图清单,打印模块头部约定的那份 JSON 信封。"""
    return _snippet({"ws": ws, "rel": rel, "max_bytes": max_bytes}, _FIGURE_INVENTORY_MAIN)


def _describe_figure(figure: Mapping[str, Any]) -> str:
    kind_label = {
        "picture": "图片",
        "chart": "图表",
        "diagram": "SmartArt",
        "scanned_page": "疑似扫描页",
    }.get(str(figure.get("kind")), str(figure.get("kind")))
    parts = [f"第 {figure.get('unit')} 处", kind_label]
    count = figure.get("count")
    if isinstance(count, int) and count > 1:
        parts.append(f"x{count}")
    title = figure.get("title")
    if title:
        parts.append(f"《{title}》")
    w_pt, h_pt = figure.get("w_pt"), figure.get("h_pt")
    if w_pt and h_pt:
        parts.append(f"{w_pt:g}x{h_pt:g}pt")
    anchor = figure.get("anchor")
    if anchor:
        parts.append(f"-- 紧邻:{str(anchor)[:40]}")
    return " ".join(parts)


def render_figure_map(env: Mapping[str, Any]) -> str:
    """信封 -> 前置在 ``content`` 头部的清单块(空串 = 不加块)。

    **前置不是尾置**:``read_document`` 返回 ``text[:cap]`` 硬截断,尾部的告警
    会被砍掉。hermes 出于同构的理由(它那边是 ``read_file`` 分页,footer 可能
    永远取不到)也是 PREPEND。

    三态:``undetermined`` 必须明确说「测不了」而不是悄悄变成「没有图」;
    ``none`` 或空清单不加块(维持今天的行为);``figures`` 才渲染清单,并且
    永远带上三条文案纪律(spec 10.2,抄 hermes):禁止全量、给精确恢复动作、
    不点名具体 skill。
    """
    state = env.get("state")
    if state == "undetermined":
        return (
            "[图片情况无法确定:本文档的图片清单没能生成"
            f"(原因:{env.get('reason', 'unknown')})。"
            "**这不等于文档里没有图** —— 如果内容读起来有缺口,先怀疑这里。]\n\n"
        )
    figures = list(env.get("figures") or ())
    if state != "figures" or not figures:
        return ""

    total = len(figures)
    shown = figures[:MAX_MAP_ENTRIES]
    parts = [f"[本文档有 {total} 处图/图表,未包含在下面的文字里:"]
    for figure in shown:
        parts.append("\n  " + _describe_figure(figure))
    remaining = total - len(shown)
    if remaining > 0:
        parts.append(f"\n  ...另有 {remaining} 处未列出")
    parts.append(
        "\n这些内容不在上面的文字里。挑你真正需要的那几处 —— 不要把所有页都取一遍。"
        "\n要看某一处:调用 read_page,传文档路径和上面的编号。"
        "\n如果缺口很大而且都要看,先确认有没有可用的 OCR 技能(skills_list)。]\n\n"
    )
    return "".join(parts)
