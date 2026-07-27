import random

import pytest

from expert_work.common.dlp import scan_and_redact
from orchestrator.graph_builder.streaming_redact import (
    HOLD_CHARS,
    StreamingRedactor,
    TokenSink,
    make_token_sink,
)
from orchestrator.llm.providers._streaming import LLMDelta


def test_dlp_redacts_card_split_across_feeds() -> None:
    r = StreamingRedactor(dlp=True, screen=False)
    a = r.feed("your card is 4111 1111 ")
    b = r.feed("1111 1111 thanks")
    tail = r.flush()
    full = a + b + tail
    assert "4111" not in full  # raw digits never leaked
    assert full == "your card is [redacted] thanks"


def test_prefix_monotonic_chunked_equals_oneshot() -> None:
    text = "call 4111 1111 1111 1111 or 13800138000 now " + "x" * 80
    r = StreamingRedactor(dlp=True, screen=False)
    out = "".join(r.feed(c) for c in text) + r.flush()
    assert out == scan_and_redact(text).redacted


def test_screen_block_withholds_all() -> None:
    r = StreamingRedactor(dlp=False, screen=True)
    key = "sk-" + "a" * 24  # matches output_screen _SECRET_PATTERNS
    assert r.feed("here is the key " + key) == ""
    assert r.feed(" more text") == ""  # stays blocked
    assert r.flush() == ""


def test_screen_off_does_not_block_credentials() -> None:
    r = StreamingRedactor(dlp=False, screen=False)
    key = "sk-" + "a" * 24
    out = r.feed("key " + key) + r.flush()
    assert key in out  # screen disabled → not withheld


def test_max_clamp_boundary_retreat() -> None:
    # Leading safe filler pushes emission past HOLD_CHARS (a non-empty
    # prefix comes out), THEN a second feed completes a card pattern that
    # was only partially formed — the redacted text's length barely grows
    # (raw digits collapse into "[redacted]"), exercising the
    # max(_emitted_len, ...) clamp so the boundary never retreats below what
    # was already emitted.
    full_input = "x" * 70 + " card 4111 1111 1111 " + "1111 done"
    r = StreamingRedactor(dlp=True, screen=False)
    prefix1 = r.feed("x" * 70 + " card 4111 1111 1111 ")
    assert prefix1 != ""
    prefix2 = r.feed("1111 done")
    tail = r.flush()
    full = prefix1 + prefix2 + tail
    assert "4111" not in full  # raw digits never leaked across fragments
    assert full == scan_and_redact(full_input).redacted


def test_dlp_and_screen_both_enabled() -> None:
    r = StreamingRedactor(dlp=True, screen=True)
    out = r.feed("my card is 4111 1111 1111 1111 thanks") + r.flush()
    assert "4111" not in out and "[redacted]" in out  # PII redacted
    assert out != ""  # not blocked

    r2 = StreamingRedactor(dlp=True, screen=True)
    key = "sk-" + "a" * 24
    out2 = r2.feed("here is the key " + key) + r2.flush()
    assert out2 == ""  # credential trips the screen → all output withheld


@pytest.mark.asyncio
async def test_token_sink_publishes_content_frames() -> None:
    frames: list[dict] = []

    async def pub(f: dict) -> None:
        frames.append(f)

    sink = TokenSink(step=3, publish=pub, dlp=False, screen=False)
    await sink(LLMDelta(content="A" * 100))
    await sink.flush()
    assert all(f["step"] == 3 and f["channel"] == "content" for f in frames)
    assert "".join(f["text"] for f in frames) == "A" * 100


@pytest.mark.asyncio
async def test_token_sink_redacts_pii() -> None:
    frames: list[dict] = []

    async def pub(f: dict) -> None:
        frames.append(f)

    sink = TokenSink(step=0, publish=pub, dlp=True, screen=False)
    await sink(LLMDelta(content="card 4111 1111 1111 1111 done " + "x" * 60))
    await sink.flush()
    joined = "".join(f["text"] for f in frames)
    assert "4111" not in joined and "[redacted]" in joined


