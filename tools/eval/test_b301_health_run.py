"""B-301 PR 2 —— 真 ``AskImageTool`` + 按像素回答的假看图模型:转正读对、对照读错、快看路由、续跑、
止损(含第 2 步 gather 里的止损)、续跑时止损线从账本起算、§8.1 厂商出错不崩、独占锁。全部离线。"""

from __future__ import annotations

import fcntl
import io
import json
import math
import sys
from collections.abc import Sequence
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
import b301_report as report_mod
import b301_spend as spend

from orchestrator.multimodal import DispatchingImageResolver
from orchestrator.tools.image_orientation import ORIENTATION_QUESTION, ROTATED_NOTE

IMAGES = images_mod.build_set()
MODEL = health.DEFAULT_MODELS[1]
PROBE = health.PROBE_MODELS[0]
PRICES = {
    MODEL: health.Price(6.0, 30.0, 1.2, "test"),
    PROBE: health.Price(2.0, 7.0, 0.57, "test"),
}
USAGE = {"input_tokens": 1200, "output_tokens": 3, "total_tokens": 1203}
PER_CALL = (1200 * 6 + 3 * 30) / 1e6
WRONG = "看不清"
SUBSET = ("ui-2", "ds-1", "vt-2", "rc-3")


def _thumb(data: bytes) -> np.ndarray:
    image = Image.open(io.BytesIO(data)).convert("L")
    image.thumbnail((48, 48))
    canvas = np.zeros((48, 48), dtype=np.float32)
    arr = np.asarray(image, dtype=np.float32)
    canvas[: arr.shape[0], : arr.shape[1]] = arr
    return canvas


LIBRARY = [(image, _thumb(image.data)) for image in IMAGES]


