"""kb_corpus_inventory.py 的分层 / 重复组 / 汇总(B-175 L1-T0)。语料全在 tmp_path 里现造。"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pymupdf
import pytest
from kb_corpus_inventory import (  # type: ignore[import-not-found]
    DEFAULT_CHAT_DIR,
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_MATERIAL_DIRS,
    MANIFEST_FIELDS,
    main,
)

_TEXT = "这一页有文字层,字数肯定超过二十个字,用来判成有文字层的页。"
_BIG = ("重复内容" * 400).encode("utf-8")  # > 1024 字节才参与 md5 去重


def _pdf(path: Path, pages: tuple[bool, ...]) -> None:
    """``pages`` 每一项 True = 文字页,False = 纯图页(只画一个矩形,没有文字层)。"""
    doc = pymupdf.open()
    for has_text in pages:
        page = doc.new_page()
        if has_text:
            page.insert_text((72, 72), _TEXT, fontname="china-s")
        else:
            page.draw_rect(pymupdf.Rect(40, 40, 300, 300), color=(0, 0, 0), fill=(0.6, 0.6, 0.6))
    doc.save(str(path))
    doc.close()


def _docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")


def _build_corpus(root: Path) -> None:
    manual = root / "说明书"
    manual.mkdir()
    _pdf(manual / "text.pdf", (True, True, False))  # 3 页 1 页纯图 → 还是 A
    _pdf(manual / "scan.pdf", (False, True))  # 2 页 1 页纯图(≥ 一半)→ B
    _docx(manual / "手册V2.docx")
    (manual / "link.url").write_text("[InternetShortcut]\nURL=https://example.invalid\n")
    (manual / ".DS_Store").write_bytes(b"\x00" * 64)
    (manual / "dup_a.txt").write_bytes(_BIG)
    (manual / "dup_b.txt").write_bytes(_BIG)
    (manual / "unique.txt").write_bytes(_BIG[::-1])
    (manual / "archive.zip").write_bytes(_BIG + b"zip")  # X-zip 也参与去重,内容要不同
    (manual / "weird.xyz").write_bytes(_BIG + b"xyz")
    chat = root / "聊天记录"
    chat.mkdir()
    (chat / "x.xlsx").write_bytes(b"PK\x03\x04" + _BIG)
    material = root / "物料"
    material.mkdir()
    (material / "leaflet.jpg").write_bytes(b"\xff\xd8\xff" + _BIG)
    _pdf(material / "sheet.pdf", (True,))
    _pdf(material / "photo.pdf", (False,))
    skipped = root / "skipme"
    skipped.mkdir()
    (skipped / "ignored.txt").write_bytes(_BIG)


@pytest.fixture(scope="module")
def inventory(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, dict[str, str]], str]:
    root = tmp_path_factory.mktemp("corpus")
    out = tmp_path_factory.mktemp("out")
    _build_corpus(root)
    argv = [
        str(root),
        str(out),
        "--exclude-dir",
        "skipme",
        "--chat-dir",
        "聊天记录",
        "--material-dir",
        "物料",
    ]
    assert main(argv) == 0
    with (out / "manifest.csv").open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == list(MANIFEST_FIELDS)
        rows = {row["path"]: row for row in reader}
    return rows, (out / "summary.txt").read_text(encoding="utf-8")


def _tier(rows: dict[str, dict[str, str]], rel: str) -> str:
    return rows[str(Path(rel))]["tier"]


def test_tiers(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, _ = inventory
    assert _tier(rows, "说明书/text.pdf") == "A"
    assert _tier(rows, "说明书/scan.pdf") == "B"
    assert _tier(rows, "说明书/手册V2.docx") == "A"
    assert _tier(rows, "说明书/link.url") == "X"
    assert _tier(rows, "说明书/.DS_Store") == "X"
    assert _tier(rows, "说明书/archive.zip") == "X-zip"
    assert _tier(rows, "说明书/weird.xyz") == "X-other"
    assert _tier(rows, "聊天记录/x.xlsx") == "Q"
    assert _tier(rows, "物料/leaflet.jpg") == "D"
    assert _tier(rows, "物料/sheet.pdf") == "D-A"
    assert _tier(rows, "物料/photo.pdf") == "D-B"


def test_excluded_dir_is_not_walked(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, _ = inventory
    assert not any(p.startswith("skipme") for p in rows)


def test_pdf_page_stats(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, _ = inventory
    text = rows[str(Path("说明书/text.pdf"))]
    assert (text["pages"], text["scanned_pages"]) == ("3", "1")
    assert int(text["text_chars"]) > 20
    scan = rows[str(Path("说明书/scan.pdf"))]
    assert (scan["pages"], scan["scanned_pages"]) == ("2", "1")
    assert rows[str(Path("说明书/手册V2.docx"))]["pages"] == "0"


def test_duplicate_pair_shares_dup_group(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, _ = inventory
    a = rows[str(Path("说明书/dup_a.txt"))]["dup_group"]
    b = rows[str(Path("说明书/dup_b.txt"))]["dup_group"]
    assert a and a == b
    assert rows[str(Path("说明书/unique.txt"))]["dup_group"] == ""
    assert rows[str(Path("说明书/.DS_Store"))]["dup_group"] == ""


def test_version_marker(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, _ = inventory
    assert rows[str(Path("说明书/手册V2.docx"))]["version_marker"] == "V数字"
    assert rows[str(Path("说明书/text.pdf"))]["version_marker"] == ""


def test_manifest_has_no_document_content(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, summary = inventory
    blob = "\n".join(",".join(r.values()) for r in rows.values()) + summary
    assert _TEXT[:6] not in blob
    assert "重复内容" not in blob


def test_summary_counts(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, summary = inventory
    assert f"files={len(rows)}" in summary
    assert len(rows) == 14
    assert "dup_groups=1" in summary
    assert "dup_extra=1" in summary
    lines = {line.split()[0]: line.split() for line in summary.splitlines() if line.strip()}
    # tier  files  size  pdf_pages  scanned
    assert lines["A"][1] == "5"  # text.pdf 手册V2.docx dup_a dup_b unique
    assert lines["B"][1] == "1"
    assert lines["B"][3:5] == ["2", "1"]
    assert lines["X"][1] == "2"
    assert lines["Q"][1] == "1"
    assert lines["D"][1] == "1"
    assert lines["D-A"][1] == "1"
    assert lines["D-B"][1] == "1"
    assert lines["X-zip"][1] == "1"
    assert lines["X-other"][1] == "1"
    assert "version-marked files: 1" in summary
    assert "pdf errors: 0" in summary


def test_defaults_are_the_huada_layout() -> None:
    assert DEFAULT_CHAT_DIR == "聊天记录"
    assert ".workbuddy" in DEFAULT_EXCLUDE_DIRS
    assert len(DEFAULT_MATERIAL_DIRS) == 5


def test_empty_root_writes_header_only(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    out = tmp_path / "out"
    assert main([str(root), str(out)]) == 0
    assert (out / "manifest.csv").read_text(encoding="utf-8").splitlines() == [
        ",".join(MANIFEST_FIELDS)
    ]
    assert "files=0" in (out / "summary.txt").read_text(encoding="utf-8")
