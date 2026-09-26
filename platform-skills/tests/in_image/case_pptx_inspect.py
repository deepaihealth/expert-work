import json
import os

import pptx
from _harness import check, run, script
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml import parse_xml
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

print("PASS case_pptx_inspect")
