"""Operate on an existing PDF: info / merge / split / rotate / watermark / stamp.

All sub-commands write to a new file (never the input) and reject an encrypted input
except ``info``, which reports ``encrypted`` truthfully instead of failing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from html import escape
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli import emit_json, fail, resolve_io
from pypdf import PdfReader, PdfWriter
from pypdf.errors import FileNotDecryptedError, PdfReadError

# pypdf logs recoverable parse issues (e.g. "EOF marker not found") as WARNINGs on child
# loggers under "pypdf.*"; with no handler configured, Python's logging "handler of last
# resort" prints those straight to stderr in English, ahead of our own Chinese error. Raise
# the threshold so stderr carries only the message we emit via fail().
logging.getLogger("pypdf").setLevel(logging.ERROR)

_ENCRYPTED_MSG = "PDF 已加密，本技能不处理加密文件"  # noqa: RUF001

_WATERMARK_HTML = """<html><head><style>
@page {{ size: {w}pt {h}pt; margin: 0 }}
body {{ margin:0; width:{w}pt; height:{h}pt; display:flex;
        align-items:center; justify-content:center }}
div {{ font-family:"{font}"; font-size:{size:.0f}pt; color: rgba(128,128,128,{opacity});
       transform: rotate(-{angle}deg); white-space: nowrap }}
