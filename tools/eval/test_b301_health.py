"""B-301 PR 2 —— 体检工具:报价算术与单价核对、止损、真 ``AskImageTool`` + 假看图模型的整条链路、
续跑、判分与结论。全部离线:看图模型是按像素回答的替身,不发任何付费调用。"""

from __future__ import annotations

import io
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from langchain_core.messages import AIMessage, BaseMessage
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b301_health as health
import b301_health_run as runner
import b301_images as images_mod

from orchestrator.multimodal import DispatchingImageResolver
from orchestrator.tools.image_orientation import ORIENTATION_QUESTION, ROTATED_NOTE

IMAGES = images_mod.build_set()
BY_ID = {image.image_id: image for image in IMAGES}
DOUBAO_OLD, DOUBAO_NEW = health.DEFAULT_MODELS
WRONG = "看不清"


# --------------------------------------------------------------------------- 报价与单价


def test_prices_come_from_the_rate_card_migration_and_the_tier_rule() -> None:
    new = health.price_of(DOUBAO_NEW, accept_documented=False)
    old = health.price_of(DOUBAO_OLD, accept_documented=False)

    assert (new.input, new.output) == (6.0, 30.0)
    assert "0187" in new.source
    assert (old.input, old.output) == (6.0, 30.0)
    assert old.source.startswith("tier price")


def test_a_model_without_a_validated_price_is_refused() -> None:
    with pytest.raises(health.PlanError, match="no validated price"):
        health.price_of("doubao-seed-9-pro-999999", accept_documented=False)
    with pytest.raises(health.PlanError, match="accept-documented-prices"):
        health.price_of("glm-5.3-flashx", accept_documented=False)
    rows = {"qwen3.8-max-0902": health.Price(12.0, 36.0, 1.5, "x")}  # 260915 那一行没了
    with pytest.raises(health.PlanError, match="tier price source"):
        health.price_of(DOUBAO_OLD, accept_documented=False, rows=rows)


def test_quote_counts_tokens_and_money_for_the_two_doubao_models() -> None:
    q = health.quote(
        models=health.DEFAULT_MODELS, probe_models=(), stop_line=None, accept_documented=False
    )

    for model in health.DEFAULT_MODELS:
        entry = q["models"][model]
        # 问方向:136 张各 1 次 + 转过的 99 张各 3 次;读:开着 136 + 对照 99。
        assert entry["calls"] == {
            "orientation": 433,
            "orientation_worst": 544,
            "read": 235,
            "total": 668,
        }
        orient_in, read_in = 1361 + 150, 1361 + 150
        assert entry["tokens"] == {
            "input": 433 * orient_in + 235 * read_in,
            "output": 433 * 5 + 235 * 60,
        }
        expected = (entry["tokens"]["input"] * 6 + entry["tokens"]["output"] * 30) / 1e6
        assert entry["expected_yuan"] == round(expected, 2)
        assert entry["per_call_cap_yuan"] == round((8000 * 6 + 4000 * 30) / 1e6, 4)
    assert q["models"][DOUBAO_OLD]["image_tokens"]["source"].startswith("not measured")
    total = q["expected_total_yuan"]
    assert q["stop_line_yuan"] == math.ceil(max(q["worst_total_yuan"], total * 1.5))
    # 只有一家(豆包):在飞的最多 2 个 ask_image x 3 个并行调用,各按单次上限。
    assert q["hard_cap_yuan"] == round(q["stop_line_yuan"] + 6 * (8000 * 6 + 4000 * 30) / 1e6, 2)
    assert q["items"] == 2 * (136 + 99)


def test_quote_id_changes_with_the_stop_line_the_price_and_the_models() -> None:
    def qid(**kw: Any) -> str:
        args = {
            "models": health.DEFAULT_MODELS,
            "probe_models": (),
            "stop_line": 20.0,
            "accept_documented": False,
        }
        return str(health.quote(**{**args, **kw})["quote_id"])

    base = qid()
    assert qid() == base
    assert qid(stop_line=21.0) != base
    assert qid(models=(DOUBAO_NEW,)) != base
    cheaper = {**health.rate_card_rows(), DOUBAO_NEW: health.Price(5.0, 30.0, 1.2, "x")}
    assert qid(rows=cheaper) != base


