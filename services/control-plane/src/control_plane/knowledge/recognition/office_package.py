"""B-209 PR-2 —— 读 Word / 幻灯片的图片之前,先把能读多少字节框住(在解析进程池里跑)。

**Word**:图片字节只在 mammoth 的 ``convert_image``(:class:`DocxPictureReader`)里读 —— 那正是
转换器读字节的地方,所以上限就设在那里,不靠猜哪些条目是图片:

* 每个图片部件(按部件名)只读一次,每次最多读 :data:`MAX_IMAGE_BYTES` + 1 字节,超了就跳过;
* 整份文档实际读到的字节合计不超过 :data:`MAX_OFFICE_MEDIA_BYTES`(按读到的,重复引用不重复读);
* 正文里只留一个短标记,同一张图被引用多少次都不会再展开一份字节;
* 读不出来的(外链图片、指向不存在的部件)跳过,计作「不支持」。

**幻灯片**:python-pptx 打开时就把所有部件读进内存,读的那一刻挂不了钩子,所以在交给它之前按
压缩包声明的大小改写:用真正的 XML 解析器读每个 ``.rels``(任何引号、任何长度),每个部件里
``r:embed`` / ``r:link`` / ``r:id`` 引用到的目标(不管关系类型、TargetMode、扩展名大小写),除了
python-pptx 会当 XML 解析的部件(按 ``[Content_Types].xml`` 给它登记的类型,和 python-pptx 一样
解析:先按部件名的 Override、再按扩展名的 Default,都不分大小写 —— 幻灯片、版式、母版这些也是被
``r:id`` 引用的,它们按 XML 部件的上限算),都算图片,按「大小 x 引用次数」计:单个超过
:data:`MAX_IMAGE_BYTES`、或合计超过 :data:`MAX_OFFICE_MEDIA_BYTES`(从最大的开始)的,换成一个
1 像素的替身。引用次数按 python-pptx 的解析方式数(lxml,不展开实体、不联网、不加载 DTD);
python-pptx 要解析的部件读不出来,这份文档直接解析失败。

**两种都查**(读之前):中央目录尾记录里声明的条目数超过 :data:`MAX_PACKAGE_ENTRIES`、或声明的
中央目录大小超过 :data:`MAX_PACKAGE_ENTRIES` x :data:`DIRECTORY_BYTES_PER_ENTRY`(都在 ``zipfile``
建条目列表之前,按 ``zipfile`` 自己的读法)、两个条目同名(不分大小写)、解压后合计超过
:data:`MAX_OFFICE_PACKAGE_BYTES`、按名字或登记类型是 XML 的单个部件超过 :data:`MAX_XML_PART_BYTES`
—— 这份文档直接解析失败。压缩包条目声明的大小就是读出来的上限(``zipfile`` 读到声明的长度为止
并校验)。
"""

from __future__ import annotations

import hashlib
import io
import posixpath
import shutil
import zipfile
from collections import Counter
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote

import defusedxml.ElementTree as SafeElementTree  # type: ignore[import-untyped]
from lxml import etree  # type: ignore[import-untyped]  # python-pptx's parser
from PIL import Image

from control_plane.knowledge.office_docx import FIGURE_TOKEN
from control_plane.knowledge.parsing import DocumentParseError

#: A single embedded picture larger than this (bytes) is not read.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: The pictures of one Word / slide file read at most this much together
#: (the largest are left out first).
MAX_OFFICE_MEDIA_BYTES = 40 * 1024 * 1024
#: A Word / slide package that unpacks to more than this is not read at all
#: (uploads are capped at 50 MiB by default, ``knowledge_document_max_bytes``; a
#: real file of pictures and text unpacks to a few times its size at most —
#: past this is a zip bomb).
MAX_OFFICE_PACKAGE_BYTES = 256 * 1024 * 1024
#: One XML part (the document's text, a slide) may unpack to at most this.
MAX_XML_PART_BYTES = 64 * 1024 * 1024
#: Entries one package may hold (a 1,000-slide deck has a few thousand).
MAX_PACKAGE_ENTRIES = 20_000

