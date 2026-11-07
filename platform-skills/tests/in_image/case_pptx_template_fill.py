import json
import os
from pathlib import Path

import pptx
from _harness import check, run, script
from _pptx_fixtures import build_designed_deck
from pypdf import PdfReader

os.chdir("/workspace")
roles = build_designed_deck("设计好的 模板.pptx")

# 1. inspect --texts: compact, every structure addressed, locked content listed
res = json.loads(
    run(["python", script("pptx", "inspect_template.py"), "设计好的 模板.pptx", "--texts"]).stdout
)
check("layouts" not in res, "--texts must leave layouts out")
texts = {t["addr"]: t for s in res["slides"] for t in s["texts"]}
for role in ("s1_title", "s2_body", "s3_head", "s4_g_a", "s4_g_b", "s5_table", "s6_caption"):
    check(roles[role] in texts, f"{role} ({roles[role]}) missing from texts: {sorted(texts)}")
locked = {t["addr"]: t["kind"] for s in res["slides"] for t in s["locked"]}
check(locked.get(roles["s6_chart"]) == "chart", f"chart not locked: {locked}")
check(locked.get(roles["s6_smartart"]) == "smartart", f"smartart not locked: {locked}")
check(
    texts[roles["s2_body"]]["font_pt"] == [32, 28, 28],
    f"inherited sizes: {texts[roles['s2_body']]}",
)

# 2. fill: placeholder, group child, table cell, CJK + Latin; one obvious overflow
spec = {
    "slides": [
        1,
        {
            "from": 3,
            "fill": {
                roles["s3_head"]: "第一节",
                roles["s3_small"]: "这里放了明显放不下的很长一段文字",
            },
        },
        {"from": 3, "fill": {roles["s3_head"]: "第二节 Part 2"}},
        {"from": 4, "fill": {roles["s4_g_b"]: "嵌套组新字"}},
        {"from": 5, "fill": {roles["s5_table"] + "@2,3": "新格"}},
    ],
    "drop_unlisted": True,
}
Path("spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
rep = json.loads(
    run(
        [
            "python",
            script("pptx", "fill_template.py"),
            "设计好的 模板.pptx",
            "spec.json",
            "成品.pptx",
        ]
    ).stdout
)
check(rep["saved"] and all(r["status"] == "ok" for r in rep["results"]), f"fill report: {rep}")
check(rep["slides"] == [1, 3, 3, 4, 5], f"slide plan: {rep}")
check([o["addr"] for o in rep["overflow"]] == [roles["s3_small"]], f"overflow: {rep['overflow']}")
out = pptx.Presentation("成品.pptx")
check(len(out.slides) == 5, f"expected 5 slides, got {len(out.slides)}")
head_id = int(roles["s3_head"].split(":")[1])
heads = [
    next(s for s in out.slides[i].shapes if s.shape_id == head_id).text_frame.text for i in (1, 2)
]
check(heads == ["第一节", "第二节 Part 2"], f"duplicated slides text: {heads}")

# 3. unknown / locked addresses: nothing saved, exit 1, all problems listed
Path("bad.json").write_text(
    json.dumps({"fill": {"3:999": "x", roles["s6_chart"]: "x"}}), encoding="utf-8"
)
bad = run(
    ["python", script("pptx", "fill_template.py"), "设计好的 模板.pptx", "bad.json", "坏.pptx"],
    expect=1,
)
statuses = {r["addr"]: r["status"] for r in json.loads(bad.stdout)["results"]}
check(statuses == {"3:999": "not_found", roles["s6_chart"]: "locked:chart"}, f"{statuses}")
check(not Path("坏.pptx").exists(), "a failed fill must not write the output")

# 4. LibreOffice opens the result: one PDF page per slide
run(["python", script("pptx", "convert.py"), "成品.pptx", "--to", "pdf", "--out-dir", "pdf"])
pages = len(PdfReader("pdf/成品.pdf").pages)
check(pages == 5, f"pdf pages={pages}")

print("PASS case_pptx_template_fill")
