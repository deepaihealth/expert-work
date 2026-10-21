"""B-209 §5.3 —— 两种识别接口的薄适配 + 重试。不联网:``httpx.MockTransport`` 回放与设计
§4 / §5.3 记录的响应同形的报文(``layout_parsing`` 回 ``md_results`` + ``layout_details``,
``chat_vlm`` 回 OpenAI 兼容的 ``choices``)。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from control_plane.knowledge.recognition.cleaning import NO_TEXT_SENTENCE
from control_plane.knowledge.recognition.clients import (
    CHAT_COMPLETIONS_URLS,
    LAYOUT_PARSING_URLS,
    OCR_PROMPT,
    ChatVlmClient,
    LayoutParsingClient,
    Reading,
    RecognitionCallError,
    read_with_retry,
)
from orchestrator.agent_factory import thinking_off_request_fields

_IMAGE = b"\xff\xd8\xff\xe0fake-jpeg"

#: A ``layout_parsing`` reply as glm-ocr returns it (design §4 / §5.3): the
#: page Markdown with tables as HTML and image regions as placeholders, plus
#: one list of labelled, bbox'd blocks per page; no ``finish_reason``.
LAYOUT_REPLY: dict[str, Any] = {
    "id": "task-1",
    "created": 1792473600,
    "model": "GLM-OCR",
    "md_results": (
        "## 第二章 请假制度\n\n员工请假须提前提交申请。\n\n"
        "![](page=0,bbox=[57, 199, 995, 637])\n\n"
        "<table><tr><th>类型</th><th>天数</th></tr><tr><td>年假</td><td>5</td></tr></table>"
    ),
    "layout_details": [
        [
            {"index": 0, "label": "text", "bbox_2d": [60, 40, 900, 90], "content": "## 第二章"},
            {"index": 1, "label": "text", "bbox_2d": [60, 100, 900, 160], "content": "员工请假"},
            {"index": 2, "label": "image", "bbox_2d": [57, 199, 995, 637], "content": ""},
            {"index": 3, "label": "table", "bbox_2d": [60, 660, 900, 900], "content": "<table>"},
        ]
    ],
    "data_info": {"num_pages": 1, "pages": [{"width": 1414, "height": 2000}]},
    "usage": {"prompt_tokens": 1180, "completion_tokens": 212, "total_tokens": 1392},
    "request_id": "req-1",
}


def _chat_reply(content: Any, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "qwen3.7-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 1460, "completion_tokens": 388, "total_tokens": 1848},
    }


def _http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_layout_parsing_request_and_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=LAYOUT_REPLY)

    async with _http(handler) as http:
        client = LayoutParsingClient(
            url=LAYOUT_PARSING_URLS["glm"], api_key="k-glm", model="glm-ocr", http=http
        )
        reading = await client.read(_IMAGE)

    (request,) = seen
    assert str(request.url) == "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
    assert request.headers["authorization"] == "Bearer k-glm"
    body = json.loads(request.content)
    assert body == {
        "model": "glm-ocr",
        "file": "data:image/jpeg;base64," + base64.b64encode(_IMAGE).decode(),
    }
    assert reading.text == LAYOUT_REPLY["md_results"]
    assert reading.finish_reason is None
    assert reading.layout_blocks == 3  # the image region holds no text
    assert (reading.input_tokens, reading.output_tokens, reading.usage_reported) == (
        1180,
        212,
        True,
    )


async def test_layout_parsing_without_md_results_is_an_error() -> None:
    async with _http(lambda _: httpx.Response(200, json={"id": "x"})) as http:
        client = LayoutParsingClient(url="https://x/lp", api_key="k", model="glm-ocr", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert not caught.value.retryable and not caught.value.unavailable


async def test_chat_vlm_request_carries_the_fixed_prompt_and_thinking_off() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_reply("# 标题\n正文"))

    fields = thinking_off_request_fields("qwen", "qwen3.7-flash", max_tokens=8192)
    async with _http(handler) as http:
        client = ChatVlmClient(
            url=CHAT_COMPLETIONS_URLS["qwen"],
            api_key="k-qwen",
            model="qwen3.7-flash",
            request_fields=fields,
            http=http,
        )
        reading = await client.read(_IMAGE)

    (request,) = seen
    assert str(request.url) == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    body = json.loads(request.content)
    assert body["model"] == "qwen3.7-flash"
    assert body["temperature"] == 0.1
    assert body["enable_thinking"] is False
    assert body["max_completion_tokens"] == 8192
    system, user = body["messages"]
    assert system == {"role": "system", "content": OCR_PROMPT}
    assert user["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(_IMAGE).decode()},
    }
    assert reading.text == "# 标题\n正文"
    assert reading.finish_reason == "stop"
    assert (reading.input_tokens, reading.output_tokens, reading.usage_reported) == (
        1460,
        388,
        True,
    )


def test_the_prompt_asks_for_the_fixed_no_text_sentence() -> None:
    assert OCR_PROMPT.rstrip().endswith(NO_TEXT_SENTENCE)


async def test_chat_vlm_reads_content_parts_and_length_finish() -> None:
    reply = _chat_reply([{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}])
    reply["choices"][0]["finish_reason"] = "length"
    async with _http(lambda _: httpx.Response(200, json=reply)) as http:
        reading = await ChatVlmClient(url="https://x/c", api_key="k", model="m", http=http).read(
            _IMAGE
        )
    assert reading.text == "第一段第二段"
    assert reading.finish_reason == "length"


@pytest.mark.parametrize(
    ("status", "body", "retryable", "unavailable"),
    [
        # Zhipu 1302: too many concurrent requests — load, retried.
        (429, {"error": {"code": "1302", "message": "您当前使用该API的并发数过高"}}, True, False),
        (429, {"error": {"code": "1303", "message": "您当前使用该API的频率过高"}}, True, False),
        # 1113: account in arrears — waits for 「补识别」, no retry.
        (429, {"error": {"code": "1113", "message": "您的账户已欠费"}}, False, True),
        (429, {}, True, False),
        (401, {"error": {"code": "1000", "message": "身份验证失败"}}, False, True),
        (402, {}, False, True),
        (403, {}, False, True),
        (408, {}, True, False),
        (500, {}, True, False),
        (503, {}, True, False),
        (400, {"error": {"code": "1210", "message": "图片尺寸不符合要求"}}, False, False),
    ],
)
async def test_http_errors_are_classified(
    status: int, body: dict[str, Any], retryable: bool, unavailable: bool
) -> None:
    async with _http(lambda _: httpx.Response(status, json=body)) as http:
        client = LayoutParsingClient(url="https://x/lp", api_key="k", model="glm-ocr", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert (caught.value.retryable, caught.value.unavailable) == (retryable, unavailable)
    assert str(status) in str(caught.value)


async def test_transport_errors_are_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _http(handler) as http:
        client = ChatVlmClient(url="https://x/c", api_key="k", model="m", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert caught.value.retryable


class _Scripted:
    """A client failing with the scripted errors before answering."""

    def __init__(self, *errors: RecognitionCallError) -> None:
        self._errors = list(errors)
        self.calls = 0

    async def read(self, image: bytes) -> Reading:
        del image
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return Reading(text="ok")


async def test_retry_backs_off_then_answers() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    busy = RecognitionCallError("429", retryable=True)
    client = _Scripted(busy, busy)
    reading = await read_with_retry(client, _IMAGE, sleep=sleep)
    assert reading.text == "ok"
    assert client.calls == 3
    assert delays == [0.5, 1.0]


async def test_retry_gives_up_after_three_retries() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    busy = RecognitionCallError("503", retryable=True)
    client = _Scripted(busy, busy, busy, busy, busy)
    with pytest.raises(RecognitionCallError):
        await read_with_retry(client, _IMAGE, sleep=sleep)
    assert client.calls == 4
    assert delays == [0.5, 1.0, 2.0]


async def test_client_errors_are_not_retried() -> None:
    async def sleep(delay: float) -> None:
        raise AssertionError("no backoff for a 400")

    client = _Scripted(RecognitionCallError("400"), RecognitionCallError("400"))
    with pytest.raises(RecognitionCallError):
        await read_with_retry(client, _IMAGE, sleep=sleep)
    assert client.calls == 1
