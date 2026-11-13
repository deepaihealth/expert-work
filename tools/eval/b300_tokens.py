"""B-300 设计里平台文字的 token 数(设计 §4.4、§5.2:「token 数是用 ``o200k_base`` 对草稿文字估的,
脚本 PR 1 入库」)。不调模型。**注意联网**:``tiktoken`` 第一次用 ``o200k_base`` 时会去下载词表;
要完全离线,先把 ``TIKTOKEN_CACHE_DIR`` 指到已缓存词表的目录。拿不到词表就报错,不退回按字数估。

    python tools/eval/b300_tokens.py                       # 设计文档里的每个 ```text 块
    python tools/eval/b300_tokens.py --file a.txt --file b.txt

每块打印:所在的小节标题、字符数、token 数。只是量级:国内模型分词不同(§11 #10),围栏插入的
标记字符没算。PR 1 落代码后,用 ``--file`` 量代码里实际的常量文字(先导出成文件)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

DESIGN = (
    Path(__file__).resolve().parents[2]
    / "docs/superpowers/specs/2026-11-11-b300-memory-provenance-design.md"
)
_HEADING = re.compile(r"^(#{2,4})\s+(.*)$")


@dataclass(frozen=True)
class Block:
    heading: str
    text: str


@dataclass(frozen=True)
class Counted:
    label: str
    chars: int
    tokens: int


def text_blocks(markdown: str) -> list[Block]:
    """文档里每个 ```text 围栏块,带它前面最近的小节标题(块里的 ``## ...`` 行不算标题)。"""
    blocks: list[Block] = []
    heading = ""
    inside = False
    current: list[str] = []
    for line in markdown.splitlines():
        if inside:
            if line.strip() == "```":
                blocks.append(Block(heading, "\n".join(current)))
                inside, current = False, []
            else:
                current.append(line)
            continue
        if line.strip() == "```text":
            inside = True
            continue
        match = _HEADING.match(line)
        if match:
            heading = match.group(2).strip()
    return blocks


def o200k() -> Callable[[str], int]:
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    return lambda text: len(encoding.encode(text))


def count(blocks: Sequence[Block], counter: Callable[[str], int]) -> list[Counted]:
    seen: dict[str, int] = {}
    out = []
    for block in blocks:
        seen[block.heading] = seen.get(block.heading, 0) + 1
        label = f"{block.heading} #{seen[block.heading]}"
        out.append(Counted(label, len(block.text), counter(block.text)))
    return out


def main(argv: Sequence[str] | None = None, *, counter: Callable[[str], int] | None = None) -> int:
    parser = argparse.ArgumentParser(description="o200k_base token counts of B-300 platform text")
    parser.add_argument("--design", type=Path, default=DESIGN)
    parser.add_argument("--file", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    if args.file:
        blocks = [Block(str(p), p.read_text(encoding="utf-8")) for p in args.file]
    else:
        blocks = text_blocks(args.design.read_text(encoding="utf-8"))
    rows = count(blocks, counter or o200k())
    for row in rows:
        print(json.dumps(asdict(row), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
