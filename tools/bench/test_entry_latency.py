"""分段聚合的纯函数测试,外加 ``run_rounds`` 的单轮故障容错(用 ``httpx.MockTransport``
打桩,不需要真栈)。"""

from pathlib import Path

import httpx
import yaml
from entry_latency import (
    FIRST_LLM_START_KEY,
    _exit_status,
    _prompt_fingerprint,
    _write_result,
    aggregate,
    extract_run_metrics,
    run_rounds,
)


def test_aggregate_reports_median_and_p95_per_segment() -> None:
    runs = [
        {"记忆召回": 100.0, "规划": 200.0},
        {"记忆召回": 300.0, "规划": 200.0},
        {"记忆召回": 200.0, "规划": 200.0},
    ]
    out = aggregate(runs)
    assert out["记忆召回"].median == 200.0
    assert out["规划"].median == 200.0


def test_aggregate_tolerates_a_segment_missing_from_some_runs() -> None:
    """rerank 只在配了 reranker 时才有 span;缺席的轮次不能拉低中位数,
    要按"出现过的轮次"算,并记录出现次数。"""
    runs = [{"记忆重排": 100.0}, {}, {"记忆重排": 300.0}]
    out = aggregate(runs)
    assert out["记忆重排"].median == 200.0
    assert out["记忆重排"].n == 2


def test_extract_run_metrics_keeps_only_entry_group_spans() -> None:
    """Segments come from ``group == "entry"`` spans only — an LLM ``group:
    null`` span (e.g. the main model call) must not leak into the segment
    dict under its label."""
    trace = {
        "status": "ok",
        "spans": [
            {"label": "记忆召回", "group": "entry", "latencyMs": 120, "kind": "span"},
            {"label": "LLM 调用", "group": None, "latencyMs": 900, "kind": "llm"},
        ],
    }
    metrics = extract_run_metrics(trace)
    assert metrics["记忆召回"] == 120.0
    assert "LLM 调用" not in metrics


def test_extract_run_metrics_first_llm_start_is_earliest_llm_span_start() -> None:
    """``first_llm_start`` approximates "entry chain done, generation starts"
    as the earliest ``startMs`` among ``kind == "llm"`` spans — two LLM spans
    (main call + an auxiliary one) must not both count; only the first. Not
    the same clock as Task 3's ``first_output_seconds`` (first token) — see
    the ``FIRST_LLM_START_KEY`` docstring."""
    trace = {
        "status": "ok",
        "spans": [
            {"label": "规划", "group": None, "kind": "llm", "startMs": 500},
            {"label": "LLM 调用", "group": None, "kind": "llm", "startMs": 2100},
        ],
    }
    metrics = extract_run_metrics(trace)
    assert metrics[FIRST_LLM_START_KEY] == 500.0


def test_extract_run_metrics_no_llm_span_omits_first_llm_start() -> None:
    """A trace with only entry-chain spans (no LLM call landed yet / not
    ingested) must not fabricate a ``first_llm_start`` value of 0."""
    trace = {
        "status": "ok",
        "spans": [{"label": "工作区摄取", "group": "entry", "latencyMs": 40, "kind": "span"}],
    }
    metrics = extract_run_metrics(trace)
    assert FIRST_LLM_START_KEY not in metrics


def _mock_run_response(run_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"X-Expert-Work-Run-Id": run_id},
        content=b"event: end\ndata: {}\n\n",
    )


