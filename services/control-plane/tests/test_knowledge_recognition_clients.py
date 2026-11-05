"""B-209 §5.3 —— 两种识别接口的薄适配 + 重试。不联网:``httpx.MockTransport`` 回放与设计
§4 / §5.3 记录的响应同形的报文(``layout_parsing`` 回 ``md_results`` + ``layout_details``,
``chat_vlm`` 回 OpenAI 兼容的 ``choices``)。
"""

from __future__ import annotations

import asyncio
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
            {"index": 2, "label": "image", "bbox_2d": [57, 199, 995, 637], "content": "图"},
            {
                "index": 3,
                "label": "table",
                "bbox_2d": [60, 660, 900, 900],
                "content": "<table><tr><td>年假</td></tr></table>",
            },
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
    # Text of the text-bearing blocks only (image region excluded, markup dropped).
    assert reading.layout_chars == len("##第二章") + len("员工请假") + len("年假")
    # B-209 PR-2 (review H4): the frame the region boxes are measured in.
    assert reading.layout_size == (1414, 2000)
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
    ("status", "body", "retryable", "unavailable", "kind"),
    [
        # Zhipu 1302: too many concurrent requests — load, retried.
        (429, {"error": {"code": "1302", "message": "并发数过高"}}, True, False, "busy"),
        (429, {"error": {"code": "1303", "message": "频率过高"}}, True, False, "busy"),
        # 1113: account in arrears — waits for 「补识别」, no retry.
        (429, {"error": {"code": "1113", "message": "您的账户已欠费"}}, False, True, "quota"),
        # 1308: a usage cap until its reset time — account state too (Zhipu 错误码 table).
        (429, {"error": {"code": "1308", "message": "已达到使用上限"}}, False, True, "quota"),
        (429, {}, True, False, "busy"),
        (401, {"error": {"code": "1000", "message": "身份验证失败"}}, False, True, "auth"),
        (402, {}, False, True, "quota"),
        (403, {}, False, True, "auth"),
        (408, {}, True, False, "busy"),
        (500, {}, True, False, "busy"),
        (503, {}, True, False, "busy"),
        (400, {"error": {"code": "1210", "message": "图片尺寸不符"}}, False, False, "rejected"),
    ],
)
async def test_http_errors_are_classified(
    status: int, body: dict[str, Any], retryable: bool, unavailable: bool, kind: str
) -> None:
    async with _http(lambda _: httpx.Response(status, json=body)) as http:
        client = LayoutParsingClient(url="https://x/lp", api_key="k", model="glm-ocr", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert (caught.value.retryable, caught.value.unavailable) == (retryable, unavailable)
    assert caught.value.kind == kind
    assert str(status) in str(caught.value)


async def test_transport_errors_are_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _http(handler) as http:
        client = ChatVlmClient(url="https://x/c", api_key="k", model="m", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert caught.value.retryable


@pytest.mark.parametrize(
    ("status", "body", "cause"),
    [
        (429, {}, "rate_limited"),
        (429, {"error": {"code": "1302", "message": "并发数过高"}}, "rate_limited"),
        (408, {}, "request_timeout"),
        (500, {}, "server_error"),
        (503, {}, "server_error"),
        (429, {"error": {"code": "1113", "message": "您的账户已欠费"}}, None),
        (401, {}, None),
        (400, {}, None),
    ],
)
async def test_a_busy_error_says_which_busy_it_was(
    status: int, body: dict[str, Any], cause: str | None
) -> None:
    # B-237: rate limit vs timeout vs server error, for the logs and metrics.
    async with _http(lambda _: httpx.Response(status, json=body)) as http:
        client = LayoutParsingClient(url="https://x/lp", api_key="k", model="glm-ocr", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert caught.value.cause == cause


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (httpx.ConnectTimeout("timed out"), "timeout"),
        (httpx.ReadTimeout("timed out"), "timeout"),
        (httpx.ConnectError("refused"), "transport"),
        (httpx.RemoteProtocolError("closed"), "transport"),
    ],
)
async def test_transport_errors_say_timeout_or_transport(
    error: httpx.TransportError, cause: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    async with _http(handler) as http:
        client = ChatVlmClient(url="https://x/c", api_key="k", model="m", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert (caught.value.kind, caught.value.cause) == ("busy", cause)


@pytest.mark.parametrize("reply", [b"<html>502</html>", b"[1, 2]"])
async def test_a_reply_that_is_not_a_json_object_is_not_json(reply: bytes) -> None:
    async with _http(lambda _: httpx.Response(200, content=reply)) as http:
        client = ChatVlmClient(url="https://x/c", api_key="k", model="m", http=http)
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert (caught.value.kind, caught.value.cause) == ("busy", "not_json")


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

    busy = RecognitionCallError("429", kind="busy")
    client = _Scripted(busy, busy)
    reading = await read_with_retry(client, _IMAGE, sleep=sleep)
    assert reading.text == "ok"
    assert client.calls == 3
    assert delays == [0.5, 1.0]


async def test_retry_gives_up_after_three_retries() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    busy = RecognitionCallError("503", kind="busy")
    client = _Scripted(busy, busy, busy, busy, busy)
    with pytest.raises(RecognitionCallError):
        await read_with_retry(client, _IMAGE, sleep=sleep)
    assert client.calls == 4
    assert delays == [0.5, 1.0, 2.0]


async def test_client_errors_are_not_retried() -> None:
    async def sleep(delay: float) -> None:
        raise AssertionError("no backoff for a 400")

    client = _Scripted(
        RecognitionCallError("400", kind="rejected"), RecognitionCallError("400", kind="rejected")
    )
    with pytest.raises(RecognitionCallError):
        await read_with_retry(client, _IMAGE, sleep=sleep)
    assert client.calls == 1


@pytest.mark.parametrize("api", ["layout", "chat"])
async def test_a_malformed_answer_still_carries_the_usage_billed(api: str) -> None:
    # Review #6: the vendor charged for the call whatever its shape.
    body = {"id": "x", "usage": {"prompt_tokens": 900, "completion_tokens": 40}}
    async with _http(lambda _: httpx.Response(200, json=body)) as http:
        client: Any = (
            LayoutParsingClient(url="https://x/lp", api_key="k", model="glm-ocr", http=http)
            if api == "layout"
            else ChatVlmClient(url="https://x/c", api_key="k", model="m", http=http)
        )
        with pytest.raises(RecognitionCallError) as caught:
            await client.read(_IMAGE)
    assert caught.value.kind == "bad_response"
    assert (caught.value.input_tokens, caught.value.output_tokens) == (900, 40)


def test_api_keys_stay_out_of_reprs() -> None:
    layout = LayoutParsingClient(url="https://x/lp", api_key="sk-secret-1", model="glm-ocr")
    chat = ChatVlmClient(url="https://x/c", api_key="sk-secret-2", model="m")
    assert "sk-secret" not in repr(layout) + repr(chat)


async def test_the_concurrency_slot_is_not_held_while_backing_off() -> None:
    # Review #7: a page backing off for a retry must not starve the others.
    limit = asyncio.Semaphore(1)
    held_while_sleeping: list[bool] = []

    async def sleep(delay: float) -> None:
        del delay
        held_while_sleeping.append(limit.locked())

    busy = RecognitionCallError("429", kind="busy")
    reading = await read_with_retry(_Scripted(busy, busy), _IMAGE, sleep=sleep, limit=limit)
    assert reading.text == "ok"
    assert held_while_sleeping == [False, False]
    assert not limit.locked()
