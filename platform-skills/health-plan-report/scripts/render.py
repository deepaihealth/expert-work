#!/usr/bin/env python3
"""Render a health-plan content JSON to PPTX and/or PDF with hard QA gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "shared"))

from _cli import emit_json
from hpr.blocks import RenderError
from hpr.content import iter_blocks, load_content, required_texts, validate_content
from hpr.measure import Measurer
from hpr.pdf_html import render_pdf
from hpr.ppt_draw import render_pptx
from hpr.ppt_layout import LayoutError
from hpr.qa import qa_pdf, qa_pptx
from hpr.style import NOT_APPLIED, Layer, load_layer, resolve
from hpr.theme import build_theme

_BASENAME = re.compile(r"^[^/\\\x00]{1,120}$")


def _fail(errors: list[str]) -> int:
    emit_json({"ok": False, "errors": errors})
    return 1


def _missing_images(content: dict, base_dir: Path) -> list[str]:
    errs = []
    for si, _sid, bi, blk in iter_blocks(content):
        if blk["kind"] == "image":
            p = Path(blk["path"])
            p = p if p.is_absolute() else base_dir / p
            if not p.is_file():
                errs.append(f"sections[{si}].blocks[{bi - 1}]: 找不到图片文件：{blk['path']}")  # noqa: RUF001
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content", required=True)
    ap.add_argument(
        "--style",
        action="append",
        default=[],
        help="样式层文件，按优先级从高到低重复传入",  # noqa: RUF001
    )
    ap.add_argument("--format", choices=("pptx", "pdf", "both"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--basename", required=True)
    args = ap.parse_args(argv)
    if not _BASENAME.match(args.basename) or args.basename in (".", ".."):
        return _fail(["basename 只能是文件名，不能包含路径分隔符"])  # noqa: RUF001
    try:
        content = load_content(Path(args.content))
        layers = [load_layer(Path(p)) for p in args.style]
    except ValueError as exc:
        return _fail([str(exc)])
    errs = validate_content(content)
    if errs:
        return _fail([str(e) for e in errs])
    base_dir = Path(args.content).resolve().parent
    img_errs = _missing_images(content, base_dir)
    if img_errs:
        return _fail(img_errs)
    if args.format:
        layers.insert(0, Layer("命令行", {"output.formats": args.format}))
    res = resolve(layers, content)
    style = res.style
    formats = ["pptx", "pdf"] if style["output.formats"] == "both" else [style["output.formats"]]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {f: out_dir / f"{args.basename}.{f}" for f in formats}
    outputs = [*targets.values()]
    outputs += [out_dir / f"{args.basename}.qa-failed.{f}" for f in formats]
    outputs += [
        out_dir / f"{args.basename}.qa.json",
        out_dir / f"{args.basename}.params-report.json",
    ]
    existing = [str(p) for p in outputs if p.exists()]
    if existing:
        return _fail(
            [f"文件已存在，不覆盖：{p}（请换一个 basename，例如用新的生成时间）" for p in existing]  # noqa: RUF001
        )
    m = Measurer()
    required = required_texts(content)
    files: dict[str, str] = {}
    qa: dict[str, dict] = {}
    warnings: list[str] = []
    try:
        if "pptx" in targets:
            warnings += render_pptx(content, style, targets["pptx"], base_dir, m)
            qa["pptx"] = qa_pptx(targets["pptx"], required, build_theme(style, "pptx"), m)
            files["pptx"] = str(targets["pptx"])
        if "pdf" in targets:
            warnings += render_pdf(content, style, targets["pdf"], base_dir)
            qa["pdf"] = qa_pdf(targets["pdf"], content, required, build_theme(style, "pdf"))
            files["pdf"] = str(targets["pdf"])
    except (RenderError, LayoutError, RuntimeError) as exc:
        return _fail([str(exc)])
    warnings = list(dict.fromkeys(warnings))
    ok = all(q["status"] == "passed" for q in qa.values())
    if not ok:
        for fmt, q in qa.items():
            if q["status"] != "passed":
                bad = out_dir / f"{args.basename}.qa-failed.{fmt}"
                Path(files[fmt]).rename(bad)
                files[fmt] = str(bad)
    (out_dir / f"{args.basename}.qa.json").write_text(
        json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"{args.basename}.params-report.json").write_text(
        json.dumps(res.report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    emit_json(
        {
            "ok": ok,
            "files": files,
            "qa": qa,
            "not_applied": [e for e in res.report if e["status"] in NOT_APPLIED],
            "warnings": warnings,
            "errors": [],
        }
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
