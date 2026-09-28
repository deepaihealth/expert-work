"""A card grid that fits on a page never splits across PDF pages (live replay: goal cards
three on one page, two on the next); a grid taller than a page still renders, split."""

import copy
import json
import os
import re
import shutil
from pathlib import Path

import pdfplumber
from _harness import check, run, script
from PIL import Image

os.chdir("/workspace")
skill = Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "health-plan-report"
shutil.copy(skill / "sample" / "sample-plan.json", "plan.json")
Image.new("RGB", (640, 360), "#DDEEEE").save("trend-note.png")
SAMPLE = json.loads(Path("plan.json").read_text(encoding="utf-8"))


def goals_section(plan: dict) -> dict:
    return next(s for s in plan["sections"] if any(b["kind"] == "goals" for b in s["blocks"]))


def render(plan: dict, name: str) -> str:
    Path(f"{name}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    cmd = ["python", script("health-plan-report", "render.py"), "--content", f"{name}.json"]
    cmd += ["--format", "pdf", "--out-dir", "out", "--basename", name]
    res = json.loads(run(cmd, expect=0, timeout=600).stdout)
    check(res["ok"], f"{name} renders")
    return res["files"]["pdf"]


def pages_with(path: str, needle: str) -> list[int]:
    with pdfplumber.open(path) as pdf:
        return [
            n
            for n, p in enumerate(pdf.pages, 1)
            if needle in re.sub(r"\s+", "", p.extract_text() or "")
        ]


for n in (4, 10, 16, 22, 28):
    plan = copy.deepcopy(SAMPLE)
    sec = goals_section(plan)
    goals = next(b for b in sec["blocks"] if b["kind"] == "goals")
    goals["items"] = [
        {"name": f"网格目标{i}", "current": 72.5, "target": 71.0, "unit": "kg", "due": "第2周末"}
        for i in range(5)
    ]
    filler = "\n".join(f"填充说明第{i}行" for i in range(n))
    sec["blocks"].insert(sec["blocks"].index(goals), {"kind": "paragraph", "text": filler})
    pdf = render(plan, f"grid{n}")
    spots = {p for i in range(5) for p in pages_with(pdf, f"网格目标{i}")}
    check(len(spots) == 1, f"filler {n}: all five goal cards on one page, got {sorted(spots)}")

plan = copy.deepcopy(SAMPLE)
goals = next(b for b in goals_section(plan)["blocks"] if b["kind"] == "goals")
goals["items"] = [
    {"name": f"长目标{i}", "target": 71.0, "unit": "kg", "note": "说明" * 20} for i in range(30)
]
pdf = render(plan, "gridbig")
check(len({p for i in range(30) for p in pages_with(pdf, f"长目标{i}")}) > 1, "tall grid splits")
print("PASS case_hpr_grid_whole")
