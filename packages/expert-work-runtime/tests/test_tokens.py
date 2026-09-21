"""Tests for :mod:`expert_work.runtime.tokens` — Stream HX-1.

The estimator suite stays network-free: every test that needs a real
encoding injects a fake one; the single smoke test that loads the
actual ``o200k_base`` BPE skips itself when the file cannot be
fetched (offline CI must never fail on the fail-open path).
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from tiktoken import Encoding

from expert_work.runtime.tokens import (
    CHARS_PER_TOKEN,
    CharTokenEstimator,
    TiktokenEstimator,
    count_image_blocks,
    default_estimator,
    estimate_message,
    estimate_messages,
    flatten_message,
)


class _FakeEncoding:
    """Counts ``encode`` invocations; one token per character."""

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text: str, *, disallowed_special: tuple[str, ...] = ()) -> list[int]:
        del disallowed_special
        self.calls += 1
        return list(range(len(text)))


def test_char_estimator_matches_legacy_heuristic() -> None:
    est = CharTokenEstimator()
    assert est.count("abcdefgh") == 8 // CHARS_PER_TOKEN
    assert est.count("") == 1  # max(1, …) floor


def test_tiktoken_estimator_uses_loaded_encoding() -> None:
    est = TiktokenEstimator()
    fake = _FakeEncoding()
    est._encoding = cast(Encoding, fake)
    assert est.count("一二三四五六七八") == 8  # 1 token/char, not 8 // 4
    assert fake.calls == 1


def test_tiktoken_estimator_memoises_repeat_texts() -> None:
    est = TiktokenEstimator()
    fake = _FakeEncoding()
    est._encoding = cast(Encoding, fake)
    assert est.count("repeated text") == est.count("repeated text")
    assert fake.calls == 1  # second count served from the memo


def test_tiktoken_estimator_memo_is_bounded() -> None:
    est = TiktokenEstimator(memo_max_entries=2)
    est._encoding = cast(Encoding, _FakeEncoding())
    for text in ("a", "bb", "ccc"):
        est.count(text)
    assert len(est._memo) == 2  # oldest entry evicted


def test_tiktoken_estimator_load_failure_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    est = TiktokenEstimator(encoding_name="no-such-encoding")
    with caplog.at_level(logging.WARNING):
        first = est.count("abcdefgh")
        second = est.count("ijklmnop")
    assert first == 8 // CHARS_PER_TOKEN
    assert second == 8 // CHARS_PER_TOKEN
    warnings = [r for r in caplog.records if "tiktoken_unavailable" in r.getMessage()]
    assert len(warnings) == 1  # warn once, then silently degrade


def test_tiktoken_estimator_encode_failure_falls_back() -> None:
    class _Exploding:
        def encode(self, text: str, *, disallowed_special: tuple[str, ...] = ()) -> list[int]:
            raise RuntimeError("boom")

    est = TiktokenEstimator()
    est._encoding = cast(Encoding, _Exploding())
    assert est.count("abcdefgh") == 8 // CHARS_PER_TOKEN
    assert est._failed is True


def test_flatten_message_folds_block_content() -> None:
    msg = AIMessage(
        content=[
            {"type": "text", "text": "hello "},
            "raw-block",
            {"type": "tool_use", "id": "t1", "name": "noop"},
        ]
    )
    flat = flatten_message(msg)
    assert flat.startswith("hello raw-block")
    assert "tool_use" in flat  # non-text blocks still count


def test_estimate_messages_sums_per_message() -> None:
    est = CharTokenEstimator()
    messages = [HumanMessage(content="abcd" * 4), AIMessage(content="efgh" * 4)]
    assert estimate_messages(messages, est) == 8


def test_default_estimator_is_a_process_singleton() -> None:
    assert default_estimator() is default_estimator()


def test_real_o200k_smoke_cjk_far_above_chars_heuristic() -> None:
    """Real-vocab smoke — skipped when the BPE file is unavailable."""
    est = TiktokenEstimator()
    text = "上下文压缩在中文对话里触发得太晚因为字符数除四严重低估了词元数" * 8
    count = est.count(text)
    if est._failed:
        pytest.skip("o200k_base BPE unavailable (offline) — fail-open path covered elsewhere")
    # chars//4 would report ~len/4; real tokenisation of CJK is >=2x that.
    assert count > (len(text) // CHARS_PER_TOKEN) * 2


def _image_msg(n: int) -> HumanMessage:
    blocks: list[str | dict[str, Any]] = [{"type": "text", "text": "看这几页"}]
    blocks += [{"type": "image_ref", "ref": f"expert_work://image/t/th/{i}.png"} for i in range(n)]
    return HumanMessage(content=blocks)


def test_image_blocks_are_counted() -> None:
    assert count_image_blocks(_image_msg(3)) == 3
    assert count_image_blocks(HumanMessage(content="纯文本")) == 0


def test_estimate_message_charges_for_images() -> None:
    est = default_estimator()
    text_only = est.count(flatten_message(_image_msg(0)))
    with_images = estimate_message(_image_msg(3), est)
    # 断言里不许出现 IMAGE_BLOCK_TOKEN_COST —— 引用被变异的常量会让这条断言
    # 随变异一起塌成重言式(常量归零时它照样绿)。用一个独立的绝对下界:
    # 100 dpi 一页约 1000 token,三张图至少 3000。
    assert with_images >= text_only + 3_000


def test_image_cost_dwarfs_its_string_repr() -> None:
    """低估 65 倍就是这条测出来的:代价不能由 repr 的长度决定。"""
    est = default_estimator()
    one = _image_msg(1)
    repr_tokens = est.count(flatten_message(one))
    assert estimate_message(one, est) > 10 * repr_tokens


def test_flatten_message_is_not_padded() -> None:
    """``flatten_message`` 还要给 coalesce / 摘要器造**真文本**,不许掺填充。"""
    flat = flatten_message(_image_msg(2))
    assert "看这几页" in flat
    assert len(flat) < 500  # 两个 ref 的 repr 而已,没有 8000 字填充