async def _noop_pub(f: dict) -> None:
    return None


def test_make_token_sink_gates_off_when_judge_enabled() -> None:
    assert (
        make_token_sink(step=0, publish=_noop_pub, dlp=False, screen=False, judge_enabled=True)
        is None
    )


def test_make_token_sink_none_without_publish() -> None:
    assert (
        make_token_sink(step=0, publish=None, dlp=False, screen=False, judge_enabled=False) is None
    )


def test_make_token_sink_builds_when_enabled() -> None:
    sink = make_token_sink(step=1, publish=_noop_pub, dlp=True, screen=True, judge_enabled=False)
    assert isinstance(sink, TokenSink)


def test_card_straddling_long_prefix_still_redacted() -> None:
    # 安全长前缀(>WINDOW=128)推动冻结指针前进、发射前沿(end-HOLD)靠前,
    # 再让一张卡号跨多个 delta 完成——卡号字符落在"已越过冻结/发射区"之后仍必须
    # 整体脱敏、不泄漏(验冻结指针推进后 straddle 的卡号不漏)。
    prefix = "safe filler text. " * 12  # 216 chars, no PII
    r = StreamingRedactor(dlp=True, screen=False)
    out = r.feed(prefix)
    out += r.feed("account 4111 1111 ")
    out += r.feed("1111 1111 end")
    out += r.flush()
    assert "4111" not in out
    assert out == scan_and_redact(prefix + "account 4111 1111 1111 1111 end").redacted


def test_random_split_equals_oneshot_bounded_corpus() -> None:
    # 含 card / id / phone 的 bounded 语料;多种随机切分点,每种 join 都等于全扫。
    corpus = (
        "contact 13800138000 or card 4111 1111 1111 1111, "
        "id 11010119900307123X, thanks. " + "padding words here. " * 20
    )
    expected = scan_and_redact(corpus).redacted
    rng = random.Random(20260717)  # noqa: S311 固定 seed → test determinism
    for _ in range(25):
        r = StreamingRedactor(dlp=True, screen=False)
        i = 0
        out = ""
        while i < len(corpus):
            step = rng.randint(1, 17)
            out += r.feed(corpus[i : i + step])
            i += step
        out += r.flush()
        assert out == expected, "mismatch for this split; expected == oneshot redact"


def test_screen_latches_on_credential_after_long_safe_prefix() -> None:
    # 长安全填充(远超 WINDOW)之后才出现凭据:screen 窗必须仍抓到并全扣。
    r = StreamingRedactor(dlp=False, screen=True)
    out = r.feed("x" * 300)  # 已释放一部分安全前缀
    key = "sk-" + "a" * 24  # 命中 _SECRET_PATTERNS
    out += r.feed("here is the key " + key)
    out += r.feed(" trailing")
    out += r.flush()
    assert key not in out  # 凭据不泄漏
    # latch 后不再释放新内容(尾部 " trailing" 也被扣)
    assert "trailing" not in out


def test_email_within_hold_is_redacted_streaming() -> None:
    # 短于 hold 窗的 email 在释放前已被完整缓冲 → 流式脱敏与全扫一致。
    # (email 无界:长于 HOLD 的地址是"文档化的 provisional 残留"——由权威帧兜底,
    #  且其部分头泄漏本就依赖分片边界,不作断言。此处只锁"落窗 email 仍脱敏"。)
    text = "reach me at user@example.com anytime " + "z" * 80
    r = StreamingRedactor(dlp=True, screen=False)
    out = "".join(r.feed(c) for c in text) + r.flush()
    assert "user@example.com" not in out  # 落窗 email 被脱敏
    assert out == scan_and_redact(text).redacted  # 逐字节等价全扫


