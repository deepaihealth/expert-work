"""Render pages of a docx/pptx/xlsx/pdf (and doc/ppt/xls) to PNG for visual checking."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _office
from _cli import emit_json, fail, resolve_io
from pypdf import PdfReader


def _parse_pages(spec: str, total: int) -> list[int]:
    if spec == "all":
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--out-dir")
    ap.add_argument("--pages", default="1-3")
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd() / f"{src.stem}_preview"
    out_dir = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="preview-"))
    try:
        if src.suffix.lower() == ".pdf":
            pdf = src
        else:
            try:
                pdf = _office.to_pdf(src, tmp, timeout=args.timeout)
            except (_office.OfficeError, _office.OfficeTimeout) as exc:
                fail(str(exc))
        total = len(PdfReader(str(pdf)).pages)
        try:
            pages = _parse_pages(args.pages, total)
        except ValueError:
            fail(f"--pages 格式不对：{args.pages}（例：1-3、2,4、all）", 2)  # noqa: RUF001
        images: list[str] = []
        for n in pages:
            prefix = out_dir / f"page-{n:03d}"
            subprocess.run(  # noqa: S603
                [  # noqa: S607 — resolved via PATH, matches _office.py's soffice lookup pattern
                    "pdftoppm",
                    "-png",
                    "-r",
                    str(args.dpi),
                    "-f",
                    str(n),
                    "-l",
                    str(n),
                    "-singlefile",
                    str(pdf),
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                timeout=args.timeout,
            )
            images.append(str(prefix.with_suffix(".png")))
        emit_json({"pages_total": total, "images": images})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
