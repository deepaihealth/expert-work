#!/usr/bin/env python3
"""Resolve style layers (highest first) into the effective style + a parameter report,
or merge validated keys into a personal-default style file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "shared"))

from _cli import emit_json, fail
from hpr.content import load_content
from hpr.style import Layer, load_layer, resolve


def _parse_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _merge(path: Path, sets: list[str], unsets: list[str]) -> int:
    new: dict[str, object] = {}
    for item in sets:
        if "=" not in item:
            fail(f"--set 需要 键=值 形式：{item}")  # noqa: RUF001
        key, raw = item.split("=", 1)
        new[key.strip()] = _parse_value(raw.strip())
    res = resolve([Layer("new", new)])
    bad = [e for e in res.report if e["status"] not in ("applied", "adjusted")]
    if bad:
        emit_json({"ok": False, "rejected": bad})
        return 1
    current: dict[str, object] = {}
    if path.is_file():
        current = load_layer(path).values
    merged = {**current, **new}
    for key in unsets:
        merged.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(sorted(merged.items())), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    emit_json({"ok": True, "file": str(path), "values": merged})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--style",
        action="append",
        default=[],
        help="样式层文件，按优先级从高到低重复传入",  # noqa: RUF001
    )
    ap.add_argument("--content", help="内容 JSON（用于校验按章节/积木定位的版式键）")  # noqa: RUF001
    ap.add_argument("--merge-into", help="个人默认样式文件路径")
    ap.add_argument("--set", action="append", default=[], help="键=值（配合 --merge-into）")  # noqa: RUF001
    ap.add_argument("--unset", action="append", default=[], help="要删除的键（配合 --merge-into）")  # noqa: RUF001
    args = ap.parse_args(argv)
    try:
        if args.merge_into:
            return _merge(Path(args.merge_into), args.set, args.unset)
        layers = [load_layer(Path(p)) for p in args.style]
        content = load_content(Path(args.content)) if args.content else None
    except ValueError as exc:
        fail(str(exc))
    res = resolve(layers, content)
    emit_json({"style": res.style, "report": res.report})
    return 0


if __name__ == "__main__":
    sys.exit(main())
