"""Tiny CLI helpers shared by platform-skill scripts (stdlib only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn


def fail(msg: str, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(code)


def emit_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def resolve_io(inp: str, out: str | None) -> tuple[Path, Path | None]:
    src = Path(inp).expanduser()
    src = (Path.cwd() / src) if not src.is_absolute() else src
    if not src.is_file():
        fail(f"找不到输入文件：{inp}")  # noqa: RUF001
    if out is None:
        return src, None
    dst = Path(out).expanduser()
    dst = (Path.cwd() / dst) if not dst.is_absolute() else dst
    if dst.resolve() == src.resolve():
        fail("输出不能覆盖原文件：请换一个输出文件名")  # noqa: RUF001
    dst.parent.mkdir(parents=True, exist_ok=True)
    return src, dst
