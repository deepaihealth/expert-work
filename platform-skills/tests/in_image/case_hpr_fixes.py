"""Final fix wave probes that need the real PDF runtime (weasyprint + fonts)."""

import json
import os
import shutil
import sys
from pathlib import Path

from _harness import check, run, script
from PIL import Image

os.chdir("/workspace")
skill = Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "health-plan-report"
shutil.copy(skill / "sample" / "sample-plan.json", "plan.json")
Image.new("RGB", (640, 360), "#DDEEEE").save("trend-note.png")
SAMPLE = json.loads(Path("plan.json").read_text(encoding="utf-8"))


def blocks(plan: dict, kind: str) -> list[dict]:
    return [b for s in plan["sections"] for b in s["blocks"] if b["kind"] == kind]


def render(plan: dict, name: str, *, fmt: str = "both", style: dict | None = None, expect=0):
    Path(f"{name}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    cmd = ["python", script("health-plan-report", "render.py"), "--content", f"{name}.json"]
    if style:
        Path(f"{name}.style.json").write_text(json.dumps(style, ensure_ascii=False), "utf-8")
        cmd += ["--style", f"{name}.style.json"]
    cmd += ["--format", fmt, "--out-dir", "out", "--basename", name]
    return json.loads(run(cmd, expect=expect, timeout=600).stdout)


# I1: CRLF / \v / \f in caller text renders in both formats and QA finds every text
cc = json.loads(json.dumps(SAMPLE))
blocks(cc, "paragraph")[0]["text"] = "第一段\r\n第二段\r第三段"
blocks(cc, "callout")[0]["text"] = "第一行\x0b第二行"
blocks(cc, "table")[0]["rows"][0][0] = "A\x0cB"
blocks(cc, "bullets")[0]["items"][0] = "甲\u2028乙\u2029丙"
r = render(cc, "ctrl")
check(r["ok"] is True, f"control characters: {r}")
check(r["qa"]["pptx"]["missing"] == [] and r["qa"]["pdf"]["missing"] == [], f"ctrl qa: {r}")

# I2: two focus items sharing an area in the phases table both reach both formats
ph = json.loads(json.dumps(SAMPLE))
blocks(ph, "phases")[0]["items"][0]["focus"] = [
    {"area": "饮食", "text": "控糖"},
    {"area": "饮食", "text": "晚餐控糖饮食"},
]
r = render(ph, "phases", style={"blocks.phases.variant": "table"})
check(r["ok"] is True, f"shared phase area: {r}")

# I2: the PDF gate counts occurrences (a body text required twice but printed once is missing)
sys.path.insert(0, str(skill / "scripts"))
from hpr.content import required_texts  # noqa: E402
from hpr.qa import qa_pdf  # noqa: E402
from hpr.style import resolve  # noqa: E402
from hpr.theme import build_theme  # noqa: E402

once = blocks(SAMPLE, "paragraph")[0]["text"]
theme = build_theme(resolve([], SAMPLE).style, "pdf")
q = qa_pdf(Path("out/phases.pdf"), [*required_texts(ph), once], theme)
check(q["missing"] == [once], f"PDF gate must count occurrences: {q['missing']}")

# I3: the PDF bar trend prints every value, negatives included
neg = json.loads(json.dumps(SAMPLE))
tr = blocks(neg, "trend")[0]
tr["points"] = [{"date": d, "value": v} for d, v in (("W1", -0.8), ("W2", -1.2), ("W3", 0.3))]
r = render(neg, "negbar", fmt="pdf", style={"blocks.trend.variant": "bar"})
check(r["ok"] is True, f"negative bar: {r}")
from pypdf import PdfReader  # noqa: E402

text = "".join("".join((p.extract_text() or "").split()) for p in PdfReader("out/negbar.pdf").pages)
check(all(v in text for v in ("-0.8", "-1.2", "0.3")), "PDF bar chart must print every value")

# N1: a required text spelled across two adjacent cells must not make real cells "missing"
st = json.loads(json.dumps(SAMPLE))
tb = blocks(st, "table")[0]
tb["columns"], tb["rows"] = ["指标", "状态"], [["空腹血糖", "偏高"], ["血压", "偏高"]]
blocks(st, "paragraph")[0]["text"] = "高血压"
r = render(st, "straddle")
check(r["ok"] is True, f"straddling cells: {r}")

print("PASS case_hpr_fixes")
