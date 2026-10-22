"""B-206 K-7c — the Cohere-style rerank client: one request per call, spelled
the way each vendor family spells it, answered in one shape, and a failed
request classified for the vendor-neutral guard."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.llm.rerank import RerankClient
from orchestrator.llm.rerank_cohere import (
    COHERE_STYLE_SHAPES,
    RERANK_SHAPE_BY_PROVIDER,
    CohereStyleShape,
    HTTPCohereStyleRerankClient,
    cohere_style_client,
)
from orchestrator.llm.rerank_guard import RerankCallError

_ANSWER = {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]}


def _recording(answer: Any, seen: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(status, json=answer)

    return httpx.MockTransport(_handler)


def _client(shape: str | CohereStyleShape, transport: httpx.MockTransport) -> RerankClient:
    resolved = COHERE_STYLE_SHAPES[shape] if isinstance(shape, str) else shape
    return HTTPCohereStyleRerankClient(
        api_key="sk-x", url="https://rerank.example/rerank", shape=resolved, transport=transport
    )


@pytest.mark.asyncio
async def test_zhipu_request_and_answer() -> None:
    """The Zhipu body as the 2026-10-22 live call took it: the raw logit is
    asked for (``return_raw_scores``), the echoed documents are not."""
    seen: dict[str, Any] = {}
    client = cohere_style_client("glm", api_key="sk-x", transport=_recording(_ANSWER, seen))
    body = await client.rerank(model="rerank", query="q", documents=["d0", "d1"], top_n=2)
    assert seen["url"] == "https://open.bigmodel.cn/api/paas/v4/rerank"
    assert seen["auth"] == "Bearer sk-x"
    assert seen["body"] == {
        "model": "rerank",
        "query": "q",
        "documents": ["d0", "d1"],
        "top_n": 2,
        "return_documents": False,
        "return_raw_scores": True,
    }
    assert body == _ANSWER


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["jina", "siliconflow", "xinference"])
async def test_the_common_shape(family: str) -> None:
    seen: dict[str, Any] = {}
    body = await _client(family, _recording(_ANSWER, seen)).rerank(
        model="m", query="q", documents=["d0", "d1"], top_n=1
    )
    assert seen["body"] == {
        "model": "m",
        "query": "q",
        "documents": ["d0", "d1"],
        "top_n": 1,
        "return_documents": False,
    }
    assert body == _ANSWER


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["cohere", "qianfan", "vllm"])
async def test_families_without_a_return_documents_parameter(family: str) -> None:
    """Cohere v2 never echoes documents; Qianfan and vLLM do not document the
    field — an undocumented field is not sent."""
    seen: dict[str, Any] = {}
    body = await _client(family, _recording(_ANSWER, seen)).rerank(
        model="m", query="q", documents=["d0", "d1"], top_n=2
    )
    assert seen["body"] == {"model": "m", "query": "q", "documents": ["d0", "d1"], "top_n": 2}
    assert body == _ANSWER


@pytest.mark.asyncio
async def test_voyage_counts_with_top_k_and_answers_in_data() -> None:
    seen: dict[str, Any] = {}
    answer = {
        "object": "list",
        "data": [{"relevance_score": 0.4, "index": 1}],
        "usage": {"total_tokens": 9},
    }
    body = await _client("voyage", _recording(answer, seen)).rerank(
        model="rerank-2.5", query="q", documents=["d0", "d1"], top_n=1
    )
    assert seen["body"]["top_k"] == 1
    assert "top_n" not in seen["body"]
    assert body == {"results": [{"index": 1, "relevance_score": 0.4}]}


@pytest.mark.asyncio
async def test_tei_takes_texts_and_answers_a_bare_list() -> None:
    """TEI serves one model: no ``model``, no result count (it scores every
    text), ``texts`` / ``return_text``, and a list of ``{index, score}``."""
    seen: dict[str, Any] = {}
    answer = [{"index": 1, "score": 0.97}, {"index": 0, "score": 0.01}]
    body = await _client("tei", _recording(answer, seen)).rerank(
        model="bge-reranker-v2-m3", query="q", documents=["d0", "d1"], top_n=1
    )
    assert seen["body"] == {"query": "q", "texts": ["d0", "d1"], "return_text": False}
    assert body == {
        "results": [{"index": 1, "relevance_score": 0.97}, {"index": 0, "relevance_score": 0.01}]
    }


@pytest.mark.asyncio
async def test_the_instruction_goes_in_the_family_field_only() -> None:
    seen: dict[str, Any] = {}
    shape = CohereStyleShape(path="/rerank", instruction_field="instruction")
    await _client(shape, _recording(_ANSWER, seen)).rerank(
        model="m", query="q", documents=["d"], top_n=1, instruction="judge it"
    )
    assert seen["body"]["instruction"] == "judge it"
    # A family with no instruction field sends none, and nothing is sent without one.
    await _client("jina", _recording(_ANSWER, seen)).rerank(
        model="m", query="q", documents=["d"], top_n=1, instruction="judge it"
    )
    assert "judge it" not in json.dumps(seen["body"])
    await _client(shape, _recording(_ANSWER, seen)).rerank(
        model="m", query="q", documents=["d"], top_n=1
    )
    assert "instruction" not in seen["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [{"unexpected": "shape"}, {"results": "x"}, [{"index": 0}], "not json"],
)
async def test_an_unreadable_answer_comes_back_empty(answer: Any) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        if answer == "not json":
            return httpx.Response(200, text="<html>ok</html>")
        return httpx.Response(200, json=answer)

    body = await _client("zhipu", httpx.MockTransport(_handler)).rerank(
        model="m", query="q", documents=["d"], top_n=1
    )
    assert body == {}


@pytest.mark.asyncio
async def test_rows_keep_only_index_and_score() -> None:
    answer = {
        "results": [
            {"index": 0, "relevance_score": 0.5, "document": {"text": "d0"}},
            "junk",
            {"relevance_score": 0.1},
        ]
    }
    body = await _client("jina", _recording(answer, {})).rerank(
        model="m", query="q", documents=["d0"], top_n=1
    )
    assert body == {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": None, "relevance_score": 0.1},
        ]
    }


#: Zhipu's answers recorded by the 2026-10-22 live probes (``rerank``): query +
#: documents over 128,000 characters; 129 documents (a parameter error worded
#: like a size limit, under the generic code); an unknown model.
_ZHIPU_TOO_LONG = {"error": {"code": "1214", "message": "query+document超长"}}
_ZHIPU_TOO_MANY = {"error": {"code": "1210", "message": "query+ documents不能超过32k字符"}}
_ZHIPU_NO_MODEL = {"error": {"code": "1211", "message": "模型不存在\uff0c请检查模型代码。"}}
#: 1214 is any field's 「参数非法」: only a length wording makes it a size refusal.
_ZHIPU_BAD_FIELD = {"error": {"code": "1214", "message": "top_n 参数非法。请检查文档"}}
#: Zhipu 429 codes (docs.bigmodel.cn 错误码 table): 1113 no balance, 1304 the
#: day's quota used up, 1308 a usage cap until its reset — account state; 1302
#: rate limit, 1305 model busy — load.
_ZHIPU_NO_BALANCE = {"error": {"code": "1113", "message": "余额不足"}}
_ZHIPU_DAILY_QUOTA = {"error": {"code": "1304", "message": "当日调用次数已用完"}}
_ZHIPU_USAGE_CAP = {"error": {"code": "1308", "message": "已达到使用上限"}}
_ZHIPU_BUSY = {"error": {"code": "1302", "message": "并发数过高"}}
_ZHIPU_MODEL_BUSY = {"error": {"code": "1305", "message": "该模型当前访问量过大"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "status", "error", "headers", "failure", "retry_after"),
    [
        ("zhipu", 429, _ZHIPU_BUSY, {}, "rate_limited", None),
        ("zhipu", 429, _ZHIPU_BUSY, {"Retry-After": "3"}, "rate_limited", 3.0),
        ("zhipu", 429, _ZHIPU_NO_BALANCE, {}, "rejected", None),
        ("zhipu", 429, _ZHIPU_DAILY_QUOTA, {}, "rejected", None),
        ("zhipu", 429, _ZHIPU_USAGE_CAP, {}, "rejected", None),
        ("zhipu", 429, _ZHIPU_MODEL_BUSY, {}, "rate_limited", None),
        # Another family's 429 with the same code is load: the codes are Zhipu's.
        ("jina", 429, _ZHIPU_NO_BALANCE, {}, "rate_limited", None),
        ("zhipu", 400, _ZHIPU_TOO_LONG, {}, "too_large", None),
        # Not a size refusal: the generic code, or the size code for another field.
        ("zhipu", 400, _ZHIPU_TOO_MANY, {}, "rejected", None),
        ("zhipu", 400, _ZHIPU_BAD_FIELD, {}, "rejected", None),
        # Under Zhipu only its size codes count, whatever the wording.
        (
            "zhipu",
            400,
            {"error": {"code": "1210", "message": "query too long"}},
            {},
            "rejected",
            None,
        ),
        # A family without codes: "exceed" alone is not about size.
        ("jina", 400, {"detail": "quota exceeded for this key"}, {}, "rejected", None),
        ("zhipu", 400, _ZHIPU_NO_MODEL, {}, "rejected", None),
        ("cohere", 400, {"message": "query is too long"}, {}, "too_large", None),
        ("jina", 422, {"detail": "Input length exceeds the maximum"}, {}, "too_large", None),
        ("vllm", 400, {"error": {"message": "invalid model", "code": 400}}, {}, "rejected", None),
        (
            "tei",
            413,
            {"error": "payload too large", "error_type": "Validation"},
            {},
            "too_large",
            None,
        ),
        ("jina", 401, {"detail": "Invalid token"}, {}, "rejected", None),
        ("jina", 500, {"detail": "input too long"}, {}, "transient", None),
        ("jina", 503, "<html>Service Unavailable</html>", {}, "transient", None),
        ("jina", 408, "timeout", {}, "transient", None),
        ("jina", 400, "<html>Bad Request</html>", {}, "rejected", None),
    ],
)
async def test_each_refusal_is_classified(
    family: str,
    status: int,
    error: Any,
    headers: dict[str, str],
    failure: str,
    retry_after: float | None,
) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(error, str):
            return httpx.Response(status, text=error, headers=headers)
        return httpx.Response(status, json=error, headers=headers)

    with pytest.raises(RerankCallError) as excinfo:
        await _client(family, httpx.MockTransport(_handler)).rerank(
            model="m", query="q", documents=["d"], top_n=1
        )
    assert (excinfo.value.failure, excinfo.value.status) == (failure, status)
    assert excinfo.value.retry_after_s == retry_after


@pytest.mark.asyncio
async def test_a_size_refusal_is_read_from_the_parsed_message() -> None:
    """A body that escapes its Chinese (``\\u8d85\\u8fc7``) still reads as a size
    refusal: the wording is looked for in the parsed message, not the raw text."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        escaped = json.dumps(_ZHIPU_TOO_LONG)  # ensure_ascii: no CJK in the raw text
        return httpx.Response(400, content=escaped, headers={"content-type": "application/json"})

    with pytest.raises(RerankCallError) as excinfo:
        await _client("zhipu", httpx.MockTransport(_handler)).rerank(
            model="m", query="q", documents=["d"], top_n=1
        )
    assert excinfo.value.failure == "too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.RemoteProtocolError("x")],
)
async def test_an_unreachable_vendor_is_transient(exc: httpx.TransportError) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(RerankCallError) as excinfo:
        await _client("zhipu", httpx.MockTransport(_handler)).rerank(
            model="m", query="q", documents=["d"], top_n=1
        )
    assert (excinfo.value.failure, excinfo.value.status) == ("transient", None)


def test_the_platform_reaches_zhipu_through_glm() -> None:
    assert RERANK_SHAPE_BY_PROVIDER == {"glm": "zhipu"}
    # Every family a platform provider maps to has a public host to call.
    for family in RERANK_SHAPE_BY_PROVIDER.values():
        assert COHERE_STYLE_SHAPES[family].host is not None
    with pytest.raises(ValueError, match="openai"):
        cohere_style_client("openai", api_key="k")


@pytest.mark.asyncio
async def test_a_validation_error_body_stays_out_of_the_message() -> None:
    """A 422 may quote the documents back; the error (which reaches logs) does not."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"input": "secret passage text"}]})

    with pytest.raises(RerankCallError) as excinfo:
        await _client("jina", httpx.MockTransport(_handler)).rerank(
            model="m", query="q", documents=["secret passage text"], top_n=1
        )
    assert "secret passage text" not in str(excinfo.value)
    assert excinfo.value.status == 422


def test_the_key_is_not_in_the_client_repr() -> None:
    client = cohere_style_client("glm", api_key="sk-very-secret")
    assert "sk-very-secret" not in repr(client)
