import json
import os
import re
from pathlib import Path

import docx
import pdfplumber
from _harness import check, run, script
from docx.oxml.ns import qn

os.chdir("/workspace")
body = (Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "docx" / "SKILL.md").read_text(
    encoding="utf-8"
)
m = re.search(r"```python title=skeleton\n(.*?)```", body, re.S)
check(m is not None, "SKILL.md has no ```python title=skeleton block")
exec(compile(m.group(1), "skeleton", "exec"), {})  # noqa: S102
out = Path("骨架 示例.docx")
check(out.is_file(), "skeleton did not produce 骨架 示例.docx")
doc = docx.Document(str(out))
rfonts = doc.styles["Normal"].element.rPr.rFonts
check(rfonts.get(qn("w:eastAsia")) == "微软雅黑", f"eastAsia font={rfonts.get(qn('w:eastAsia'))}")
check(
    len(doc.tables) >= 1 and len(doc.inline_shapes) >= 1, "skeleton must show a table and a picture"
)
pdf = json.loads(
    run(
        ["python", script("docx", "convert.py"), str(out), "--to", "pdf", "--out-dir", "pdf"]
    ).stdout
)["output"]
with pdfplumber.open(pdf) as pp:
    text = "".join(pg.extract_text() or "" for pg in pp.pages)
check(
    "中文" in text or "方案" in text,
    f"CJK text not extractable after 微软雅黑 fallback: {text[:200]!r}",
)
res = json.loads(run(["python", script("docx", "preview.py"), str(out)]).stdout)
check(res["images"], "preview produced nothing")
print("PASS case_docx_create")
