"""Replace text in a .docx while keeping formatting (body, tables incl. nested, headers/footers).

Also reaches into hyperlink runs (``paragraph.runs`` alone silently skips text inside
``w:hyperlink``) and guards against a replacement that itself contains the search text
(search resumes right after the just-written replacement instead of re-scanning it, so
it always terminates).
"""

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
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run


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


def _iter_runs(paragraph: Paragraph) -> list[Run]:
    """Flatten a paragraph's runs in document order, descending into hyperlinks.

    ``paragraph.runs`` silently skips text inside ``w:hyperlink`` (confirmed on
    python-docx 1.2.0); ``iter_inner_content`` sees both plain runs and hyperlinks.
    ``Hyperlink.runs`` returns live proxies over the same ``w:r`` elements, so editing
    ``.text`` on them edits the hyperlink's visible text in place without touching the
    surrounding ``w:hyperlink`` (and its ``r:id`` / address).
    """
    units: list[Run] = []
    for item in paragraph.iter_inner_content():
        units.extend(item.runs if isinstance(item, Hyperlink) else (item,))
    return units


def replace_in_paragraph(paragraph: Paragraph, find: str, replace: str) -> int:
    """Replace every non-overlapping, left-to-right occurrence of ``find`` — including
    inside hyperlinks — even when it spans several runs.

    The replaced text lands in the run where the match starts (keeping that run's
    formatting); the consumed parts of the following runs are removed. The search cursor
    always resumes at ``idx + len(replace)`` (past the text just written), so a
    ``replace`` that itself contains ``find`` (e.g. "客户" -> "尊敬的客户") cannot loop
    forever or re-match its own output.
    """
    if not find:
        return 0
    count = 0
    cursor = 0
    while True:
        runs = _iter_runs(paragraph)
        texts = [r.text for r in runs]
        full = "".join(texts)
        idx = full.find(find, cursor)
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
        cursor = idx + len(replace)


def _load_rules(path: Path) -> list[tuple[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"--rules 读取失败：{exc}", 2)  # noqa: RUF001
    except ValueError as exc:
        fail(f"--rules 不是合法的 JSON：{exc}", 2)  # noqa: RUF001
    if not isinstance(raw, list) or not raw:
        fail('--rules 必须是非空列表，每项形如 {"find": "...", "replace": "..."}', 2)  # noqa: RUF001
    pairs: list[tuple[str, str]] = []
    for i, rule in enumerate(raw, start=1):
        find = rule.get("find") if isinstance(rule, dict) else None
        repl = rule.get("replace") if isinstance(rule, dict) else None
        if not isinstance(find, str) or not isinstance(repl, str):
            fail(f"--rules 第 {i} 条格式不对：find / replace 必须是字符串", 2)  # noqa: RUF001
        pairs.append((find, repl))
    empty_at = [i for i, (find, _) in enumerate(pairs, start=1) if find == ""]
    if empty_at:
        fail(f"--rules 第 {empty_at} 条的 find 不能是空字符串", 1)
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--rules", required=True, help='JSON 文件：[{"find": "...", "replace": "..."}]')  # noqa: RUF001
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    pairs = _load_rules(Path(args.rules))
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
