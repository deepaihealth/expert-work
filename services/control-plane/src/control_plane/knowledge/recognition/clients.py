"""B-209 §5.3 —— 识别模型两种接口形状各一个薄适配,外加重试。

* ``layout_parsing``(glm-ocr):``POST …/layout_parsing``,``{"model", "file": data URI}``;
  不收提示词,自带版式模型去页眉页脚;回 ``md_results``(表格是 HTML)+ ``layout_details``
  (每块带标签与 bbox);没有 ``finish_reason``。
* ``chat_vlm``(qwen3.7-flash 等 OpenAI 兼容看图模型):图片走 ``image_url`` data URI,
  系统固定的扫描页识别提示词;温度 0.1、关思考、输出上限按目录(厂商字段由
  :func:`~orchestrator.agent_factory.thinking_off_request_fields` 统一给出)。

重试:429 / 5xx / 408 / 超时 / 连接错误按 0.5 s 乘 2 的 n 次方(上限 8 s)最多重试 3 次(同 B-206
K-7);其余 4xx 不重试。密钥 / 账户类错误(401 / 402 / 403、欠费)记为「厂商不可用」——
这一页标「待识别」,配好后补识别,而不是「失败」。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from control_plane.knowledge.recognition.cleaning import NO_TEXT_SENTENCE
from orchestrator.llm import (
    DEEPSEEK_BASE_URL,
    DEFAULT_CHAT_COMPLETIONS_PATH,
    DOUBAO_BASE_URL,
    DOUBAO_CHAT_COMPLETIONS_PATH,
    GLM_BASE_URL,
    GLM_CHAT_COMPLETIONS_PATH,
    KIMI_BASE_URL,
    QWEN_BASE_URL,
)

#: Layout-parsing endpoints by provider (only Zhipu offers one today).
LAYOUT_PARSING_URLS: Mapping[str, str] = {
    "glm": f"{GLM_BASE_URL}/api/paas/v4/layout_parsing",
}

#: OpenAI-compatible chat-completions endpoints by provider — the same hosts
#: and paths the agent runtime calls.
CHAT_COMPLETIONS_URLS: Mapping[str, str] = {
    "openai": f"https://api.openai.com{DEFAULT_CHAT_COMPLETIONS_PATH}",
    "glm": f"{GLM_BASE_URL}{GLM_CHAT_COMPLETIONS_PATH}",
    "qwen": f"{QWEN_BASE_URL}{DEFAULT_CHAT_COMPLETIONS_PATH}",
    "deepseek": f"{DEEPSEEK_BASE_URL}{DEFAULT_CHAT_COMPLETIONS_PATH}",
    "kimi": f"{KIMI_BASE_URL}{DEFAULT_CHAT_COMPLETIONS_PATH}",
    "doubao": f"{DOUBAO_BASE_URL}{DOUBAO_CHAT_COMPLETIONS_PATH}",
}

#: Bumped whenever :data:`OCR_PROMPT` changes — part of the cache key, so a
#: new prompt re-reads pages instead of serving the old prompt's text.
OCR_PROMPT_VERSION = "scan-ocr-v1"

#: The fixed scanned-page prompt (B-209 §5.3, rewritten from WeKnora's
#: ``vlmOCRScannedPDFPrompt``). A tenant's own instructions never reach it.
OCR_PROMPT = f"""你是扫描文档的文字识别器。把图片里这一页的文字按原样转写成 Markdown:
1. 按人阅读的顺序输出:多栏版面先读完左栏再读右栏,从上到下。
2. 标题用 Markdown 标题,列表保留列表;表格用 Markdown 表格,第一行是表头,
   合并单元格把内容填进它覆盖的每一格。
3. 不输出页眉、页脚、页码和水印。
4. 原文是什么语言就输出什么语言:不翻译、不总结、不解释、不补全。
5. 看不清的字不要猜,只输出能看清的部分;不要写图片上没有的内容,不要描述图片。
6. 只输出转写的文字,不要代码块围栏,不要任何说明。
7. 整页没有任何文字时,只输出:{NO_TEXT_SENTENCE}"""

#: The user turn sent with the image (some vendors want text in the turn).
_OCR_USER_TEXT = "转写这一页。"

#: ``chat_vlm`` defaults (design §5.3) when the catalog declares none.
OCR_TEMPERATURE = 0.1
DEFAULT_OCR_MAX_TOKENS = 8_192

_TIMEOUT_S = 120.0
_ERROR_BODY_LIMIT = 300

#: Retry schedule: 0.5 s * 2**n, capped at 8 s, three retries (B-206 K-7).
_RETRY_BASE_S = 0.5
_RETRY_CAP_S = 8.0
_RETRIES = 3

#: Zhipu 429 codes that are account state, not load: 1113 = 余额不足 / 欠费,
#: 1304 = 当日调用次数用完. Any other 429 (1302 并发过高, 1303 频率过高, …) is load
#: and retried.
_ACCOUNT_429_CODES = frozenset({"1113", "1304"})

#: Layout block labels that are not text (their area holds no characters).
_NON_TEXT_LABELS = frozenset({"image", "figure", "chart", "seal"})


class RecognitionCallError(Exception):
    """One recognition call failed. ``retryable``: load / transport — try the
    same call again. ``unavailable``: the model cannot be used right now (no
    or bad key, account out of credit) — the page waits for 「补识别」."""

    def __init__(self, message: str, *, retryable: bool = False, unavailable: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.unavailable = unavailable


@dataclass(frozen=True)
class Reading:
    """One model call's raw answer (before cleaning)."""

    text: str
    #: Chat API ``finish_reason`` (``None`` for layout parsing).
    finish_reason: str | None = None
    #: Text blocks the vendor's layout model found (layout parsing only).
    layout_blocks: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: The response carried a ``usage`` object (billing needs it).
    usage_reported: bool = False