def test_quote_refuses_a_stop_line_below_the_expected_spend() -> None:
    with pytest.raises(health.PlanError, match="below the expected"):
        health.quote(
            models=health.DEFAULT_MODELS, probe_models=(), stop_line=1.0, accept_documented=False
        )


def test_probe_needs_documented_prices_accepted_and_asks_70_images_once() -> None:
    with pytest.raises(health.PlanError):
        health.quote(
            models=(), probe_models=health.PROBE_MODELS, stop_line=None, accept_documented=False
        )
    q = health.quote(
        models=(), probe_models=health.PROBE_MODELS, stop_line=None, accept_documented=True
    )
    for model in health.PROBE_MODELS:
        assert q["models"][model]["calls"]["orientation"] == 70
        assert q["models"][model]["calls"]["read"] == 0


def test_run_refuses_a_quote_id_that_does_not_match(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = health.main(
        ["run", "--quote-id", "000000000000", "--results", str(tmp_path / "r.jsonl")]
    )

    assert code == 2
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "r.jsonl").exists()


# --------------------------------------------------------------------------- 止损


PRICES = {DOUBAO_NEW: health.Price(6.0, 30.0, 1.2, "test")}


def _usage(tokens_in: int = 1000, tokens_out: int = 10) -> dict[str, int]:
    return {
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }


class _Inner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[no-any-return]


def _msgs() -> list[BaseMessage]:
    from orchestrator.tools.vision import _messages

    return _messages("expert_work://image/x", ORIENTATION_QUESTION)


async def test_the_stop_line_refuses_the_next_call_before_it_is_sent() -> None:
    guard = runner.SpendGuard(stop_line=0.01, prices=PRICES)
    inner = _Inner(AIMessage(content="yes", usage_metadata=_usage(2000, 10)))
    caller = runner.MeteredCaller(inner, DOUBAO_NEW, guard)

    await caller(messages=_msgs(), tools=[])  # ¥0.0123,越过 ¥0.01
    with pytest.raises(runner.SpendStopError, match="stop line"):
        await caller(messages=_msgs(), tools=[])

    assert inner.calls == 1
    assert guard.spent == pytest.approx((2000 * 6 + 10 * 30) / 1e6)


async def test_an_unknown_provider_error_stops_everything() -> None:
    guard = runner.SpendGuard(stop_line=100.0, prices=PRICES)
    failing = runner.MeteredCaller(_Inner(ValueError("HTTP 400")), DOUBAO_NEW, guard)
    healthy = _Inner(AIMessage(content="yes", usage_metadata=_usage()))

    with pytest.raises(ValueError):
        await failing(messages=_msgs(), tools=[])
    with pytest.raises(runner.SpendStopError, match="unknown status"):
        await runner.MeteredCaller(healthy, DOUBAO_NEW, guard)(messages=_msgs(), tools=[])

    assert healthy.calls == 0
    assert guard.spent == pytest.approx(health.per_call_cap_yuan(PRICES[DOUBAO_NEW]))


async def test_a_response_without_usage_stops_and_is_charged_the_cap() -> None:
    guard = runner.SpendGuard(stop_line=100.0, prices=PRICES)
    caller = runner.MeteredCaller(_Inner(AIMessage(content="yes")), DOUBAO_NEW, guard)

    with pytest.raises(runner.SpendStopError, match="no usable token usage"):
        await caller(messages=_msgs(), tools=[])
    assert guard.spent == pytest.approx(health.per_call_cap_yuan(PRICES[DOUBAO_NEW]))


async def test_a_model_without_a_price_and_an_oversized_input_both_stop() -> None:
    unpriced = runner.SpendGuard(stop_line=100.0, prices={})
    with pytest.raises(runner.SpendStopError, match="no validated price"):
        await runner.MeteredCaller(_Inner(None), DOUBAO_NEW, unpriced)(messages=_msgs(), tools=[])

    guard = runner.SpendGuard(stop_line=100.0, prices=PRICES)
    big = _Inner(AIMessage(content="yes", usage_metadata=_usage(9000, 1)))
    with pytest.raises(runner.SpendStopError, match="input tokens"):
        await runner.MeteredCaller(big, DOUBAO_NEW, guard)(messages=_msgs(), tools=[])


