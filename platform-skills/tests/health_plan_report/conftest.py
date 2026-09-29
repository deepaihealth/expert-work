import copy
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[2] / "health-plan-report"
_SCRIPTS = _SKILL_DIR / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture
def sample() -> dict:
    path = _SKILL_DIR / "sample" / "sample-plan.json"
    return copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))


def _huge_png(width: int, height: int) -> bytes:
    """A few dozen bytes: a PNG header declaring huge dimensions (nothing is allocated)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.fixture
def huge_png():
    return _huge_png