</style></head><body><div>{text}</div></body></html>"""


def _parse_pages(spec: str, total: int) -> list[int]:
    """Parse a page-range spec ("1-3,5" / "all") into sorted 1-based page numbers <= total.

    Kept semantically identical to preview.py's copy of this function (case_pdf_ops.py
    asserts both agree on the same inputs) — mirror any edit there too.
    """
    if spec == "all":
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


def _resolve_pages(spec: str, total: int) -> list[int]:
    try:
        pages = _parse_pages(spec, total)
    except ValueError:
        fail(f"--pages 格式不对：{spec}（例：1-3、2,4、all）", 2)  # noqa: RUF001
    if not pages:
        fail(f"--pages 没有匹配到任何页码：{spec}（文档共 {total} 页）", 2)  # noqa: RUF001
    return pages


def _open_reader(path: Path) -> PdfReader:
    try:
        return PdfReader(str(path))
    except PdfReadError as exc:
        fail(f"{path.name} 不是合法的 PDF 或已损坏：{exc}")  # noqa: RUF001


def _reject_encrypted(reader: PdfReader, path: Path) -> None:
    if reader.is_encrypted:
        fail(f"{_ENCRYPTED_MSG}：{path.name}")  # noqa: RUF001


def cmd_info(args: argparse.Namespace) -> None:
    src, _ = resolve_io(args.input, None)
    reader = _open_reader(src)
    encrypted = reader.is_encrypted
    try:
        pages = reader.pages
        sizes = [[float(p.mediabox.width), float(p.mediabox.height)] for p in pages]
        has_text = any((p.extract_text() or "").strip() for p in pages)
        count = len(pages)
    except FileNotDecryptedError:
        # Password-protected (non-empty user password): pypdf can confirm encryption
        # but cannot read page content or even the page count without the password.
        count, sizes, has_text = 0, [], False
    emit_json({"pages": count, "sizes_pt": sizes, "encrypted": encrypted, "has_text": has_text})


def cmd_merge(args: argparse.Namespace) -> None:
    _, dst = resolve_io(args.inputs[0], args.output)  # nargs="+" guarantees >= 1 input
    writer = PdfWriter()
    for inp in args.inputs:
        src, _ = resolve_io(inp, args.output)
        reader = _open_reader(src)
        _reject_encrypted(reader, src)
        writer.append(reader)
    writer.write(str(dst))
    emit_json({"output": str(dst), "pages": len(writer.pages)})


def cmd_split(args: argparse.Namespace) -> None:
    src, _ = resolve_io(args.input, None)
    reader = _open_reader(src)
    _reject_encrypted(reader, src)
    total = len(reader.pages)
    out_dir = Path(args.out_dir)
    out_dir = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    if args.pages is None:
        width = max(3, len(str(total)))  # zero-pad wide enough that >999 pages still sort right
        for i, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            dst = out_dir / f"{src.stem}-p{i:0{width}d}.pdf"
            if dst.resolve() == src.resolve():
                fail("输出不能覆盖原文件：请换一个输出文件名")  # noqa: RUF001
            writer.write(str(dst))
            outputs.append(str(dst))
    else:
        pages = _resolve_pages(args.pages, total)
        writer = PdfWriter()
        for n in pages:
            writer.add_page(reader.pages[n - 1])
        dst = out_dir / f"{src.stem}-pages.pdf"
        if dst.resolve() == src.resolve():
            fail("输出不能覆盖原文件：请换一个输出文件名")  # noqa: RUF001
        writer.write(str(dst))
        outputs.append(str(dst))
    emit_json({"outputs": outputs})


def cmd_rotate(args: argparse.Namespace) -> None:
    src, dst = resolve_io(args.input, args.output)
    reader = _open_reader(src)
    _reject_encrypted(reader, src)
    pages = _resolve_pages(args.pages, len(reader.pages))
    writer = PdfWriter()
    writer.append(reader)
    for n in pages:
        writer.pages[n - 1].rotate(args.degrees)
    writer.write(str(dst))
    emit_json({"output": str(dst), "rotated": pages})


def cmd_watermark(args: argparse.Namespace) -> None:
    src, dst = resolve_io(args.input, args.output)
    reader = _open_reader(src)
    _reject_encrypted(reader, src)
    writer = PdfWriter()
    writer.append(reader)
    from weasyprint import HTML  # imported lazily: only watermark/create need it

    cache: dict[tuple[float, float], object] = {}
    for page in writer.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        key = (round(w, 2), round(h, 2))
        stamp_page = cache.get(key)
        if stamp_page is None:
            html = _WATERMARK_HTML.format(
                w=w,
                h=h,
                font=args.font,
                size=min(w, h) / 10,
                opacity=args.opacity,
                angle=args.angle,
                text=escape(args.text),
            )
            pdf_bytes = HTML(string=html).write_pdf()
            stamp_page = PdfReader(BytesIO(pdf_bytes)).pages[0]
            cache[key] = stamp_page
        page.merge_page(stamp_page)
    writer.write(str(dst))
    emit_json({"output": str(dst)})


def cmd_stamp(args: argparse.Namespace) -> None:
    src, dst = resolve_io(args.input, args.output)
    reader = _open_reader(src)
    _reject_encrypted(reader, src)
    stamp_src, _ = resolve_io(args.stamp, args.output)  # also rejects stamp-src == output
    stamp_reader = _open_reader(stamp_src)
    _reject_encrypted(stamp_reader, stamp_src)
    if not stamp_reader.pages:
        fail(f"{stamp_src.name} 没有页面")
    stamp_page = stamp_reader.pages[0]
    pages = _resolve_pages(args.pages, len(reader.pages))
    writer = PdfWriter()
    writer.append(reader)
    for n in pages:
        writer.pages[n - 1].merge_page(stamp_page)
    writer.write(str(dst))
    emit_json({"output": str(dst), "stamped": pages})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="页数 / 页面尺寸 / 是否加密 / 有无文字层")
    p.add_argument("input")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("merge", help="合并多个 PDF 成一个新文件")
    p.add_argument("output")
    p.add_argument("inputs", nargs="+")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("split", help="拆分: 不给 --pages 则每页一个文件")
    p.add_argument("input")
    p.add_argument("out_dir")
    p.add_argument("--pages")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("rotate", help="旋转指定页(默认全部)")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--degrees", type=int, required=True, choices=(90, 180, 270))
    p.add_argument("--pages", default="all")
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("watermark", help="每页叠加文字水印(weasyprint 生成, 嵌入 CJK 字体)")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--text", required=True)
    p.add_argument("--opacity", type=float, default=0.15)
    p.add_argument("--angle", type=float, default=45)
    p.add_argument("--font", default="Noto Sans CJK SC")
    p.set_defaults(func=cmd_watermark)

    p = sub.add_parser("stamp", help="把一份 PDF 的第 1 页叠加到指定页(默认全部)")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--stamp", required=True)
    p.add_argument("--pages", default="all")
    p.set_defaults(func=cmd_stamp)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
