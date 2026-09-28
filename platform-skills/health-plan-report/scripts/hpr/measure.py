"""Text measurement and line wrapping in points (Pillow + the sandbox CJK font, or an
approximation when the font file is absent — local tests and CI)."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_ENV = "HPR_FONT_REGULAR"
TTC_INDEX_SC = 2
SAFETY = 1.15
LINE = 1.35
BOLD_FACTOR = 1.04
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,:%/+\-~–_]*|\s+|.", re.S)  # noqa: RUF001
_CLOSING = frozenset("，。；：！？、）》】」』,.;:!?%)]}")  # noqa: RUF001


@dataclass(frozen=True)
class Line:
    text: str
    ends_para: bool


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


@lru_cache(maxsize=4)
def _font(path: str):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    return ImageFont.truetype(path, size=100, index=TTC_INDEX_SC)


class Measurer:
    def __init__(self, font_path: str | None = None) -> None:
        path = font_path or os.environ.get(FONT_ENV) or DEFAULT_FONT
        self._path = path if Path(path).is_file() else None

    @property
    def real(self) -> bool:
        return self._path is not None

    def width(self, text: str, size: float, bold: bool = False) -> float:
        if self._path:
            w = _font(self._path).getlength(text) * size / 100
        else:
            w = sum(size if _wide(ch) else size * 0.55 for ch in text)
        return w * (BOLD_FACTOR if bold else 1.0)

    def wrap(self, text: str, max_width: float, size: float, bold: bool = False) -> list[Line]:
        limit = max_width / SAFETY
        out: list[Line] = []
        for para in text.split("\n"):
            lines: list[str] = []
            cur = ""
            for tok in _TOKEN.findall(para):
                if self.width(cur + tok, size, bold) <= limit:
                    cur += tok
                    continue
                if cur and tok in _CLOSING:
                    cur += tok
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                if self.width(tok, size, bold) <= limit:
                    cur = tok
                    continue
                for ch in tok:
                    if cur and self.width(cur + ch, size, bold) > limit:
                        lines.append(cur)
                        cur = ""
                    cur += ch
            lines.append(cur)
            out.extend(Line(ln, ends_para=(i == len(lines) - 1)) for i, ln in enumerate(lines))
        return out

    def lines(self, text: str, max_width: float, size: float, bold: bool = False) -> int:
        return len(self.wrap(text, max_width, size, bold))

    def split_text(self, text: str, max_width: float, size: float, n_lines: int) -> tuple[str, str]:
        wrapped = self.wrap(text, max_width, size)
        head, tail = wrapped[:n_lines], wrapped[n_lines:]

        def join(lines: list[Line]) -> str:
            buf = ""
            for i, ln in enumerate(lines):
                buf += ln.text
                if ln.ends_para and i != len(lines) - 1:
                    buf += "\n"
            return buf

        return join(head), join(tail)
