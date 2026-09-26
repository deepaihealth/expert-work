"""Fill {{placeholders}} in a .docx template, or list them."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx
from _cli import emit_json, fail, resolve_io
from replace_text import iter_paragraphs, replace_in_paragraph

_PH = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


def _placeholders(document) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for p in iter_paragraphs(document):
        for m in _PH.finditer(p.text):
            found.setdefault(m.group(1), set()).add(m.group(0))
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--data", help='JSON 文件：{"占位符名": "值"}')  # noqa: RUF001
    args = ap.parse_args()
    if args.list:
        src, _ = resolve_io(args.template, None)
        emit_json({"placeholders": sorted(_placeholders(docx.Document(str(src))))})
        return
    if not args.output or not args.data:
        fail("填充需要 输出文件 与 --data；只想看占位符用 --list", 2)  # noqa: RUF001
    src, dst = resolve_io(args.template, args.output)
    try:
        data = json.loads(Path(args.data).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("顶层必须是一个 JSON 对象")
    except (OSError, ValueError, TypeError) as exc:
        fail(f"--data 需要一个 JSON 对象：{exc}", 2)  # noqa: RUF001
    document = docx.Document(str(src))
    found = _placeholders(document)
    for key, spellings in found.items():
        if key not in data:
            continue
        for p in list(iter_paragraphs(document)):
            for spelling in spellings:
                replace_in_paragraph(p, spelling, str(data[key]))
    document.save(str(dst))
    emit_json(
        {
            "filled": sorted(k for k in found if k in data),
            "missing": sorted(k for k in found if k not in data),
            "unused": sorted(k for k in data if k not in found),
        }
    )


if __name__ == "__main__":
    main()
