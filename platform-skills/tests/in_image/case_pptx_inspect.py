import copy
import json
import os

import pptx
from _harness import check, run, script
from pptx.enum.shapes import MSO_SHAPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.packuri import PackURI
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.parts.slide import SlideLayoutPart, SlideMasterPart
from pptx.util import Inches

os.chdir("/workspace")

# default python-pptx template: 11 layouts, layout 0 = "Title Slide" with placeholders
# idx 0 (title) and idx 1 (subtitle); slide count must match what we actually added.
p = pptx.Presentation()
p.slides.add_slide(p.slide_layouts[0])
p.slides.add_slide(p.slide_layouts[1])
p.save("模板.pptx")
res = json.loads(run(["python", script("pptx", "inspect_template.py"), "模板.pptx"]).stdout)
check(len(res["layouts"]) == 11, f"layouts count={len(res['layouts'])}")
layout0 = res["layouts"][0]
check(layout0["name"] == "Title Slide", f"layout0 name={layout0['name']}")
idxs = {ph["idx"] for ph in layout0["placeholders"]}
check({0, 1} <= idxs, f"layout0 placeholder idxs={idxs}")
check(len(res["slides"]) == 2, f"slides count={len(res['slides'])}")
check(res["slide_size"] == [9144000, 6858000], f"slide_size={res['slide_size']}")

# realistic edge case #1: a group shape on a slide must not crash inspection (it has no
# placeholder_format and no text frame of its own).
p2 = pptx.Presentation()
s2 = p2.slides.add_slide(p2.slide_layouts[6])
shp1 = s2.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(1), Inches(1))
shp2 = s2.shapes.add_shape(MSO_SHAPE.OVAL, Inches(2), Inches(1), Inches(1), Inches(1))
grp = s2.shapes.add_group_shape([shp1, shp2])
grp.name = "分组"
p2.save("含分组.pptx")
res2 = json.loads(run(["python", script("pptx", "inspect_template.py"), "含分组.pptx"]).stdout)
shapes2 = res2["slides"][0]["shapes"]
by_name = {sh["name"]: sh for sh in shapes2}
check("分组" in by_name and by_name["分组"]["has_text"] is False, f"group shape wrong: {shapes2}")

# realistic edge case #2: a graphicFrame whose content is neither chart/table/OLE (e.g.
# SmartArt) makes python-pptx's shape.shape_type return None — inspect_template.py must
# report "unknown" instead of crashing on str(None) or letting the JSON encoder choke.
p3 = pptx.Presentation()
s3 = p3.slides.add_slide(p3.slide_layouts[6])
graphic_frame_xml = (
    '<p:graphicFrame xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<p:nvGraphicFramePr><p:cNvPr id="99" name="示意图 99"/>'
    "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
    '<p:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/></p:xfrm>'
    "<a:graphic><a:graphicData "
    'uri="http://schemas.openxmlformats.org/drawingml/2006/diagram"/></a:graphic>'
    "</p:graphicFrame>"
)
s3.shapes._spTree.append(parse_xml(graphic_frame_xml))
p3.save("含示意图.pptx")
res3 = json.loads(run(["python", script("pptx", "inspect_template.py"), "含示意图.pptx"]).stdout)
by_name3 = {sh["name"]: sh for sh in res3["slides"][0]["shapes"]}
check(by_name3["示意图 99"]["type"] == "unknown", f"smartart-like shape: {by_name3}")

# realistic edge case #3: an empty presentation (0 slides) must not crash — layouts are
# still listed, slides is an empty list.
p4 = pptx.Presentation()
p4.save("空.pptx")
res4 = json.loads(run(["python", script("pptx", "inspect_template.py"), "空.pptx"]).stdout)
check(len(res4["layouts"]) == 11, f"empty presentation layouts={len(res4['layouts'])}")
check(res4["slides"] == [], f"empty presentation slides={res4['slides']}")

