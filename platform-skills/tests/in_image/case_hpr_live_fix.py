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

# ---- defect 1 + cover ruling: minimal cover fits at every scale; facts go to the band ----
TITLE_30 = "王女士二〇二六年秋季控糖减重与睡眠改善综合健康管理方案"[:30]
SUB_40 = "第 1 阶段（前 2 周）控糖与减重执行方案，配合饮食记录与每周复盘调整计划安排"  # noqa: RUF001
FACTS = [
    ("性别 / 年龄", "女 / 52 岁（1974-03）"),  # noqa: RUF001
    ("身高 / 体重", "160 cm / 68.4 kg（9-26 晨称）"),  # noqa: RUF001
    ("健康状态", "糖前期（2023-04 发现，病程 3 年余）"),  # noqa: RUF001
    ("管理方向", "控糖 + 减重"),
    ("饮食限制", "乳糖不耐受（少量酸奶可以），晚餐清淡"),  # noqa: RUF001
    ("用药情况", "暂无"),
    ("睡眠", "入睡困难，平均 6 小时"),  # noqa: RUF001
    ("运动基础", "每周快走 2 次，每次约 20 分钟"),  # noqa: RUF001
    ("家族史", "母亲 2 型糖尿病"),
    ("数据截至", "2026-09-28"),
]
cv = json.loads(json.dumps(SAMPLE))
cv["title"], cv["subtitle"] = TITLE_30, SUB_40
cv["client"]["facts"] = [{"label": k, "value": v} for k, v in FACTS]
Image.new("RGB", (300, 120), "#0B4F5C").save("logo.png")
cv["brand"]["logo_path"] = "logo.png"
TOKENS = {"1974-03": 1, "68.4kg": 1, "2023-04": 1, "2026-09-28": 1, "160cm": 1, "52岁": 1}
for scale in SCALES:
    for variant in ("band", "split", "minimal"):
        name = f"cover-{scale}-{variant}"
        r = render(cv, name, style={"type.scale": scale, "cover.variant": variant})
        check(r["ok"] is True, f"{name}: {r}")
        with pdfplumber.open(f"out/{name}.pdf") as pdf:
            cover = (pdf.pages[0].extract_text() or "").replace(" ", "")
        shown = [v for _, v in FACTS[:-1] if v.replace(" ", "") in cover]  # [-1] = generated_at
        check(shown == [], f"{name}: facts on cover: {shown}")
        check(cv["client"]["name"] in cover and "2026-09-28" in cover, f"{name}: {cover}")
        lines = pdf_lines(f"out/{name}.pdf")
        check(whole_tokens(lines, TOKENS) == [], f"{name}: {whole_tokens(lines, TOKENS)}")

print("PASS case_hpr_live_fix")
