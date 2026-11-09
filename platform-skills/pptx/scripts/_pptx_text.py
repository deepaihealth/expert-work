"""Text model shared by inspect_template.py and fill_template.py (not called directly).

Address of a text target, stable across runs on the same file:
  "S:ID"       shape ID (``shape_id``) at the top level of slide S (slides from 1)
  "S:G/ID"     shape ID inside group G (nested groups chain further: "S:G1/G2/ID")
  "S:ID@R,C"   table cell, row R / column C from 1, of the table shape ID
Two shapes with the same id at the same level make that address ``ambiguous``.

Font size follows the inheritance chain run -> paragraph -> shape list style ->
layout / master placeholder -> master text styles -> presentation default; when
nothing in the file sets it the size is unknown (None) and estimates assume 18 pt.
All fit numbers are estimates (see ``wrap_lines``): real fonts and kerning differ.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

EMU_PER_PT = 12700
EMU_PER_CM = 360000
DEFAULT_PT = 18.0
LINE_FACTOR = 1.2  # single line spacing ~= 1.2 x font size
NEAR_FULL = 0.9  # report boxes filled to >= 90% of their height
CJK_EM, LATIN_EM, SPACE_EM = 1.0, 0.5, 0.28  # fits LibreOffice renders
LATIN_PER_CJK = round(CJK_EM / LATIN_EM, 1)
_SHAPE_INSETS = (91440, 45720, 91440, 45720)  # bodyPr lIns, tIns, rIns, bIns defaults
_CELL_INSETS = (91440, 45720, 91440, 45720)  # tcPr marL, marT, marR, marB defaults
_MC_ALT = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"
_ADDR = re.compile(r"^(\d+):(\d+(?:/\d+)*)(?:@(\d+),(\d+))?$", re.ASCII)
_URI_KIND = {"table": "table", "chart": "chart", "diagram": "smartart", "ole": "ole"}
_TITLE_PH = {"title", "ctrTitle"}
_BODY_PH = {"body", "subTitle", "obj"}
_VERTICAL = {"vert", "vert270", "eaVert", "wordArtVert", "wordArtVertRtl", "mongolianVert"}
NO_TEXT_PLACEHOLDER = "placeholder_without_text"
_RUN_DROP_ATTRS = ("err", "dirty")  # spell-check / layout-cache flags of the old words


def parse_addr(addr: str) -> tuple[int, list[int], tuple[int, int] | None] | None:
    m = _ADDR.match(addr)
    if not m:
        return None
    cell = (int(m.group(3)), int(m.group(4))) if m.group(3) else None
    return int(m.group(1)), [int(x) for x in m.group(2).split("/")], cell


@dataclass
class Target:
    addr: str
    name: str
    kind: str  # "ph:<type>#<idx>", "textbox", "shape", "cell", a locked kind, or "error"
    tx_body: Any = None  # lxml txBody; None = no text body (locked, or an empty cell)
    box: tuple[int, int, int, int] | None = None  # absolute EMU: left, top, width, height
    insets: tuple[int, int, int, int] = _SHAPE_INSETS
    chain: tuple[Any, ...] = ()  # list-style containers (hold a:lvlNpPr), strongest first
    autofit: str | None = None  # "shrink" / "resize" / "off" / None (not set)
    scale: float = 1.0  # normAutofit fontScale already stored in the file
    wrap: bool = True
    vertical: bool = False
    listed: bool = True  # inspect lists it (has text, or is a placeholder / text box)
    table: str | None = None  # table address, for cells
    merged: bool = False  # cell covered by another cell's span
    el: Any = None  # the shape element (the a:tc for cells)
    hidden: bool = False
    ambiguous: bool = False  # another shape has the same address
    error: str | None = None  # reading this shape failed; nothing about it is trusted


# ---------------------------------------------------------------- walking the slide


def walk_slide(slide, number: int, default_style) -> list[Target]:
    out: list[Target] = []
    _walk(slide.shapes, slide.shapes._spTree, f"{number}:", (), False, slide, default_style, out)
    seen = Counter(t.addr for t in out)
    for t in out:
        t.ambiguous = seen[t.addr] > 1
    return out


def _walk(shapes, container_el, prefix, groups, hidden, slide, default_style, out) -> None:
    for shape in shapes:
        el = shape._element
        c_nv = el.find(".//" + qn("p:cNvPr"))
        addr = f"{prefix}{c_nv.get('id') if c_nv is not None else '?'}"
        name = c_nv.get("name", "") if c_nv is not None else ""
        is_hidden = hidden or (c_nv is not None and c_nv.get("hidden") in ("1", "true"))
        start = len(out)
        try:  # one odd shape must not hide the rest of the deck
            _shape(shape, el, addr, groups, is_hidden, slide, default_style, out)
        except Exception as exc:  # reported per shape, never swallowed
            del out[start:]
            out.append(Target(addr, name, "error", error=f"{type(exc).__name__}: {exc}"[:160]))
        for t in out[start:]:
            t.hidden = t.hidden or is_hidden
    for alt in container_el.iterchildren(_MC_ALT):  # python-pptx skips these entirely
        c_nv = next(alt.iter(qn("p:cNvPr")), None)
        if c_nv is not None:
            out.append(Target(f"{prefix}{c_nv.get('id')}", c_nv.get("name", ""), "other"))


def _shape(shape, el, addr, groups, hidden, slide, default_style, out) -> None:
    tag = el.tag.rsplit("}", 1)[-1]
    if tag == "grpSp":
        groups = (*groups, _group_xf(el))
        _walk(shape.shapes, el, addr + "/", groups, hidden, slide, default_style, out)
    elif tag == "graphicFrame":
        _graphic_frame(shape, addr, groups, default_style, out)
    elif tag == "pic":
        out.append(Target(addr, shape.name, "picture", box=_box(shape, groups), el=el))
    elif tag == "sp":
        out.append(_text_shape(shape, addr, groups, slide, default_style))


def _graphic_frame(shape, addr, groups, default_style, out) -> None:
    data = shape._element.find(".//" + qn("a:graphicData"))
    uri = "" if data is None else data.get("uri", "")
    kind = _URI_KIND.get(uri.rsplit("/", 1)[-1], "graphic")
    if kind != "table":
        out.append(Target(addr, shape.name, kind, box=_box(shape, groups), el=shape._element))
        return
    out.append(Target(addr, shape.name, "table", box=_box(shape, groups), listed=False))
    tbl = shape.table
    widths = [c.width for c in tbl.columns]
    heights = [r.height for r in tbl.rows]
    for r, row in enumerate(tbl.rows):
        for c, cell in enumerate(row.cells):
            span_w = sum(widths[c : c + max(cell.span_width, 1)])
            span_h = sum(heights[r : r + max(cell.span_height, 1)])
            local = (shape.left + sum(widths[:c]), shape.top + sum(heights[:r]), span_w, span_h)
            tc_pr = cell._tc.find(qn("a:tcPr"))
            out.append(
                Target(
                    f"{addr}@{r + 1},{c + 1}",
                    shape.name,
                    "cell",
                    tx_body=cell._tc.find(qn("a:txBody")),
                    box=_apply_groups(local, groups),
                    insets=_insets(tc_pr, ("marL", "marT", "marR", "marB"), _CELL_INSETS),
                    chain=() if default_style is None else (default_style,),
                    autofit="resize",  # table rows grow to fit their text
                    table=addr,
                    merged=bool(cell.is_spanned),
                    el=cell._tc,
                )
            )


def _text_shape(shape, addr, groups, slide, default_style) -> Target:
    el = shape._element
    tx_body = el.find(qn("p:txBody"))
    ph = el.find(qn("p:nvSpPr") + "/" + qn("p:nvPr") + "/" + qn("p:ph"))
    bases = _placeholder_bases(shape) if ph is not None else []
    bodies = [
        b for b in [tx_body, *(s._element.find(qn("p:txBody")) for s in bases)] if b is not None
    ]
    body_prs = [b.find(qn("a:bodyPr")) for b in bodies]
    list_styles = [b.find(qn("a:lstStyle")) for b in bodies]
    if ph is not None:
        ph_type, idx = ph.get("type", "obj"), ph.get("idx", "0")
        kind = f"ph:{ph_type}#{idx}" if tx_body is not None else NO_TEXT_PLACEHOLDER
        master_styles = slide.slide_layout.slide_master._element.find(qn("p:txStyles"))
        style_name = (
            "p:titleStyle"
            if ph_type in _TITLE_PH
            else ("p:bodyStyle" if ph_type in _BODY_PH else "p:otherStyle")
        )
        master_style = None if master_styles is None else master_styles.find(qn(style_name))
        chain = (*list_styles, master_style, default_style)
    else:
        is_box = el.find(qn("p:nvSpPr") + "/" + qn("p:cNvSpPr")).get("txBox") == "1"
        kind = "textbox" if is_box else "shape"
        chain = (*list_styles, default_style)
    autofit, scale = _autofit(body_prs)
    has_text = tx_body is not None and bool(paragraphs_text(tx_body).strip())
    return Target(
        addr,
        shape.name,
        kind,
        tx_body=tx_body,
        box=_box(shape, groups),
        insets=tuple(  # type: ignore[arg-type]
            int(v) if (v := _first_attr(body_prs, k)) is not None else d
            for k, d in zip(("lIns", "tIns", "rIns", "bIns"), _SHAPE_INSETS, strict=True)
        ),
        chain=tuple(c for c in chain if c is not None),
        autofit=autofit,
        scale=scale,
        wrap=_first_attr(body_prs, "wrap") != "none",
        vertical=_first_attr(body_prs, "vert") in _VERTICAL,
        listed=has_text or kind != "shape",
        el=el,
    )


def _base(shape):
    # python-pptx maps a layout placeholder's type to its master type with a fixed dict
    # that lacks a few types (e.g. sldImg, hdr): KeyError = no known base
    try:
        return getattr(shape, "_base_placeholder", None)
    except KeyError:
        return None


def _placeholder_bases(shape) -> list:
    bases = []
    base = _base(shape)
    while base is not None and len(bases) < 2:
        bases.append(base)
        base = _base(base)
    return bases


def pct(value: str | None, default: float) -> float:
    """OOXML percentage: transitional "62500" (1/1000 %) or strict "62.5%"."""
    if value is None:
        return default
    value = value.strip()
    return float(value[:-1]) / 100 if value.endswith("%") else int(value) / 100000


def _autofit(body_prs) -> tuple[str | None, float]:
    for bp in body_prs:
        if bp is None:
            continue
        norm = bp.find(qn("a:normAutofit"))
        if norm is not None:
            scale = pct(norm.get("fontScale"), 1.0)
            return "shrink", scale if scale > 0 else 1.0
        if bp.find(qn("a:spAutoFit")) is not None:
            return "resize", 1.0
        if bp.find(qn("a:noAutofit")) is not None:
            return "off", 1.0
    return None, 1.0


def _first_attr(elements, name: str) -> str | None:
    for e in elements:
        if e is not None and e.get(name) is not None:
            return e.get(name)
    return None


def _insets(el, names, defaults) -> tuple[int, int, int, int]:
    vals = [
        int(el.get(n)) if el is not None and el.get(n) else d
        for n, d in zip(names, defaults, strict=True)
    ]
    return vals[0], vals[1], vals[2], vals[3]


def _group_xf(grp_el):
    xfrm = grp_el.find(qn("p:grpSpPr") + "/" + qn("a:xfrm"))
    parts = [
        None if xfrm is None else xfrm.find(qn(f"a:{n}")) for n in ("off", "ext", "chOff", "chExt")
    ]
    if any(p is None for p in parts):
        return None
    off, ext, ch_off, ch_ext = parts
    return (
        int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy")),
        int(ch_off.get("x")), int(ch_off.get("y")), int(ch_ext.get("cx")), int(ch_ext.get("cy")),
    )  # fmt: skip


def _box(shape, groups) -> tuple[int, int, int, int] | None:
    try:  # placeholders inherit geometry; see _base for the KeyError
        vals = (shape.left, shape.top, shape.width, shape.height)
    except KeyError:
        return None
    return None if any(v is None for v in vals) else _apply_groups(vals, groups)


def _apply_groups(box, groups):
    """Child-space box -> slide-space box, innermost group first."""
    x, y, w, h = box
    for g in reversed(groups):
        if g is None:
            continue
        ox, oy, cx, cy, chx, chy, chcx, chcy = g
        sx = cx / chcx if chcx else 1.0
        sy = cy / chcy if chcy else 1.0
        x, y, w, h = ox + (x - chx) * sx, oy + (y - chy) * sy, w * sx, h * sy
    return round(x), round(y), round(w), round(h)


# ---------------------------------------------------------------- paragraph properties


@dataclass(frozen=True)
class ParaProps:
    size_pt: float | None  # None = not set anywhere in the file
    line_pct: float = 1.0  # lnSpc spcPct
    line_pts: float | None = None  # lnSpc spcPts (exact line height)
    mar_l: int = 0  # EMU
    spc_bef: tuple[str, float] = ("pts", 0.0)
    spc_aft: tuple[str, float] = ("pts", 0.0)


def paragraphs(tx_body) -> list:
    """The paragraphs of a text body; a body without any acts as one empty paragraph."""
    found = tx_body.findall(qn("a:p")) if tx_body is not None else []
    return found or [OxmlElement("a:p")]


def para_props(p, chain) -> ParaProps:
    ppr = p.find(qn("a:pPr"))
    level = int(ppr.get("lvl", "0")) if ppr is not None else 0
    lvl_tag = qn(f"a:lvl{level + 1}pPr")
    sources = [ppr] if ppr is not None else []
    sources += [c.find(lvl_tag) for c in chain if c.find(lvl_tag) is not None]
    size = _run_size(p)
    if size is None:
        size = next((float(d.get("sz")) / 100 for d in _defrprs(sources) if d.get("sz")), None)
    line = _spacing(sources, "a:lnSpc")
    mar_l = next((int(s.get("marL")) for s in sources if s.get("marL") is not None), 0)
    return ParaProps(
        size_pt=size,
        line_pct=line[1] if line[0] == "pct" and line[1] > 0 else 1.0,
        line_pts=line[1] if line[0] == "pts" and line[1] > 0 else None,
        mar_l=mar_l,
        spc_bef=_spacing(sources, "a:spcBef"),
        spc_aft=_spacing(sources, "a:spcAft"),
    )


def _run_size(p) -> float | None:
    for child in p:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in ("r", "fld"):
            rpr = child.find(qn("a:rPr"))
            return float(rpr.get("sz")) / 100 if rpr is not None and rpr.get("sz") else None
    end = p.find(qn("a:endParaRPr"))
    return float(end.get("sz")) / 100 if end is not None and end.get("sz") else None


def _defrprs(sources):
    for s in sources:
        d = s.find(qn("a:defRPr"))
        if d is not None:
            yield d


def _spacing(sources, tag) -> tuple[str, float]:
    for s in sources:
        sp = s.find(qn(tag))
        if sp is None:
            continue
        pct_el, pts = sp.find(qn("a:spcPct")), sp.find(qn("a:spcPts"))
        if pct_el is not None:
            return "pct", pct(pct_el.get("val"), 1.0)
        if pts is not None:
            return "pts", int(pts.get("val")) / 100
    return ("pct", 1.0) if tag == "a:lnSpc" else ("pts", 0.0)


# ---------------------------------------------------------------- text and estimates


def paragraph_text(p) -> str:
    parts = []
    for child in p:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in ("r", "fld"):
            t = child.find(qn("a:t"))
            parts.append((t.text or "") if t is not None else "")
        elif tag == "br":
            parts.append("\n")
    return "".join(parts)


def paragraphs_text(tx_body) -> str:
    if tx_body is None:
        return ""
    return "\n".join(paragraph_text(p) for p in tx_body.findall(qn("a:p")))


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def char_em(ch: str) -> float:
    if ch == " ":
        return SPACE_EM
    if _wide(ch):
        return CJK_EM
    # ambiguous-width punctuation (curly quotes, dashes, ellipsis) is full width in CJK
    # fonts; ambiguous letters (Greek, Cyrillic) stay proportional
    ambiguous = unicodedata.east_asian_width(ch) == "A"
    return CJK_EM if ambiguous and not unicodedata.category(ch).startswith("L") else LATIN_EM


def text_em(text: str) -> float:
    return sum(char_em(ch) for ch in text)


def _tokens(text: str) -> list[str]:
    """Words = runs of non-space, non-wide chars (any script) plus trailing spaces;
    every wide (CJK) char is a token of its own, so lines may break between them."""
    tokens: list[str] = []
    cur = ""
    for ch in text:
        if ch == " ":
            cur += ch
            continue
        if cur and (_wide(ch) or cur.endswith(" ") or _wide(cur[-1])):
            tokens.append(cur)
            cur = ""
        cur += ch
    if cur:
        tokens.append(cur)
    return tokens


def wrap_lines(text: str, line_em: float) -> int:
    """Greedy word wrap: words move whole to the next line, CJK breaks anywhere."""
    if line_em <= 0:
        return max(1, len(text))
    lines, cur = 1, 0.0
    for tok in _tokens(text):
        width = text_em(tok)
        visible = text_em(tok.rstrip(" "))
        if cur > 0 and cur + visible > line_em:
            lines, cur = lines + 1, 0.0
        while visible > line_em:  # a word longer than the line breaks inside itself
            lines, visible, width = lines + 1, visible - line_em, width - line_em
        cur += width
    return lines


@dataclass(frozen=True)
class Fit:
    need_lines: int
    fit_lines: int
    cap_chars: int  # ~CJK chars that fit at the first paragraph's size
    over: bool
    near: bool  # not over, but >= NEAR_FULL of the height is used
    too_wide: bool  # no-wrap box: a line is wider than the box
    font_pt: float | None


def _spc(spec: tuple[str, float], size: float) -> float:
    kind, val = spec
    return val * size * LINE_FACTOR if kind == "pct" else val


def estimate(target: Target) -> Fit | None:
    """Estimated fit of the target's current paragraphs in its box (None = unknown)."""
    if target.box is None or target.tx_body is None:
        return None
    paras = paragraphs(target.tx_body)
    props = [para_props(p, target.chain) for p in paras]
    texts = [paragraph_text(p) for p in paras]
    left, top, right, bottom = target.insets
    inner_w = (target.box[2] - left - right) / EMU_PER_PT
    inner_h = (target.box[3] - top - bottom) / EMU_PER_PT
    if target.vertical:  # lines run top to bottom and stack right to left
        inner_w, inner_h = inner_h, inner_w
    need_h, need_lines, too_wide = 0.0, 0, False
    for i, (text, pp) in enumerate(zip(texts, props, strict=True)):
        size = (pp.size_pt or DEFAULT_PT) * target.scale
        avail = inner_w - pp.mar_l / EMU_PER_PT
        n = 0
        for seg in text.split("\n"):
            if target.wrap:
                n += wrap_lines(seg, avail / size)
            else:
                n += 1
                too_wide = too_wide or text_em(seg) * size > avail
        line_h = pp.line_pts or size * LINE_FACTOR * pp.line_pct
        need_h += n * line_h + (_spc(pp.spc_bef, size) if i else 0.0)
        need_h += _spc(pp.spc_aft, size) if i < len(texts) - 1 else 0.0
        need_lines += n
    first = next((pp for t, pp in zip(texts, props, strict=True) if t.strip()), props[0])
    size0 = (first.size_pt or DEFAULT_PT) * target.scale
    line0 = first.line_pts or size0 * LINE_FACTOR * first.line_pct
    avg_line = need_h / need_lines if need_lines and need_h > 0 else line0
    per_line = max(0, int((inner_w - first.mar_l / EMU_PER_PT) // size0))
    over = need_h > inner_h or too_wide
    return Fit(
        need_lines=need_lines,
        fit_lines=max(0, int(inner_h // avg_line)),
        cap_chars=per_line * max(0, int(inner_h // line0)),
        over=over,
        near=not over and inner_h > 0 and need_h >= NEAR_FULL * inner_h,
        too_wide=too_wide,
        font_pt=first.size_pt,
    )


# ---------------------------------------------------------------- replacing text


@dataclass(frozen=True)
class NewPara:
    text: str  # "\n" inside = line break within the paragraph
    level: int | None = None


def replace_text(tx_body, new_paras: list[NewPara]) -> None:
    """Swap the paragraphs of ``tx_body`` for ``new_paras``; bodyPr / lstStyle stay.

    New paragraph i copies the paragraph properties of old paragraph i (the last one
    when there are more new paragraphs; the first old paragraph at the same level when
    a level is given) and the character formatting of that paragraph's first run.
    """
    old = paragraphs(tx_body)
    built = [_build_para(_pick_template(old, i, np.level), np) for i, np in enumerate(new_paras)]
    anchor = tx_body.findall(qn("a:p"))
    insert_at = list(tx_body).index(anchor[0]) if anchor else len(tx_body)
    for p in anchor:
        tx_body.remove(p)
    for offset, p in enumerate(built):
        tx_body.insert(insert_at + offset, p)


def _level(p) -> int:
    ppr = p.find(qn("a:pPr"))
    return int(ppr.get("lvl", "0")) if ppr is not None else 0


def _pick_template(old: list, i: int, level: int | None):
    if level is not None:
        for p in old:
            if _level(p) == level:
                return p
    return old[min(i, len(old) - 1)]


def _first_run_format(p):
    """rPr of the paragraph's actual first run; endParaRPr only when it has no run."""
    for child in p:
        if child.tag in (qn("a:r"), qn("a:fld")):
            rpr = child.find(qn("a:rPr"))
            return None if rpr is None else copy.deepcopy(rpr)
    end = p.find(qn("a:endParaRPr"))
    if end is None:
        return None
    rpr = copy.deepcopy(end)
    rpr.tag = qn("a:rPr")
    return rpr


def _build_para(template, new: NewPara):
    p = copy.deepcopy(template)
    rpr = _first_run_format(p)
    for child in list(p):
        if child.tag in (qn("a:r"), qn("a:fld"), qn("a:br"), _MC_ALT):
            p.remove(child)
    if rpr is not None:  # a link belongs to the old words, not to their formatting
        for link in rpr.findall(qn("a:hlinkClick")) + rpr.findall(qn("a:hlinkMouseOver")):
            rpr.remove(link)
        for attr in _RUN_DROP_ATTRS:
            rpr.attrib.pop(attr, None)
    if new.level is not None:
        ppr = p.find(qn("a:pPr"))
        if ppr is None:
            ppr = OxmlElement("a:pPr")
            p.insert(0, ppr)
        if _level(template) != new.level:  # the template's indent belongs to its own level
            for attr in ("marL", "indent"):
                ppr.attrib.pop(attr, None)
        ppr.set("lvl", str(new.level))
    pieces: list = []
    for n, seg in enumerate(new.text.split("\n")):
        if n:
            pieces.append(_with_rpr("a:br", rpr))
        if seg:
            run = _with_rpr("a:r", rpr)
            t = OxmlElement("a:t")
            t.text = seg
            run.append(t)
            pieces.append(run)
    end = p.find(qn("a:endParaRPr"))
    if end is None and rpr is not None:
        end = copy.deepcopy(rpr)
        end.tag = qn("a:endParaRPr")
        p.append(end)
    elif end is not None:
        for attr in _RUN_DROP_ATTRS:
            end.attrib.pop(attr, None)
    pos = list(p).index(end) if end is not None else len(p)
    for offset, piece in enumerate(pieces):
        p.insert(pos + offset, piece)
    return p


def _with_rpr(tag: str, rpr):
    el = OxmlElement(tag)
    if rpr is not None:
        el.append(copy.deepcopy(rpr))
    return el