class RecognitionClient(Protocol):
    """Reads one page image into text — one per (model, credential)."""

    async def read(self, image: bytes) -> Reading:
        """:raises RecognitionCallError: the call failed (see its flags)."""


def _data_uri(image: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")


async def _post(
    http: httpx.AsyncClient | None, url: str, api_key: str, body: dict[str, Any]
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if http is not None:
            response = await http.post(url, json=body, headers=headers, timeout=_TIMEOUT_S)
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:  # timeouts + transport errors
        raise RecognitionCallError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
    if response.is_error:
        raise _status_error(response)
    try:
        data = response.json()
    except ValueError as exc:
        raise RecognitionCallError("response is not JSON", retryable=True) from exc
    if not isinstance(data, dict):
        raise RecognitionCallError("response is not a JSON object", retryable=True)
    return data


def _status_error(response: httpx.Response) -> RecognitionCallError:
    status = response.status_code
    message = f"{status} {response.text[:_ERROR_BODY_LIMIT]}"
    if status in (401, 402, 403):
        return RecognitionCallError(message, unavailable=True)
    if status == 429:
        if _vendor_error_code(response) in _ACCOUNT_429_CODES:
            return RecognitionCallError(message, unavailable=True)
        return RecognitionCallError(message, retryable=True)
    if status == 408 or status >= 500:
        return RecognitionCallError(message, retryable=True)
    return RecognitionCallError(message)


def _vendor_error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return str(code) if code is not None else None


def _usage(data: Mapping[str, Any]) -> tuple[int, int, bool]:
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0, False
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), True


@dataclass(frozen=True)
class LayoutParsingClient:
    """``layout_parsing`` adapter (glm-ocr)."""

    url: str
    api_key: str
    model: str
    http: httpx.AsyncClient | None = None

    async def read(self, image: bytes) -> Reading:
        data = await _post(
            self.http, self.url, self.api_key, {"model": self.model, "file": _data_uri(image)}
        )
        markdown = data.get("md_results")
        if not isinstance(markdown, str):
            raise RecognitionCallError("layout_parsing response has no md_results")
        input_tokens, output_tokens, reported = _usage(data)
        return Reading(
            text=markdown,
            layout_blocks=_text_blocks(data.get("layout_details")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_reported=reported,
        )


def _text_blocks(details: object) -> int | None:
    """Text blocks in ``layout_details`` (a list per page of block dicts)."""
    if not isinstance(details, list):
        return None
    blocks = [
        block
        for page in details
        if isinstance(page, list)
        for block in page
        if isinstance(block, dict)
    ]
    return sum(1 for block in blocks if str(block.get("label", "")).lower() not in _NON_TEXT_LABELS)


@dataclass(frozen=True)
class ChatVlmClient:
    """``chat_vlm`` adapter (OpenAI-compatible vision chat)."""

    url: str
    api_key: str
    model: str
    #: Vendor fields: thinking off + output cap (catalog-driven).
    request_fields: Mapping[str, Any] = field(default_factory=dict)
    http: httpx.AsyncClient | None = None

    async def read(self, image: bytes) -> Reading:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": OCR_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_uri(image)}},
                        {"type": "text", "text": _OCR_USER_TEXT},
                    ],
                },
            ],
            "temperature": OCR_TEMPERATURE,
            **self.request_fields,
        }
        data = await _post(self.http, self.url, self.api_key, body)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RecognitionCallError("chat response has no choices")
        choice = choices[0]
        message = choice.get("message")
        input_tokens, output_tokens, reported = _usage(data)
        finish = choice.get("finish_reason")
        return Reading(
            text=_message_text(message.get("content") if isinstance(message, dict) else None),
            finish_reason=finish if isinstance(finish, str) else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_reported=reported,
        )


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


async def read_with_retry(
    client: RecognitionClient,
    image: bytes,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Reading:
    """``client.read`` retried on load / transport errors (see module doc);
    the last error propagates."""
    for attempt in range(_RETRIES + 1):
        try:
            return await client.read(image)
        except RecognitionCallError as exc:
            if not exc.retryable or attempt == _RETRIES:
                raise
            await sleep(min(_RETRY_BASE_S * 2**attempt, _RETRY_CAP_S))
    raise AssertionError("unreachable")  # pragma: no cover