def test_rescan_work_is_bounded_not_quadratic(monkeypatch) -> None:
    # monkeypatch 记录每次传给守卫的文本长度;喂长文,断言 max 入参有界(常数),
    # 证每 feed 重扫量不随总长增长(O(n) 全程,非 O(n²))。
    from orchestrator.graph_builder import streaming_redact as sr

    max_len = 0
    real_scan = sr.scan_and_redact
    real_screen = sr.screen_output

    def spy_scan(text):
        nonlocal max_len
        max_len = max(max_len, len(text))
        return real_scan(text)

    def spy_screen(text, **kw):
        nonlocal max_len
        max_len = max(max_len, len(text))
        return real_screen(text, **kw)

    monkeypatch.setattr(sr, "scan_and_redact", spy_scan)
    monkeypatch.setattr(sr, "screen_output", spy_screen)

    r = sr.StreamingRedactor(dlp=True, screen=True)
    total = "abcdefghij " * 400  # 4400 chars, no PII
    delta = 50
    for i in range(0, len(total), delta):
        r.feed(total[i : i + delta])
    r.flush()
    assert max_len <= 3 * sr.WINDOW  # 384 << 4400 → 每 feed 重扫为常数,非 O(n)


def test_collapse_guard_no_negative_index_leak() -> None:
    # PII 折叠(digits→[redacted])使 redacted-length 瞬时回缩;若冻结点 redacted-count
    # 越过已 emit,下帧 lo=_emitted_out-_frozen_out<0 → Python 负索引回绕取 buffer 尾。
    # 构造:安全前缀把 emit 前沿推到卡号完成点附近,分片完成卡号。
    prefix = "y" * 130 + " your card number is "
    r = StreamingRedactor(dlp=True, screen=False)
    out = r.feed(prefix)
    out += r.feed("4111 1111 1111 ")
    out += r.feed("1111 tail-marker")
    out += r.flush()
    assert "4111" not in out  # 不泄漏原数字
    assert out.count("tail-marker") == 1  # 无回绕重复
    assert out == scan_and_redact(prefix + "4111 1111 1111 1111 tail-marker").redacted


def test_clean_split_not_fooled_by_overlapping_match_shapes() -> None:
    # 冻结洁净判据回归:18 位 id_card 的前 16 位数字独立命中 credit_card 形状,
    # 两者都折成定长 "[redacted]"。若 _advance_frozen 用前缀判据(startswith),
    # 冻结点会假阳性落进 id_card 中间 → _frozen_out 计数错 → 下游重复发射
    # ("words"→"wordrds")。精确分割等价判据必须拒绝该 straddle。
    # 长 padding 使 new_frozen(=len-WINDOW)随 buffer 增长恰好追进 id_card 区间。
    corpus = (
        "contact 13800138000 or card 4111 1111 1111 1111, "
        "id 11010119900307123X, thanks. " + "padding words here. " * 20
    )
    expected = scan_and_redact(corpus).redacted
    r = StreamingRedactor(dlp=True, screen=False)
    out = "".join(r.feed(corpus[i : i + 7]) for i in range(0, len(corpus), 7)) + r.flush()
    assert out == expected  # 无重复/无错位
    assert "words here. padding words" in out  # 无 "wordrds" 类回绕垃圾


def test_screen_latch_survives_large_single_delta() -> None:
    # A single large delta carrying a credential followed by >HOLD safe chars:
    # a fixed-size screen window would miss the credential (its start falls
    # outside the window) while emission advances past it -> leak. Screening the
    # full unfrozen tail catches it — this is the exact divergence a bounded
    # fixed-window screen scan introduces vs the whole-buffer scan.
    r = StreamingRedactor(dlp=False, screen=True)
    key = "AIza" + "B" * 35  # Google API key shape, matches _SECRET_PATTERNS
    out = r.feed("x" * 200)  # safe prefix, emission caught up
    out += r.feed(" " + key + " " + "z" * 90)  # one big delta: key + long tail
    out += r.flush()
    assert key not in out  # credential never emitted


