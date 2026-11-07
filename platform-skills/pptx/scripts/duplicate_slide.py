"""Duplicate one slide (shapes + its own copies of picture/media relationships)."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx
from _cli import emit_json, fail, resolve_io
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SLIDE_ATTRS = ("show", "showMasterSp", "showMasterPhAnim")
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_IGNORABLE = f"{{{_MC_NS}}}Ignorable"
# Relationships whose *data part* is not duplicated — the new slide's relationship points
# at the very same part as the original, so editing one's data (chart series, SmartArt
# text, embedded workbook, ...) silently edits the other's too. Speaker notes are NOT in
# this set: they get their own independent part (see _copy_notes) because a later, separate
# edit to just the copy's notes silently overwriting the original's would be a much worse
# (delayed, easy-to-miss) footgun than the one-time disclosure this warning gives for the
# other types, and copying them independently is cheap (no embedded relationship graph).
_SHARED_WARN = {
    RT.CHART: "图表",
    RT.DIAGRAM_DATA: "SmartArt",
    RT.OLE_OBJECT: "嵌入对象",
    RT.PACKAGE: "嵌入文件",
}


def _copy_notes(src, new) -> bool:
    """Give the duplicate its own independent notes part (not shared with the source).

    Returns True if formatting could not be preserved and a plain-text-only copy was used
    instead (``notes_format_lost``); False when there was nothing to copy or the structural
    (rich-formatting) copy succeeded.
    """
    if not src.has_notes_slide:
        return False
    src_ph = src.notes_slide.notes_placeholder
    if src_ph is None:
        return False
    dst_ph = new.notes_slide.notes_placeholder  # new.notes_slide auto-vivifies a fresh part
    if dst_ph is None:
        return False
    # `ph._element.txBody` is the raw (non-mutating) ZeroOrOne lookup — None when the
    # placeholder has never had a `<p:txBody>` written into it (e.g. a notes placeholder
    # shape present but stripped of its text body by some other tool). `.text_frame`
    # instead *auto-creates* an empty txBody via `get_or_add_txBody()`, which would both
    # mutate `src` as a side effect and give us nothing real to deep-copy — so check the
    # raw element first and only take the rich-copy path when there is an actual txBody.
    src_tx_body = src_ph._element.txBody
    if src_tx_body is None:
        # No txBody means no text either — set "" directly instead of reading
        # src_ph.text_frame.text, which would auto-vivify (and so mutate) a txBody on the
        # *source* placeholder via the same get_or_add_txBody() this branch exists to avoid.
        dst_ph.text_frame.text = ""
        return True
    # Deep-copy the whole txBody (not just .text) to keep run-level formatting (bold,
    # bullets, multiple paragraphs, ...).
    dst_tx_body = dst_ph.text_frame._element
    dst_tx_body.getparent().replace(dst_tx_body, copy.deepcopy(src_tx_body))
    return False


def _remapped(el, rid_map: dict[str, str]):
    clone = copy.deepcopy(el)
    for node in clone.iter():
        for attr, val in list(node.attrib.items()):
            if attr.startswith(f"{{{_R_NS}}}") and val in rid_map:
                node.set(attr, rid_map[val])
    return clone


def _copy_slide_level(src_el, new_el, rid_map: dict[str, str]) -> None:
    """Everything of the slide besides its shapes: background (and the rest of cSld),
    colour-map override, transition, timing, extensions, hidden / master-shape flags."""
    for attr in _SLIDE_ATTRS:
        if src_el.get(attr) is None:
            new_el.attrib.pop(attr, None)
        else:
            new_el.set(attr, src_el.get(attr))
    src_csld, new_csld = src_el.find(qn("p:cSld")), new_el.find(qn("p:cSld"))
    if src_csld.get("name") is None:
        new_csld.attrib.pop("name", None)
    else:
        new_csld.set("name", src_csld.get("name"))
    tree = new_csld.find(qn("p:spTree"))
    for child in list(new_csld):
        if child is not tree:
            new_csld.remove(child)
    pos = 0
    for child in src_csld:
        if child.tag == qn("p:spTree"):
            pos = list(new_csld).index(tree) + 1
            continue
        new_csld.insert(pos, _remapped(child, rid_map))
        pos += 1
    for child in list(new_el):
        if child is not new_csld:
            new_el.remove(child)
    for child in src_el:
        if child is not src_csld:
            new_el.append(_remapped(child, rid_map))


def _carry_ignorable(src, new) -> None:
    """Copy mc:Ignorable with the namespace declarations it names onto the new root.

    The prefixes it lists must be declared where the attribute sits; lxml cannot add a
    declaration to an existing element, so the root is rebuilt and the children moved
    across (moved, not copied: python-pptx's cached views of the shapes stay valid).
    """
    ignorable = src._element.get(_IGNORABLE)
    if ignorable is None:
        return
    old = new._element
    nsmap = dict(old.nsmap)
    kept = []
    for prefix in ignorable.split():
        uri = src._element.nsmap.get(prefix)
        if uri is None or nsmap.get(prefix, uri) != uri:
            continue  # undeclared on the source, or bound to another namespace on the copy
        nsmap[prefix] = uri
        kept.append(prefix)
    if not kept:
        return
    if nsmap.get("mc", _MC_NS) == _MC_NS:
        nsmap["mc"] = _MC_NS
    decls = " ".join(f'xmlns:{k}="{v}"' if k else f'xmlns="{v}"' for k, v in nsmap.items())
    root = parse_xml(f"<p:sld {decls}/>")
    for name, value in old.attrib.items():
        root.set(name, value)
    root.set(_IGNORABLE, " ".join(kept))
    for child in list(old):
        root.append(child)
    new.part._element = root
    new._element = root


def duplicate(prs, index: int, after: int) -> tuple[int, list[str], bool]:
    src = prs.slides[index - 1]
    new = prs.slides.add_slide(src.slide_layout)
    for shape in list(new.shapes):
        shape._element.getparent().remove(shape._element)
    rid_map: dict[str, str] = {}
    warnings: set[str] = set()
    for rid, rel in src.part.rels.items():
        if rel.reltype in (RT.SLIDE_LAYOUT, RT.NOTES_SLIDE):
            continue  # layout already set by add_slide(); notes copied independently below
        if rel.is_external:
            rid_map[rid] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            rid_map[rid] = new.part.relate_to(rel.target_part, rel.reltype)
        if rel.reltype in _SHARED_WARN:
            warnings.add(_SHARED_WARN[rel.reltype])
    for el in src.shapes._spTree.iterchildren():
        if el.tag.endswith("}nvGrpSpPr") or el.tag.endswith("}grpSpPr"):
            continue
        new.shapes._spTree.append(_remapped(el, rid_map))
    _copy_slide_level(src._element, new._element, rid_map)
    _carry_ignorable(src, new)
    notes_format_lost = _copy_notes(src, new)
    ids = prs.slides._sldIdLst
    moved = ids[-1]
    ids.remove(moved)
    ids.insert(after, moved)
    return after + 1, sorted(warnings), notes_format_lost


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--index", type=int, required=True, help="要复制的页码（从 1 开始）")  # noqa: RUF001
    ap.add_argument("--after", type=int, help="放在第几页之后（0 = 最前）；默认紧跟原页")  # noqa: RUF001
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    prs = pptx.Presentation(str(src))
    total = len(prs.slides)
    if not 1 <= args.index <= total:
        fail(f"--index 超出范围：共 {total} 页")  # noqa: RUF001
    after = args.index if args.after is None else args.after
    if not 0 <= after <= total:
        fail(f"--after 超出范围：0..{total}")  # noqa: RUF001
    number, warnings, notes_format_lost = duplicate(prs, args.index, after)
    prs.save(str(dst))
    emit_json(
        {
            "new_slide_number": number,
            "shared_parts_warning": warnings,
            "notes_format_lost": notes_format_lost,
        }
    )


if __name__ == "__main__":
    main()
