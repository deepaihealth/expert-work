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

out = json.loads(
    run(
        [
            "python",
            script("health-plan-report", "render.py"),
            "--content",
            "plan.json",
            "--format",
            "both",
            "--out-dir",
            "out",
            "--basename",
            "样例_20260928100000",
        ],
        timeout=600,
    ).stdout
)
check(out["ok"] is True, f"render not ok: {out}")
check(out["qa"]["pptx"]["status"] == "passed", f"pptx qa: {out['qa']['pptx']}")
check(out["qa"]["pdf"]["status"] == "passed", f"pdf qa: {out['qa']['pdf']}")
check(out["qa"]["pdf"]["fonts_embedded"], "CJK font not embedded in PDF")
check(out["qa"]["pdf"]["pages"] >= 4, f"too few PDF pages: {out['qa']['pdf']['pages']}")

sys.path.insert(0, str(skill / "scripts"))
from hpr.measure import Measurer  # noqa: E402
from pypdf import PdfReader  # noqa: E402

check(Measurer().real, "real CJK font not found in image — measurement would be approximate")
cover = "".join((PdfReader("out/样例_20260928100000.pdf").pages[0].extract_text() or "").split())
check("阶段" in cover and "第1阶段" in cover, f"cover meta lacks the period: {cover}")

again = json.loads(
    run(
        [
            "python",
            script("health-plan-report", "render.py"),
            "--content",
            "plan.json",
            "--format",
            "pptx",
            "--out-dir",
            "out",
            "--basename",
            "样例_20260928100000",
        ],
        expect=1,
    ).stdout
)
check(any("已存在" in e for e in again["errors"]), "existing output must be refused")

run(
    [
        "soffice",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        "lo",
        "out/样例_20260928100000.pptx",
    ],
    timeout=300,
)
check(Path("lo/样例_20260928100000.pdf").is_file(), "LibreOffice could not open the PPTX")
print("PASS case_hpr_render")
