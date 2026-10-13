"""kb_corpus_inventory.py 的分层 / 重复组 / 汇总(B-175 L1-T0)。语料全在 tmp_path 里现造。"""

from __future__ import annotations

import csv
import os
import zipfile
from pathlib import Path

import pymupdf
import pytest
import yaml
from kb_corpus_inventory import (  # type: ignore[import-not-found]
    DEFAULT_CHAT_DIR,
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_EXCLUDE_NAMES,
    DEFAULT_MATERIAL_DIRS,
    MANIFEST_FIELDS,
    InventoryConfig,
    LayoutError,
    load_layout,
    main,
)

_HUADA_LAYOUT = Path(__file__).resolve().parent / "datasets" / "kb" / "huada_v1.layout.yaml"

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
    material2 = root / "物料2"  # 只在 CLI 上加,证明 CLI 是叠加到 layout 上
    material2.mkdir()
    (material2 / "poster.png").write_bytes(b"\x89PNG" + _BIG)
    skipped = root / "skipme"  # layout 里排除
    skipped.mkdir()
    (skipped / "ignored.txt").write_bytes(_BIG)
    skipped2 = root / "skipme2"  # CLI 上排除
    skipped2.mkdir()
    (skipped2 / "ignored.txt").write_bytes(_BIG)
    (root / "名单.doc").write_bytes(_BIG + b"doc")  # layout exclude_names → X


def _write_layout(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "exclude_dirs": ["skipme"],
                "exclude_names": ["名单.doc"],
                "chat_dir": "聊天记录",
                "material_dirs": ["物料"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def inventory(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, dict[str, str]], str]:
    root = tmp_path_factory.mktemp("corpus")
    out = tmp_path_factory.mktemp("out")
    _build_corpus(root)
    layout = _write_layout(tmp_path_factory.mktemp("layout") / "layout.yaml")
    argv = [
        str(root),
        str(out),
        "--layout",
        str(layout),
        "--exclude-dir",
        "skipme2",
        "--material-dir",
        "物料2",
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
    assert _tier(rows, "名单.doc") == "X"  # layout exclude_names


def test_cli_flags_add_to_layout(inventory: tuple[dict[str, dict[str, str]], str]) -> None:
    rows, _ = inventory
    assert _tier(rows, "物料2/poster.png") == "D"  # CLI --material-dir 叠加在 layout 的「物料」上
    assert _tier(rows, "物料/leaflet.jpg") == "D"  # layout 的那条没被 CLI 替换掉
    assert not any(p.startswith("skipme2") for p in rows)  # CLI --exclude-dir
    assert not any(p.startswith(f"skipme{os.sep}") for p in rows)  # layout exclude_dirs


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
    assert len(rows) == 16
    assert "dup_groups=1" in summary
    assert "dup_extra=1" in summary
    lines = {line.split()[0]: line.split() for line in summary.splitlines() if line.strip()}
    # tier  files  size  pdf_pages  scanned
    assert lines["A"][1] == "5"  # text.pdf 手册V2.docx dup_a dup_b unique
    assert lines["B"][1] == "1"
    assert lines["B"][3:5] == ["2", "1"]
    assert lines["X"][1] == "3"  # link.url .DS_Store 名单.doc
    assert lines["Q"][1] == "1"
    assert lines["D"][1] == "2"  # leaflet.jpg poster.png
    assert lines["D-A"][1] == "1"
    assert lines["D-B"][1] == "1"
    assert lines["X-zip"][1] == "1"
    assert lines["X-other"][1] == "1"
    assert "version-marked files: 1" in summary
    assert "pdf errors: 0" in summary


def test_code_defaults_are_tenant_neutral() -> None:
    """客户目录名只能在 layout 数据文件里,代码默认值不带任何租户内容。"""
    assert DEFAULT_EXCLUDE_DIRS == (".workbuddy",)
    assert DEFAULT_EXCLUDE_NAMES == ()
    assert DEFAULT_MATERIAL_DIRS == ()
    assert DEFAULT_CHAT_DIR is None


def test_without_layout_or_flags_nothing_is_q_or_d(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "聊天记录").mkdir()
    (root / "聊天记录" / "x.xlsx").write_bytes(b"PK\x03\x04" + _BIG)
    (root / "物料").mkdir()
    (root / "物料" / "a.jpg").write_bytes(b"\xff\xd8\xff" + _BIG)
    out = tmp_path / "out"
    assert main([str(root), str(out)]) == 0
    with (out / "manifest.csv").open(encoding="utf-8", newline="") as fh:
        tiers = {row["path"]: row["tier"] for row in csv.DictReader(fh)}
    assert tiers == {str(Path("聊天记录/x.xlsx")): "A", str(Path("物料/a.jpg")): "B"}


def test_huada_layout_file_loads() -> None:
    cfg = load_layout(_HUADA_LAYOUT)
    assert isinstance(cfg, InventoryConfig)
    assert cfg.chat_dir == "聊天记录"
    assert ".workbuddy" in cfg.exclude_dirs
    assert len(cfg.material_dirs) == 5
    assert len(cfg.exclude_names) == 1


def test_layout_file_partial_keys_fall_back_to_neutral_defaults(tmp_path: Path) -> None:
    path = tmp_path / "layout.yaml"
    path.write_text(yaml.safe_dump({"material_dirs": ["m"]}), encoding="utf-8")
    cfg = load_layout(path)
    assert cfg == InventoryConfig(
        exclude_dirs=frozenset({".workbuddy"}),
        exclude_names=frozenset(),
        chat_dir=None,
        material_dirs=frozenset({"m"}),
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"material_dirs": "m"},  # 不是列表
        {"chat_dir": ["a"]},  # 不是字符串
        {"exclude_dirs": [1]},  # 列表元素不是字符串
        {"unknown_key": []},  # 未知键
        [],  # 顶层不是映射
    ],
)
def test_layout_file_validation(tmp_path: Path, bad: object) -> None:
    path = tmp_path / "layout.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(LayoutError):
        load_layout(path)


