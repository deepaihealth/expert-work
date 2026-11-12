"""B-301 PR 2 —— 真 ``AskImageTool`` + 按像素回答的假看图模型:转正读对、对照读错、快看路由、续跑、
止损(含第 2 步 gather 里的止损)、续跑时止损线从账本起算、§8.1 厂商出错不崩、独占锁。全部离线。"""

from __future__ import annotations

import asyncio
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
        delay_s: float = 0.0,
    ) -> None:
        self.resolver = resolver
        self.upright_says_no = upright_says_no
        self.fail_on = fail_on
        self.usage = usage or USAGE
        self.delay_s = delay_s
        self.calls = 0

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
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


def _state(tmp_path: Path) -> Path:
    return tmp_path / "state"


async def _run(
    tmp_path: Path,
    items: Sequence[health.Item],
    factory: _Factory,
    *,
    stop_line: float = 100.0,
) -> runner.RunSummary:
    ledger = spend.SpendLedger(spend.ledger_path(_state(tmp_path), "p1"), "p1")
    return await runner.run_items(
        items,
        image_set=IMAGES,
        guard=spend.SpendGuard(stop_line=stop_line, prices=PRICES, ledger=ledger),
        factory=factory,
        results=tmp_path / "results.jsonl",
        stamp={"plan_id": "p1", "quote_id": "q1"},
        workdir=tmp_path / "nas",
    )


def _records(tmp_path: Path, results: str = "results.jsonl") -> list[dict[str, Any]]:
    return health.read_records(tmp_path / results)


def _ledger(tmp_path: Path, plan_id: str = "p1") -> list[dict[str, Any]]:
    path = spend.ledger_path(_state(tmp_path), plan_id)
    return spend.read_ledger(path, broken_line_cost=health.per_call_cap_yuan(PRICES[MODEL]))


def _numeric(image: images_mod.SetImage) -> bool:
    return image.kind in report_mod.NUMERIC_KINDS


@pytest.mark.parametrize("quick", [False, True], ids=["vl-route", "quick-route"])
async def test_the_real_tool_turns_each_image_and_reads_it_while_the_control_reads_wrong(
    tmp_path: Path, quick: bool
) -> None:
    factory = _Factory(quick=quick)
    summary = await _run(tmp_path, _items(), factory)

    assert summary.stopped_reason is None and summary.done == 17 + 12
    records = {(r["mode"], r["image_id"]): r for r in _records(tmp_path)}
    for image in (i for i in IMAGES if i.base_id in SUBSET):
        key, numeric = image.key or "", _numeric(image)
        on = records[("on", image.image_id)]
        assert on["orientation"]["degrees"] == image.rotation, image.image_id
        assert report_mod.field_matches_strict(key, on["answer"], numeric=numeric), on["answer"]
        if image.rotation:
            assert ROTATED_NOTE.format(degrees=image.rotation) in on["content"]
            control = records[("control", image.image_id)]["answer"]
            assert not report_mod.field_matches(key, control, numeric=numeric)
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


QUOTE = health.quote(models=(MODEL,), probe_models=(), stop_line=10.0, accept_documented=False)
PLAN = str(QUOTE["plan_id"])


async def _run_from_quote(
    tmp_path: Path,
    factory: _Factory,
    *,
    results: str = "results.jsonl",
    items: Sequence[health.Item] | None = None,
) -> runner.RunSummary:
    return await runner.run_from_quote(
        QUOTE,
        models=(MODEL,),
        probe_models=(),
        results=tmp_path / results,
        env_files=[],
        state_dir=_state(tmp_path),
        factory=factory,
        items=_items() if items is None else items,
    )


async def test_after_an_error_the_resumed_run_starts_from_everything_charged(
    tmp_path: Path,
) -> None:
    """厂商出错 → 停;作废的条目、出错那次(按上限)、别的路跑完又被作废的,钱都在账上。续跑的
    止损线从账本合计起算,而不是从结果文件里跑完的条目算。"""
    first = await _run_from_quote(tmp_path, _Factory(fail_on=12))

    assert first.stopped_reason is not None and "unknown status" in first.stopped_reason
    charged = spend.ledger_total(_ledger(tmp_path, PLAN))
    assert first.spent_yuan == pytest.approx(charged)
    in_records = sum(float(r["cost_yuan"]) for r in _records(tmp_path))
    assert charged > in_records  # 出错那次的上限 + 作废条目的钱不在结果文件里

    second = await _run_from_quote(tmp_path, _Factory())

    assert second.spent_at_start_yuan == pytest.approx(charged)
    assert second.stopped_reason is None
    keys = sorted(f"{r['model']}|{r['mode']}|{r['image_id']}" for r in _records(tmp_path))
    assert keys == sorted(item.key for item in _items())
    result = report_mod.report(_records(tmp_path), IMAGES, _ledger(tmp_path, PLAN))
    assert result["health"][0]["error_stops"] == 1


async def test_a_different_results_path_still_resumes_from_the_plan_budget(
    tmp_path: Path,
) -> None:
    """同一个计划换了结果路径(手误、新目录):止损线照样从这个计划已花的钱起算,不从 ¥0。"""
    first = await _run_from_quote(tmp_path, _Factory(fail_on=12), results="a/run.jsonl")
    charged = spend.ledger_total(_ledger(tmp_path, PLAN))
    assert first.stopped_reason is not None and charged > 0

    second = await _run_from_quote(tmp_path, _Factory(), results="b/sub/run.jsonl")

    assert second.spent_at_start_yuan == pytest.approx(charged)
    assert second.skipped == 0  # 新的结果文件里还没有条目,全部重跑
    assert second.spent_yuan > charged


