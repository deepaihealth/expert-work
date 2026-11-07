"""Fill a designed .pptx: replace the text of addressed shapes, keep everything else.

Addresses come from inspect_template.py (``texts[].addr``). Spec (JSON file), either

  {"fill": {"2:5": "标题", "3:4/7": ["第一段", "第二段"], "4:9@2,1": "单元格"}}
      every slide kept as is, in place;

  {"slides": [1, {"from": 3, "fill": {...}}, {"from": 3, "fill": {...}}],
   "drop_unlisted": true}
      output = the listed source slides in this order; a slide listed twice is
      duplicated (addresses keep the source slide number); slides not listed are
      deleted only with "drop_unlisted": true, otherwise it is an error.

A value is a string ("\\n" starts a new paragraph) or a list of paragraphs, each a string
("\\n" inside = line break) or {"text": "...", "level": 0-8}. Paragraph i keeps the
paragraph format of the old paragraph i (the last one when there are more) and the
character format of its first run. Unknown / non-editable addresses: nothing is saved,
exit 1, and every problem is reported at once. Overflow is an estimate, reported but
not fatal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx
from _cli import emit_json, fail, resolve_io
from _pptx_text import NewPara, estimate, parse_addr, replace_text, walk_slide
from duplicate_slide import duplicate
from pptx.oxml.ns import qn

OVERFLOW_NOTE = (
    "估算值:按字号、框尺寸、汉字 1 字宽 / 西文约半字宽、1.2 倍行距推算;"
    "中文较准,西文随字体约差 ±20%,"
    "以渲染为准。autofit=resize 框会变高、可能压住下方内容;shrink 打开时字会被缩小;"
    "表格单元格溢出时整行变高。"
)


def _paras(value, where: str, errors: list[str]) -> list[NewPara]:
    if isinstance(value, str):
        return [NewPara(line) for line in value.split("\n")]
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: 值必须是字符串或非空列表")
        return []
    out = []
    for k, item in enumerate(value):
        if isinstance(item, str):
            out.append(NewPara(item))
        elif (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and set(item) <= {"text", "level"}
            and (item.get("level") is None or item.get("level") in range(9))
        ):
            out.append(NewPara(item["text"], item.get("level")))
        else:
            errors.append(
                f'{where}[{k}]: 段落要么是字符串, 要么是 {{"text": 字符串, "level": 0-8}}'
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
        src = (
            item if isinstance(item, int) else item.get("from") if isinstance(item, dict) else None
        )
        if not isinstance(src, int) or not 1 <= src <= total:
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
    if target.merged:
        return "merged_cell"
    return "ok" if target.tx_body is not None else f"locked:{target.kind}"


def _reorder(prs, slides, drop: bool) -> None:
    ids = prs.slides._sldIdLst
    by_part = {prs.part.related_part(sid.rId): sid for sid in ids}
    keep = [by_part[s.part] for s in slides]
    for sid in list(ids):
        ids.remove(sid)
        if drop and sid not in keep:
            prs.part.drop_rel(sid.rId)
    for sid in keep:
        ids.append(sid)


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
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"spec 不是合法 JSON: {exc}")
    prs = pptx.Presentation(str(src))
    errors: list[str] = []
    entries, restructured = _entries(spec, len(prs.slides), errors)
    if errors:
        fail("spec 有误:\n" + "\n".join(errors))
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
        fail("有地址找不到或不能改字, 没有保存; 按 results 一次改完 spec 再跑")
    overflow = []
    for page, addr, target, paras in plan:
        replace_text(target.tx_body, paras)
        fit = estimate(target)
        if fit is not None and fit.over:
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
            overflow.append(item)
    if restructured:
        _reorder(prs, slides, drop=spec.get("drop_unlisted") is True)
    prs.save(str(dst))
    report = {
        "saved": True,
        "output": str(dst),
        "changed_pages": sorted({page for page, *_ in plan}),
        "results": results,
        "overflow": overflow,
    }
    if overflow:
        report["overflow_note"] = OVERFLOW_NOTE
    if restructured:
        report["slides"] = [s for s, _ in entries]
        report["shared_parts_warning"] = shared_warnings
    emit_json(report)


if __name__ == "__main__":
    main()
