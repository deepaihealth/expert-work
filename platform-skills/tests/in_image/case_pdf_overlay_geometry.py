"""watermark / stamp honor /Rotate and non-zero box origins (final review Minor 11).

Before the fix the overlay was merged in raw user space at (0, 0): on a /Rotate 90 page the
watermark came out sideways, and on a page whose mediabox starts at (100, 200) it was
shifted off-centre / off-page. pdfplumber reports chars in displayed coordinates (rotation
applied, origin moved to the box corner), so we assert on what a reader actually sees
(pdfplumber keeps a non-zero origin in page.bbox, so positions are taken relative to it).
"""

import json
import os

import pdfplumber
from _harness import check, run, script
from pypdf import PdfWriter
from pypdf.generic import RectangleObject
from weasyprint import HTML

os.chdir("/workspace")

w = PdfWriter()
w.add_blank_page(595, 842).rotate(90)  # 1: portrait page shown landscape
p2 = w.add_blank_page(595, 842)  # 2: offset mediabox
p2.mediabox = RectangleObject([100, 200, 695, 1042])
p3 = w.add_blank_page(595, 842)  # 3: offset mediabox + /Rotate 270
p3.mediabox = RectangleObject([100, 200, 695, 1042])
p3.rotate(270)
w.add_blank_page(595, 842).rotate(180)  # 4: upside-down
w.write("几何.pdf")
DISPLAY = [(842, 595), (595, 842), (842, 595), (595, 842)]

res = json.loads(
    run(
        [
            "python",
            script("pdf", "pdf_ops.py"),
            "watermark",
            "几何.pdf",
            "几何_水印.pdf",
            "--text",
            "水印",
            "--angle",
            "0",
            "--opacity",
            "1",
        ]
    ).stdout
)
with pdfplumber.open(res["output"]) as pdf:
    for n, (page, (dw, dh)) in enumerate(zip(pdf.pages, DISPLAY, strict=True), start=1):
        check((round(page.width), round(page.height)) == (dw, dh), f"p{n} size {page.bbox}")
        chars = [c for c in page.chars if c["text"] in "水印"]
        check(len(chars) == 2, f"p{n}: watermark chars {[c['text'] for c in page.chars]}")
        check(all(c["upright"] for c in chars), f"p{n}: watermark not upright")
        bx, by = page.bbox[0], page.bbox[1]
        cx = (min(c["x0"] for c in chars) + max(c["x1"] for c in chars)) / 2 - bx
        cy = (min(c["top"] for c in chars) + max(c["bottom"] for c in chars)) / 2 - by
        check(
            abs(cx - dw / 2) < dw * 0.05 and abs(cy - dh / 2) < dh * 0.05,
            f"p{n}: watermark centre ({cx:.0f},{cy:.0f}) not near ({dw / 2:.0f},{dh / 2:.0f})",
        )
        check(
            [c["text"] for c in sorted(chars, key=lambda c: c["x0"])] == ["水", "印"],
            f"p{n}: watermark reads backwards",
        )

# a small stamp page, text near its top-left; after stamping it must sit, upright, at the
# lower-left of every displayed page (overlay anchored at the visible box's lower-left).
HTML(
    string="<html><head><style>@page { size: 200pt 100pt; margin: 0 }"
    "body { margin: 0 } div { position: absolute; left: 10pt; top: 10pt;"
    ' font-family: "Noto Sans CJK SC"; font-size: 20pt }</style></head>'
    "<body><div>已盖章</div></body></html>"
).write_pdf("小章.pdf")
res = json.loads(
    run(
        [
            "python",
            script("pdf", "pdf_ops.py"),
            "stamp",
            "几何.pdf",
            "几何_盖章.pdf",
            "--stamp",
            "小章.pdf",
        ]
    ).stdout
)
with pdfplumber.open(res["output"]) as pdf:
    for n, (page, (_dw, dh)) in enumerate(zip(pdf.pages, DISPLAY, strict=True), start=1):
        chars = [c for c in page.chars if c["text"] in "已盖章"]
        check(len(chars) == 3, f"p{n}: stamp chars {[c['text'] for c in page.chars]}")
        check(all(c["upright"] for c in chars), f"p{n}: stamp not upright")
        x0 = min(c["x0"] for c in chars) - page.bbox[0]
        top = min(c["top"] for c in chars) - page.bbox[1]
        check(
            5 < x0 < 20 and dh - 100 < top < dh - 80,
            f"p{n}: stamp at x0={x0:.0f} top={top:.0f}, want ~10 / ~{dh - 90:.0f}",
        )
print("PASS case_pdf_overlay_geometry")
