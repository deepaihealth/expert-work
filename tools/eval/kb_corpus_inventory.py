"""只读清点评测语料(B-175 L1-T0):按层出 ``manifest.csv`` + ``summary.txt``。

分层:A 有文字层可直接入库 / B 扫描件或图片(要 OCR)/ D 物料目录下的图片 /
D-A、D-B 物料目录下的文档 / Q 聊天记录(问题来源,不入库)/ X、X-zip、X-other 排除。
只记路径、类型、大小、页数、扫描页数、文字字符数、重复组、版本标记 —— **不记任何内容**。
原文档在仓库外,CI 不碰。代码里的默认值不带任何客户目录名:哪个顶层目录是聊天记录 / 物料、
要跳过哪些目录和文件,写在一份 layout YAML 里(例:``datasets/kb/huada_v1.layout.yaml``),
命令行参数在它之上**叠加**。

用法(仓库根目录)::

    uv run --no-sync python -I tools/eval/kb_corpus_inventory.py <资料根目录> <输出目录> \\
        --layout tools/eval/datasets/kb/huada_v1.layout.yaml
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf
import yaml

#: 代码默认值只有工具自己的垃圾目录;客户语料的目录名一律进 layout 文件。
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (".workbuddy",)
DEFAULT_EXCLUDE_NAMES: tuple[str, ...] = ()
DEFAULT_CHAT_DIR: str | None = None
DEFAULT_MATERIAL_DIRS: tuple[str, ...] = ()
LAYOUT_KEYS = frozenset({"exclude_dirs", "exclude_names", "chat_dir", "material_dirs"})
EXCLUDE_EXT = frozenset({".ini", ".drivedownload", ".url", ".psd", ".eps"})
EXCLUDE_BASENAMES = frozenset({".DS_Store"})  # 没有扩展名,按文件名整个匹配
TEXT_EXT = frozenset({".docx", ".doc", ".xlsx", ".pptx", ".md", ".csv", ".txt"})
IMG_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif"})
#: 一页去掉首尾空白后不足这么多字符,算扫描页。
SCANNED_PAGE_MAX_CHARS = 20
#: 小于这个字节数的文件不算重复(图标、空壳文件会误并)。
HASH_MIN_BYTES = 1024
#: 不参与去重的层:X 是明确排除,Q 是聊天记录(不入库也不比重复)。
UNHASHED_TIERS = frozenset({"X", "Q"})
VERSION_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[Vv]\d"), "V数字"),
    (re.compile(r"最终|终版|定稿"), "终版"),
    (re.compile(r"新版|旧版|更新|修改|修订"), "修订"),
    (re.compile(r"副本|\(\d\)|（\d）|拷贝"), "副本"),  # noqa: RUF001 — 全角括号是要匹配的目标
)
MANIFEST_FIELDS: tuple[str, ...] = (
    "path",
    "top_dir",
    "ext",
    "size",
    "tier",
    "pages",
    "scanned_pages",
    "text_chars",
    "dup_group",
    "version_marker",
    "error",
)


class LayoutError(ValueError):
    """layout YAML 不合格式。"""


@dataclass(frozen=True)
class InventoryConfig:
    exclude_dirs: frozenset[str] = frozenset(DEFAULT_EXCLUDE_DIRS)
    exclude_names: frozenset[str] = frozenset(DEFAULT_EXCLUDE_NAMES)
    chat_dir: str | None = DEFAULT_CHAT_DIR
    material_dirs: frozenset[str] = frozenset(DEFAULT_MATERIAL_DIRS)

    def extend(
        self,
        *,
        exclude_dirs: Sequence[str] = (),
        exclude_names: Sequence[str] = (),
        chat_dir: str | None = None,
        material_dirs: Sequence[str] = (),
    ) -> InventoryConfig:
        """命令行参数叠加:集合取并,``chat_dir`` 给了就覆盖。"""
        return InventoryConfig(
            exclude_dirs=self.exclude_dirs | frozenset(exclude_dirs),
            exclude_names=self.exclude_names | frozenset(exclude_names),
            chat_dir=chat_dir if chat_dir is not None else self.chat_dir,
            material_dirs=self.material_dirs | frozenset(material_dirs),
        )


def load_layout(path: Path) -> InventoryConfig:
    """读 layout YAML:``{exclude_dirs: [...], exclude_names: [...], chat_dir: str|null,
    material_dirs: [...]}``,键都可省;省掉的用代码默认值。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise LayoutError(f"{path}: top level must be a mapping")
    unknown = set(raw) - LAYOUT_KEYS
    if unknown:
        raise LayoutError(f"{path}: unknown keys {sorted(unknown)}; allowed {sorted(LAYOUT_KEYS)}")
    chat_dir = raw.get("chat_dir")
    if chat_dir is not None and not isinstance(chat_dir, str):
        raise LayoutError(f"{path}: 'chat_dir' must be a string or null")
    return InventoryConfig().extend(
        exclude_dirs=_str_list(raw, "exclude_dirs", path=path),
        exclude_names=_str_list(raw, "exclude_names", path=path),
        chat_dir=chat_dir,
        material_dirs=_str_list(raw, "material_dirs", path=path),
    )


