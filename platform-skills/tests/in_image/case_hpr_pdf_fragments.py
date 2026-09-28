"""Review I-2: the PDF keeps the no-stub rule — a split table keeps >= 2 rows and a split
paragraph >= 3 lines on each side of a page break (checked with pdfplumber over one full page
cycle of break positions)."""

import json
import os
import sys
from pathlib import Path

import pdfplumber
from _harness import check

os.chdir("/workspace")
skill = Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "health-plan-report"
SAMPLE = json.loads((skill / "sample" / "sample-plan.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(skill / "scripts"))
from hpr.content import normalize_content  # noqa: E402
from hpr.pdf_html import build_html  # noqa: E402
from hpr.style import resolve as resolve_style  # noqa: E402
from hpr.theme import build_theme  # noqa: E402
from weasyprint import HTML  # noqa: E402


def probe(n: int, rows: int) -> list[str]:
    filler = "\n".join(f"填充第{i}行" for i in range(n))
    table = {
        "kind": "table",
        "columns": ["日期", "安排"],
        "rows": [[f"表行{i}", "快走 30 分钟"] for i in range(rows)],
    }
    para = {"kind": "paragraph", "text": "\n".join(f"段落第{i}行" for i in range(5))}
    plan = json.loads(json.dumps(SAMPLE))
    plan["client"].pop("facts", None)
    plan.pop("period", None)
    plan.pop("data_basis", None)
    plan["sections"] = [
        {"id": "p", "title": "分页", "blocks": [{"kind": "paragraph", "text": filler}, table, para]}
    ]
    plan = normalize_content(plan)
    style = resolve_style([], plan).style
    html, _ = build_html(plan, style, build_theme(style, "pdf"), Path("."))
    HTML(string=html, base_url=".").write_pdf("probe.pdf")
    bad = []
    with pdfplumber.open("probe.pdf") as pdf:
        texts = [(p.extract_text() or "").replace(" ", "") for p in pdf.pages[1:]]
    for marker, least in (("表行", 2), ("段落第", 3)):
        counts = [t.count(marker) for t in texts if marker in t]
        if len(counts) > 1 and min(counts) < least:
            bad.append(f"{marker}{counts}")
    return bad


found = [(n, r, b) for r in (3, 6) for n in range(8, 52) if (b := probe(n, r))]
check(found == [], f"PDF fragments below the no-stub minimum: {found}")

print("PASS case_hpr_pdf_fragments")
