"""Tests for :mod:`expert_work.runtime.tokens` — Stream HX-1.

The estimator suite stays network-free: every test that needs a real
encoding injects a fake one; the smoke tests that load the actual
``o200k_base`` BPE skip themselves when the file cannot be fetched
(offline CI must never fail on the fail-open path).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, cast

import pytest
import tiktoken.load
from langchain_core.messages import AIMessage, HumanMessage
from tiktoken import Encoding

import expert_work.runtime.tokens as tokens_module
from expert_work.runtime.tokens import (
    CHARS_PER_TOKEN,
    CharTokenEstimator,
    TiktokenEstimator,
    count_image_blocks,
    default_estimator,
    estimate_message,
    estimate_messages,
    flatten_message,
    warm_default_estimator,
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


def test_warm_default_estimator_loads_the_shared_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-106 —— 预热的必须是 run 里用的那个进程级单例,不是另 new 一个。"""
    monkeypatch.setattr(tokens_module, "_default_instance", None)
    fake = _FakeEncoding()
    encoding = cast(Encoding, fake)
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: encoding)
    assert warm_default_estimator() is True
    shared = default_estimator()
    assert isinstance(shared, TiktokenEstimator)
    assert shared._encoding is encoding
    shared.count("abc")
    assert fake.calls == 1  # run 路径直接用已加载的编码


def test_warm_failure_is_permanent_no_reload_in_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-106 —— 预热失败后 run 内不再尝试加载(不在事件循环上重下)。"""
    monkeypatch.setattr(tokens_module, "_default_instance", None)
    calls: list[str] = []

    def _offline(name: str) -> Encoding:
        calls.append(name)
        raise OSError("offline")

    monkeypatch.setattr(tiktoken, "get_encoding", _offline)
    assert warm_default_estimator() is False
    assert default_estimator().count("abcdefgh") == 8 // CHARS_PER_TOKEN
    assert calls == ["o200k_base"]


def test_cache_dir_hit_never_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B-106 —— 镜像依赖的 tiktoken 行为:缓存目录里有且哈希对就不下载。

    文件名 = URL 的 sha1,内容按 sha256 校验(tiktoken ``load.read_file_cached``)。
    """
    blobpath = "https://example.invalid/o200k_base.tiktoken"
    data = b"aGk= 0\n"
    (tmp_path / hashlib.sha1(blobpath.encode(), usedforsecurity=False).hexdigest()).write_bytes(
        data
    )
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))

    def _no_network(path: str) -> bytes:
        raise AssertionError(f"tiktoken tried to download {path}")

    monkeypatch.setattr(tiktoken.load, "read_file", _no_network)
    got = tiktoken.load.read_file_cached(blobpath, hashlib.sha256(data).hexdigest())
    assert got == data


def test_real_o200k_loads_from_cache_dir_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 o200k_base:先填缓存目录(同 Dockerfile builder),再断网重建编码。"""
    from tiktoken_ext import openai_public  # type: ignore[import-untyped]

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    try:
        openai_public.o200k_base()
    except Exception:
        pytest.skip("o200k_base BPE unavailable (offline) — cannot seed the cache dir")

    def _no_network(path: str) -> bytes:
        raise AssertionError(f"tiktoken tried to download {path}")

    monkeypatch.setattr(tiktoken.load, "read_file", _no_network)
    assert openai_public.o200k_base()["name"] == "o200k_base"


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