class _PixelModel:
    """假看图模型:把收到的图与图集里每张图比缩略图,认出「这是哪张底图、转了多少」。
    正着的才答 yes、才读得出关键字段;无文字图答 none。``upright_says_no`` 里的底图正放也答 no、
    在「正图顺时针转 90 度」那张副本上答 yes(用来造「正图被转歪」)。``fail_on`` = 第几次调用
    抛厂商错误(从 1 数)。"""

    def __init__(
        self,
        resolver: DispatchingImageResolver,
        upright_says_no: frozenset[str] = frozenset(),
        fail_on: int | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.resolver = resolver
        self.upright_says_no = upright_says_no
        self.fail_on = fail_on
        self.usage = usage or USAGE
        self.calls = 0

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            msg = "HTTP 400 from the vendor"
            raise ValueError(msg)
        question, ref = runner._question_and_ref(messages)
        thumb = _thumb((await self.resolver.resolve(ref)).data)
        seen = min(LIBRARY, key=lambda pair: float(np.abs(pair[1] - thumb).mean()))[0]
        upright = seen.rotation == 0 and seen.base_id not in self.upright_says_no
        if question == ORIENTATION_QUESTION:
            reply = "none" if seen.key is None else ("yes" if upright else "no")
            # 正图顺时针转 90 度 = 存成「要再顺时针转 270 度才正」的那张。
            if seen.base_id in self.upright_says_no and seen.rotation == 270:
                reply = "yes"
        else:
            reply = seen.key if (seen.rotation == 0 and seen.key) else WRONG
        return AIMessage(content=reply, usage_metadata=dict(self.usage))


class _Unused:
    calls = 0

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls += 1
        msg = "this route must not be used"
        raise AssertionError(msg)


class _Factory:
    """记下建出来的假模型,测试好数调用次数。``quick=True``:假模型挂在快看路由上,深入看不许用。"""

    def __init__(self, *, quick: bool = False, **kw: Any) -> None:
        self.quick, self.kw = quick, kw
        self.models: list[_PixelModel] = []
        self.unused: list[_Unused] = []

    async def __call__(
        self, model: str, provider: str, resolver: DispatchingImageResolver
    ) -> runner.Callers:
        fake = _PixelModel(resolver, **self.kw)
        self.models.append(fake)
        if not self.quick:
            return runner.Callers(vl=fake)
        unused = _Unused()
        self.unused.append(unused)
        return runner.Callers(vl=unused, quick=fake)

    @property
    def calls(self) -> int:
        return sum(m.calls for m in self.models)


def _items(model: str = MODEL) -> list[health.Item]:
    wanted = [i for i in IMAGES if i.base_id in SUBSET or i.image_id == "nt-1@0"]
    on = [health.Item(model, "on", i.image_id) for i in wanted]
    control = [health.Item(model, "control", i.image_id) for i in wanted if not i.upright]
    return [*on, *control]


async def _run(
    tmp_path: Path,
    items: Sequence[health.Item],
    factory: _Factory,
    *,
    stop_line: float = 100.0,
) -> runner.RunSummary:
    ledger = spend.SpendLedger(spend.ledger_path(tmp_path / "results.jsonl"), "p1")
    return await runner.run_items(
        items,
        image_set=IMAGES,
        guard=spend.SpendGuard(stop_line=stop_line, prices=PRICES, ledger=ledger),
        factory=factory,
        results=tmp_path / "results.jsonl",
        stamp={"plan_id": "p1", "quote_id": "q1"},
        workdir=tmp_path / "nas",
    )


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    return health.read_records(tmp_path / "results.jsonl")


def _ledger(tmp_path: Path) -> list[dict[str, Any]]:
    return spend.read_ledger(spend.ledger_path(tmp_path / "results.jsonl"))


@pytest.mark.parametrize("quick", [False, True], ids=["vl-route", "quick-route"])
async def test_the_real_tool_turns_each_image_and_reads_it_while_the_control_reads_wrong(
    tmp_path: Path, quick: bool
) -> None:
    factory = _Factory(quick=quick)
    summary = await _run(tmp_path, _items(), factory)

    assert summary.stopped_reason is None and summary.done == 17 + 12
    records = {(r["mode"], r["image_id"]): r for r in _records(tmp_path)}
    for image in (i for i in IMAGES if i.base_id in SUBSET):
        on = records[("on", image.image_id)]
        assert on["orientation"]["degrees"] == image.rotation, image.image_id
        assert report_mod.field_matches_strict(image.key or "", on["answer"]), on["answer"]
        if image.rotation:
            assert ROTATED_NOTE.format(degrees=image.rotation) in on["content"]
            control = records[("control", image.image_id)]["answer"]
            assert not report_mod.field_matches(image.key or "", control)
            turns = [c["turn"] for c in on["calls"] if c["kind"] == "orientation"]
            assert turns[0] == 0  # 第 1 步问 0 度候选,第 2 步三个角度
            assert sorted(turns[1:]) == [90, 180, 270]
    assert records[("on", "nt-1@0")]["orientation"]["conclusion"] == "no_text"
    assert spend.ledger_total(_ledger(tmp_path)) == pytest.approx(factory.calls * PER_CALL)
    if quick:  # 快看路由(关思考)接住了全部调用,深入看那条一次没用
        assert sum(u.calls for u in factory.unused) == 0 and factory.calls > 0


async def test_a_second_run_resumes_and_skips_what_is_done(tmp_path: Path) -> None:
    items = _items()
    await _run(tmp_path, items[:5], _Factory())

    summary = await _run(tmp_path, items, _Factory())

    assert summary.skipped == 5 and summary.done == len(items) - 5
    keys = [f"{r['model']}|{r['mode']}|{r['image_id']}" for r in _records(tmp_path)]
    assert sorted(keys) == sorted(item.key for item in items)


async def test_crossing_the_stop_line_stops_sending(tmp_path: Path) -> None:
    """每次调用 ¥0.132(接近单次上限 ¥0.168,在飞的预留不会把线压得太早)。不止损的话这 29 条
    要发约 80 次调用、约 ¥10.6;止损线 ¥2 ≈ 15 次的钱,之后最多再有在飞的 6 次。"""
    big = {"input_tokens": 7000, "output_tokens": 3000, "total_tokens": 10000}
    per_call = (7000 * 6 + 3000 * 30) / 1e6
    factory = _Factory(usage=big)
    summary = await _run(tmp_path, _items(), factory, stop_line=2.0)

    assert summary.stopped_reason is not None and "stop line" in summary.stopped_reason
    assert factory.calls <= math.ceil(2.0 / per_call) + 6
    assert summary.spent_yuan <= 2.0 + 6 * health.per_call_cap_yuan(PRICES[MODEL])
    rows = _records(tmp_path)
    assert 0 < len(rows) < len(_items())
    assert all(row["status"] == "ok" and row["calls"] for row in rows)


async def test_a_stop_inside_the_step_2_gather_discards_the_item_cleanly(tmp_path: Path) -> None:
    """第 1 步那次调用的钱就越过了线:第 2 步三个并行调用在发出前全被拦下,这一条作废、没有
    记录、账上只有第 1 步那一次。"""
    factory = _Factory()
    summary = await _run(tmp_path, [health.Item(MODEL, "on", "ui-2@90")], factory, stop_line=0.007)

    assert summary.stopped_reason is not None and "stop line" in summary.stopped_reason
    assert summary.discarded == 1 and _records(tmp_path) == []
    assert factory.calls == 1
    assert spend.ledger_total(_ledger(tmp_path)) == pytest.approx(PER_CALL)


def _quote() -> dict[str, Any]:
    return health.quote(models=(MODEL,), probe_models=(), stop_line=10.0, accept_documented=False)


async def _run_from_quote(tmp_path: Path, factory: _Factory) -> runner.RunSummary:
    return await runner.run_from_quote(
        _quote(),
        models=(MODEL,),
        probe_models=(),
        results=tmp_path / "results.jsonl",
        env_files=[],
        factory=factory,
        items=_items(),
    )


async def test_after_an_error_the_resumed_run_starts_from_everything_charged(
    tmp_path: Path,
) -> None:
    """厂商出错 → 停;作废的条目、出错那次(按上限)、别的路跑完又被作废的,钱都在账上。续跑的
    止损线从账本合计起算,而不是从结果文件里跑完的条目算。"""
    first = await _run_from_quote(tmp_path, _Factory(fail_on=12))

    assert first.stopped_reason is not None and "unknown status" in first.stopped_reason
    charged = spend.ledger_total(_ledger(tmp_path))
    assert first.spent_yuan == pytest.approx(charged)
    in_records = sum(float(r["cost_yuan"]) for r in _records(tmp_path))
    assert charged > in_records  # 出错那次的上限 + 作废条目的钱不在结果文件里

    second = await _run_from_quote(tmp_path, _Factory())

    assert second.spent_at_start_yuan == pytest.approx(charged)
    assert second.stopped_reason is None
    keys = sorted(f"{r['model']}|{r['mode']}|{r['image_id']}" for r in _records(tmp_path))
    assert keys == sorted(item.key for item in _items())
    assert (
        report_mod.report(_records(tmp_path), IMAGES, _ledger(tmp_path))["health"][0]["error_stops"]
        == 1
    )


async def test_results_or_ledger_of_another_plan_are_refused(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    other = {"plan_id": "other", "model": "m", "mode": "on", "image_id": "x"}
    results.write_text(json.dumps(other) + "\n")
    with pytest.raises(runner.RunRefusedError, match="another plan"):
        await _run_from_quote(tmp_path, _Factory())

    results.unlink()
    spend.SpendLedger(spend.ledger_path(results), "other").append(
        {"call_id": "a", "model": MODEL, "cost_yuan": 0.1, "reason": "ok"}
    )
    with pytest.raises(runner.RunRefusedError, match="another plan"):
        await _run_from_quote(tmp_path, _Factory())


async def test_a_concurrent_run_on_the_same_results_file_is_refused(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    with spend.lock_path(results).open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        factory = _Factory()
        with pytest.raises(runner.RunRefusedError, match="another run"):
            await _run_from_quote(tmp_path, factory)
    assert factory.calls == 0 and not results.exists()


async def test_a_vendor_error_in_the_probe_stops_the_run_without_crashing(tmp_path: Path) -> None:
    items = [health.Item(PROBE, "probe", image_id) for image_id in ("ui-1@0", "ui-1@90", "nt-1@0")]
    factory = _Factory(fail_on=2)

    summary = await _run(tmp_path, items, factory)

    assert summary.stopped_reason is not None and "unknown status" in summary.stopped_reason
    rows = _records(tmp_path)
    assert len(rows) <= 1  # 两路并行:出错之前已跑完的那一条可以留下,出错的那条作废
    assert all(r["probe_answer"] in ("yes", "no") for r in rows)
    assert spend.error_stops(_ledger(tmp_path)) == {PROBE: 1}


async def test_the_probe_asks_the_main_model_once_per_image(tmp_path: Path) -> None:
    items = [health.Item(PROBE, "probe", image_id) for image_id in ("ui-1@0", "ui-1@90", "nt-1@0")]

    summary = await _run(tmp_path, items, _Factory())

    assert summary.done == 3
    answers = {r["image_id"]: r["probe_answer"] for r in _records(tmp_path)}
    assert answers == {"ui-1@0": "yes", "ui-1@90": "no", "nt-1@0": "none"}


async def test_a_turned_upright_image_shows_up_in_the_report_as_a_failure(tmp_path: Path) -> None:
    """ds-1 正放时假模型答 no、在 90 度那张上答 yes:工具把正图转歪 —— L1' 必须抓到它。"""
    items = [health.Item(MODEL, "on", "ds-1@0")]
    await _run(tmp_path, items, _Factory(upright_says_no=frozenset({"ds-1"})))

    rows = _records(tmp_path)
    assert report_mod.orientation_of(rows[0])[:2] == ("rotated", 90)
    result = report_mod.model_report(MODEL, rows, IMAGES)
    assert result["L1'"]["turned"] == ["ds-1@0"]
    assert result["L1'"]["pass"] is False
