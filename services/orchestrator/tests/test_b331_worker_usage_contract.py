"""B-331 —— 对外平面的 worker end 帧 ``usage`` 补缓存;控制台(不传 keys)保持原值。"""

from __future__ import annotations

import copy
from typing import Any

from orchestrator.sse import contract_worker_usage, strip_frame_state_keys

_EXT = frozenset({"recalled_memories"})


def _bucket(provider: str | None, model: str, inp: int, read: int, create: int) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "input_tokens": inp,
        "output_tokens": 10,
        "total_tokens": inp + 10,
        "input_token_details": {"cache_read": read, "cache_creation": create},
        "output_token_details": {"reasoning": 0},
    }


def _frame(buckets: list[dict[str, Any]], usage: dict[str, Any] | None = None) -> dict[str, Any]:
    total = {
        "input_tokens": sum(b["input_tokens"] for b in buckets),
        "output_tokens": sum(b["output_tokens"] for b in buckets),
        "total_tokens": sum(b["total_tokens"] for b in buckets),
        "input_token_details": {"cache_read": 0, "cache_creation": 0},
        "output_token_details": {"reasoning": 0},
    }
    return {
        "worker_id": "wk-1",
        "kind": "end",
        "wseq": 2,
        "data": {
            "outcome": "success",
            "llm_call_count": 2,
            "usage": usage or total,
            "usage_by_model": buckets,
        },
    }


def test_external_anthropic_bucket_and_total_include_cache() -> None:
    frame = _frame(
        [
            _bucket("anthropic", "claude-sonnet-4-5", 100, 700, 50),
            _bucket("glm", "glm-5.3", 1000, 800, 0),
        ]
    )
    out = strip_frame_state_keys("worker", frame, _EXT, external_plane=True)
    claude, glm = out["data"]["usage_by_model"]
    assert claude["input_tokens"] == 850 and claude["total_tokens"] == 860
    assert glm == frame["data"]["usage_by_model"][1]  # 非 anthropic 桶一字不变
    # 顶层 usage = 各桶之和(原来 1100 = 混加)
    assert out["data"]["usage"]["input_tokens"] == 850 + 1000
    assert out["data"]["usage"]["total_tokens"] == 860 + 1010
    assert claude["provider"] == "anthropic"


def test_console_stream_keeps_raw_frame() -> None:
    frame = _frame([_bucket("anthropic", "claude-sonnet-4-5", 100, 700, 50)])
    assert strip_frame_state_keys("worker", frame, frozenset()) is frame
    # 不传 external_plane 即便带了 keys 也不补(平面由显式开关决定,不由 keys 推断)
    assert strip_frame_state_keys("worker", frame, _EXT) is frame
    # 反过来:keys 为空但显式对外,照样补 —— 清空 keys 集合不会悄悄关掉口径
    out = strip_frame_state_keys("worker", frame, frozenset(), external_plane=True)
    assert out["data"]["usage_by_model"][0]["input_tokens"] == 850


def test_non_anthropic_frame_is_unchanged_and_never_mutated() -> None:
    frame = _frame([_bucket("glm", "glm-5.3", 1000, 800, 0), _bucket("kimi", "kimi-k3", 40, 8, 0)])
    before = copy.deepcopy(frame)
    assert contract_worker_usage(frame) == before
    assert frame == before


def test_input_frame_is_not_mutated_when_normalized() -> None:
    frame = _frame([_bucket("anthropic", "claude-sonnet-4-5", 100, 700, 50)])
    before = copy.deepcopy(frame)
    contract_worker_usage(frame)
    assert frame == before


def test_null_provider_claude_bucket_recognised_by_model() -> None:
    frame = _frame([_bucket(None, "claude-opus-4-8", 100, 700, 50)])
    out = contract_worker_usage(frame)
    assert out["data"]["usage_by_model"][0]["input_tokens"] == 850
    assert out["data"]["usage_by_model"][0]["provider"] is None


def test_frame_without_usage_by_model_passes_through() -> None:
    frame = {"kind": "end", "data": {"usage": {"input_tokens": 5}}}
    assert contract_worker_usage(frame) is frame


def test_non_end_worker_frames_and_other_events_pass_through() -> None:
    upd = {"kind": "update", "data": {"messages": []}}
    assert strip_frame_state_keys("worker", upd, _EXT, external_plane=True) is upd
    payload = {"x": 1}
    assert strip_frame_state_keys("end", payload, _EXT, external_plane=True) is payload
