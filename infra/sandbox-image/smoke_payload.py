"""Sandbox-image smoke payload — runs INSIDE the sandbox via the runner.

Sent as the ``code`` of one runner request (``python -E -P -c``) by
``smoke_test.py``. Asserts the single full image (sandbox-image-consolidation)
ships a working toolchain: Python 3.12 + every office/data/media library +
the system binaries skills shell out to (soffice/poppler/ffmpeg) + Node.js, with
CJK rendering, a working markdown→PDF (weasyprint) path, and pip available for
on-demand installs.

Exit 0 + a trailing ``OK`` line == pass. Any failure raises, so the child
process exits non-zero and the runner reports ``exit_code != 0``.

matplotlib picks the headless Agg backend automatically here (no display in
the sandbox), so no explicit backend selection is needed.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import defusedxml
import imageio
import markdown
import pandas as pd
import pdfplumber
import pypdf
import weasyprint
from docx import Document
from matplotlib import font_manager
from matplotlib import pyplot as plt
from openpyxl import Workbook
from pdf2image import convert_from_path
from PIL import Image
from pptx import Presentation

_CJK = "中文办公能力验证 — 报表/图表"

# Base interpreter sanity (a broken base image fails loudly here).
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"unexpected Python: {sys.version}")

# pip is intentionally present: the sandbox supports on-demand ``pip install``
# into the per-user workspace (PIP_USER) so a skill can pull a missing lib at
# runtime. Assert it stays importable — a base-image change must not silently
# drop the install path.
if importlib.util.find_spec("pip") is None:
    raise RuntimeError("pip missing from the sandbox image (on-demand install path broken)")

# The baked matplotlibrc must default to a CJK family with NO per-call config —
# that is its whole point. Check the default *before* the explicit override
# below, so a malformed/ignored baked rc fails the smoke.
_baked_default = plt.rcParams["font.sans-serif"]
if not any("CJK" in family for family in _baked_default):
    raise RuntimeError(f"baked matplotlibrc lost its CJK default: {_baked_default}")

# A CJK sans font must be registered, else matplotlib renders tofu for
# Chinese. Pick it dynamically (the .ttc face matplotlib registers may be
# named "...CJK JP"/"...CJK SC" depending on face order) rather than pinning
# one regional name.
_cjk_names = sorted(
    n for n in {f.name for f in font_manager.fontManager.ttflist} if "Sans" in n and "CJK" in n
)
if not _cjk_names:
    raise RuntimeError("no Noto Sans CJK face registered by matplotlib")
cjk_font = _cjk_names[0]
plt.rcParams["font.sans-serif"] = [cjk_font, "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# xlsx (openpyxl) with Chinese cells.
wb = Workbook()
ws = wb.active
ws["A1"] = _CJK
ws["A2"] = 123
wb.save("/workspace/smoke.xlsx")

# docx with a Chinese paragraph.
doc = Document()
doc.add_paragraph(_CJK)
doc.save("/workspace/smoke.docx")

# pptx with a Chinese title slide.
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = _CJK
prs.save("/workspace/smoke.pptx")

# pandas round-trip through the xlsx engine.
df = pd.DataFrame({"名称": ["甲", "乙"], "值": [1, 2]})
df.to_excel("/workspace/df.xlsx", index=False)
if pd.read_excel("/workspace/df.xlsx").shape != (2, 2):
    raise RuntimeError("pandas xlsx round-trip changed shape")

# matplotlib chart with a Chinese title — the real CJK-render check.
fig, ax = plt.subplots()
ax.bar(["甲", "乙"], [1, 2])
ax.set_title(_CJK)
fig.savefig("/workspace/chart.png")

# Pillow can open the chart we just wrote.
with Image.open("/workspace/chart.png") as img:
    if img.size[0] <= 0:
        raise RuntimeError("Pillow read a zero-width chart")

# import is enough to prove these wheels + deps load (no separate exercise).
_ = (pypdf.__name__, pdfplumber.__name__, imageio.__name__, defusedxml.__name__)

# PR-C — the runner must exec children with -E -P (user site ON, safe path ON).
if sys.flags.no_user_site or sys.flags.isolated:
    raise RuntimeError(f"user site disabled in exec child (flags={sys.flags})")
if not sys.flags.safe_path:
    raise RuntimeError("safe_path (-P) missing — script dir / cwd would shadow stdlib")

# The system binaries skills shell out to. Having the Python lib is not enough;
# assert the binary is present AND does real work.
for _bin in ("soffice", "pdftoppm", "ffmpeg", "node"):
    if shutil.which(_bin) is None:
        raise RuntimeError(f"system binary missing from PATH: {_bin}")

# Real conversion: pptx → pdf via LibreOffice headless (the xlsx-recalc /
# pptx-thumbnail / docx-accept-changes paths all run soffice this way). A
# throwaway profile under the writable HOME avoids a stale-lock hang.
_soffice = subprocess.run(
    [
        "soffice",
        "--headless",
        "--nolockcheck",
        "-env:UserInstallation=file:///workspace/.lo",
        "--convert-to",
        "pdf",
        "--outdir",
        "/workspace",
        "/workspace/smoke.pptx",
    ],
    capture_output=True,
    text=True,
    timeout=120,
    check=False,
)
_pdf = Path("/workspace/smoke.pdf")
if _soffice.returncode != 0 or not _pdf.is_file() or _pdf.stat().st_size == 0:
    raise RuntimeError(
        f"soffice pptx→pdf failed rc={_soffice.returncode} stderr={_soffice.stderr!r}"
    )

# Real PDF→image through pdf2image → proves the poppler binaries are wired.
_pages = convert_from_path(str(_pdf), dpi=50)
if not _pages or _pages[0].size[0] <= 0:
    raise RuntimeError("pdf2image/poppler produced no page image")

# Node.js — skill-bundled .js runs via the `bash` tool (subprocess). Prove the
# runtime executes, not just that the binary is on PATH.
_node = subprocess.run(
    ["node", "-e", "process.stdout.write('node-ok')"],
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
)
if _node.returncode != 0 or _node.stdout.strip() != "node-ok":
    raise RuntimeError(f"node exec failed rc={_node.returncode} stderr={_node.stderr!r}")

# markdown → HTML → PDF via markdown + weasyprint (the report-generation path).
# A real render proves the weasyprint/pydyf pairing AND the pango/cairo system
# libs are wired — a pydyf-version mismatch crashes here at render time, not at
# import (the ``'super' object has no attribute 'transform'`` failure mode).
_md_html = markdown.markdown(f"# {_CJK}\n\n中文 **PDF** 生成验证")
weasyprint.HTML(string=_md_html).write_pdf("/workspace/smoke_md.pdf")
_md_pdf = Path("/workspace/smoke_md.pdf")
if not _md_pdf.is_file() or _md_pdf.stat().st_size == 0:
    raise RuntimeError("weasyprint markdown→pdf produced no output")

print(f"font={cjk_font}")
print(f"node={_node.stdout.strip()}")
print("OK")
