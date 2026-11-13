"""B-300 评测脚本第四轮审查的修复:离线。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_live as live
import b300_phases as phases
import b300_runs as runs
import b300_spend as spend
import httpx
import pytest
from b301_plan import Price

PRICES = {"m": Price(1.0, 1.0, 1.0, "t")}
FLASH = {"glm-5.3-flash": Price(0.8, 2.8, 0.23, "t")}


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


# --------------------------------------------------------------------------- MEDIUM 1


@pytest.mark.parametrize(
    "dirs",
    [
        ("X", "DEFAULT"),  # 反方向:先在 X、再回默认目录
        ("X", "Y"),  # 两个都不是默认目录
    ],
)
def test_both_probes_are_refused_without_the_test_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirs: tuple[str, str]
) -> None:
    """一个报价 ¥350 + 换目录再 ¥300 = ¥650 的两种走法:只有一个状态目录(默认的),
    ``--state-dir`` 只有测试开关打开时才收。"""
    monkeypatch.delenv(spend.TEST_STATE_DIR_ENV, raising=False)
    first = tmp_path / dirs[0]
    with pytest.raises(spend.StateDirError, match="default state dir"):
        spend.Guard.resume(stop_line=400.0, prices=PRICES, state_dir=first, quote_id="Q")
    with pytest.raises(spend.StateDirError):
        with spend.state_lock(first):
            pass
    out: list[str] = []
    code = cli.main(
        ["quote", "--state-dir", str(first), "--accept-documented-prices"], emit=out.append
    )
    assert code == 2 and spend.TEST_STATE_DIR_ENV in out[-1]


def test_there_is_only_one_dir_to_sum() -> None:
    assert not hasattr(spend, "state_dirs") and not hasattr(spend, "check_one_dir")


# --------------------------------------------------------------------------- MEDIUM 2


def _finish(out: Path, q: dict[str, Any], name: str) -> list[dict[str, Any]]:
    units = phases.build_units(phases.PHASES[name], q)
    phases.save_units(out, name, units, q)
    scenarios = phases.all_scenarios()
    rows = [
        {
            **u,
            "key": phases.unit_key(u),
            "convs": [{"run_id": f"r{i}", "final_text": "a"}]
            * len(scenarios[u["scenario_id"]].conversations),
        }
        for i, u in enumerate(units)
    ]
    (out / name / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / name / "verdicts.jsonl").write_text(json.dumps({"key": "v1", "verdict": False}) + "\n")
    return rows


def _progress(tmp_path: Path, old_out: Path) -> Path:
    path = tmp_path / "progress.json"
    assert (
        cli.main(["progress", "--out", str(old_out), "--write", str(path)], emit=lambda _l: None)
        == 0
    )
    return path


def _prepare(
    tmp_path: Path, q: dict[str, Any], phase: str, out: Path, *extra: str
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.main(
        [
            "prepare",
            "--phase",
            phase,
            "--out",
            str(out),
            "--tenant-id",
            "11111111-1111-4111-8111-111111111111",
            "--approved-quote",
            q["quote_id"],
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
            *extra,
        ],
        emit=lines.append,
    )
    return code, lines


def test_resume_prepares_exactly_what_the_quote_counted(tmp_path: Path) -> None:
    old_q = _quote(tmp_path, "--phases", "c")
    old_out = tmp_path / "old"
    half = phases.build_units(phases.PHASES["c"], old_q)
    phases.save_units(old_out, "c", half, old_q)
    scenarios = phases.all_scenarios()
    done = half[: len(half) // 3]
    (old_out / "c").mkdir(exist_ok=True)
    (old_out / "c" / "records.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    **u,
                    "key": phases.unit_key(u),
                    "convs": [{"run_id": "r"}] * len(scenarios[u["scenario_id"]].conversations),
                }
            )
            + "\n"
            for u in done
        )
    )
    progress_file = _progress(tmp_path, old_out)
    new_q = _quote(tmp_path, "--phases", "c", "--progress", str(progress_file))
    counted = new_q["remaining_units_by_phase"]
    new_out = tmp_path / "new"
    for name in ("c", "c4", "c5", "c2-baseline", "c4-baseline", "c5-baseline"):
        code, lines = _prepare(
            tmp_path,
            new_q,
            name,
            new_out,
            "--resume-from",
            str(old_out),
            "--phases",
            "c",
            "--progress",
            str(progress_file),
        )
        assert code == 0, lines
        prepared = phases.load_units(new_out, name, new_q["quote_id"])
        assert len(prepared) == counted[name], name  # 报价数的 == prepare 出来的
    assert counted["c"] == len(half) - len(done)
    carried = [json.loads(x) for x in (new_out / "c" / "records.jsonl").read_text().splitlines()]
    assert len(carried) == len(done) and all(
        r["carried_from_quote"] == old_q["quote_id"] for r in carried
    )
    audit = [json.loads(x) for x in (new_out / "c" / "carried.jsonl").read_text().splitlines()]
    assert len(audit) == len(done) and audit[0]["from_quote"] == old_q["quote_id"]
    # 新单元只是还没做完的那些,用新报价的用户名(要重新种)
    prepared_c = phases.load_units(new_out, "c", new_q["quote_id"])
    done_slots = {(u["scenario_id"], u["vendor"], u["repeat"]) for u in done}
    assert not done_slots & {(u["scenario_id"], u["vendor"], u["repeat"]) for u in prepared_c}
    assert all(new_q["quote_id"][:6] in u["user"] for u in prepared_c)


def test_resume_refuses_when_the_old_out_moved_on_since_quoting(tmp_path: Path) -> None:
    old_q = _quote(tmp_path, "--phases", "c")
    old_out = tmp_path / "old"
    _finish(old_out, old_q, "c4")
    progress_file = _progress(tmp_path, old_out)
    new_q = _quote(tmp_path, "--phases", "c", "--progress", str(progress_file))
    _finish(old_out, old_q, "c5")  # 报价之后旧目录里又做完了一段
    code, lines = _prepare(
        tmp_path,
        new_q,
        "c",
        tmp_path / "new",
        "--resume-from",
        str(old_out),
        "--phases",
        "c",
        "--progress",
        str(progress_file),
    )
    assert code == 2 and "progress" in lines[-1]


def test_report_merges_carried_and_new_records(tmp_path: Path) -> None:
    old_q = _quote(tmp_path, "--phases", "c")
    old_out = tmp_path / "old"
    rows = _finish(old_out, old_q, "c")
    progress_file = _progress(tmp_path, old_out)
    new_q = _quote(tmp_path, "--phases", "c", "--progress", str(progress_file))
    new_out = tmp_path / "new"
    code, lines = _prepare(
        tmp_path,
        new_q,
        "c",
        new_out,
        "--resume-from",
        str(old_out),
        "--phases",
        "c",
        "--progress",
        str(progress_file),
    )
    assert code == 0, lines
    assert phases.load_units(new_out, "c", new_q["quote_id"]) == []  # 全做完了:没有新单元
    body = cli.report(new_out, new_q)
    assert body["carried"]["c"] == len(rows)
    assert (new_out / "c" / "verdicts.jsonl").exists()  # 判分随着搬过来


# --------------------------------------------------------------------------- LOW


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("t"), httpx.ReadError("e"), httpx.RemoteProtocolError("p")],
)
def test_mid_stream_errors_without_a_run_id_keep_the_writeback_reserved(
    tmp_path: Path, exc: Exception
) -> None:
    assert live.never_started(exc, None) is False  # 请求可能已经建了运行
    guard = spend.Guard.resume(
        stop_line=100.0, prices=FLASH, state_dir=tmp_path / "s", quote_id="q"
    )
    call = live.reserve_writeback(guard, item="k|c0", model="glm-5.3-flash", phase="c4")
    (tmp_path / "c4").mkdir()
    res = {
        "call_id": call,
        "conv": 0,
        "run_id": None,
        "run_started": not live.never_started(exc, None),
    }
    record = {"key": "k", "user": "u", "writeback_reservations": [res], "convs": []}
    (tmp_path / "c4" / "records.jsonl").write_text(json.dumps(record) + "\n")
    usage = [{"model": "glm-5.3-flash", "input_tokens": 1000, "output_tokens": 0}]
    report = {"op": "writeback_usage", "key": "u", "jobs": {}, "models": usage}
    runs.settle_writeback_usage(tmp_path, "c4", [report], guard)
    assert guard.open_cap(call) > 0


def test_connect_errors_and_4xx_are_never_started() -> None:
    request = httpx.Request("POST", "https://cp/v1/agents/a/runs")
    assert live.never_started(httpx.ConnectError("c"), None) is True
    rejected = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    assert live.never_started(rejected, None) is True
    gateway = httpx.HTTPStatusError(
        "502", request=request, response=httpx.Response(502, request=request)
    )
    assert live.never_started(gateway, None) is False


@pytest.mark.asyncio
async def test_a_refused_run_reservation_is_marked_never_started() -> None:
    guard = spend.Guard(stop_line=0.001, prices={"main": Price(2.0, 8.0, 0.5, "t")})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as http:
        with pytest.raises(spend.SpendStopError) as caught:
            await live.run_conversation(
                http,
                agent="a",
                user="u",
                turn=live.sc.Turn("hi"),
                guard=guard,
                models=["main"],
                item="k",
                phase="b",
            )
    assert caught.value.b300_run_started is False  # type: ignore[attr-defined]


def test_writeback_settled_file_is_written_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[tuple[str, str]] = []
    import os

    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(runs.os, "replace", spy)
    guard = spend.Guard.resume(
        stop_line=100.0, prices=FLASH, state_dir=tmp_path / "s", quote_id="q"
    )
    (tmp_path / "c4").mkdir()
    runs.settle_writeback_usage(tmp_path, "c4", [], guard)
    assert replaced and replaced[-1][1].endswith("writeback-settled.json")
