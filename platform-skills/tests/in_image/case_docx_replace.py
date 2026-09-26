import json
import os

import docx
from _harness import check, run, script
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

os.chdir("/workspace")
d = docx.Document()
p = d.add_paragraph()
r1 = p.add_run("客户")
r1.bold = True
p.add_run("姓名：")  # noqa: RUF001
p.add_run("张三")
t = d.add_table(rows=1, cols=1)
t.cell(0, 0).paragraphs[0].add_run("客户姓名：张三")  # noqa: RUF001
inner = t.cell(0, 0).add_table(rows=1, cols=1)
inner.cell(0, 0).paragraphs[0].add_run("嵌套 张三")
d.sections[0].header.paragraphs[0].add_run("页眉 张三")
d.save("原 件.docx")
with open("rules.json", "w", encoding="utf-8") as f:
    json.dump(
        [
            {"find": "客户姓名：张三", "replace": "客户姓名：李四"},  # noqa: RUF001
            {"find": "张三", "replace": "李四"},
            {"find": "不存在的词", "replace": "x"},
        ],
        f,
        ensure_ascii=False,
    )
res = json.loads(
    run(
        [
            "python",
            script("docx", "replace_text.py"),
            "原 件.docx",
            "输出/新 件.docx",
            "--rules",
            "rules.json",
        ]
    ).stdout
)
check(res["unmatched"] == ["不存在的词"], f"unmatched={res['unmatched']}")
out = docx.Document("输出/新 件.docx")
first = out.paragraphs[0]
check(first.text == "客户姓名：李四", f"split-run replace wrong: {first.text!r}")  # noqa: RUF001
check(first.runs[0].bold is True, "first run lost bold")
cell = out.tables[0].cell(0, 0)
check(cell.paragraphs[0].text == "客户姓名：李四", f"table text {cell.paragraphs[0].text!r}")  # noqa: RUF001
check(cell.tables[0].cell(0, 0).paragraphs[0].text == "嵌套 李四", "nested table")
check(out.sections[0].header.paragraphs[0].text == "页眉 李四", "header")
check(docx.Document("原 件.docx").paragraphs[0].text == "客户姓名：张三", "original was modified")  # noqa: RUF001
run(
    [
        "python",
        script("docx", "replace_text.py"),
        "原 件.docx",
        "原 件.docx",
        "--rules",
        "rules.json",
    ],
    expect=1,
)

# regression: replace containing find ("李四" -> "李四先生") used to loop forever;
# short timeout so a regression fails fast instead of hanging.
d2 = docx.Document()
d2.add_paragraph("李四李四李四")
d2.save("自含.docx")
with open("self_rule.json", "w", encoding="utf-8") as f:
    json.dump([{"find": "李四", "replace": "李四先生"}], f, ensure_ascii=False)
res = json.loads(
    run(
        [
            "python",
            script("docx", "replace_text.py"),
            "自含.docx",
            "自含_出.docx",
            "--rules",
            "self_rule.json",
        ],
        timeout=15,
    ).stdout
)
check(res["rules"][0]["count"] == 3, f"self-containing replace count wrong: {res}")
out2 = docx.Document("自含_出.docx")
check(
    out2.paragraphs[0].text == "李四先生李四先生李四先生",
    f"self-containing replace text wrong: {out2.paragraphs[0].text!r}",
)

# hyperlink anchor text must also be reachable, without breaking its r:id/address;
# python-docx has no add_hyperlink API, so build the raw OXML by hand.
dh = docx.Document()
ph = dh.add_paragraph("前缀 张三 ")
rid = ph.part.relate_to("https://example.com/张三", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
hlink = OxmlElement("w:hyperlink")
hlink.set(qn("r:id"), rid)
hr = OxmlElement("w:r")
ht = OxmlElement("w:t")
ht.text = "张三"
hr.append(ht)
hlink.append(hr)
ph._element.append(hlink)
ph.add_run(" 后缀")
dh.save("超链接.docx")
with open("hyperlink_rule.json", "w", encoding="utf-8") as f:
    json.dump([{"find": "张三", "replace": "李四"}], f, ensure_ascii=False)
run(
    [
        "python",
        script("docx", "replace_text.py"),
        "超链接.docx",
        "超链接_出.docx",
        "--rules",
        "hyperlink_rule.json",
    ]
)
dh_out = docx.Document("超链接_出.docx")
ph_out = dh_out.paragraphs[0]
check(ph_out.text == "前缀 李四 李四 后缀", f"hyperlink replace wrong: {ph_out.text!r}")
check(len(ph_out.hyperlinks) == 1, "hyperlink element lost")
check(
    ph_out.hyperlinks[0].runs[0].text == "李四",
    f"hyperlink text not replaced: {ph_out.hyperlinks[0].runs[0].text!r}",
)
check(
    ph_out.hyperlinks[0].address == "https://example.com/张三",
    f"hyperlink address lost: {ph_out.hyperlinks[0].address!r}",
)

# reject empty find (exit 1) and validate rules shape (exit 2).
with open("bad_rules_empty.json", "w", encoding="utf-8") as f:
    json.dump([{"find": "", "replace": "x"}], f, ensure_ascii=False)
run(
    [
        "python",
        script("docx", "replace_text.py"),
        "原 件.docx",
        "输出/空find.docx",
        "--rules",
        "bad_rules_empty.json",
    ],
    expect=1,
)
with open("bad_rules_shape.json", "w", encoding="utf-8") as f:
    json.dump([{"find": "张三"}], f, ensure_ascii=False)
run(
    [
        "python",
        script("docx", "replace_text.py"),
        "原 件.docx",
        "输出/形状错.docx",
        "--rules",
        "bad_rules_shape.json",
    ],
    expect=2,
)

print("PASS case_docx_replace")