async def test_a_concurrent_run_of_the_same_plan_is_refused_on_any_results_path(
    tmp_path: Path,
) -> None:
    _state(tmp_path).mkdir()
    with spend.lock_path(_state(tmp_path), PLAN).open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        factory = _Factory()
        for results in ("results.jsonl", "elsewhere/other.jsonl"):
            with pytest.raises(runner.RunRefusedError, match="another run"):
                await _run_from_quote(tmp_path, factory, results=results)
    assert factory.calls == 0 and not (tmp_path / "results.jsonl").exists()


async def test_a_failed_ledger_write_discards_the_item_and_the_resume_redoes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """账本写不进去(第 3 次写:第 2 次调用的预留):停、这一条不留记录(否则会被当成跑完、续跑
    跳过),第 2 次调用没发;续跑重做这一条。"""
    real_append = spend.SpendLedger.append
    writes = {"n": 0}

    def flaky(self: spend.SpendLedger, row: Any) -> None:
        writes["n"] += 1
        if writes["n"] == 3:
            raise OSError(28, "No space left on device")
        real_append(self, row)

    item = [health.Item(MODEL, "on", "ui-2@90")]
    monkeypatch.setattr(spend.SpendLedger, "append", flaky)
    factory = _Factory()
    first = await _run_from_quote(tmp_path, factory, items=item)

    assert first.stopped_reason is not None and "ledger" in first.stopped_reason
    assert _records(tmp_path) == [] and factory.calls == 1
    assert spend.ledger_total(_ledger(tmp_path, PLAN)) == pytest.approx(PER_CALL)

    monkeypatch.setattr(spend.SpendLedger, "append", real_append)
    second = await _run_from_quote(tmp_path, _Factory(), items=item)

    assert second.spent_at_start_yuan == pytest.approx(PER_CALL)
    rows = _records(tmp_path)
    assert [r["image_id"] for r in rows] == ["ui-2@90"]
    assert rows[0]["orientation"]["degrees"] == 90


async def test_results_or_ledger_of_another_plan_are_refused(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    other = {"plan_id": "other", "model": "m", "mode": "on", "image_id": "x"}
    results.write_text(json.dumps(other) + "\n")
    with pytest.raises(runner.RunRefusedError, match="another plan"):
        await _run_from_quote(tmp_path, _Factory())

    results.unlink()
    spend.SpendLedger(spend.ledger_path(_state(tmp_path), PLAN), "other").append(
        {"call_id": "a", "model": MODEL, "cost_yuan": 0.1, "reason": "ok"}
    )
    with pytest.raises(runner.RunRefusedError, match="another plan"):
        await _run_from_quote(tmp_path, _Factory())


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


async def test_records_without_their_ledger_are_refused_naming_the_state_dir(
    tmp_path: Path,
) -> None:
    """跑过一段(结果里有花了钱的条目),再用另一个状态目录跑同一个计划:那里没有账本 —— 拒跑,
    不从 ¥0 重新起算;错误里写明用的状态目录。"""
    await _run_from_quote(tmp_path, _Factory(), items=_items()[:3])
    assert _records(tmp_path)

    factory = _Factory()
    wrong = tmp_path / "other-state"
    with pytest.raises(runner.RunRefusedError, match="no spend ledger") as refused:
        await runner.run_from_quote(
            QUOTE,
            models=(MODEL,),
            probe_models=(),
            results=tmp_path / "results.jsonl",
            env_files=[],
            state_dir=wrong,
            factory=factory,
            items=_items(),
        )
    assert str(wrong) in str(refused.value) and factory.calls == 0


async def test_a_ledger_holding_less_than_the_records_is_refused(tmp_path: Path) -> None:
    await _run_from_quote(tmp_path, _Factory(), items=_items()[:3])
    ledger = spend.ledger_path(_state(tmp_path), PLAN)
    small = {"plan_id": PLAN, "call_id": "x", "model": MODEL, "cost_yuan": 0.001, "reason": "ok"}
    ledger.write_text(json.dumps(small) + "\n")  # 账上只剩 ¥0.001,结果里记的远多于它

    factory = _Factory()
    with pytest.raises(runner.RunRefusedError, match="less than") as refused:
        await _run_from_quote(tmp_path, factory)
    assert str(_state(tmp_path)) in str(refused.value) and factory.calls == 0


async def test_a_failed_settle_write_in_the_cancel_path_discards_the_item_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8.1 那次调用超时(``asyncio.timeout`` 取消它),计量层在取消路径里写结算行时账本写不进去:
    ``SpendStopError`` 取代了 ``CancelledError`` —— 这一条作废、不留记录,这次调用按预留(单次上限)
    留在账上。"""
    real_append = spend.SpendLedger.append
    writes = {"n": 0}

    def settle_fails(self: spend.SpendLedger, row: Any) -> None:
        writes["n"] += 1
        if writes["n"] == 2:  # 第 1 行是预留,第 2 行是取消路径里的结算
            raise OSError(5, "Input/output error")
        real_append(self, row)

    monkeypatch.setattr(spend.SpendLedger, "append", settle_fails)
    monkeypatch.setattr(runner, "ORIENTATION_TIMEOUT_S", 0.05)
    factory = _Factory(delay_s=5.0)

    summary = await _run(tmp_path, [health.Item(PROBE, "probe", "ui-1@0")], factory)

    assert summary.stopped_reason is not None and "ledger" in summary.stopped_reason
    assert summary.discarded == 1 and _records(tmp_path) == []
    rows = _ledger(tmp_path)
    assert [row["reason"] for row in rows] == ["reserve"]
    assert spend.ledger_total(rows) == pytest.approx(health.per_call_cap_yuan(PRICES[PROBE]))
