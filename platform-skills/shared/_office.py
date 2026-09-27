"""The single place that runs LibreOffice (soffice) headless.

Each call gets its own user-profile dir (parallel runs would otherwise lock
each other out), runs in its own process group, and on timeout the whole
group is SIGKILLed so no orphan soffice keeps the sandbox busy.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

# Split into adjacent string literals (implicit concatenation) purely to keep every
# physical source line under the repo's ruff line-length limit — the resulting string
# value is byte-identical to a single triple-quoted literal.
_RECALC_XCU = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
    'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    '<item oor:path="/org.openoffice.Office.Calc/Formula/Load">'
    '<prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>\n'
    '<item oor:path="/org.openoffice.Office.Calc/Formula/Load">'
    '<prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>\n'
    "</oor:items>\n"
)

_FORMATS = {"pdf", "docx", "pptx", "xlsx"}


class OfficeError(RuntimeError):
    pass


class OfficeTimeout(TimeoutError):  # noqa: N818 — name mirrors stdlib TimeoutError (matches call sites)
    pass


def run_soffice(
    args: list[str], *, timeout: float = 120.0, recalc: bool = False
) -> subprocess.CompletedProcess:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe is None:
        raise OfficeError("沙箱里找不到 soffice（LibreOffice）")  # noqa: RUF001
    profile = Path(tempfile.mkdtemp(prefix="lo-profile-"))
    try:
        if recalc:
            user = profile / "user"
            user.mkdir(parents=True)
            (user / "registrymodifications.xcu").write_text(_RECALC_XCU, encoding="utf-8")
        cmd = [
            exe,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nologo",
            *args,
        ]
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise OfficeTimeout(f"转换超时 {timeout:.0f}s：文件可能过大或损坏") from None  # noqa: RUF001
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def convert(
    src: Path, fmt: str, out_dir: Path, *, timeout: float = 120.0, recalc: bool = False
) -> Path:
    if fmt not in _FORMATS:
        raise OfficeError(f"不支持转换成 {fmt}")
    if src.suffix.lower().lstrip(".") == fmt:
        raise OfficeError(f"输入已经是 .{fmt}，不需要转换")  # noqa: RUF001
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = run_soffice(
        ["--convert-to", fmt, "--outdir", str(out_dir), str(src)], timeout=timeout, recalc=recalc
    )
    target = out_dir / f"{src.stem}.{fmt}"
    if proc.returncode != 0 or not target.is_file():
        raise OfficeError(f"转换失败（退出码 {proc.returncode}）：{proc.stderr.strip()[-500:]}")  # noqa: RUF001
    return target


def to_pdf(src: Path, out_dir: Path, *, timeout: float = 120.0) -> Path:
    return convert(src, "pdf", out_dir, timeout=timeout)
