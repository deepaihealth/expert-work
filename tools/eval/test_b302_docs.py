"""B-302 PR 2 A/B —— 合成文档生成器(设计 §5.2)。"""

from __future__ import annotations

import io
import json
import random
import re
from pathlib import Path

import b302_docs as docs
import numpy as np
import pytest
from PIL import Image

_INCIDENT = {
    "key": "incident",
    "language": "zh",
    "title": "合成样张",
    "fields": [
        {"label": f"字段{i}", "kind": kind}
        for i, kind in enumerate(
            [
                "name",
                "org",
                "date",
                "code",
                "phrase",
                "address",
                "code",
                "name",
                "amount",
                "quantity",
            ]
        )
    ],
    "filler": ["这是一行合成的说明文字。"],
    "sections": ["一", "二"],
}


#: 形近字对里出现过的字(从 ``docs.LOOKALIKES`` 展开)。
_LOOKALIKE_CHARS = frozenset(c for pair in docs.LOOKALIKES for c in pair)


def _incident(tmp_path: Path) -> docs.Category:
    path = tmp_path / "incident.json"
    path.write_text(json.dumps(_INCIDENT, ensure_ascii=False), encoding="utf-8")
    return docs.load_incident_template(path)


def _full_set(tmp_path: Path) -> list[docs.GoldDoc]:
    return docs.build_set([*docs.CATEGORIES, _incident(tmp_path)], seed=7)


def test_twelve_documents_two_per_category(tmp_path: Path) -> None:
    built = _full_set(tmp_path)
    assert len(built) == 12
    by_category: dict[str, int] = {}
    for d in built:
        by_category[d.category] = by_category.get(d.category, 0) + 1
    assert set(by_category.values()) == {2}
    assert len(by_category) == 6


def test_each_document_has_four_to_six_pages_and_ten_key_fields(tmp_path: Path) -> None:
    for d in _full_set(tmp_path):
        assert 4 <= len(d.pages) <= 6
        assert len(d.fields) == 10
        assert sum(f.kind in docs.NON_NUMERIC for f in d.fields) >= 6
        for f in d.fields:
            assert f"[{f.tag}] {f.label}: {f.value}" in d.pages[f.page - 1]


def test_key_fields_carry_look_alike_characters(tmp_path: Path) -> None:
    for d in _full_set(tmp_path):
        with_lookalikes = [f for f in d.fields if any(c in _LOOKALIKE_CHARS for c in f.value)]
        assert len(with_lookalikes) >= 2, d.doc_id


def test_tags_are_unique_and_appear_once_in_their_document(tmp_path: Path) -> None:
    built = _full_set(tmp_path)
    for d in built:
        text = "\n".join(d.pages)
        for f in d.fields:
            assert text.count(f.tag) == 1
    all_tags = [f.tag for d in built for f in d.fields]
    # 只在本文档出现:跨文档也不重复,轨迹里看到一个标签就知道是哪份文档的哪一项。
    assert len(all_tags) == len(set(all_tags))


def test_templates_corrections_and_deliverables_are_spread_as_designed(tmp_path: Path) -> None:
    built = _full_set(tmp_path)
    assert sum(d.template is not None for d in built) == 4
    assert sum(d.correction is not None for d in built) == 6
    assert {d.deliverable for d in built} == set(docs.DELIVERABLES)
    for d in built:
        if d.correction is not None:
            target = next(f for f in d.fields if f.tag == d.correction["tag"])
            assert d.correction["value"] != target.value
            assert d.correction["tag"] in d.prompts["turn3"]
        else:
            assert "turn3" not in d.prompts


def test_the_set_is_deterministic(tmp_path: Path) -> None:
    first = [d.to_json() for d in _full_set(tmp_path)]
    second = [d.to_json() for d in _full_set(tmp_path)]
    assert first == second


def test_a_correction_changes_a_look_alike_or_a_digit() -> None:
    rng = random.Random(1)  # noqa: S311
    assert docs.correction_value("QX-1O5S-l7", rng) != "QX-1O5S-l7"
    assert docs.correction_value("林未然", rng) == "林末然"
    assert docs.correction_value("Unit A3", rng) != "Unit A3"


def test_an_incident_template_with_too_many_numbers_is_refused(tmp_path: Path) -> None:
    bad = dict(_INCIDENT)
    bad["fields"] = [{"label": f"n{i}", "kind": "amount"} for i in range(10)]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="at least 6"):
        docs.load_incident_template(path)


def test_without_the_incident_template_the_set_is_marked_incomplete(tmp_path: Path) -> None:
    manifest = docs.write_set(tmp_path / "out", seed=3, incident=None, font=None, render=False)
    assert manifest["complete"] is False
    assert len(manifest["docs"]) == 10


def test_the_regression_set_has_four_and_six_page_tables(tmp_path: Path) -> None:
    for pages in (4, 6):
        table = docs.regression_table(pages, seed=1)
        assert table["pages"] == pages
        assert {r["page"] for r in table["rows"]} == set(range(1, pages + 1))
        for row in table["rows"]:
            assert row["total"] == sum(row["quarters"])


def test_rendering_makes_a_scanned_pdf(tmp_path: Path) -> None:
    font = _font_or_skip()
    built = _full_set(tmp_path)
    minutes = next(d for d in built if d.category == "minutes")
    category = next(c for c in docs.CATEGORIES if c.key == "minutes")
    pdf = docs.render_doc(minutes, category, font, seed=7)
    assert pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == len(minutes.pages)
    page = docs.render_page(minutes.pages[0], font, handwriting=True, rng=random.Random(1))  # noqa: S311
    jpeg = docs.scan(page, random.Random(2))  # noqa: S311
    scanned = Image.open(io.BytesIO(jpeg))
    assert scanned.format == "JPEG"
    assert scanned.size == docs.PAGE_PX
    original = np.asarray(page, dtype=float)
    assert original.min() < 100, "nothing was drawn on the page"
    # 扫描效果确实改了像素:不是原样白底黑字。
    assert np.abs(np.asarray(scanned.convert("L"), dtype=float) - original).mean() > 5


def _font_or_skip() -> str:
    font: str | None = None
    try:
        font = docs.find_font()
    except FileNotFoundError:
        pass
    if font is None:
        pytest.skip("no CJK font on this machine")
    return font


def test_a_line_wider_than_the_page_is_refused(tmp_path: Path) -> None:
    """独立审查 LOW-5:模板里的一行在页面上放不下就拒收,不然排版时被裁掉、标准答案对不上。"""
    font = _font_or_skip()
    for category in docs.CATEGORIES:
        docs.check_category(category, font_path=font)
    wide = dict(_INCIDENT, filler=["这一行合成的说明文字写得特别长" * 8])
    path = tmp_path / "wide.json"
    path.write_text(json.dumps(wide, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="wider than the page"):
        docs.load_incident_template(path, font_path=font)
    long_label = dict(_INCIDENT)
    long_label["fields"] = [
        {**f, "label": "非常非常长的字段名称" * 6} if i == 0 else f
        for i, f in enumerate(_INCIDENT["fields"])
    ]
    path.write_text(json.dumps(long_label, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="wider than the page"):
        docs.load_incident_template(path, font_path=font)
    with pytest.raises(ValueError, match="wider than the page"):
        docs.render_page("宽" * 200, font, handwriting=False, rng=random.Random(1))  # noqa: S311
