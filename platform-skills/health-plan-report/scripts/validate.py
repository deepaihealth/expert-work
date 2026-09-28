#!/usr/bin/env python3
"""Validate a health-plan content JSON. Exit 0 with {"ok": true}, or exit 1 with the error list."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "shared"))

from _cli import emit_json, fail
from hpr.content import load_content, validate_content


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("content", help="内容 JSON 文件")
    args = ap.parse_args(argv)
    try:
        content = load_content(Path(args.content))
    except ValueError as exc:
        fail(str(exc))
    errs = validate_content(content)
    if errs:
        emit_json({"ok": False, "errors": [str(e) for e in errs]})
        return 1
    emit_json({"ok": True, "sections": len(content["sections"])})
    return 0


if __name__ == "__main__":
    sys.exit(main())
