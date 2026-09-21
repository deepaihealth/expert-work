"""Token estimation — Stream HX-1 (STREAM-HX-DESIGN §2).

One estimation truth source for the three context-management consumers
(:class:`~orchestrator.context.compressor.ContextCompressor`,
``WorkingWindow``, ``DynamicContextMiddleware``):

* :class:`TokenEstimator` — the injection protocol (Mini-ADR HX-A1).
* :class:`CharTokenEstimator` — the legacy ``chars // 4`` heuristic,
  kept as the zero-dependency default for direct construction (unit
  tests stay network-free).
* :class:`TiktokenEstimator` — ``o200k_base`` real tokenisation
  (Mini-ADR HX-A2). Lazy-loaded; *any* load or encode failure logs one
  warning and permanently falls back to ``chars // 4`` — the fail-open
  axiom: an offline BPE file can only cost estimation accuracy, never
  capability. A bounded LRU memo (Mini-ADR HX-A3) keeps the per-turn
  re-estimation of the append-mostly message prefix cheap.

The factory injects :func:`default_estimator` (a process-level
singleton so the BPE vocabulary loads once) into all three consumers.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import BaseMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tiktoken import Encoding

logger = logging.getLogger(__name__)

#: Anthropic-documented rule of thumb — the legacy heuristic divisor.
CHARS_PER_TOKEN = 4

#: Bounded memo size (Mini-ADR HX-A3). Entries hold references to
#: message text already alive in graph state, so the bound caps entry
#: *count*, not bytes copied.
_MEMO_MAX_ENTRIES = 4096


class TokenEstimator(Protocol):
    """Estimated token count for a raw text fragment."""

    def count(self, text: str) -> int:
        """Return the estimated number of tokens in ``text``."""


class CharTokenEstimator:
    """Legacy heuristic — ``max(1, len(text) // 4)``.

    Slightly conservative for English/code, a ~2.5-3x *under*estimate
    for CJK. Kept as the dependency-free default so directly-constructed
    components (unit tests) never touch the network.
    """

    def count(self, text: str) -> int:
        return max(1, len(text) // CHARS_PER_TOKEN)


class TiktokenEstimator:
    """Real BPE token counting via tiktoken ``o200k_base``.

    ``o200k_base`` over the repo's existing ``cl100k_base`` (knowledge
    chunk sizing): the newer encoding's CJK compression is much closer
    to the BPE vocabularies of the qwen/deepseek model families that
    dominate zh-CN tenant traffic (Mini-ADR HX-A2 — one universal
    approximation, no per-provider tokenizers).

    Thread-safety: a lock guards lazy load + memo mutation — the
    estimator is a process-level singleton shared across agent builds.
    """

    def __init__(
        self,
        encoding_name: str = "o200k_base",
        *,
        memo_max_entries: int = _MEMO_MAX_ENTRIES,
    ) -> None:
        self._encoding_name = encoding_name
        self._memo_max_entries = max(0, memo_max_entries)
        self._memo: OrderedDict[str, int] = OrderedDict()
        self._encoding: Encoding | None = None
        self._failed = False
        self._lock = threading.Lock()

    def count(self, text: str) -> int:
        with self._lock:
            cached = self._memo.get(text)
            if cached is not None:
                self._memo.move_to_end(text)
                return cached
        value = self._count_uncached(text)
        if self._memo_max_entries:
            with self._lock:
                self._memo[text] = value
                self._memo.move_to_end(text)
                while len(self._memo) > self._memo_max_entries:
                    self._memo.popitem(last=False)
        return value

    def _count_uncached(self, text: str) -> int:
        encoding = self._load_encoding()
        if encoding is None:
            return max(1, len(text) // CHARS_PER_TOKEN)
        try:
            return max(1, len(encoding.encode(text, disallowed_special=())))
        except Exception:
            # Fail-open axiom — estimation accuracy is the only
            # acceptable casualty; the call path never sees a raise.
            self._mark_failed("encode")
            return max(1, len(text) // CHARS_PER_TOKEN)

    def _load_encoding(self) -> Encoding | None:
        encoding = self._encoding
        if encoding is not None:
            return encoding
        if self._failed:
            return None
        with self._lock:
            if self._encoding is None and not self._failed:
                try:
                    import tiktoken

                    self._encoding = tiktoken.get_encoding(self._encoding_name)
                except Exception:
                    # Covers missing dependency *and* the first-use BPE
                    # file download failing in an offline deployment.
                    self._mark_failed("load", locked=True)
            return self._encoding

    def _mark_failed(self, stage: str, *, locked: bool = False) -> None:
        if locked:
            first = not self._failed
            self._failed = True
        else:
            with self._lock:
                first = not self._failed
                self._failed = True
        if first:
            logger.warning(
                "token_estimator.tiktoken_unavailable stage=%s encoding=%s — "
                "falling back to chars//%d permanently",
                stage,
                self._encoding_name,
                CHARS_PER_TOKEN,
            )


def flatten_message(msg: BaseMessage) -> str:
    """Flatten a message to a single text representation.

    Block-list content (J.6 multimodal, L1 cache_control wrappers) is
    folded by concatenating each block's ``text`` field; non-text
    blocks (images, tool_use) contribute their string representation
    so they still count toward the token estimate.
    """
    content: Any = msg.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            else:
                # Tool-use / image / other → coarse repr keeps the
                # estimate honest even when the actual bytes are
                # downstream-owned (base64 etc.).
                parts.append(str(block))
    return "".join(parts)


#: 一个图片内容块的 token 代价。
#:
#: 为什么需要这个常量:``flatten_message`` 把图片块折成它的字符串表示
#: (``{"type":"image_ref","ref":"expert_work://…"}`` 约 80 字符 ≈ 20 token),
#: 而一页 100 dpi 的渲染图按 ``宽x高/750`` 约 1000 token —— 低估约 65 倍。
#: 裁剪 / 压缩 / working window 三个闸门都靠估算值决定何时动手,低估的直接
#: 后果是它们对图片**完全看不见**:塞三张图进上下文,它们以为只加了 60 token。
#:
#: 取值:1568x1568(Anthropic 建议的长边上限)/750 ≈ 3277 是最坏情况;本设计
#: 渲染在 100 dpi、约 1.0 MP,实际约 1333。取 1300 作为常规档,宁可略低估
#: 最坏情况也不让常规档虚高 —— 虚高会让压缩过早触发,那是另一种失效。
#:
#: 两份参考实现都有同一个常量(hermes ``agent/image_token_cost.py``、
#: openclaw ``IMAGE_CHAR_ESTIMATE = 8_000``),它们同样把它喂给压缩触发器。
IMAGE_BLOCK_TOKEN_COST = 1_300

#: 计入 :data:`IMAGE_BLOCK_TOKEN_COST` 的块类型。``image_ref`` 是平台内部的
#: 引用块(J.6 Path A,字节由适配器在调用时解析);另外两个是厂商线格式,
#: 适配器翻译后的形状,历史重放时可能出现。
_IMAGE_BLOCK_TYPES = frozenset({"image_ref", "image", "image_url"})


def count_image_blocks(msg: BaseMessage) -> int:
    """消息内容里的图片块个数(``str`` 内容恒为 0)。"""
    content: Any = msg.content
    if isinstance(content, str):
        return 0
    return sum(
        1
        for block in content
        if isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES
    )


def estimate_message(msg: BaseMessage, estimator: TokenEstimator) -> int:
    """单条消息的 token 估算 —— 文本走 ``estimator``,图片走固定档。

    **不要把这个代价加进** :func:`flatten_message`:那个函数还要给
    ``llm.coalesce``(把多条 system 合成真提示词)和压缩器的摘要格式化造
    **真文本**,掺进填充会直接污染发给模型的内容。代价只加在估算这一侧。
    """
    return estimator.count(flatten_message(msg)) + count_image_blocks(msg) * IMAGE_BLOCK_TOKEN_COST


def estimate_messages(messages: Sequence[BaseMessage], estimator: TokenEstimator) -> int:
    """Per-message estimate sum over ``messages`` via ``estimator``(含图片代价)。"""
    return sum(estimate_message(msg, estimator) for msg in messages)


_default_lock = threading.Lock()
_default_instance: TiktokenEstimator | None = None


def default_estimator() -> TokenEstimator:
    """Process-level shared :class:`TiktokenEstimator` (vocab loads once)."""
    global _default_instance
    with _default_lock:
        if _default_instance is None:
            _default_instance = TiktokenEstimator()
        return _default_instance
