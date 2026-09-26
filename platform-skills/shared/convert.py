"""Convert office files with LibreOffice: docx/pptx/xlsx -> pdf, doc -> docx, ppt -> pptx,
xls -> xlsx."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _office
from _cli import emit_json, fail, resolve_io


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--to", required=True, choices=["pdf", "docx", "pptx", "xlsx"])
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    out_dir = Path(args.out_dir)
    out_dir = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
    try:
        out = _office.convert(src, args.to, out_dir, timeout=args.timeout)
    except (_office.OfficeError, _office.OfficeTimeout) as exc:
        fail(str(exc))
    emit_json({"output": str(out)})


if __name__ == "__main__":
    main()
