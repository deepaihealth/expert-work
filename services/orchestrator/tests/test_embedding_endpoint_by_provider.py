"""B-308 §4.9 — embeddings go to the vendor the platform selected.

Before this fix every embeddings request went to one vendor's address whatever
provider was configured, so another vendor's key was sent to the wrong host.
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.llm.embedder import embedding_client_for
from orchestrator.llm.endpoints import EndpointNotSupportedError, official_endpoint

_OFFICIAL_EMBEDDINGS = {
    "openai": "https://api.openai.com/v1/embeddings",
    "glm": "https://open.bigmodel.cn/api/paas/v4/embeddings",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "expected"), sorted(_OFFICIAL_EMBEDDINGS.items()))
async def test_embeddings_request_goes_to_the_providers_official_address(
    provider: str, expected: str
) -> None:
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = embedding_client_for(
        provider, api_key="sk-test", transport=httpx.MockTransport(_handler)
    )
    await client.embeddings(model="m", texts=["hi"])

    assert seen == [expected]


def test_provider_without_an_embeddings_endpoint_is_refused() -> None:
    with pytest.raises(EndpointNotSupportedError):
        official_endpoint("kimi", "embeddings")
    with pytest.raises(EndpointNotSupportedError):
        embedding_client_for("kimi", api_key="k")
