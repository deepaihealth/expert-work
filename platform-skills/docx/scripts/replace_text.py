"""Replace text in a .docx while keeping formatting (body, tables incl. nested, headers/footers)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx
from _cli import emit_json, fail, resolve_io
from docx.table import Table
from docx.text.paragraph import Paragraph


def _iter_block(container) -> Iterator[Paragraph]:
    yield from container.paragraphs
    for table in container.tables:
        yield from _iter_table(table)


def _iter_table(table: Table) -> Iterator[Paragraph]:
    for row in table.rows:
        for cell in row.cells:
            yield from _iter_block(cell)


def iter_paragraphs(document) -> Iterator[Paragraph]:
    """Walk body paragraphs, all tables (incl. nested), and every section's
    header/footer (incl. first-page and even-page variants)."""
    yield from _iter_block(document)
    for section in document.sections:
        parts = (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        )
        for part in parts:
            if part is not None and not part.is_linked_to_previous:
                yield from _iter_block(part)


def replace_in_paragraph(paragraph: Paragraph, find: str, replace: str) -> int:
    """Replace every occurrence of ``find`` even when it spans several runs.

    The replaced text lands in the run where the match starts (keeping that
    run's formatting); the consumed parts of the following runs are removed.
    """
    if not find:
        return 0
    count = 0
    while True:
        runs = paragraph.runs
        texts = [r.text for r in runs]
        full = "".join(texts)
        idx = full.find(find)
        if idx < 0:
            return count
        starts = []
        pos = 0
        for t in texts:
            starts.append(pos)
            pos += len(t)
        end = idx + len(find)
        first = max(i for i, s in enumerate(starts) if s <= idx)
        for i, run in enumerate(runs):
            s, e = starts[i], starts[i] + len(texts[i])
            if e <= idx or s >= end:
                continue
            keep_before = texts[i][: max(0, idx - s)]
            keep_after = texts[i][max(0, end - s) :] if e > end else ""
            run.text = keep_before + (replace if i == first else "") + keep_after
        count += 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--rules", required=True, help='JSON 文件：[{"find": "...", "replace": "..."}]')  # noqa: RUF001
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    try:
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        pairs = [(str(r["find"]), str(r["replace"])) for r in rules]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail(f'--rules 读取失败（需要 [{{"find": ..., "replace": ...}}]）：{exc}', 2)  # noqa: RUF001
    document = docx.Document(str(src))
    counts = [0] * len(pairs)
    for paragraph in list(iter_paragraphs(document)):
        for i, (find, repl) in enumerate(pairs):
            counts[i] += replace_in_paragraph(paragraph, find, repl)
    document.save(str(dst))
    emit_json(
        {
            "rules": [
                {"find": f, "replace": r, "count": c}
                for (f, r), c in zip(pairs, counts, strict=True)
            ],
            "unmatched": [f for (f, _), c in zip(pairs, counts, strict=True) if c == 0],
        }
    )


if __name__ == "__main__":
    main()
