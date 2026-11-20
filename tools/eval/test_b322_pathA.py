"""B-322 PR ① E 组体检脚本:报价、真跑的驱动(厂商换成替身,不花钱)、报告的判线。"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b301_images as images_mod  # noqa: E402
import b301_plan  # noqa: E402
import b322_pathA as e_group  # noqa: E402

from expert_work.protocol import model_catalog  # noqa: E402
from orchestrator import image_views  # noqa: E402

_USAGE = {"input_tokens": 1500, "output_tokens": 2, "total_tokens": 1502}


def _subset() -> list[images_mod.SetImage]:
    image_set = images_mod.build_set()
    by_id = {image.image_id: image for image in image_set}
    base = images_mod.base_specs()[0].base_id
    return [by_id[f"{base}@{r}"] for r in (0, 90, 180, 270)] + [by_id["nt-1@0"]]


def test_quote_matches_the_spec_estimate() -> None:
    q = e_group.quote(models=("qwen/qwen4-max",), stop_line=None)
    entry = q["models"]["qwen/qwen4-max"]
    assert entry["calls"] == {"expected": 136 + 99 * 3, "worst": 136 * 4}
    assert 6.0 <= q["expected_total_yuan"] <= 8.0  # 设计 §6.2:qwen4-max ≈ ¥7
    assert q["quote_id"] == e_group.quote(models=("qwen/qwen4-max",), stop_line=None)["quote_id"]


def test_quote_refuses_a_model_without_a_rate_card_row() -> None:
    with pytest.raises(b301_plan.PlanError):
        e_group.quote(models=("deepseek/deepseek-flash",), stop_line=None)


def _fake_factory(rotation_of: dict[str, int], no_text: set[str]) -> Any:
    async def build(model: str, resolver: Any) -> Any:
        del model

        async def call(*, messages: Sequence[BaseMessage], tools: Any) -> AIMessage:
            del tools
            content = messages[-1].content
            assert isinstance(content, list)
            block = content[1]
            assert isinstance(block, dict)
            ref, degrees = str(block["ref"]), int(block["view"]["degrees"])
            if ref in no_text:
                reply = "none"
            else:
                reply = "yes" if degrees == rotation_of[ref] else "no"
            return AIMessage(content=reply, usage_metadata=_USAGE)  # type: ignore[arg-type]

        return call

    return build


async def test_run_drives_the_real_two_steps_without_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        model_catalog.MODEL_CATALOG,
        "qwen",
        tuple(
            e.model_copy(update={"orientation_verified": False}) if e.name == "qwen4-max" else e
            for e in model_catalog.MODEL_CATALOG["qwen"]
        ),
    )
    images = _subset()
    rotation_of = {e_group._ref(i): i.rotation for i in images}
    no_text = {e_group._ref(i) for i in images if i.image_class == images_mod.NO_TEXT_CLASS}
    q = e_group.quote(models=("qwen/qwen4-max",), stop_line=None)
    items = e_group.plan_items(("qwen/qwen4-max",), images)
    results = tmp_path / "out.jsonl"
    image_views.UPRIGHT_VIEWS.clear()
    summary = await e_group.run_from_quote(
        q,
        models=("qwen/qwen4-max",),
        results=results,
        env_files=[],
        state_dir=tmp_path / "state",
        factory=_fake_factory(rotation_of, no_text),
        image_set=images,
        items=items,
    )
    assert summary.stopped_reason is None
    assert summary.done == len(images)
    records = b301_plan.read_records(results)
    got = {r["image_id"]: (r["conclusion"], r["degrees"]) for r in records}
    for image in images:
        if image.image_class == images_mod.NO_TEXT_CLASS:
            assert got[image.image_id] == ("no_text", 0)
        elif image.rotation == 0:
            assert got[image.image_id] == ("upright", 0)
        else:
            assert got[image.image_id] == ("rotated", image.rotation)
    # 体检只在本进程里临时当作核实,跑完恢复(开头把目录里的核实标记摘掉,才看得出恢复)。
    assert not model_catalog.orientation_judge_allowed("qwen", "qwen4-max")
    # 账本按计划落在状态目录里(不能删)。
    assert (tmp_path / "state" / f"{q['plan_id']}.spend.jsonl").exists()
    # 再跑一次:已完成的条目跳过,不再花钱。
    again = await e_group.run_from_quote(
        q,
        models=("qwen/qwen4-max",),
        results=results,
        env_files=[],
        state_dir=tmp_path / "state",
        factory=_fake_factory(rotation_of, no_text),
        image_set=images,
        items=items,
    )
    assert (again.done, again.skipped) == (0, len(images))


def _record(image_id: str, rotation: int, conclusion: str, degrees: int = 0) -> dict[str, Any]:
    calls = [{"outcome": "ok", "reply": "no", "output_tokens": 2}]
    return {
        "model": "qwen/qwen4-max",
        "image_id": image_id,
        "rotation": rotation,
        "conclusion": conclusion,
        "degrees": degrees,
        "duration_s": 3.0,
        "calls": calls,
    }


def _full(**overrides: tuple[str, int]) -> list[dict[str, Any]]:
    rows = [_record(f"u{i}", 0, "upright") for i in range(37)]
    rows += [_record(f"r{i}", 90, "rotated", 90) for i in range(99)]
    for image_id, (conclusion, degrees) in overrides.items():
        for row in rows:
            if row["image_id"] == image_id:
                row.update(conclusion=conclusion, degrees=degrees)
    return rows


def test_report_passes_a_clean_run() -> None:
    assert e_group.report(_full())["qwen/qwen4-max"]["E_pass"] is True


def test_report_fails_on_one_upright_image_turned() -> None:
    result = e_group.report(_full(u3=("rotated", 90)))["qwen/qwen4-max"]
    assert result["S3_turned"] == ["u3"]
    assert result["E_pass"] is False


def test_report_fails_on_one_wrong_angle() -> None:
    result = e_group.report(_full(r5=("rotated", 270)))["qwen/qwen4-max"]
    assert result["W_wrong_angle"] == ["r5"]
    assert result["E_pass"] is False


def test_report_allows_three_undetermined_upright_but_not_four() -> None:
    three = {f"u{i}": ("undetermined", 0) for i in range(3)}
    four = {f"u{i}": ("undetermined", 0) for i in range(4)}
    assert e_group.report(_full(**three))["qwen/qwen4-max"]["E_pass"] is True
    assert e_group.report(_full(**four))["qwen/qwen4-max"]["E_pass"] is False


def test_report_fails_when_too_many_calls_go_unanswered() -> None:
    rows = _full()
    for row in rows[:10]:
        row["calls"] = [{"outcome": "timeout"}]
    result = e_group.report(rows)["qwen/qwen4-max"]
    assert result["H_timeout_or_off_topic"]["bad"] == 10
    assert result["E_pass"] is False


async def test_run_stops_at_the_stop_line_without_recording_a_fake_answer(tmp_path: Path) -> None:
    """止损:过线之后不再发调用;``SpendStopError`` 不是 ``Exception``,问方向那一层的兜底接不住,
    那一条作废(不写结果)—— 不会被当成「没答成」写进结果、算进判线。"""
    images = _subset()
    rotation_of = {e_group._ref(i): i.rotation for i in images}
    no_text = {e_group._ref(i) for i in images if i.image_class == images_mod.NO_TEXT_CLASS}
    q = dict(e_group.quote(models=("qwen/qwen4-max",), stop_line=None))
    q["stop_line_yuan"] = 0.03  # 两三次调用就到线
    results = tmp_path / "out.jsonl"
    image_views.UPRIGHT_VIEWS.clear()
    summary = await e_group.run_from_quote(
        q,
        models=("qwen/qwen4-max",),
        results=results,
        env_files=[],
        state_dir=tmp_path / "state",
        factory=_fake_factory(rotation_of, no_text),
        image_set=images,
        items=e_group.plan_items(("qwen/qwen4-max",), images),
    )
    assert summary.stopped_reason is not None
    assert "stop line" in summary.stopped_reason
    assert summary.done + summary.discarded <= len(images)
    assert summary.discarded >= 1
    for record in b301_plan.read_records(results):
        assert all(call.get("outcome") == "ok" for call in record["calls"])
        assert record["conclusion"] != "unjudged"


def test_a_model_without_a_per_image_budget_is_refused() -> None:
    """单图预算是核实的前提:没有的模型不报价、不体检(复审 L4)。"""
    with pytest.raises(b301_plan.PlanError, match="max_image_pixels"):
        e_group.quote(models=("qwen/qwen3.8-max",), stop_line=None)