async def test_a_timeout_is_charged_the_cap_but_does_not_stop() -> None:
    guard = runner.SpendGuard(stop_line=100.0, prices=PRICES)
    calls: list[dict[str, Any]] = []
    token = runner._CALLS.set(calls)
    try:
        with pytest.raises(TimeoutError):
            await runner.MeteredCaller(_Inner(TimeoutError()), DOUBAO_NEW, guard)(
                messages=_msgs(), tools=[]
            )
    finally:
        runner._CALLS.reset(token)

    assert guard.blocked is None
    assert calls[0]["outcome"] == "timeout" and calls[0]["answer"] == "timeout"
    assert guard.spent == pytest.approx(health.per_call_cap_yuan(PRICES[DOUBAO_NEW]))


def test_missing_credentials_name_the_variable_and_never_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for names in runner.KEY_VARIABLES.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    env = tmp_path / "eval.env"
    env.write_text("HELIX_EVAL_LLM_API_KEY=sk-test-secret-value\n", encoding="utf-8")

    assert runner.load_keys(["qwen"], env) == {"qwen": "sk-test-secret-value"}
    with pytest.raises(runner.RunRefusedError) as refused:
        runner.load_keys(["doubao", "qwen"], env)
    assert "EXPERT_WORK_EVAL_DOUBAO_API_KEY" in str(refused.value)
    assert "sk-test" not in str(refused.value)


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        (DOUBAO_OLD, "doubao"),
        (DOUBAO_NEW, "doubao"),
        (health.QWEN_MODEL, "qwen"),
        ("glm-5.3-flashx", "glm"),
    ],
)
async def test_the_real_routers_cap_output_and_turn_thinking_off_without_calling(
    model: str, provider: str, tmp_path: Path
) -> None:
    """硬上限的前提:每次调用的输出被 ``max_tokens`` 截住;快看关思考(与生产快看同一条)。
    只建路由,不发请求。"""
    from orchestrator.multimodal import InMemoryImageResolver, NasWorkspaceImageResolver

    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver(images={}), workspace=NasWorkspaceImageResolver(root=tmp_path)
    )
    callers = await runner.real_callers_factory({provider: "dummy-key"})(model, provider, resolver)

    assert callers.quick is not None
    adapter = callers.quick.providers[0].provider.inner  # type: ignore[attr-defined]
    assert health.MAX_OUTPUT_TOKENS in adapter.output_cap_payload.values()
    assert callers.quick.providers[0].provider.limiter.max_rate == runner.EVAL_RATE_LIMIT_RPM  # type: ignore[attr-defined]
    assert adapter.thinking_payload in (
        {"thinking": {"type": "disabled"}},
        {"enable_thinking": False},
        {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
    )


# ------------------------------------------------------------------ 整条链路(真工具 + 假模型)


def _thumb(data: bytes) -> np.ndarray:
    image = Image.open(io.BytesIO(data)).convert("L")
    image.thumbnail((48, 48))
    canvas = np.zeros((48, 48), dtype=np.float32)
    arr = np.asarray(image, dtype=np.float32)
    canvas[: arr.shape[0], : arr.shape[1]] = arr
    return canvas


class _PixelModel:
    """假看图模型:把收到的图与图集里每张图比缩略图,认出「这是哪张底图、转了多少」。
    正着的才答 yes、才读得出关键字段;无文字图答 none。``upright_says_no`` 里的底图正放也答 no、
    在「正图顺时针转 90 度」那张副本上答 yes(用来造「正图被转歪」)。"""

    def __init__(
        self, resolver: DispatchingImageResolver, upright_says_no: frozenset[str] = frozenset()
    ) -> None:
        self.resolver = resolver
        self.upright_says_no = upright_says_no
        self.library = [(image, _thumb(image.data)) for image in IMAGES]

    def _which(self, data: bytes) -> images_mod.SetImage:
        thumb = _thumb(data)
        return min(self.library, key=lambda pair: float(np.abs(pair[1] - thumb).mean()))[0]

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        question, ref = runner._question_and_ref(messages)
        seen = self._which((await self.resolver.resolve(ref)).data)
        upright = seen.rotation == 0 and seen.base_id not in self.upright_says_no
        if question == ORIENTATION_QUESTION:
            reply = "none" if seen.key is None else ("yes" if upright else "no")
            # 正图顺时针转 90 度 = 存成「要再顺时针转 270 度才正」的那张。
            if seen.base_id in self.upright_says_no and seen.rotation == 270:
                reply = "yes"
        else:
            reply = seen.key if (seen.rotation == 0 and seen.key) else WRONG
        return AIMessage(
            content=reply,
            usage_metadata={"input_tokens": 1200, "output_tokens": 3, "total_tokens": 1203},
        )


def _factory(**kw: Any) -> runner.CallerFactory:
    async def build(
        model: str, provider: str, resolver: DispatchingImageResolver
    ) -> runner.Callers:
        return runner.Callers(vl=_PixelModel(resolver, **kw))

    return build


SUBSET = ("ui-2", "ds-1", "vt-2", "rc-3")


def _items(model: str = DOUBAO_NEW) -> list[health.Item]:
    wanted = [i for i in IMAGES if i.base_id in SUBSET or i.image_id == "nt-1@0"]
    on = [health.Item(model, "on", i.image_id) for i in wanted]
    control = [health.Item(model, "control", i.image_id) for i in wanted if not i.upright]
    return [*on, *control]


async def _run(
    tmp_path: Path, items: Sequence[health.Item], *, stop_line: float = 100.0, **kw: Any
) -> runner.RunSummary:
    return await runner.run_items(
        items,
        image_set=IMAGES,
        guard=runner.SpendGuard(stop_line=stop_line, prices=PRICES),
        factory=_factory(**kw),
        results=tmp_path / "results.jsonl",
        stamp={"plan_id": "p1", "quote_id": "q1"},
        workdir=tmp_path / "nas",
    )


async def test_the_real_tool_turns_each_image_and_reads_it_while_the_control_reads_wrong(
    tmp_path: Path,
) -> None:
    summary = await _run(tmp_path, _items())

    assert summary.stopped_reason is None and summary.done == 17 + 12
    records = {
        (r["mode"], r["image_id"]): r for r in health.read_records(tmp_path / "results.jsonl")
    }
    for image in (i for i in IMAGES if i.base_id in SUBSET):
        on = records[("on", image.image_id)]
        assert on["orientation"]["degrees"] == image.rotation, image.image_id
        assert health.field_matches(image.key or "", on["answer"]), on["answer"]
        if image.rotation:
            assert ROTATED_NOTE.format(degrees=image.rotation) in on["content"]
            assert not health.field_matches(
                image.key or "", records[("control", image.image_id)]["answer"]
            )
            turns = [c["turn"] for c in on["calls"] if c["kind"] == "orientation"]
            assert turns[0] == 0  # 第 1 步问 0 度候选,第 2 步三个角度
            assert sorted(turns[1:]) == [90, 180, 270]
    assert records[("on", "nt-1@0")]["orientation"]["conclusion"] == "no_text"
    assert all(
        c["latency_s"] >= 0 and c["cost_yuan"] > 0 for r in records.values() for c in r["calls"]
    )


async def test_a_second_run_resumes_and_skips_what_is_done(tmp_path: Path) -> None:
    items = _items()
    await _run(tmp_path, items[:5])

    summary = await _run(tmp_path, items)

    assert summary.skipped == 5 and summary.done == len(items) - 5
    keys = [
        f"{r['model']}|{r['mode']}|{r['image_id']}"
        for r in health.read_records(tmp_path / "results.jsonl")
    ]
    assert sorted(keys) == sorted(item.key for item in items)


async def test_crossing_the_stop_line_stops_the_run_and_keeps_only_whole_records(
    tmp_path: Path,
) -> None:
    summary = await _run(tmp_path, _items(), stop_line=0.05)

    assert summary.stopped_reason is not None and "stop line" in summary.stopped_reason
    rows = health.read_records(tmp_path / "results.jsonl")
    assert 0 < len(rows) < len(_items())
    for row in rows:
        assert row["status"] == "ok" and row["calls"]
    # 硬上限:线 + 每家在飞的最多 2 x 3 次调用各按单次上限。
    assert summary.spent_yuan <= 0.05 + 6 * health.per_call_cap_yuan(PRICES[DOUBAO_NEW])


async def test_results_of_another_plan_are_refused(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps({"plan_id": "other", "model": "m", "mode": "on", "image_id": "x"}) + "\n"
    )
    q = health.quote(models=(DOUBAO_NEW,), probe_models=(), stop_line=None, accept_documented=False)

    with pytest.raises(runner.RunRefusedError, match="another plan"):
        await runner.run_from_quote(
            q,
            models=(DOUBAO_NEW,),
            probe_models=(),
            results=results,
            env_file=None,
            factory=_factory(),
        )


async def test_a_turned_upright_image_shows_up_in_the_report_as_a_failure(tmp_path: Path) -> None:
    """ds-1 正放时假模型答 no、在 90 度那张上答 yes:工具把正图转歪 —— L1' 必须抓到它。"""
    items = [health.Item(DOUBAO_NEW, "on", "ds-1@0")]
    await _run(tmp_path, items, upright_says_no=frozenset({"ds-1"}))

    rows = health.read_records(tmp_path / "results.jsonl")
    assert health.orientation_of(rows[0])[:2] == ("rotated", 90)
    result = health.model_report(DOUBAO_NEW, rows, IMAGES)
    assert result["L1'"]["turned"] == ["ds-1@0"]
    assert result["L1'"]["pass"] is False


# --------------------------------------------------------------------------- 判分与结论(手写的结果)


def _record(
    image: images_mod.SetImage,
    mode: str,
    *,
    degrees: int | None = None,
    answer: str | None = None,
    first: str = "yes",
    conclusion: str | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if degrees is None:
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
    if calls is None:
        calls = (
            []
            if mode == "control"
            else [
                {
                    "kind": "orientation",
                    "turn": 0,
                    "outcome": "ok",
                    "answer": "yes",
                    "latency_s": 1.0,
                    "cost_yuan": 0.01,
                }
            ]
        )
    if answer is None:
        answer = (image.key or "") if mode == "on" else WRONG
    return {
        "model": DOUBAO_NEW,
        "mode": mode,
        "image_id": image.image_id,
        "status": "ok",
        "orientation": orientation,
        "answer": answer,
        "calls": calls,
        "cost_yuan": 0.01,
    }


def _perfect() -> list[dict[str, Any]]:
    rows = [_record(image, "on") for image in IMAGES]
    rows += [_record(image, "control") for image in IMAGES if not image.upright]
    return rows


def test_a_perfect_run_passes_with_every_class_showing_a_gain() -> None:
    result = health.model_report(DOUBAO_NEW, _perfect(), IMAGES)

    assert result["verdict"] == "通过" and result["why"] == []
    assert result["L1"]["ok"] == 99 and result["L1"]["pass"]
    assert {c["result"] for c in result["L2"].values()} == {"有收益"}
    assert "通过" in health.render_report(health.report(_perfect(), IMAGES))


def test_missing_records_make_the_verdict_incomplete() -> None:
    result = health.model_report(DOUBAO_NEW, _perfect()[:-1], IMAGES)

    assert result["verdict"] == "不完整"


def test_more_than_three_undetermined_upright_images_fail_l1_prime() -> None:
    upright = [i for i in IMAGES if i.upright and i.key is not None][:4]
    rows = [
        r
        for r in _perfect()
        if not (r["mode"] == "on" and r["image_id"] in {i.image_id for i in upright})
    ]
    rows += [_record(i, "on", conclusion="undetermined", first="no") for i in upright]

    result = health.model_report(DOUBAO_NEW, rows, IMAGES)

    assert result["verdict"] == "不通过"
    assert len(result["L1'"]["undetermined"]) == 4 and result["L1'"]["first_step_no"] == 4


def test_l6_fails_above_five_percent_timeouts_or_off_topic() -> None:
    rows = _perfect()
    bad = {
        "kind": "orientation",
        "turn": 0,
        "outcome": "timeout",
        "answer": "timeout",
        "latency_s": 30.0,
    }
    for row in rows[:8]:  # 8 / 136 = 5.9 %
        row["calls"] = [bad]
    result = health.model_report(DOUBAO_NEW, rows, IMAGES)

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

    result = health.model_report(DOUBAO_NEW, rows, IMAGES)

    assert result["L2"]["chart"]["result"] == "变差"
    assert result["L2"]["table"]["result"] == "无收益"
    assert result["L2"]["label"]["result"].startswith("模型本来就读不好")
    assert result["L1"]["by_class"]["chart"] == 0 and result["L1"]["pass"] is False
    assert result["needs_user"] == ["L2「变差」:chart"]
    assert result["L1'"]["undetermined"] == []  # 那 9 张是转过的图
    assert result["verdict"] == "通过"


def test_orientation_is_derived_from_the_calls_when_the_tool_failed() -> None:
    calls = [
        {"kind": "orientation", "turn": 0, "outcome": "ok", "answer": "no"},
        {"kind": "orientation", "turn": 90, "outcome": "ok", "answer": "no"},
        {"kind": "orientation", "turn": 180, "outcome": "ok", "answer": "yes"},
        {"kind": "orientation", "turn": 270, "outcome": "ok", "answer": "no"},
        {"kind": "read", "turn": None, "outcome": "timeout"},
    ]
    assert health.orientation_of({"orientation": None, "calls": calls}) == ("rotated", 180, "no")
    assert health.orientation_of({"orientation": None, "calls": calls[:2]}) == (
        "not_asked",
        0,
        "no",
    )
    assert health.orientation_of({"orientation": None, "calls": []}) == ("not_asked", 0, None)


@pytest.mark.parametrize(
    ("key", "answer", "ok"),
    [
        ("69.1", "69.1", True),
        ("69.1", "读数是 69.1 kg。", True),
        ("69.1", "69.1kg", True),
        ("KX-450B", "KX-450BX", False),
        ("69.1", "169.1", False),
        ("69.1", "69.15", False),
        ("2026-03-17", "2026-03-17", True),
        ("2026-03-17", "2026年3月17日", False),
        ("Harbor_Guest_5G", "**Harbor_Guest_5G**", True),
        # 全角括号 / 逗号 / 弯引号写成转义(ruff 的模糊字符规则会把它们「修」成半角)。
        ("青禾便利店(南湖店)", "青禾便利店\uff08南湖店\uff09", True),
        ("每日两次,每次一片", "每日两次\uff0c每次一片。", True),
        ("Riverside Terminal", "\u201cRiverside Terminal\u201d", True),
        ("4:30 pm", "4:30 PM", True),
        ("Room 204", "Room 2048", False),
        ("128.46", "合计 128.46 元", True),
    ],
)
def test_field_matching(key: str, answer: str, ok: bool) -> None:
    assert health.field_matches(key, answer) is ok


def test_notes_added_by_the_tool_are_not_part_of_the_answer() -> None:
    content = "69.1\n\n" + ROTATED_NOTE.format(degrees=90)
    assert health.strip_notes(content) == "69.1"


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
    p = health.probe_report("glm-5.3-flashx", rows, IMAGES)

    assert len(images) == 70
    assert (p["upright_yes"], p["upright"], p["rotated_no"], p["rotated"], p["no_text_none"]) == (
        33,
        33,
        33,
        33,
        4,
    )
    assert p["unanswered"] == 0


def test_report_cli_runs_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results = tmp_path / "r.jsonl"
    results.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _perfect()), encoding="utf-8"
    )

    assert health.main(["report", "--results", str(results)]) == 0
    assert f"{DOUBAO_NEW}:通过" in capsys.readouterr().out
