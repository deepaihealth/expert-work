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

# hostile title / org: markup and control characters stay text, render succeeds, QA finds them
hostile = json.loads(Path("plan.json").read_text(encoding="utf-8"))
hostile["title"] = '李先生</style><img src="http://x/y">\f方案'
hostile["brand"]["org_name"] = '示例中心</style><link rel="stylesheet" href="http://x/s.css">'
Path("hostile.json").write_text(json.dumps(hostile, ensure_ascii=False), encoding="utf-8")
h = json.loads(
    run(
        [
            "python",
            script("health-plan-report", "render.py"),
            "--content",
            "hostile.json",
            "--format",
            "pdf",
            "--out-dir",
            "out",
            "--basename",
            "hostile",
        ],
        timeout=600,
    ).stdout
)
check(h["ok"] is True and h["qa"]["pdf"]["missing"] == [], f"hostile title render: {h}")
check(not any("不允许" in w for w in h["warnings"]), f"nothing should even try to load: {h}")

# an over-long disclaimer is refused instead of running into the paper edge
long_ = json.loads(Path("plan.json").read_text(encoding="utf-8"))
long_["brand"]["disclaimer"] = "本方案为生活方式管理建议，不构成医学诊断或治疗建议。" * 12  # noqa: RUF001
Path("long.json").write_text(json.dumps(long_, ensure_ascii=False), encoding="utf-8")
lf = json.loads(
    run(
        [
            "python",
            script("health-plan-report", "render.py"),
            "--content",
            "long.json",
            "--format",
            "pdf",
            "--out-dir",
            "out",
            "--basename",
            "long",
        ],
        expect=1,
    ).stdout
)
check(lf["errors"] and lf["errors"][0].startswith("brand.disclaimer"), f"long footer: {lf}")
check(not list(Path("out").glob("long*")), "failed run must leave no files behind")

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
