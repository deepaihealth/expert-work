import importlib.util
import json
import os
import sys

from _harness import check, run, script
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML

os.chdir("/workspace")
HTML(
    string="<html><body>"
    + "".join(f"<h1 style='break-before:page'>第{i}页</h1>" for i in range(1, 5))
    + "</body></html>"
).write_pdf("原 文件.pdf")
HTML(string="<p>另一份</p>").write_pdf("附件.pdf")
info = json.loads(run(["python", script("pdf", "pdf_ops.py"), "info", "原 文件.pdf"]).stdout)
check(info["pages"] == 4 and info["has_text"] and not info["encrypted"], f"info={info}")
m = json.loads(
    run(
        ["python", script("pdf", "pdf_ops.py"), "merge", "合并/合并.pdf", "原 文件.pdf", "附件.pdf"]
    ).stdout
)
check(m["pages"] == 5 and len(PdfReader(m["output"]).pages) == 5, f"merge={m}")
s = json.loads(run(["python", script("pdf", "pdf_ops.py"), "split", "原 文件.pdf", "拆分"]).stdout)
check(len(s["outputs"]) == 4, f"split={s}")
s2 = json.loads(
    run(
        ["python", script("pdf", "pdf_ops.py"), "split", "原 文件.pdf", "拆分2", "--pages", "2-3"]
    ).stdout
)
check(len(s2["outputs"]) == 1 and len(PdfReader(s2["outputs"][0]).pages) == 2, f"split pages={s2}")
r = json.loads(
    run(
        [
            "python",
            script("pdf", "pdf_ops.py"),
            "rotate",
            "原 文件.pdf",
            "转.pdf",
            "--degrees",
            "90",
            "--pages",
            "2",
        ]
    ).stdout
)
rd = PdfReader("转.pdf")
check(rd.pages[1].rotation == 90 and rd.pages[0].rotation == 0, f"rotate={r}")
run(
    [
        "python",
        script("pdf", "pdf_ops.py"),
        "watermark",
        "原 文件.pdf",
        "水印.pdf",
        "--text",
        "内部资料 勿外传",
    ]
)
wm = PdfReader("水印.pdf")
check(
    len(wm.pages) == 4 and "内部资料" in (wm.pages[2].extract_text() or ""),
    "watermark text missing on page 3",
)
# NOTE (deviation from the plan's literal check): `str(page["/Resources"])` never contains
# "CJK"/"Noto" — pypdf keeps the /Font entry as an unresolved IndirectObject at that level, so
# the substring check always fails regardless of whether the font is really embedded (verified
# empirically: same false result with merge_page present or removed). pdffonts is the tool that
# actually resolves and reports embedding, matching what the task asked us to verify ("rather
# than tofu"), so we shell out to it instead.
fonts_out = run(["pdffonts", "水印.pdf"]).stdout
embedded_cjk = any(
    cols and ("CJK" in cols[0] or "Noto" in cols[0]) and "yes" in cols
    for cols in (line.split() for line in fonts_out.splitlines()[2:])
)
check(embedded_cjk, f"watermark font not embedded CJK: {fonts_out[:300]}")
HTML(string="<p style='color:red;font-size:40pt'>已审核</p>").write_pdf("章.pdf")
st = json.loads(
    run(
        [
            "python",
            script("pdf", "pdf_ops.py"),
            "stamp",
            "原 文件.pdf",
            "盖章.pdf",
            "--stamp",
            "章.pdf",
            "--pages",
            "4",
        ]
    ).stdout
)
check(
    st["stamped"] == [4] and "已审核" in (PdfReader("盖章.pdf").pages[3].extract_text() or ""),
    f"stamp={st}",
)
run(
    [
        "python",
        script("pdf", "pdf_ops.py"),
        "rotate",
        "原 文件.pdf",
        "原 文件.pdf",
        "--degrees",
        "90",
    ],
    expect=1,
)

# --- extra edge cases beyond the plan's baseline (realistic inputs pdf_ops.py must handle) ---

# encrypted input: info reports it truthfully; every other command refuses with a Chinese error.
enc = PdfWriter()
enc.append("原 文件.pdf")
enc.encrypt(user_password="secret123")
enc.write("加密.pdf")
info_enc = json.loads(run(["python", script("pdf", "pdf_ops.py"), "info", "加密.pdf"]).stdout)
check(info_enc["encrypted"] is True, f"encrypted info should report encrypted=true: {info_enc}")
enc_fail = run(
    ["python", script("pdf", "pdf_ops.py"), "rotate", "加密.pdf", "解密转.pdf", "--degrees", "90"],
    expect=1,
)
check("加密" in enc_fail.stderr, f"expected Chinese '已加密' error, got: {enc_fail.stderr}")