#: Central-directory bytes one entry may take on average. A record is 46 bytes
#: plus the part name, extra fields and comment; Office names its parts in
#: under 100 bytes, writes under 40 bytes of extras and no comments — so a real
#: package stays under ~190 bytes an entry, and 256 leaves headroom. zipfile
#: lists one entry per 46+ bytes of the directory size the end record declares
#: (the declared entry count is never used to stop it), so this — not the
#: count — bounds the list it builds: at most 20,000 x 256 / 46 ≈ 111,000 entries.
DIRECTORY_BYTES_PER_ENTRY = 256

#: Read by its exact name, as python-pptx does.
_CONTENT_TYPES = "[Content_Types].xml"
_RELATIONSHIPS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REFERENCES = tuple(f"{_RELATIONSHIPS}{name}" for name in ("embed", "link", "id"))
#: Bytes per read while copying a package part.
_COPY_CHUNK = 1024 * 1024
#: zipfile's own reading of the end record (ZIP64 included), so the check sees
#: what zipfile will; private — a Python without it fails here, at import.
_END_RECORD = zipfile._EndRecData  # type: ignore[attr-defined]
_END_ENTRIES = zipfile._ECD_ENTRIES_TOTAL  # type: ignore[attr-defined]
_END_DIRECTORY_SIZE = zipfile._ECD_SIZE  # type: ignore[attr-defined]
#: How python-pptx parses a part (no entities, no network, no DTD).
_PPTX_PARSER = {"resolve_entities": False, "no_network": True, "load_dtd": False}


def _stand_in() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(out, format="PNG")
    return out.getvalue()


#: What an oversized slide picture is replaced by before python-pptx reads it.
STAND_IN = _stand_in()