async def test_run_rounds_survives_a_malformed_trace_response_body() -> None:
    """Code-review Important-1: a 2xx trace response with a non-JSON body
    (a gateway mangled it, say) makes ``resp.json()`` raise
    ``json.JSONDecodeError`` — a ``ValueError`` subclass, NOT an
    ``httpx.HTTPError``. Before this fix that escaped the per-round
    try/except entirely and crashed the whole batch, discarding every
    earlier round's already-paid-for real LLM data. Round 3 of 3 here
    returns a mangled body; rounds 1-2's real measurements must survive it.
    """
    trace_gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/runs"):
            return _mock_run_response("11111111-1111-1111-1111-111111111111")
        if request.method == "GET" and request.url.path.endswith(
            "/runs/11111111-1111-1111-1111-111111111111"
        ):
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.method == "GET" and request.url.path.endswith("/trace"):
            trace_gets["n"] += 1
            if trace_gets["n"] == 3:
                return httpx.Response(200, content=b"<html>not json</html>")
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "spans": [
                        {"label": "记忆召回", "group": "entry", "latencyMs": 100, "kind": "span"}
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        per_run, failed_runs = await run_rounds(client, "thread-1", "hello", 3, trace_timeout_s=5.0)

    assert per_run == [{"记忆召回": 100.0}, {"记忆召回": 100.0}, {}]
    assert failed_runs == 1


async def test_run_with_error_status_does_not_pollute_the_baseline() -> None:
    """真栈首跑逮到的 bug:graph 崩掉的 run 照样把 SSE 流干净地放完、照样
    留下 trace —— HTTP 层全绿。修复前这种 run 被记成 successful_runs=1,
    半截 trace(入口链跑了、LLM 挂了)混进聚合,正是"被静默污染的中位数"。
    现在 ``_run_once`` 问权威 run 记录的终态,非 success 一律判该轮失败。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/runs"):
            return _mock_run_response("22222222-2222-2222-2222-222222222222")
        if request.method == "GET" and request.url.path.endswith(
            "/runs/22222222-2222-2222-2222-222222222222"
        ):
            return httpx.Response(200, json={"data": {"status": "error"}})
        if request.method == "GET" and request.url.path.endswith("/trace"):
            raise AssertionError("a failed run's trace must never be fetched")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        per_run, failed_runs = await run_rounds(client, "thread-1", "hi", 1, trace_timeout_s=5.0)

    assert per_run == [{}]
    assert failed_runs == 1


def test_write_result_distinguishes_zero_success_from_zero_spans(tmp_path: Path) -> None:
    """Code-review Important-2: ``meta`` must record ``successful_runs`` /
    ``failed_runs`` so a reader can tell "0 real measurements because every
    round failed" (e.g. a typo'd ``--agent`` version → all 404) apart from
    "0 real measurements because this agent legitimately has no entry-chain
    spans" — both would otherwise write an identical ``segments: {}``."""
    out_path = tmp_path / "baseline.yaml"
    per_run = [{}, {}, {}]  # all three rounds failed
    _write_result(out_path, per_run, {"agent": "x@1", "runs": 3}, failed_runs=3)

    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["segments"] == {}
    assert written["meta"]["successful_runs"] == 0
    assert written["meta"]["failed_runs"] == 3
    assert written["meta"]["runs"] == 3  # requested count untouched


def test_exit_status_all_failed_returns_nonzero_and_warns() -> None:
    """Code-review Important-2: an all-failed batch must not exit 0 as if it
    had captured real data — the coordinator's concrete scenario is a
    typo'd ``--agent`` version silently 404ing every round."""
    code, warning = _exit_status(successful_runs=0, total_runs=10)
    assert code != 0
    assert warning is not None
    assert "10" in warning


def test_exit_status_full_success_is_clean() -> None:
    code, warning = _exit_status(successful_runs=10, total_runs=10)
    assert code == 0
    assert warning is None


def test_prompt_fingerprint_changes_with_content() -> None:
    """Code-review Important-3: the fingerprint (not just the file path,
    which the caller records separately) must change when the prompt
    content changes — a same-path-different-content edit between a
    before/after run pair must be detectable."""
    a = _prompt_fingerprint("总结今天的待办")
    b = _prompt_fingerprint("总结今天的待办事项,并列出优先级")
    assert a != b
    assert a == _prompt_fingerprint("总结今天的待办")  # deterministic