def test_cli_chat_dir_overrides_layout(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "群聊").mkdir()
    (root / "群聊" / "x.xlsx").write_bytes(b"PK\x03\x04" + _BIG)
    layout = _write_layout(tmp_path / "layout.yaml")
    out = tmp_path / "out"
    assert main([str(root), str(out), "--layout", str(layout), "--chat-dir", "群聊"]) == 0
    with (out / "manifest.csv").open(encoding="utf-8", newline="") as fh:
        tiers = {row["path"]: row["tier"] for row in csv.DictReader(fh)}
    assert tiers == {str(Path("群聊/x.xlsx")): "Q"}


def test_empty_root_writes_header_only(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    out = tmp_path / "out"
    assert main([str(root), str(out)]) == 0
    assert (out / "manifest.csv").read_text(encoding="utf-8").splitlines() == [
        ",".join(MANIFEST_FIELDS)
    ]
    assert "files=0" in (out / "summary.txt").read_text(encoding="utf-8")


def test_empty_file_is_tier_x_with_error(tmp_path: Path) -> None:
    """0 字节的 PDF / docx 归 X 层并记 ``EmptyFile``:语料里真有一对这样的质检报告,
    入库必失败,不该算进 A 层拖累基线。"""
    from kb_corpus_inventory import InventoryConfig, classify  # type: ignore[import-not-found]

    cfg = InventoryConfig(
        exclude_dirs=frozenset(),
        exclude_names=frozenset(),
        chat_dir=None,
        material_dirs=frozenset(),
    )
    empty = tmp_path / "report.pdf"
    empty.write_bytes(b"")
    tier, stats = classify(empty, top=".", cfg=cfg)
    assert tier == "X"
    assert stats.error == "EmptyFile"
    real = tmp_path / "note.md"
    real.write_text("x", encoding="utf-8")
    assert classify(real, top=".", cfg=cfg)[0] == "A"