def _str_list(raw: Mapping[str, Any], key: str, *, path: Path) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise LayoutError(f"{path}: '{key}' must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class PdfStats:
    pages: int = 0
    scanned_pages: int = 0
    text_chars: int = 0
    error: str = ""


@dataclass(frozen=True)
class FileRecord:
    path: str
    top_dir: str
    ext: str
    size: int
    tier: str
    pages: int
    scanned_pages: int
    text_chars: int
    dup_group: str
    version_marker: str
    error: str


def pdf_stats(path: Path) -> PdfStats:
    """页数 / 扫描页数 / 文字字符数;打不开的 PDF 记异常类名,不抛。"""
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # 坏文件只记异常类名,不中断清点
        return PdfStats(error=type(exc).__name__)
    try:
        texts = [str(page.get_text()) for page in doc]
    finally:
        doc.close()
    scanned = sum(1 for t in texts if len(t.strip()) < SCANNED_PAGE_MAX_CHARS)
    return PdfStats(pages=len(texts), scanned_pages=scanned, text_chars=sum(map(len, texts)))


def classify(path: Path, *, top: str, cfg: InventoryConfig) -> tuple[str, PdfStats]:
    """返回 (层, PDF 统计)。非 PDF 的统计全 0。"""
    name = path.name
    ext = path.suffix.lower()
    stats = PdfStats()
    if ext in EXCLUDE_EXT or name in cfg.exclude_names or name in EXCLUDE_BASENAMES:
        tier = "X"
    elif path.stat().st_size == 0:
        tier = "X"  # a 0-byte file has nothing to parse; keep it out of the corpus tiers
        stats = PdfStats(error="EmptyFile")
    elif cfg.chat_dir is not None and top == cfg.chat_dir:
        tier = "Q"
    elif ext == ".zip":
        tier = "X-zip"
    elif ext == ".pdf":
        stats = pdf_stats(path)
        tier = "B" if stats.pages and stats.scanned_pages * 2 >= stats.pages else "A"
    elif ext in TEXT_EXT:
        tier = "A"
    elif ext in IMG_EXT:
        tier = "D" if top in cfg.material_dirs else "B"
    else:
        tier = "X-other"
    if top in cfg.material_dirs and tier in ("A", "B"):
        tier = f"D-{tier}"
    return tier, stats


def version_marker(name: str) -> str:
    return ";".join(label for pattern, label in VERSION_MARKERS if pattern.search(name))


def iter_files(root: Path, cfg: InventoryConfig) -> Iterator[tuple[Path, str]]:
    """按名字排序遍历,产出 (文件路径, 顶层目录名);顶层目录名对根目录直属文件是 ``.``。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in cfg.exclude_dirs)
        rel_dir = Path(dirpath).relative_to(root)
        top = rel_dir.parts[0] if rel_dir.parts else "."
        for name in sorted(filenames):
            yield Path(dirpath) / name, top


def _content_hash(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)  # 只为找重复,不是安全用途
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _record(path: Path, *, root: Path, top: str, cfg: InventoryConfig) -> FileRecord:
    tier, stats = classify(path, top=top, cfg=cfg)
    return FileRecord(
        path=str(path.relative_to(root)),
        top_dir=top,
        ext=path.suffix.lower().lstrip("."),
        size=path.stat().st_size,
        tier=tier,
        pages=stats.pages,
        scanned_pages=stats.scanned_pages,
        text_chars=stats.text_chars,
        dup_group="",
        version_marker=version_marker(path.name),
        error=stats.error,
    )


def assign_dup_groups(
    records: Sequence[FileRecord], hashes: Sequence[str]
) -> tuple[FileRecord, ...]:
    """同 md5 且出现 ≥ 2 次的文件共一个 ``g<n>``;``hashes[i]`` 为空表示第 i 条不参与。"""
    counts = Counter(h for h in hashes if h)
    group_ids: dict[str, str] = {}
    for h in hashes:
        if h and counts[h] > 1 and h not in group_ids:
            group_ids[h] = f"g{len(group_ids) + 1}"
    return tuple(
        replace(rec, dup_group=group_ids.get(h, "")) if h else rec
        for rec, h in zip(records, hashes, strict=True)
    )


def scan(root: Path, cfg: InventoryConfig) -> tuple[FileRecord, ...]:
    records: list[FileRecord] = []
    hashes: list[str] = []
    for path, top in iter_files(root, cfg):
        rec = _record(path, root=root, top=top, cfg=cfg)
        records.append(rec)
        hashable = rec.tier not in UNHASHED_TIERS and rec.size > HASH_MIN_BYTES
        hashes.append(_content_hash(path) if hashable else "")
    return assign_dup_groups(records, hashes)


def write_manifest(records: Sequence[FileRecord], out: Path) -> None:
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerows(asdict(r) for r in records)


def summarize(records: Sequence[FileRecord], *, root: Path) -> str:
    files = Counter(r.tier for r in records)
    size = Counter[str]()
    pages = Counter[str]()
    scanned = Counter[str]()
    for r in records:
        size[r.tier] += r.size
        pages[r.tier] += r.pages
        scanned[r.tier] += r.scanned_pages
    dup_members = [r.dup_group for r in records if r.dup_group]
    dup_groups = len(set(dup_members))
    lines = [
        f"root={root}",
        f"files={len(records)}  dup_groups={dup_groups}  dup_extra={len(dup_members) - dup_groups}",
        "",
        f"{'tier':8} {'files':>6} {'size':>8} {'pdf_pages':>9} {'scanned':>8}",
    ]
    lines.extend(
        f"{t:8} {files[t]:6} {size[t] / 1e6:7.0f}M {pages[t]:9} {scanned[t]:8}"
        for t in sorted(files)
    )
    lines.append("")
    lines.append(
        f"A-tier text chars (pdf only): {sum(r.text_chars for r in records if r.tier == 'A')}"
    )
    lines.append(f"version-marked files: {sum(1 for r in records if r.version_marker)}")
    lines.append(f"pdf errors: {sum(1 for r in records if r.error)}")
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("root", type=Path, help="资料根目录(仓库外)")
    parser.add_argument("out", type=Path, help="输出目录,写 manifest.csv + summary.txt")
    parser.add_argument(
        "--layout",
        type=Path,
        metavar="YAML",
        help="语料目录布局文件(exclude_dirs / exclude_names / chat_dir / material_dirs)",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="跳过的目录名(可重复;叠加到 layout 上)",
    )
    parser.add_argument(
        "--exclude-name",
        action="append",
        default=[],
        metavar="FILENAME",
        help="按文件名整个排除(可重复;叠加到 layout 上)",
    )
    parser.add_argument("--chat-dir", help="聊天记录顶层目录名 → Q 层(覆盖 layout 里的)")
    parser.add_argument(
        "--material-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="物料顶层目录名 → D / D-A / D-B(可重复;叠加到 layout 上)",
    )
    return parser


def _config(args: argparse.Namespace) -> InventoryConfig:
    base = load_layout(args.layout) if args.layout is not None else InventoryConfig()
    return base.extend(
        exclude_dirs=args.exclude_dir,
        exclude_names=args.exclude_name,
        chat_dir=args.chat_dir,
        material_dirs=args.material_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root: Path = args.root
    out: Path = args.out
    if not root.is_dir():
        raise SystemExit(f"root is not a directory: {root}")
    try:
        cfg = _config(args)
    except LayoutError as exc:
        raise SystemExit(str(exc)) from exc
    records = scan(root, cfg)
    out.mkdir(parents=True, exist_ok=True)
    write_manifest(records, out / "manifest.csv")
    summary = summarize(records, root=root)
    (out / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