def declared_directory(raw: bytes) -> tuple[int, int] | None:
    """The entry count and central-directory size (bytes) the end record
    declares, read as ``zipfile`` reads them (the ZIP64 record when there is
    one); ``None`` when there is no readable record (``zipfile`` reports that
    file)."""
    try:
        record = _END_RECORD(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    if record is None:
        return None
    return int(record[_END_ENTRIES]), int(record[_END_DIRECTORY_SIZE])


def declared_entry_count(raw: bytes) -> int | None:
    """The entry count the end record declares (see :func:`declared_directory`)."""
    declared = declared_directory(raw)
    return declared[0] if declared is not None else None


def bound_office_package(raw: bytes, ext: str) -> tuple[bytes, int]:
    """``raw`` checked (see module doc) — for a slide file also with every
    picture over the limits replaced by :data:`STAND_IN` — and how many
    pictures were replaced. A file that is not a readable zip is returned as
    it is (the converter reports it).

    :raises DocumentParseError: too many entries, the package unpacks past
        :data:`MAX_OFFICE_PACKAGE_BYTES`, or an XML part past
        :data:`MAX_XML_PART_BYTES`."""
    declared = declared_directory(raw)
    if declared is not None and (
        declared[0] > MAX_PACKAGE_ENTRIES
        or declared[1] > MAX_PACKAGE_ENTRIES * DIRECTORY_BYTES_PER_ENTRY
    ):
        msg = f"the file holds more than {MAX_PACKAGE_ENTRIES} parts"
        raise DocumentParseError(msg)
    try:
        package = zipfile.ZipFile(io.BytesIO(raw))
        entries = [entry for entry in package.infolist() if not entry.is_dir()]
    except (zipfile.BadZipFile, ValueError):
        return raw, 0
    if len(entries) > MAX_PACKAGE_ENTRIES:
        msg = f"the file holds more than {MAX_PACKAGE_ENTRIES} parts"
        raise DocumentParseError(msg)
    if len({entry.filename.lower() for entry in entries}) != len(entries):
        # Part names are case-insensitive; two of one name would let this
        # check read a different part than the converter does.
        msg = "the file holds two parts of the same name"
        raise DocumentParseError(msg)
    if sum(entry.file_size for entry in entries) > MAX_OFFICE_PACKAGE_BYTES:
        msg = "the file unpacks to more than a document may (a zip bomb?)"
        raise DocumentParseError(msg)
    xml = _parsed_as_xml(package, entries)
    pictures = Counter(
        {
            name: count
            for name, count in (
                _references(package, entries, xml) if ext == ".pptx" else {}
            ).items()
            if name not in xml
        }
    )
    if any(
        entry.file_size > MAX_XML_PART_BYTES
        for entry in entries
        if (entry.filename in xml or _is_markup(entry.filename)) and entry.filename not in pictures
    ):
        msg = "a part of the file unpacks to more than a document may"
        raise DocumentParseError(msg)
    over = _over_the_caps(entries, pictures)
    if not over:
        return raw, 0
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bounded:
        for entry in package.infolist():
            copy = zipfile.ZipInfo(entry.filename, entry.date_time)
            copy.compress_type, copy.external_attr = entry.compress_type, entry.external_attr
            if entry.filename in over:
                bounded.writestr(copy, STAND_IN)
                continue
            with package.open(entry) as source, bounded.open(copy, "w") as target:
                shutil.copyfileobj(source, target, _COPY_CHUNK)
    return out.getvalue(), len(over)


def _is_markup(name: str) -> bool:
    return name.lower().endswith((".xml", ".rels"))


def _parsed_as_xml(package: zipfile.ZipFile, entries: Sequence[zipfile.ZipInfo]) -> set[str]:
    """The parts whose registered content type is XML (not ``image/…``),
    resolved as python-pptx does: the part's Override, else its extension's
    Default, both case-insensitive."""
    found = [entry for entry in entries if entry.filename == _CONTENT_TYPES]
    if not found:
        return set()
    if found[0].file_size > MAX_XML_PART_BYTES:
        msg = "a part of the file unpacks to more than a document may"
        raise DocumentParseError(msg)
    try:
        root = SafeElementTree.fromstring(package.read(found[0]))
    except Exception as exc:
        msg = "unreadable content types part"
        raise DocumentParseError(msg) from exc
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for element in root.iter():
        tag, kind = str(element.tag).rsplit("}", 1)[-1], (element.get("ContentType") or "").lower()
        if tag == "Override" and element.get("PartName"):
            overrides[str(element.get("PartName")).lstrip("/").lower()] = kind
        elif tag == "Default" and element.get("Extension"):
            defaults[str(element.get("Extension")).lower()] = kind
    xml = set()
    for entry in entries:
        name = entry.filename.lower()
        kind = overrides.get(name, defaults.get(posixpath.splitext(name)[1][1:], ""))
        if kind.endswith("xml") and not kind.startswith("image/"):
            xml.add(entry.filename)
    return xml


def _over_the_caps(entries: Sequence[zipfile.ZipInfo], references: Counter[str]) -> set[str]:
    """The referenced parts to replace: over :data:`MAX_IMAGE_BYTES` each, then
    the largest until size x references fits :data:`MAX_OFFICE_MEDIA_BYTES`."""
    sizes = {entry.filename: entry.file_size for entry in entries if entry.filename in references}
    over = {name for name, size in sizes.items() if size > MAX_IMAGE_BYTES}
    total = sum(size * references[name] for name, size in sizes.items() if name not in over)
    for name in sorted(sizes, key=lambda n: -sizes[n] * references[n]):
        if total <= MAX_OFFICE_MEDIA_BYTES:
            break
        if name not in over:
            over.add(name)
            total -= sizes[name] * references[name]
    return over


def _references(
    package: zipfile.ZipFile, entries: Sequence[zipfile.ZipInfo], xml: set[str]
) -> Counter[str]:
    """How often each part is referenced by ``r:embed`` / ``r:link`` / ``r:id``
    from the part its ``.rels`` describes, that part parsed as python-pptx
    parses it. One python-pptx parses (``xml``) that cannot be read fails the
    document; any other unreadable one (python-pptx never parses it, so
    nothing in it is shown) counts each of its relationships once."""
    by_lower = {entry.filename.lower(): entry for entry in entries}
    counted: Counter[str] = Counter()
    for entry in entries:
        if not entry.filename.lower().endswith(".rels"):
            continue
        targets = _relationship_targets(package, entry, by_lower)
        # ``ppt/slides/_rels/slide1.xml.rels`` describes ``ppt/slides/slide1.xml``.
        folder = posixpath.dirname(posixpath.dirname(entry.filename))
        source = by_lower.get(
            posixpath.join(folder, posixpath.basename(entry.filename)[:-5]).lower()
        )
        ids = _referenced_ids(package, source) if source is not None else None
        if ids is None and source is not None and source.filename in xml:
            msg = f"unreadable part {source.filename!r}"
            raise DocumentParseError(msg)
        for relationship_id, target in targets.items():
            if ids is None:
                counted[target] += 1
            elif ids[relationship_id]:
                counted[target] += ids[relationship_id]
    return counted


def _relationship_targets(
    package: zipfile.ZipFile, rels: zipfile.ZipInfo, by_lower: dict[str, zipfile.ZipInfo]
) -> dict[str, str]:
    """Relationship id → the package part it targets (external or missing
    targets are not parts: left out)."""
    if rels.file_size > MAX_XML_PART_BYTES:
        msg = "a part of the file unpacks to more than a document may"
        raise DocumentParseError(msg)
    try:
        root = SafeElementTree.fromstring(package.read(rels))
    except Exception as exc:
        msg = f"unreadable relationships part {rels.filename!r}"
        raise DocumentParseError(msg) from exc
    folder = posixpath.dirname(posixpath.dirname(rels.filename))
    found: dict[str, str] = {}
    for element in root.iter():
        relationship_id, target = element.get("Id"), element.get("Target")
        if not relationship_id or not target:
            continue
        target = unquote(target)
        path = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
        entry = by_lower.get(posixpath.normpath(path).lower())
        if entry is not None:
            found[relationship_id] = entry.filename
    return found


def _referenced_ids(package: zipfile.ZipFile, source: zipfile.ZipInfo) -> Counter[str] | None:
    """How often each relationship id is referenced from ``source`` (parsed
    with lxml as python-pptx does: a DTD is no reason to give up — python-pptx
    reads the part all the same); ``None`` when it cannot be parsed."""
    if source.file_size > MAX_XML_PART_BYTES:
        return None
    counted: Counter[str] = Counter()
    try:
        with package.open(source) as stream:
            for _, element in etree.iterparse(stream, events=("start",), **_PPTX_PARSER):
                for attribute in _REFERENCES:
                    value = element.get(attribute)
                    if value:
                        counted[value] += 1
    except Exception:
        return None
    return counted


class DocxPictureReader:
    """mammoth's ``convert_image`` for the figure step (see module doc):
    ``pictures`` holds each distinct picture read (bytes, content type);
    ``skipped`` counts the picture parts not read."""

    def __init__(self) -> None:
        self.pictures: list[tuple[bytes, str]] = []
        self.skipped = 0
        self._by_part: dict[str, str] = {}
        self._by_digest: dict[str, int] = {}
        self._read = 0

    def __call__(self, image: Any) -> dict[str, str]:
        part = _part_name(image)
        token = self._by_part.get(part) if part is not None else None
        if token is None:
            token = self._read_picture(image)
            if part is not None:
                self._by_part[part] = token
        return {"src": f"{FIGURE_TOKEN}{token}"}

    def _read_picture(self, image: Any) -> str:
        try:
            with image.open() as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except Exception:  # an external link, a missing part, a damaged entry
            self.skipped += 1
            return "skipped"
        if len(data) > MAX_IMAGE_BYTES or self._read + len(data) > MAX_OFFICE_MEDIA_BYTES:
            self.skipped += 1
            return "skipped"
        self._read += len(data)
        digest = hashlib.sha256(data).hexdigest()
        index = self._by_digest.get(digest)
        if index is None:
            index = self._by_digest[digest] = len(self.pictures)
            self.pictures.append((data, str(getattr(image, "content_type", "") or "")))
        return str(index)


def _part_name(image: Any) -> str | None:
    """The package part mammoth would read the picture from (its ``open``
    closes over ``image_path``); ``None`` when that cannot be told — the
    picture is then read again on each reference, still within the cap."""
    opener = getattr(image, "open", None)
    code, cells = getattr(opener, "__code__", None), getattr(opener, "__closure__", None)
    if code is None or not cells:
        return None
    values = dict(zip(code.co_freevars, (cell.cell_contents for cell in cells), strict=False))
    name = values.get("image_path")
    return name if isinstance(name, str) else None
