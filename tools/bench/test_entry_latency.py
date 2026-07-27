"""分段聚合的纯函数测试。网络/真栈部分不在单测范围。"""

from entry_latency import FIRST_LLM_START_KEY, aggregate, extract_run_metrics


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
