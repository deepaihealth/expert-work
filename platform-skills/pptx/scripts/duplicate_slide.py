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

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# Relationships whose *data part* is not duplicated — the new slide's relationship points
# at the very same part as the original, so editing one's data (chart series, SmartArt
# text, embedded workbook, speaker notes, ...) silently edits the other's too.
_SHARED_WARN = {
    RT.CHART: "图表",
    RT.DIAGRAM_DATA: "SmartArt",
    RT.OLE_OBJECT: "嵌入对象",
    RT.PACKAGE: "嵌入文件",
    RT.NOTES_SLIDE: "备注",
}


def duplicate(prs, index: int, after: int) -> tuple[int, list[str]]:
    src = prs.slides[index - 1]
    new = prs.slides.add_slide(src.slide_layout)
    for shape in list(new.shapes):
        shape._element.getparent().remove(shape._element)
    rid_map: dict[str, str] = {}
    warnings: set[str] = set()
    for rid, rel in src.part.rels.items():
        if rel.reltype == RT.SLIDE_LAYOUT:
            continue  # already set by add_slide() above
        if rel.is_external:
            rid_map[rid] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            rid_map[rid] = new.part.relate_to(rel.target_part, rel.reltype)
        if rel.reltype in _SHARED_WARN:
            warnings.add(_SHARED_WARN[rel.reltype])
    for el in src.shapes._spTree.iterchildren():
        if el.tag.endswith("}nvGrpSpPr") or el.tag.endswith("}grpSpPr"):
            continue
        clone = copy.deepcopy(el)
        for node in clone.iter():
            for attr, val in list(node.attrib.items()):
                if attr.startswith(f"{{{_R_NS}}}") and val in rid_map:
                    node.set(attr, rid_map[val])
        new.shapes._spTree.append(clone)
    ids = prs.slides._sldIdLst
    moved = ids[-1]
    ids.remove(moved)
    ids.insert(after, moved)
    return after + 1, sorted(warnings)


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
    number, warnings = duplicate(prs, args.index, after)
    prs.save(str(dst))
    emit_json({"new_slide_number": number, "shared_parts_warning": warnings})


if __name__ == "__main__":
    main()
