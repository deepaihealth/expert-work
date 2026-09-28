"""Final fix wave probes that need the real PDF runtime (weasyprint + fonts)."""

import json
import os
import shutil
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
r = render(cc, "ctrl")
check(r["ok"] is True, f"control characters: {r}")
check(r["qa"]["pptx"]["missing"] == [] and r["qa"]["pdf"]["missing"] == [], f"ctrl qa: {r}")

print("PASS case_hpr_fixes")
