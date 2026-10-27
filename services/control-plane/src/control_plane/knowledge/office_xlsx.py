"""Excel (``.xlsx``) to Markdown as its reader sees it (B-206 K-18).

MarkItDown reads a workbook with pandas, which loses what the sheet shows:
a category merged down several rows stays on the first row and the others
read ``NaN``; an empty cell reads ``NaN``; a percentage reads as its stored
fraction (61.12% → 0.6112 — a model copied that once, WeKnora ``a9290dcb6``);
a date reads ``… 00:00:00``; a ``|`` inside a cell splits the column; a title
row becomes the header and the columns under it ``Unnamed: 1`` …. On the dev
corpus 1,176 of 3,990 spreadsheet chunks carried ``NaN`` (9,209 cells), and
a spreadsheet was a source of 180 of 303 main-set cases.

Here, sheet by sheet (``## <sheet name>``, then one Markdown table):

* **merged cells** —— a range's value is copied down every row it spans, in
  its first column; across the columns it stays in its first cell (a
  heading spread over columns says nothing more for being repeated, a
  category over rows is what each row belongs to);
* **empty** —— an empty cell is empty; a row, or a trailing column, with
  nothing in it is dropped;
* **values as displayed** —— a percentage format shows ``61.12%``; a fixed
  decimal format (``0.00``) shows its decimals; a whole float shows no
  ``.0``; other floats show up to 15 significant digits; a date at midnight
  shows the date only; booleans read ``TRUE`` / ``FALSE``. Other formats
  (thousands separators, currency) show the plain number;
* **cell text** —— ``|`` escaped, line breaks folded into spaces;
* **title row** —— a first row holding a single value over a table at least
  two columns wide, with rows under it, is the sheet's title
  (``### <title>``): it then reaches every chunk of the sheet through the
  heading path instead of being the header.

Formulas read as their cached values (what pandas read too). A file openpyxl
cannot read is left to MarkItDown (:func:`~control_plane.knowledge.parsing.parse_document`).
"""

from __future__ import annotations

import datetime
import io
import re
from collections.abc import Sequence

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

#: ``0.00`` / ``0.0%`` …: the decimals a fixed format shows.
_FIXED_DECIMALS = re.compile(r"^0(?:\.(0+))?(%)?")
_LINE_BREAK = re.compile(r"\s*[\r\n]+\s*")
#: Significant digits of a float with no fixed format (drops binary noise:
#: 0.1 + 0.2 shows 0.3).
_FLOAT_DIGITS = 15


def xlsx_markdown(raw: bytes) -> str:
    """The workbook's Markdown, a sheet after another (empty sheets left out).

    :raises Exception: whatever openpyxl raises for a file it cannot read."""
    book = load_workbook(io.BytesIO(raw), data_only=True)
    sheets = [_sheet_markdown(sheet) for sheet in book.worksheets]
    return "\n\n".join(sheet for sheet in sheets if sheet)


def _sheet_markdown(sheet: Worksheet) -> str:
    rows = _trimmed(_grid(sheet))
    if not rows:
        return ""
    lines = [f"## {_cell_text(sheet.title)}"]
    first = rows[0]
    if len(rows) > 1 and len(first) > 1 and sum(1 for cell in first if cell) == 1:
        lines.append(f"### {next(cell for cell in first if cell)}")
        rows = rows[1:]
    header, *body = rows
    lines.append(_row(header))
    lines.append(_row(["---"] * len(header)))
    lines.extend(_row(row) for row in body)
    return "\n".join(lines)


def _grid(sheet: Worksheet) -> list[list[str]]:
    """Every cell of the used range as displayed, merged ranges copied down."""
    top, left = sheet.min_row, sheet.min_column
    grid = [
        [_display(cell.value, cell.number_format) for cell in row]
        for row in sheet.iter_rows(min_row=top, min_col=left)
    ]
    for merged in sheet.merged_cells.ranges:
        row, col = merged.min_row - top, merged.min_col - left
        if row < 0 or col < 0 or row >= len(grid):
            continue
        value = grid[row][col]
        for below in range(row + 1, min(merged.max_row - top + 1, len(grid))):
            grid[below][col] = value
    return grid


def _trimmed(grid: Sequence[Sequence[str]]) -> list[list[str]]:
    """``grid`` without its empty rows and trailing empty columns."""
    rows = [list(row) for row in grid if any(row)]
    width = max(
        (max((i + 1 for i, cell in enumerate(row) if cell), default=0) for row in rows), default=0
    )
    return [row[:width] + [""] * (width - len(row)) for row in rows]


def _display(value: object, number_format: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        return _datetime_text(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M:%S" if value.second else "%H:%M")
    if isinstance(value, int | float):
        return _number_text(value, number_format)
    return _cell_text(str(value))


def _datetime_text(value: datetime.datetime) -> str:
    if (value.hour, value.minute, value.second) == (0, 0, 0):
        return value.date().isoformat()
    return value.strftime("%Y-%m-%d %H:%M:%S" if value.second else "%Y-%m-%d %H:%M")


def _number_text(value: float, number_format: str) -> str:
    fixed = _FIXED_DECIMALS.match(number_format)
    if fixed is not None and fixed.group(2):
        return f"{value * 100:.{len(fixed.group(1) or '')}f}%"
    if fixed is not None and fixed.group(1) and isinstance(value, float):
        return f"{value:.{len(fixed.group(1))}f}"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.{_FLOAT_DIGITS}g}"
    return str(value)


def _cell_text(text: str) -> str:
    return _LINE_BREAK.sub(" ", text.strip()).replace("|", "\\|")


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"
