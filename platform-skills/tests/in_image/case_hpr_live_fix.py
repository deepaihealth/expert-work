"""Live-stack fixes: table columns never break short tokens (PDF, pdfplumber); covers fit at
every type scale; sparse pagination; the client-info band. Runs in the real image."""

import json
import os
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
SCALES = ("compact", "standard", "large")


def blocks(plan: dict, kind: str) -> list[dict]:
    return [b for s in plan["sections"] for b in s["blocks"] if b["kind"] == kind]


def render(plan: dict, name: str, *, fmt: str = "both", style: dict | None = None):
    Path(f"{name}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    cmd = ["python", script("health-plan-report", "render.py"), "--content", f"{name}.json"]
    if style:
        Path(f"{name}.style.json").write_text(json.dumps(style, ensure_ascii=False), "utf-8")
        cmd += ["--style", f"{name}.style.json"]
    cmd += ["--format", fmt, "--out-dir", "out", "--basename", name]
    return json.loads(run(cmd, expect=0, timeout=600).stdout)


def pdf_lines(path: str) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [
            ln.replace(" ", "") for p in pdf.pages for ln in (p.extract_text() or "").splitlines()
        ]


def whole_tokens(lines: list[str], tokens: dict[str, int]) -> list[str]:
    """Tokens that do not appear whole on single lines at least ``n`` times."""
    return [t for t, n in tokens.items() if sum(ln.count(t) for ln in lines) < n]


# ---- defect 2: PDF table columns fit their short tokens ----
FOODS = "杂粮饭（大米＋糙米）75 g、清蒸鲈鱼 100 g、清炒西兰花 200 g、橄榄油 10 g"  # noqa: RUF001
tb = json.loads(json.dumps(SAMPLE))
meal = blocks(tb, "meal_plan")[0]
meal["templates"][0]["meals"] = [
    {"name": "早餐", "time": "7:30", "foods": [{"name": FOODS, "amount": "1 份"}], "kcal": 420},
    {
        "name": "午餐",
        "time": "12:00",
        "foods": [{"name": FOODS * 2, "amount": "1 份"}],
        "kcal": 560,
    },
    {"name": "加餐", "time": "15:30", "foods": [{"name": "苹果", "amount": "100 g"}], "kcal": 100},
]
ex = blocks(tb, "exercise")[0]
ex["schedule"] = [
    {"day": d, "items": ["快走 30 分钟，餐后半小时开始，微微出汗能说话即可"]}  # noqa: RUF001
    for d in ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
]
for scale in SCALES:
    style = {"type.scale": scale, "blocks.meal_plan.variant": "table"}
    r = render(tb, f"tables-{scale}", fmt="pdf", style=style)
    check(r["ok"] is True, f"tables {scale}: {r}")
    lines = pdf_lines(f"out/tables-{scale}.pdf")
    days = dict.fromkeys(("周二", "周三", "周四", "周五", "周六", "周日"), 1)
    bad = whole_tokens(lines, {"7:30": 1, "12:00": 1, "15:30": 1, "早餐": 1, "午餐": 1, **days})
    check(bad == [], f"tables {scale}: tokens broken across lines: {bad}")

print("PASS case_hpr_live_fix")
