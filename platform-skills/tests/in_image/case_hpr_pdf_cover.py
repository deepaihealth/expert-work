import json
import os
import sys
from pathlib import Path

from _harness import check
from PIL import Image

os.chdir("/workspace")
skill = Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "health-plan-report"
sys.path.insert(0, str(skill / "scripts"))
from hpr.pdf_guard import check_cover, make_fetcher  # noqa: E402
from hpr.pdf_html import build_html  # noqa: E402
from hpr.ppt_layout import LayoutError  # noqa: E402
from hpr.style import Layer, resolve  # noqa: E402
from hpr.theme import build_theme  # noqa: E402
from weasyprint import HTML  # noqa: E402

base = Path("/workspace")
Image.new("RGB", (640, 360), "#DDEEEE").save(base / "trend-note.png")
Image.new("RGB", (300, 120), "#0B4F5C").save(base / "logo.png")
sample = json.loads((skill / "sample" / "sample-plan.json").read_text(encoding="utf-8"))
sample["brand"]["logo_path"] = "logo.png"


def cover_error(content, layers):
    style = resolve(layers, content).style
    html, _ = build_html(content, style, build_theme(style, "pdf"), base)
    try:
        check_cover(HTML(string=html, base_url=str(base)).render())
    except LayoutError as exc:
        return exc
    return None


for variant in ("band", "split", "minimal"):
    for pos in ("top-left", "center", "bottom-right"):
        layers = [Layer("x", {"cover.variant": variant, "brand.logo_position": pos})]
        err = cover_error(sample, layers)
        check(err is None, f"{variant}/{pos}: sample cover must lay out cleanly: {err}")

crowded = json.loads(json.dumps(sample))
crowded["title"] = "李先生健康管理方案之控糖减重与睡眠改善综合计划书" * 5
crowded["subtitle"] = "第 1 阶段 · 这是一个较长的副标题，用于说明本方案的适用范围与执行周期" * 20  # noqa: RUF001
for variant in ("band", "split", "minimal"):
    err = cover_error(crowded, [Layer("x", {"cover.variant": variant})])
    check(err is not None, f"{variant}: an over-full cover must be a LayoutError")

# deny-by-default fetcher: remote and out-of-tree file URLs are refused (weasyprint logs, skips)
import logging  # noqa: E402

seen: list[str] = []


class _Grab(logging.Handler):
    def emit(self, record):
        seen.append(record.getMessage())


logging.getLogger("weasyprint").addHandler(_Grab())
probe = '<p>x</p><img src="http://example.invalid/a.png"><img src="file:///etc/hostname">'
HTML(string=probe, base_url=str(base), url_fetcher=make_fetcher(base, frozenset())).render()
refused = [m for m in seen if "不允许加载外部资源" in m]
check(len(refused) == 2, f"fetcher must refuse both URLs: {seen}")

# an image block weasyprint cannot load (here: refused) fails the render instead of vanishing
import hpr.pdf_html as pdf_html  # noqa: E402
from hpr.blocks import RenderError  # noqa: E402

pdf_html.make_fetcher = lambda _base, _files: make_fetcher(Path("/nonexistent"), frozenset())
try:
    pdf_html.render_pdf(sample, resolve([], sample).style, base / "refused.pdf", base)
    raised = None
except RenderError as exc:
    raised = exc
check(raised is not None and raised.path.startswith("sections["), f"refused image: {raised}")
check(not (base / "refused.pdf").exists(), "no PDF may be written when an image is dropped")
print("PASS case_hpr_pdf_cover")
