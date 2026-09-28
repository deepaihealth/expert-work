import json
import subprocess
import sys
from pathlib import Path

from hpr.catalog import KINDS, VARIANTS
from hpr.content import (
    ContentError,
    load_content,
    normalize_content,
    required_texts,
    validate_content,
)

SKILL_DIR = Path(__file__).resolve().parents[2] / "health-plan-report"
SAMPLE = SKILL_DIR / "sample" / "sample-plan.json"
VALIDATE = SKILL_DIR / "scripts" / "validate.py"


def _paths(errs: list[ContentError]) -> list[str]:
    return [e.path for e in errs]


def test_sample_is_valid_and_covers_every_kind(sample):
    assert validate_content(sample) == []
    seen = {b["kind"] for s in sample["sections"] for b in s["blocks"]}
    assert seen == set(KINDS)


def test_kinds_and_variants_consistent():
    assert set(KINDS) == set(VARIANTS)
    assert all(VARIANTS[k] for k in KINDS)


def test_unknown_kind_reported_with_path(sample):
    sample["sections"][0]["blocks"][0] = {"kind": "medication", "items": []}
    errs = validate_content(sample)
    assert _paths(errs) == ["sections[0].blocks[0].kind"]


def test_missing_required_field(sample):
    del sample["title"]
    assert "title" in _paths(validate_content(sample))


def test_unsupported_field(sample):
    sample["sections"][0]["blocks"][0]["color"] = "red"
    assert "sections[0].blocks[0].color" in _paths(validate_content(sample))


def test_table_row_column_mismatch(sample):
    table = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "table")
    table["rows"].append(["only one cell"])
    errs = validate_content(sample)
    assert any(e.path.endswith(f"rows[{len(table['rows']) - 1}]") for e in errs)


def test_duplicate_section_id_and_bad_id(sample):
    sample["sections"][1]["id"] = sample["sections"][0]["id"]
    sample["sections"][2]["id"] = "Bad Id"
    paths = _paths(validate_content(sample))
    assert "sections[1].id" in paths
    assert "sections[2].id" in paths


def test_duplicate_block_id_within_section(sample):
    sec = next(s for s in sample["sections"] if len(s["blocks"]) >= 2)
    sec["blocks"][0]["id"] = "dup"
    sec["blocks"][1]["id"] = "dup"
    idx = sample["sections"].index(sec)
    assert f"sections[{idx}].blocks[1].id" in _paths(validate_content(sample))


def test_bad_date_url_hhmm(sample):
    sample["generated_at"] = "2026/09/28"
    mat = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "material")
    mat["url"] = "ftp://x"
    sleep = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "sleep")
    sleep["current"]["bed"] = "25:00"
    paths = _paths(validate_content(sample))
    assert "generated_at" in paths
    assert any(p.endswith(".url") for p in paths)
    assert any(p.endswith(".current.bed") for p in paths)


def test_trend_needs_two_numeric_points(sample):
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    trend["points"] = [{"date": "2026-09-01", "value": "6.1"}]
    paths = _paths(validate_content(sample))
    assert any(p.endswith(".points") for p in paths)
    assert any(p.endswith(".points[0].value") for p in paths)


def test_any_of_rule(sample):
    rules = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "diet_rules")
    for k in ("recommend", "limit", "avoid", "swaps"):
        rules.pop(k, None)
    errs = validate_content(sample)
    assert any("至少需要以下字段之一" in e.message for e in errs)


def test_bool_is_not_a_number(sample):
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    trend["points"][0]["value"] = True
    assert any(p.endswith(".points[0].value") for p in _paths(validate_content(sample)))


def test_required_texts_skip_ids_urls_and_chart_points(sample):
    texts = required_texts(sample)
    assert sample["title"] in texts
    assert sample["sections"][0]["title"] in texts
    assert sample["sections"][0]["id"] not in texts
    assert all(not t.startswith("http") for t in texts)
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    assert trend["points"][0]["date"] not in texts


def test_load_content_rejects_non_object(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2]", encoding="utf-8")
    try:
        load_content(p)
    except ValueError as exc:
        assert "对象" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_cli_exit_codes(tmp_path, sample):
    ok = subprocess.run(  # noqa: S603
        [sys.executable, str(VALIDATE), str(SAMPLE)], capture_output=True, text=True, check=False
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    del sample["sections"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(  # noqa: S603
        [sys.executable, str(VALIDATE), str(bad)], capture_output=True, text=True, check=False
    )
    assert res.returncode == 1
    assert json.loads(res.stdout)["errors"] == ["sections: 缺少必填字段"]


def test_normalize_content_maps_line_ends_and_controls_without_mutating(sample):
    para = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "paragraph")
    para["text"] = "a\r\nb\rc\x0bd\x0ce\x00f\tg\nh\x1fi"
    sample["client"]["facts"][0]["value"] = "x\r\ny"
    sample["brand"]["logo_path"] = "logo\r.png"
    before = json.dumps(sample, ensure_ascii=False)
    out = normalize_content(sample)
    assert json.dumps(sample, ensure_ascii=False) == before  # input untouched
    para_out = next(b for s in out["sections"] for b in s["blocks"] if b["kind"] == "paragraph")
    assert para_out["text"] == "a\nb\nc d e f\tg\nh i"
    assert out["client"]["facts"][0]["value"] == "x\ny"
    assert out["brand"]["logo_path"] == "logo\r.png"  # file names are not display text
    assert out["title"] == sample["title"]
