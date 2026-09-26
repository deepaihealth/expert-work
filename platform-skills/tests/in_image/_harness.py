"""Helpers for cases that run INSIDE the sandbox image (stdlib + preinstalled libs only)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def script(skill: str, name: str) -> str:
    return str(Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / skill / "scripts" / name)


def run(cmd: list[str], *, expect: int = 0, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != expect:
        print(
            f"FAIL: {cmd} -> {proc.returncode} (expected {expect})\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        sys.exit(1)
    return proc


def check(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def png_is_not_blank(path: Path) -> bool:
    from PIL import Image, ImageStat

    with Image.open(path) as im:
        return ImageStat.Stat(im.convert("L")).var[0] > 20
