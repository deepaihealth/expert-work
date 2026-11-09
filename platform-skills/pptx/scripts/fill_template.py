"""Fill a designed .pptx: replace the text of addressed shapes, keep everything else.

Addresses come from inspect_template.py --texts (``texts[].addr``). Spec (JSON file), either

  {"fill": {"2:5": "标题", "3:4/7": ["第一段", "第二段"], "4:9@2,1": "单元格"}}
      every slide kept as is, in place;

  {"slides": [1, {"from": 3, "fill": {...}}, {"from": 3, "fill": {...}}],
   "drop_unlisted": true}
      output = the listed source slides in this order; a slide listed twice is
      duplicated (addresses keep the source slide number); slides not listed are
      deleted only with "drop_unlisted": true, otherwise it is an error. Sections
      follow the new order; deleted slides leave custom shows (reported in warnings).

A value is a string ("\\n" starts a new paragraph, "\\v" breaks the line) or a list of
paragraphs, each a string ("\\n" / "\\v" inside = line break) or {"text": "...",
"level": 0-8}. Paragraph i keeps the paragraph format of the old paragraph i (the last
one when there are more) and the character format of its first run. Unknown,
ambiguous or non-editable addresses: nothing is saved, exit 1, every problem reported
at once. Overflow is an estimate, reported but not fatal.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx
from _cli import emit_json, fail, resolve_io
from _pptx_text import NewPara, estimate, parse_addr, replace_text, walk_slide
from duplicate_slide import duplicate
from pptx.oxml.ns import qn

P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
_R_PREFIX = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
OVERFLOW_NOTE = (
    "估算值:按字号、框尺寸、汉字 1 字宽 / 西文约半字宽、1.2 倍行距推算;"
    "中文较准,西文随字体约差 ±20%,"
    "以渲染为准。near 里是接近放满(≥90%)的框,不用改短,渲染时重点看;"
    "overflow_unknown=框的尺寸读不到,估不了,必须看图确认。"
    "autofit=resize 框会变高、可能压住下方内容;shrink 打开时字会被缩小;"
    "表格单元格溢出时整行变高。"
)
_CONTROL = {chr(c) for c in range(32)} - {"\t", "\n", "\v"}


class _DuplicateKeysError(ValueError):
    pass


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dups = sorted({k for k in keys if keys.count(k) > 1})
    if dups:
        raise _DuplicateKeysError(", ".join(dups))
    return dict(pairs)


def _text(raw: str, where: str, errors: list[str]) -> str:
    bad = sorted({f"U+{ord(c):04X}" for c in raw if c in _CONTROL})
    if bad:
        errors.append(f"{where}: 含控制字符 {', '.join(bad)}, 删掉再跑")
    return raw.replace("\v", "\n")


def _eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paras(value, where: str, errors: list[str]) -> list[NewPara]:
    if isinstance(value, str):
        value = _eol(value)
        return [NewPara(_text(line, where, errors)) for line in value.split("\n")]
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: 值必须是字符串或非空列表")
        return []
    out = []
    for k, item in enumerate(value):
        level = item.get("level") if isinstance(item, dict) else None
        if isinstance(item, str):
            out.append(NewPara(_text(_eol(item).replace("\n", "\v"), f"{where}[{k}]", errors)))
        elif (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and set(item) <= {"text", "level"}
            and (level is None or (type(level) is int and 0 <= level <= 8))
        ):
            text = _text(_eol(item["text"]).replace("\n", "\v"), f"{where}[{k}]", errors)
            out.append(NewPara(text, level))
        else:
            errors.append(
                f'{where}[{k}]: 段落要么是字符串, 要么是 {{"text": 字符串, "level": 0-8 的整数}}'
            )
    return out


def _entries(spec, total: int, errors: list[str]) -> tuple[list[tuple[int, dict]], bool]:
    """-> ([(source slide, {addr: paragraphs})] in output order, restructured?)."""
    if not isinstance(spec, dict) or set(spec) - {"fill", "slides", "drop_unlisted"}:
        errors.append('spec 只能有 "fill" 或 "slides"(+ "drop_unlisted") 这几个键')
        return [], False
    if ("fill" in spec) == ("slides" in spec):
        errors.append('spec 必须二选一: "fill" 或 "slides"')
        return [], False
    if "fill" in spec:
        by_slide: dict[int, dict] = {n: {} for n in range(1, total + 1)}
        for addr, paras in _fill_map(spec["fill"], "fill", errors).items():
            parsed = parse_addr(addr)
            if parsed and parsed[0] not in by_slide:
                errors.append(f"{addr}: 没有第 {parsed[0]} 页(共 {total} 页)")
            elif parsed:
                by_slide[parsed[0]][addr] = paras
        return sorted(by_slide.items()), False
    if not isinstance(spec["slides"], list) or not spec["slides"]:
        errors.append('"slides" 必须是非空列表')
        return [], True
    entries = []
    for k, item in enumerate(spec["slides"]):
        src = item if type(item) is int else item.get("from") if isinstance(item, dict) else None
        if type(src) is not int or not 1 <= src <= total:
            errors.append(f"slides[{k}]: from 必须是 1..{total} 的页码")
            continue
        if isinstance(item, dict) and set(item) - {"from", "fill"}:
            errors.append(f'slides[{k}]: 只能有 "from" 和 "fill"')
        fills = (
            _fill_map(item.get("fill", {}), f"slides[{k}].fill", errors)
            if isinstance(item, dict)
            else {}
        )
        for addr in fills:
            parsed = parse_addr(addr)
            if parsed and parsed[0] != src:
                errors.append(f"slides[{k}]: 地址 {addr} 不在第 {src} 页")
        entries.append((src, fills))
    unlisted = sorted(set(range(1, total + 1)) - {src for src, _ in entries})
    if unlisted and spec.get("drop_unlisted") is not True:
        errors.append(
            f'第 {unlisted} 页没列在 slides 里: 要删就加 "drop_unlisted": true, 要留就把页码写进去'
        )
    return entries, True


def _fill_map(raw, where: str, errors: list[str]) -> dict[str, list[NewPara]]:
    if not isinstance(raw, dict):
        errors.append(f"{where}: 必须是 {{地址: 文字}} 对象")
        return {}
    for addr in raw:
        if parse_addr(addr) is None:
            errors.append(f"{where}: 地址 {addr!r} 格式不对, 应为 页:形状id[/组内id][@行,列]")
    return {addr: _paras(value, f"{where}[{addr!r}]", errors) for addr, value in raw.items()}


def _allocate(prs, entries) -> tuple[list, list[str]]:
    """One slide per entry: the source itself the first time, a duplicate after that."""
    originals = list(prs.slides)
    seen: set[int] = set()
    slides, warnings = [], set()
    for src, _ in entries:
        if src in seen:
            number, warn, _notes_lost = duplicate(prs, src, len(prs.slides))
            warnings.update(warn)
            slides.append(prs.slides[number - 1])
        else:
            seen.add(src)
            slides.append(originals[src - 1])
    return slides, sorted(warnings)


def _status(target) -> str:
    if target is None:
        return "not_found"
    if target.ambiguous:
        return "ambiguous_id"
    if target.kind == "error":
        return f"error: {target.error}"
    if target.merged:
        return "merged_cell"
    if target.tx_body is not None or target.kind == "cell":
        return "ok"
    return f"locked:{target.kind}"


# ---------------------------------------------------------------- restructuring


def _drop_from_custom_shows(prs, rids: set[str]) -> list[str]:
    warnings = []
    for show in prs.part._element.iter(qn("p:custShow")):
        hits = [s for s in show.iter(qn("p:sld")) if s.get(qn("r:id")) in rids]
        for s in hits:
            s.getparent().remove(s)
        if hits:
            warnings.append(f"自定义放映「{show.get('name', '')}」里有被删掉的页, 已从放映中去掉")
    return warnings


def _members(section, ids: list[str]) -> None:
    lst = section.find(f"{{{P14}}}sldIdLst")
    if lst is None:
        lst = section.makeelement(f"{{{P14}}}sldIdLst", {})
        section.insert(0, lst)
    for child in list(lst):
        lst.remove(child)
    for fid in ids:
        lst.append(lst.makeelement(f"{{{P14}}}sldId", {"id": fid}))


def _rewrite_sections(prs, final_ids: list[str], source_ids: list[str]) -> None:
    """Each output slide stays in its source slide's section. Every unbroken run of one
    section becomes a section of its own: the first run keeps the section, later runs
    are copies with the same name and a new id (A[1] B[3] A[2] B[4])."""
    section_list = next(prs.part._element.iter(f"{{{P14}}}sectionLst"), None)
    sections = [] if section_list is None else list(section_list.iterchildren(f"{{{P14}}}section"))
    if not sections:
        return
    home = {s.get("id"): n for n, sec in enumerate(sections) for s in sec.iter(f"{{{P14}}}sldId")}
    runs: list[tuple[int, list[str]]] = []
    current = 0
    for fid, sid in zip(final_ids, source_ids, strict=True):
        current = home.get(sid, current)  # a slide outside every section joins its predecessor's
        if runs and runs[-1][0] == current:
            runs[-1][1].append(fid)
        else:
            runs.append((current, [fid]))
    used: set[int] = set()
    rebuilt = []
    for n, ids in runs:
        section = sections[n]
        if n in used:
            section = copy.deepcopy(section)
            section.set("id", "{" + str(uuid.uuid4()).upper() + "}")
        used.add(n)
        _members(section, ids)
        rebuilt.append(section)
    for n, section in enumerate(sections):  # sections left without slides stay, empty, last
        if n not in used:
            _members(section, [])
            rebuilt.append(section)
    for section in sections:
        section_list.remove(section)
    ext = section_list.find(f"{{{P14}}}extLst")  # schema order: sections, then extLst
    for section in rebuilt:
        if ext is None:
            section_list.append(section)
        else:
            ext.addprevious(section)


def _references(root, rid: str) -> bool:
    return any(
        v == rid for node in root.iter() for k, v in node.attrib.items() if k.startswith(_R_PREFIX)
    )


def _unlink(part, root, dropped_parts) -> str | None:
    """Remove every element that links (r:id) to a dropped slide - shape click / hover,
    run click / mouse-over, action buttons - and then the relationship itself, so the
    dropped slide part is not kept alive. -> None (no such link), "removed", or "broken"
    (some other reference to it remains, so the relationship had to stay)."""
    outcome = None
    for rid, rel in list(part.rels.items()):
        if rel.is_external or rel.target_part not in dropped_parts:
            continue
        for node in list(root.iter()):
            parent = node.getparent()
            if parent is not None and node.get(qn("r:id")) == rid:
                parent.remove(node)
        if _references(root, rid):
            outcome = "broken"
        else:
            part.drop_rel(rid)
            outcome = outcome or "removed"
    return outcome


def _restructure(prs, slides, source_ids: list[str], drop: bool) -> list[str]:
    ids = prs.slides._sldIdLst
    by_part = {prs.part.related_part(sid.rId): sid for sid in ids}
    keep = [by_part[s.part] for s in slides]
    dropped = [sid for sid in ids if sid not in keep] if drop else []
    dropped_parts = {prs.part.related_part(sid.rId) for sid in dropped}
    warnings = _drop_from_custom_shows(prs, {sid.rId for sid in dropped})
    for sid in list(ids):
        ids.remove(sid)
        if sid in dropped:
            prs.part.drop_rel(sid.rId)
    for sid in keep:
        ids.append(sid)
    _rewrite_sections(prs, [sid.get("id") for sid in keep], source_ids)
    for page, slide in enumerate(slides, start=1):
        places = [("", slide.part, slide._element)]
        if slide.has_notes_slide:
            places.append(("备注里", slide.notes_slide.part, slide.notes_slide._element))
        for where, part, root in places:
            outcome = _unlink(part, root, dropped_parts)
            if outcome == "removed":
                warnings.append(f"第 {page} 页{where}有链接指向被删掉的页, 已去掉这个链接")
            elif outcome == "broken":
                warnings.append(f"第 {page} 页{where}有指向被删掉的页的引用去不掉, 那个链接已失效")
    return warnings


# ---------------------------------------------------------------- main


def _load_spec(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeysError as exc:
        fail(f"spec 里有重复的键(后一个会悄悄覆盖前一个): {exc}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"spec 不是合法 JSON: {exc}")


def _fit_report(page, addr, target, paras) -> tuple[str | None, dict | None]:
    """-> ("overflow" / "near" / "unknown" / None, item for the first two)."""
    try:
        fit = estimate(target)
    except Exception:  # an unreadable value means "unknown", reported
        fit = None
    if fit is None:
        return "unknown", None
    if not (fit.over or fit.near):
        return None, None
    item = {
        "page": page,
        "addr": addr,
        "need_lines": fit.need_lines,
        "fit_lines": fit.fit_lines,
        "cap_chars": fit.cap_chars,
        "chars": sum(len(p.text) for p in paras),
        "font_pt": fit.font_pt,
    }
    if target.autofit:
        item["autofit"] = target.autofit
    if fit.too_wide:
        item["too_wide"] = True
    return ("overflow" if fit.over else "near"), item


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", help="模板 .pptx")
    ap.add_argument("spec", help="填值 spec(JSON 文件)")
    ap.add_argument("output", help="输出 .pptx(不能覆盖模板)")
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    spec_path, _ = resolve_io(args.spec, None)
    spec = _load_spec(spec_path)
    prs = pptx.Presentation(str(src))
    errors: list[str] = []
    entries, restructured = _entries(spec, len(prs.slides), errors)
    if errors:
        fail("spec 有误:\n" + "\n".join(errors))
    orig_ids = [sid.get("id") for sid in prs.slides._sldIdLst]
    slides, shared_warnings = _allocate(prs, entries) if restructured else (list(prs.slides), [])
    default_style = prs.part._element.find(qn("p:defaultTextStyle"))
    plan, results = [], []
    for page, ((src_no, fills), slide) in enumerate(zip(entries, slides, strict=True), start=1):
        targets = {t.addr: t for t in walk_slide(slide, src_no, default_style)}
        for addr, paras in fills.items():
            target = targets.get(addr)
            status = _status(target)
            results.append({"page": page, "addr": addr, "status": status})
            if status == "ok":
                plan.append((page, addr, target, paras))
    if any(r["status"] != "ok" for r in results):
        emit_json({"saved": False, "results": results})
        fail("有地址找不到、有歧义或不能改字, 没有保存; 按 results 一次改完 spec 再跑")
    fits: dict[str, list] = {"overflow": [], "near": [], "unknown": []}
    for page, addr, target, paras in plan:
        if target.tx_body is None:  # a table cell that never had text
            target.tx_body = target.el.get_or_add_txBody()
        replace_text(target.tx_body, paras)
        kind, item = _fit_report(page, addr, target, paras)
        if kind:
            fits[kind].append(item if item is not None else addr)
    warnings = []
    if restructured:
        source_ids = [orig_ids[src_no - 1] for src_no, _ in entries]
        warnings = _restructure(prs, slides, source_ids, drop=spec.get("drop_unlisted") is True)
    prs.save(str(dst))
    report = {
        "saved": True,
        "output": str(dst),
        "changed_pages": sorted({page for page, *_ in plan}),
        "results": results,
        "overflow": fits["overflow"],
        "near": fits["near"],
        "overflow_unknown": fits["unknown"],
    }
    if any(fits.values()):
        report["overflow_note"] = OVERFLOW_NOTE
    if restructured:
        report["slides"] = [s for s, _ in entries]
        report["shared_parts_warning"] = shared_warnings
    if warnings:
        report["warnings"] = warnings
    emit_json(report)


if __name__ == "__main__":
    main()
