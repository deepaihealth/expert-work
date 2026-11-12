"""B-301 PR 2 —— 体检报告:各线与结论、L2 严格 / 宽松两种判法(按类结论用严格)、数的统一写法、
从调用记录推方向、§8.1 计数、账本里的出错停与花费、``report`` 命令。全部离线,用手写的结果。"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b301_health as health
import b301_images as images_mod
import b301_report as report_mod
import b301_spend as spend

from orchestrator.tools.image_orientation import ROTATED_NOTE

IMAGES = images_mod.build_set()
BY_ID = {image.image_id: image for image in IMAGES}
MODEL = health.DEFAULT_MODELS[1]
WRONG = "看不清"


def _record(
    image: images_mod.SetImage,
    mode: str,
    *,
    answer: str | None = None,
    first: str = "yes",
    conclusion: str | None = None,
) -> dict[str, Any]:
    degrees = image.rotation if mode == "on" else 0
    if conclusion is None:
        conclusion = "rotated" if degrees else ("no_text" if image.key is None else "upright")
    orientation = (
        None
        if mode == "control"
        else {
            "conclusion": conclusion,
            "degrees": degrees,
            "answers": {"0": "no" if image.rotation else first},
        }
    )
    ok_call = {
        "kind": "orientation",
        "turn": 0,
        "outcome": "ok",
        "answer": "yes",
        "latency_s": 1.0,
        "cost_yuan": 0.01,
    }
    if answer is None:
        answer = (image.key or "") if mode == "on" else WRONG
    return {
        "plan_id": "p1",
        "model": MODEL,
        "mode": mode,
        "image_id": image.image_id,
        "status": "ok",
        "orientation": orientation,
        "answer": answer,
        "calls": [] if mode == "control" else [ok_call],
        "cost_yuan": 0.01,
    }


def _perfect() -> list[dict[str, Any]]:
    rows = [_record(image, "on") for image in IMAGES]
    rows += [_record(image, "control") for image in IMAGES if not image.upright]
    return rows


def test_a_perfect_run_passes_with_every_class_showing_a_gain() -> None:
    result = report_mod.model_report(MODEL, _perfect(), IMAGES)

    assert result["verdict"] == "通过" and result["why"] == []
    assert result["L1"]["ok"] == 99 and result["L1"]["pass"]
    assert {c["result"] for c in result["L2"].values()} == {"有收益"}
    assert "通过" in report_mod.render_report(report_mod.report(_perfect(), IMAGES))


def test_missing_records_make_the_verdict_incomplete() -> None:
    result = report_mod.model_report(MODEL, _perfect()[:-1], IMAGES)

    assert result["verdict"] == "不完整"


def test_more_than_three_undetermined_upright_images_fail_l1_prime() -> None:
    upright = [i for i in IMAGES if i.upright and i.key is not None][:4]
    ids = {i.image_id for i in upright}
    rows = [r for r in _perfect() if not (r["mode"] == "on" and r["image_id"] in ids)]
    rows += [_record(i, "on", conclusion="undetermined", first="no") for i in upright]

    result = report_mod.model_report(MODEL, rows, IMAGES)

    assert result["verdict"] == "不通过"
    assert len(result["L1'"]["undetermined"]) == 4 and result["L1'"]["first_step_no"] == 4


def test_l6_fails_above_five_percent_timeouts_or_off_topic() -> None:
    rows = _perfect()
    bad = {"kind": "orientation", "turn": 0, "outcome": "timeout", "answer": "timeout"}
    for row in rows[:8]:  # 8 / 136 = 5.9 %
        row["calls"] = [{**bad, "latency_s": 30.0}]
    result = report_mod.model_report(MODEL, rows, IMAGES)

    assert result["L6"]["timeouts"] == 8 and result["L6"]["pass"] is False
    assert result["verdict"] == "不通过"


def test_l1_misses_and_l2_classes_are_reported_but_do_not_block() -> None:
    rows = _perfect()
    charts = {i.image_id for i in IMAGES if i.image_class == "chart" and not i.upright}
    tables = {i.image_id for i in IMAGES if i.image_class == "table" and not i.upright}
    labels = {i.image_id for i in IMAGES if i.image_class == "label"}
    for row in rows:
        image = BY_ID[row["image_id"]]
        if row["mode"] == "on" and row["image_id"] in charts:  # 没转、读错 → 「变差」(对照全对)
            row["orientation"] = {
                "conclusion": "undetermined",
                "degrees": 0,
                "answers": {"0": "no"},
            }
            row["answer"] = WRONG
        if row["mode"] == "control" and row["image_id"] in charts | tables:  # 对照本来就对
            row["answer"] = image.key
        if row["image_id"] in labels and image.upright:  # 正放都读不对 → 不计入
            row["answer"] = WRONG

    result = report_mod.model_report(MODEL, rows, IMAGES)

    assert result["L2"]["chart"]["result"] == "变差"
    assert result["L2"]["table"]["result"] == "无收益"
    assert result["L2"]["label"]["result"].startswith("模型本来就读不好")
    assert result["L1"]["by_class"]["chart"] == 0 and result["L1"]["pass"] is False
    assert result["needs_user"] == ["L2「变差」:chart"]
    assert result["L1'"]["undetermined"] == []  # 那 9 张是转过的图
    assert result["verdict"] == "通过"


def test_hedged_answers_count_only_as_lenient_and_classes_use_strict() -> None:
    """显示屏类开着的读全部是「X,也可能是另一个数」:宽松 9/9、严格 0/9 —— 按类结论用严格,
    所以不是「有收益」。"""
    rows = _perfect()
    for row in rows:
        image = BY_ID[row["image_id"]]
        if row["mode"] == "on" and image.image_class == "display" and not image.upright:
            row["answer"] = f"可能是 {image.key},也可能是 1.69"
    result = report_mod.model_report(MODEL, rows, IMAGES)

    display = result["L2"]["display"]
    assert display["lenient"]["on"] == 9 and display["on"] == 0
    assert display["result"] != "有收益"
    lines = report_mod.render_report(report_mod.report(rows, IMAGES))
    assert "display: 3(3)/3, 0(9)/9" in lines


def test_answers_where_strict_and_lenient_disagree_are_listed_for_review() -> None:
    """「87.5(第4周)」读对了但带了别的数:严格算错 —— 报告逐条列出来给人看。"""
    rows = _perfect()
    for row in rows:
        if row["image_id"] == "ch-2@90" and row["mode"] == "on":
            row["answer"] = "87.5(第4周)"
    result = report_mod.model_report(MODEL, rows, IMAGES)

    chart = result["L2"]["chart"]
    assert chart["on"] == 8 and chart["lenient"]["on"] == 9
    assert chart["disagreements"] == [
        {"image_id": "ch-2@90", "mode": "on", "key": "87.5", "answer": "87.5(第4周)"}
    ]
    assert result["L2"]["ui"]["disagreements"] == []
    lines = report_mod.render_report(report_mod.report(rows, IMAGES))
    assert "严格≠宽松 ch-2@90 (on) 关键字段 '87.5':'87.5(第4周)'" in lines


def test_orientation_is_derived_from_the_calls_when_the_tool_failed() -> None:
    calls = [
        {"kind": "orientation", "turn": 0, "outcome": "ok", "answer": "no"},
        {"kind": "orientation", "turn": 90, "outcome": "ok", "answer": "no"},
        {"kind": "orientation", "turn": 180, "outcome": "ok", "answer": "yes"},
        {"kind": "orientation", "turn": 270, "outcome": "ok", "answer": "no"},
        {"kind": "read", "turn": None, "outcome": "timeout"},
    ]
    derive = report_mod.orientation_of
    assert derive({"orientation": None, "calls": calls}) == ("rotated", 180, "no")
    assert derive({"orientation": None, "calls": calls[:2]}) == ("not_asked", 0, "no")
    assert derive({"orientation": None, "calls": []}) == ("not_asked", 0, None)


@pytest.mark.parametrize(
    ("key", "answer", "numeric", "lenient", "strict"),
    [
        ("69.1", "69.1", True, True, True),
        ("69.1", "读数是 69.1 kg。", True, True, True),
        ("69.1", "69.1kg", True, True, True),
        ("69.1", "可能是 69.1,也可能是 1.69", True, True, False),
        ("69.1", "might be 69.1 or 61.9", True, True, False),
        ("69.1", "169.1", True, False, False),
        ("69.1", "69.15", True, False, False),
        ("87.5", "87.5(第4周)", True, True, False),
        ("36.80", "36.8", True, True, True),
        ("08:47", "8:47", True, True, True),
        # 编号、日期不做数的统一写法:前导 0 是编号的一部分。
        ("7731-0428", "7731-428", False, False, False),
        ("HT-2026-0915-07", "HT-2026-915-7", False, False, False),
        ("2026-03-17", "2026-3-17", False, False, False),
        ("KX-450B", "KX-450BX", False, False, False),
        ("KX-450B", "KX-450B or KX-460B", False, True, False),
        ("2026-03-17", "2026-03-17", False, True, True),
        ("2026-03-17", "2026年3月17日", False, False, False),
        ("Harbor_Guest_5G", "**Harbor_Guest_5G**", False, True, True),
        # 全角括号 / 逗号 / 弯引号写成转义(ruff 的模糊字符规则会把它们「修」成半角)。
        ("青禾便利店(南湖店)", "青禾便利店\uff08南湖店\uff09", False, True, True),
        ("每日两次,每次一片", "每日两次\uff0c每次一片。", False, True, True),
        ("Riverside Terminal", "\u201cRiverside Terminal\u201d", False, True, True),
        ("Riverside Terminal", "The sign says Riverside Terminal", False, True, True),
        (
            "Riverside Terminal",
            "maybe Riverside Terminal or Riverside Terminus",
            False,
            True,
            False,
        ),
        ("林记老茶铺", "林记老茶铺,也可能是林家老茶铺", False, True, False),
        ("4:30 pm", "4:30 PM", True, True, True),
        ("Room 204", "Room 2048", False, False, False),
        ("128.46", "合计 128.46 元", True, True, True),
    ],
)
def test_field_matching(key: str, answer: str, numeric: bool, lenient: bool, strict: bool) -> None:
    assert report_mod.field_matches(key, answer, numeric=numeric) is lenient
    assert report_mod.field_matches_strict(key, answer, numeric=numeric) is strict


def test_only_number_keys_get_the_number_normal_form() -> None:
    kinds = {spec.key: spec.kind for spec in images_mod.base_specs()}
    assert kinds["7731-0428"] == "code" and kinds["HT-2026-0915-07"] == "code"
    assert kinds["2026-03-17"] == "date" and kinds["08:47"] == "number"
    assert all(image.kind == kinds[image.key] for image in IMAGES if image.key is not None)


TEXT_KEYS = [spec.key for spec in images_mod.base_specs() if not any(c.isdigit() for c in spec.key)]


@pytest.mark.parametrize("key", TEXT_KEYS)
def test_strict_rejects_alternatives_and_long_answers_for_keys_without_digits(key: str) -> None:
    for hedged in (
        f"maybe {key} or Riverside Terminus",
        f"{key}, possibly something else",
        f"{key},也可能是别的",
        f"可能是{key},也可能不是",
        f"{key} 或 别的",
        f"{key} — and here is a long explanation of what else is written on the image",
    ):
        assert report_mod.field_matches(key, hedged), hedged
        assert not report_mod.field_matches_strict(key, hedged), hedged
    for plain in (key, f"答案:{key}", f"It reads {key}."):
        assert report_mod.field_matches_strict(key, plain), plain


def test_twelve_of_the_keys_have_no_digits() -> None:
    assert len(TEXT_KEYS) == 12


def test_notes_added_by_the_tool_are_not_part_of_the_answer() -> None:
    content = "69.1\n\n" + ROTATED_NOTE.format(degrees=90)
    assert report_mod.strip_notes(content) == "69.1"


def test_probe_report_counts_yes_no_none() -> None:
    images = health.probe_images(IMAGES)
    rows: list[Mapping[str, Any]] = []
    for image in images:
        word = "none" if image.key is None else ("yes" if image.upright else "no")
        rows.append(
            {
                "model": "glm-5.3-flashx",
                "mode": "probe",
                "image_id": image.image_id,
                "probe_answer": word,
                "calls": [{"outcome": "ok", "latency_s": 2.0}],
            }
        )
    p = report_mod.probe_report("glm-5.3-flashx", rows, IMAGES)

    assert len(images) == 70
    counts = (p["upright_yes"], p["upright"], p["rotated_no"], p["rotated"], p["no_text_none"])
    assert counts == (33, 33, 33, 33, 4)
    assert p["unanswered"] == 0


def test_report_cli_reads_the_plan_ledger_for_spend_and_error_stops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "anywhere" / "r.jsonl"
    results.parent.mkdir()
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _perfect())
    results.write_text(lines, encoding="utf-8")
    state = tmp_path / "state"
    book = spend.SpendLedger(spend.ledger_path(state, "p1"), "p1")
    book.append({"call_id": "a", "model": MODEL, "cost_yuan": 1.5, "reason": "ok"})
    book.append({"call_id": "b", "model": MODEL, "cost_yuan": 0.168, "reason": "cap_error"})

    assert health.main(["report", "--results", str(results), "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert f"{MODEL}:通过" in out
    assert "厂商出错而停(账本):1 次" in out
    assert "账本 ¥1.668" in out