# realistic edge case #4: a real institutional template's layout placeholder can have no
# geometry of its own AND no matching-type placeholder on the slide master either (both
# ends of python-pptx's inheritance chain come up empty) — left/top/width/height must come
# back JSON null, not crash the script or the JSON encoder.
p5 = pptx.Presentation()
master = p5.slide_masters[0]
title_master_ph = next(ph for ph in master.placeholders if ph.placeholder_format.idx == 0)
xfrm = title_master_ph._element.spPr.find(qn("a:xfrm"))
title_master_ph._element.spPr.remove(xfrm)
p5.save("无版式几何.pptx")
res5 = json.loads(run(["python", script("pptx", "inspect_template.py"), "无版式几何.pptx"]).stdout)
title_only = next(layout for layout in res5["layouts"] if layout["name"] == "Title Only")
title_ph = next(ph for ph in title_only["placeholders"] if ph["idx"] == 0)
check(
    title_ph["left"] is None
    and title_ph["top"] is None
    and title_ph["width"] is None
    and title_ph["height"] is None,
    f"geometry-less placeholder should be null, got {title_ph}",
)


# realistic edge case #5: institutional templates often carry several slide masters;
# prs.slide_layouts only covers the first. python-pptx has no API to add a master, so
# clone master 1 (and its layouts, renamed) into a second master part by hand.
def add_second_master(prs, prefix: str) -> None:
    m1 = prs.slide_masters[0].part
    pkg = prs.part.package
    m2 = SlideMasterPart(
        PackURI("/ppt/slideMasters/slideMaster2.xml"),
        m1.content_type,
        pkg,
        copy.deepcopy(m1._element),
    )
    next_id = 2147483648 + 1000  # sldLayoutId / sldMasterId ids must be unique
    for n, sid in enumerate(list(m2._element.find(qn("p:sldLayoutIdLst")))):
        old = m1.related_part(sid.get(qn("r:id")))
        lp = SlideLayoutPart(
            PackURI(f"/ppt/slideLayouts/slideLayout{100 + n}.xml"),
            old.content_type,
            pkg,
            copy.deepcopy(old._element),
        )
        lp._element.cSld.set("name", f"{prefix}{n}")
        lp.relate_to(m2, RT.SLIDE_MASTER)
        sid.set(qn("r:id"), m2.relate_to(lp, RT.SLIDE_LAYOUT))
        sid.set("id", str(next_id))
        next_id += 1
    m2.relate_to(m1.part_related_by(RT.THEME), RT.THEME)
    master_ids = prs.part._element.find(qn("p:sldMasterIdLst"))
    new = copy.deepcopy(master_ids[0])
    new.set("id", str(next_id))
    new.set(qn("r:id"), prs.part.relate_to(m2, RT.SLIDE_MASTER))
    master_ids.append(new)


p6 = pptx.Presentation()
add_second_master(p6, "二号母版-")
p6.save("双母版_中间.pptx")
p6 = pptx.Presentation("双母版_中间.pptx")
p6.slides.add_slide(p6.slide_layouts[0])
p6.slides.add_slide(p6.slide_masters[1].slide_layouts[5])
p6.save("双母版.pptx")
res6 = json.loads(run(["python", script("pptx", "inspect_template.py"), "双母版.pptx"]).stdout)
layouts6 = res6["layouts"]
check(len(layouts6) == 22, f"two masters x 11 layouts, got {len(layouts6)}")
check(
    [(la["master"], la["index"]) for la in layouts6[:11]] == [(0, i) for i in range(11)],
    f"master 0 entries must keep prs.slide_layouts indexes: {layouts6[:11]}",
)
second = [la for la in layouts6 if la["master"] == 1]
check(
    [(la["index"], la["name"]) for la in second] == [(i, f"二号母版-{i}") for i in range(11)],
    f"second master layouts missing/misindexed: {second}",
)
check(
    [(s["master"], s["layout"]) for s in res6["slides"]] == [(0, "Title Slide"), (1, "二号母版-5")],
    f"slides master attribution: {res6['slides']}",
)
check(
    res["layouts"][0]["master"] == 0, f"single-master layouts carry master=0: {res['layouts'][0]}"
)

print("PASS case_pptx_inspect")