# corrupt / not-a-PDF input: clean failure, not a raw traceback.
with open("损坏.pdf", "wb") as fh:
    fh.write(b"this is not a pdf file at all")
corrupt_fail = run(["python", script("pdf", "pdf_ops.py"), "info", "损坏.pdf"], expect=1)
check(
    "损坏" in corrupt_fail.stderr or "合法" in corrupt_fail.stderr, f"stderr={corrupt_fail.stderr}"
)

# malformed / reversed / out-of-bounds --pages: usage errors (exit 2), except a partial overlap
# with valid pages, which clips silently (same semantics preview.py already established).
run(
    [
        "python",
        script("pdf", "pdf_ops.py"),
        "rotate",
        "原 文件.pdf",
        "x1.pdf",
        "--degrees",
        "90",
        "--pages",
        "abc",
    ],
    expect=2,
)
run(
    [
        "python",
        script("pdf", "pdf_ops.py"),
        "rotate",
        "原 文件.pdf",
        "x2.pdf",
        "--degrees",
        "90",
        "--pages",
        "3-1",
    ],
    expect=2,
)
run(
    [
        "python",
        script("pdf", "pdf_ops.py"),
        "rotate",
        "原 文件.pdf",
        "x3.pdf",
        "--degrees",
        "90",
        "--pages",
        "99",
    ],
    expect=2,
)
partial = json.loads(
    run(
        [
            "python",
            script("pdf", "pdf_ops.py"),
            "rotate",
            "原 文件.pdf",
            "x4.pdf",
            "--degrees",
            "90",
            "--pages",
            "3-99",
        ]
    ).stdout
)
check(partial["rotated"] == [3, 4], f"out-of-bounds tail should clip: {partial}")
run(
    ["python", script("pdf", "pdf_ops.py"), "rotate", "原 文件.pdf", "x5.pdf", "--degrees", "45"],
    expect=2,
)  # argparse choices reject a degree value outside 90/180/270

# merging PDFs with different page sizes: no crash, and info reports each page's own size.
HTML(
    string="<html><head><style>@page{size:200pt 200pt}</style></head><body>小</body></html>"
).write_pdf("小页.pdf")
json.loads(
    run(
        ["python", script("pdf", "pdf_ops.py"), "merge", "混合尺寸.pdf", "原 文件.pdf", "小页.pdf"]
    ).stdout
)
mixed_info = json.loads(run(["python", script("pdf", "pdf_ops.py"), "info", "混合尺寸.pdf"]).stdout)
check(
    mixed_info["sizes_pt"][0] != mixed_info["sizes_pt"][-1]
    and mixed_info["sizes_pt"][-1] == [200.0, 200.0],
    f"mixed page sizes not preserved: {mixed_info['sizes_pt']}",
)

# splitting a 1-page PDF: exactly one output either way.
one_page = json.loads(
    run(["python", script("pdf", "pdf_ops.py"), "split", "附件.pdf", "单页拆分"]).stdout
)
check(len(one_page["outputs"]) == 1, f"1-page split should give exactly 1 file: {one_page}")

# very many pages: merge/split stay correct and fast at scale.
many = PdfWriter()
for _ in range(60):
    many.add_blank_page(width=100, height=100)
many.write("很多页.pdf")
many_split = json.loads(
    run(["python", script("pdf", "pdf_ops.py"), "split", "很多页.pdf", "很多页拆分"]).stdout
)
check(len(many_split["outputs"]) == 60, f"60-page split count wrong: {len(many_split['outputs'])}")
many_merge = json.loads(
    run(
        ["python", script("pdf", "pdf_ops.py"), "merge", "很多页合并.pdf", "很多页.pdf", "附件.pdf"]
    ).stdout
)
check(many_merge["pages"] == 61, f"many-page merge count wrong: {many_merge}")

# same-path rejection is not just a rotate special case: merge must also refuse it (any input
# equal to the output), with a stderr message a model can act on.
merge_same = run(
    ["python", script("pdf", "pdf_ops.py"), "merge", "原 文件.pdf", "原 文件.pdf", "附件.pdf"],
    expect=1,
)
check(
    "覆盖" in merge_same.stderr, f"expected overwrite-rejection message, got: {merge_same.stderr}"
)

# preview.py and pdf_ops.py each carry a copy of _parse_pages — the two must agree.
sys.path.insert(0, os.path.dirname(script("pdf", "pdf_ops.py")))


def _load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3] + "_cmp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prev, ops = _load(script("pdf", "preview.py")), _load(script("pdf", "pdf_ops.py"))
for spec_text, total in [("1-3", 5), ("2,4", 5), ("all", 3), ("4-9", 5), ("1,1,2", 2)]:
    check(
        prev._parse_pages(spec_text, total) == ops._parse_pages(spec_text, total),
        f"page spec disagree: {spec_text}",
    )
print("PASS case_pdf_ops")
