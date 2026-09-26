"""preview.py must exit 1 with a Chinese message when pdftoppm fails, not a raw traceback.

Fix round 1: preview.py's pdftoppm subprocess.run(check=True) call was
unguarded, so a failure (non-zero exit, missing binary, timeout) escaped
main() as a raw Python traceback instead of the platform's Chinese
user-facing `_cli.fail()` convention. This is a real (non-mocked)
reproduction of the FileNotFoundError branch: PATH for the preview.py child
process is restricted to a directory that has `python` but not `pdftoppm`
(nor `soffice`), so the pdftoppm call genuinely raises FileNotFoundError.
Input is a real, valid one-page PDF built directly with pypdf (not through
soffice) — `.pdf` input skips preview.py's `_office.to_pdf()` branch
entirely, so soffice's own absence from the restricted PATH is irrelevant.

Layer 1 (host pytest, no Docker) cannot exercise this: `pypdf` — and every
other library preview.py imports — is only installed inside the sandbox
image, not in the host `uv` project, so even loading preview.py as a module
on the host fails at the `from pypdf import PdfReader` import before any
test code runs. This case therefore lives at layer 2.
"""

import os
import subprocess
from pathlib import Path

from _harness import check, script
from pypdf import PdfWriter

os.chdir("/workspace")
writer = PdfWriter()
writer.add_blank_page(width=72, height=72)
with Path("blank.pdf").open("wb") as f:
    writer.write(f)

restricted_env = dict(os.environ)
restricted_env["PATH"] = "/usr/local/bin"  # has `python`; drops /usr/bin (pdftoppm, soffice)

proc = subprocess.run(  # noqa: S603
    ["python", script("docx", "preview.py"), "blank.pdf", "--out-dir", "out"],  # noqa: S607
    capture_output=True,
    text=True,
    timeout=60,
    env=restricted_env,
    check=False,
)
check(
    proc.returncode == 1,
    f"expected exit 1, got {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
)
check("Traceback" not in proc.stderr, f"raw traceback leaked to stderr:\n{proc.stderr}")
check(
    any("一" <= ch <= "鿿" for ch in proc.stderr),
    f"expected a Chinese user-facing error message on stderr, got:\n{proc.stderr}",
)
print("PASS case_shared_preview_pdftoppm_error")
