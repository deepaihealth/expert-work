import json
import os
import re
from pathlib import Path

import pdfplumber
from _harness import check, png_is_not_blank, run, script
from pypdf import PdfReader

os.chdir("/workspace")
body = (Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "pdf" / "SKILL.md").read_text(encoding="utf-8")
m = re.search(r"```python title=skeleton\n(.*?)```", body, re.S)
check(m is not None, "SKILL.md has no ```python title=skeleton block")
exec(compile(m.group(1), "skeleton", "exec"), {})  # noqa: S102
out = Path("骨架 示例.pdf")
check(out.is_file(), "skeleton did not produce 骨架 示例.pdf")

reader = PdfReader(str(out))
check(len(reader.pages) >= 2, f"skeleton must produce >= 2 pages, got {len(reader.pages)}")

# Page 1's font resources must embed a Noto Sans CJK face (not fall back to a non-embedding
# font that would render Chinese as tofu). pdffonts -f/-l restricts the report to page 1 only;
# `str(page["/Resources"])` (the naive approach) never shows the resolved font name — see the
# same note in case_pdf_ops.py.
fonts_page1 = run(["pdffonts", "-f", "1", "-l", "1", str(out)]).stdout
check(
    any(
        cols and "NotoSansCJK" in cols[0].replace("-", "") and "yes" in cols
        for cols in (line.split() for line in fonts_page1.splitlines()[2:])
    ),
    f"page 1 does not embed a Noto Sans CJK font: {fonts_page1}",
)

with pdfplumber.open(str(out)) as pp:
    text = "\n".join(pg.extract_text() or "" for pg in pp.pages)
check("示例方案" in text, f"skeleton heading missing from extracted text: {text[:200]!r}")
check("1 / 2" in text, f"footer page counter '1 / 2' missing: {text[:400]!r}")

res = run(["python", script("pdf", "preview.py"), str(out)])
images = json.loads(res.stdout)["images"]
check(images, "preview produced nothing")
for img in images:
    check(png_is_not_blank(Path(img)), f"blank preview page: {img}")

print("PASS case_pdf_create")