@pytest.mark.asyncio
async def test_token_sink_publishes_reasoning_frames() -> None:
    frames: list[dict] = []

    async def pub(f: dict) -> None:
        frames.append(f)

    sink = TokenSink(step=2, publish=pub, dlp=False, screen=False)
    await sink(LLMDelta(reasoning="R" * 100))
    await sink.flush()
    rf = [f for f in frames if f["channel"] == "reasoning"]
    assert rf and all(f["step"] == 2 for f in rf)
    assert "".join(f["text"] for f in rf) == "R" * 100


@pytest.mark.asyncio
async def test_token_sink_redacts_reasoning_pii() -> None:
    frames: list[dict] = []

    async def pub(f: dict) -> None:
        frames.append(f)

    sink = TokenSink(step=0, publish=pub, dlp=True, screen=False)
    await sink(LLMDelta(reasoning="card 4111 1111 1111 1111 hmm " + "y" * 60))
    await sink.flush()
    joined = "".join(f["text"] for f in frames if f["channel"] == "reasoning")
    assert "4111" not in joined and "[redacted]" in joined


@pytest.mark.asyncio
async def test_content_and_reasoning_streams_isolated() -> None:
    # Each text channel has its OWN StreamingRedactor — a card's digits in the
    # content stream and a different card in the reasoning stream each redact
    # independently; if they shared one redactor the interleaved feeds would
    # corrupt each other's buffered-release state.
    frames: list[dict] = []

    async def pub(f: dict) -> None:
        frames.append(f)

    sink = TokenSink(step=0, publish=pub, dlp=True, screen=False)
    await sink(LLMDelta(content="4111 1111 ", reasoning="9999 8888 "))
    await sink(LLMDelta(content="1111 1111", reasoning="7777 6666"))
    await sink.flush()
    content = "".join(f["text"] for f in frames if f["channel"] == "content")
    reasoning = "".join(f["text"] for f in frames if f["channel"] == "reasoning")
    assert content == "[redacted]" and reasoning == "[redacted]"
    assert "4111" not in content and "9999" not in reasoning


@pytest.mark.asyncio
async def test_token_sink_emits_tool_name_once_per_index() -> None:
    from orchestrator.llm.providers._streaming import ToolCallChunk

    frames: list[dict] = []

    async def pub(f: dict) -> None:
        frames.append(f)

    sink = TokenSink(step=1, publish=pub, dlp=False, screen=False)
    # name arrives on the first fragment for index 0; later fragments carry args only
    await sink(
        LLMDelta(
            tool_calls=(ToolCallChunk(index=0, id="c0", name="search_web", args_fragment='{"q":'),)
        )
    )
    await sink(LLMDelta(tool_calls=(ToolCallChunk(index=0, args_fragment='"hi"}'),)))
    # a second parallel tool at index 1
    await sink(
        LLMDelta(
            tool_calls=(ToolCallChunk(index=1, id="c1", name="read_file", args_fragment="{}"),)
        )
    )
    await sink.flush()
    tool_frames = [f for f in frames if f["channel"] == "tool_args"]
    assert tool_frames == [
        {"step": 1, "channel": "tool_args", "tool_index": 0, "name": "search_web"},
        {"step": 1, "channel": "tool_args", "tool_index": 1, "name": "read_file"},
    ]


def test_feed_passthrough_when_both_guards_off() -> None:
    """P3 —— dlp/screen 双关时无 64 字符 hold:feed 立即全量返回。"""
    r = StreamingRedactor(dlp=False, screen=False)
    assert r.feed("short") == "short"  # < HOLD_CHARS 也立即出
    assert r.feed("A" * 100) == "A" * 100  # 无尾部扣留
    assert r.flush() == ""  # 无 buffered 尾巴


def test_feed_still_holds_when_screen_on() -> None:
    """screen 单开 —— hold 行为必须不变(撤回语义依赖它)。"""
    r = StreamingRedactor(dlp=False, screen=True)
    out = r.feed("A" * 100)
    assert len(out) == 100 - HOLD_CHARS


def test_feed_still_holds_when_dlp_on() -> None:
    r = StreamingRedactor(dlp=True, screen=False)
    out = r.feed("A" * 100)
    assert len(out) == 100 - HOLD_CHARS
