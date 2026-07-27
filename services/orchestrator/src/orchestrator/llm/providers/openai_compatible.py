"""OpenAI-compatible domestic / regional provider presets — Stream E.11.5.

Five major China-market LLM vendors expose an OpenAI Chat Completions
compatible HTTP API. Rather than copy-paste an adapter per vendor, we
reuse :class:`~orchestrator.llm.providers.openai.OpenAIProvider` and
configure its underlying :class:`~orchestrator.llm.providers.openai.HTTPOpenAIClient`
with the right base URL + path suffix.

Vendors covered:

+----------+------------------------------+-------------------------------+--------------+
| Brand    | Operator                     | Base URL                      | Path         |
+==========+==============================+===============================+==============+
| kimi     | Moonshot AI                  | api.moonshot.cn               | /v1          |
| glm      | Zhipu AI / Tsinghua          | open.bigmodel.cn              | /api/paas/v4 |
| deepseek | DeepSeek                     | api.deepseek.com              | /v1          |
| qwen     | Alibaba DashScope            | dashscope.aliyuncs.com        | /v1          |
|          |                              |   (compatible-mode/)          |              |
| doubao   | ByteDance Volcengine ARK     | ark.cn-beijing.volces.com     | /api/v3      |
+----------+------------------------------+-------------------------------+--------------+

API-key conventions:

- **Kimi**: ``sk-...`` token from Moonshot platform → ``Authorization: Bearer``.
- **GLM**: API key from Zhipu open platform → ``Authorization: Bearer``.
  (Zhipu also has a JWT-token mode for some endpoints; we use the simple
  bearer flow that ``open.bigmodel.cn/api/paas/v4`` supports.)
- **DeepSeek**: ``sk-...`` → ``Authorization: Bearer``.
- **Qwen / DashScope**: ``sk-...`` → ``Authorization: Bearer`` (the
  ``compatible-mode/v1`` endpoint specifically accepts the OpenAI-style
  header; the native DashScope endpoint uses ``X-DashScope-API-Key``,
  which we don't target here).
- **Doubao / Volcengine ARK**: endpoint API key → ``Authorization: Bearer``.
  The ``model`` field carries the deployment endpoint id (e.g.
  ``ep-2024xxxx-abcde``) rather than a model family name.

All five share OpenAI's Chat Completions request / response shape +
tool-calling format, so :class:`HTTPOpenAIClient` (with overridable
``chat_completions_path``) handles them uniformly.

Mid-conversation ``SystemMessage`` handling (Stream RT-2, RT-ADR-5):
these presets carry no message mapping of their own — every vendor
rides :meth:`~orchestrator.llm.providers.openai.OpenAIProvider.complete`,
which coalesces mid-conversation SystemMessages (the L2
``<context-summary>``) into the leading system entry before mapping.
Several of these backends (and self-hosted vLLM / SGLang) are strict
about the ``system`` position and reject non-leading system roles with
400; see :mod:`orchestrator.llm.coalesce` for the forensics.

Region note: these base URLs target the public production regions
documented by each vendor as of E.11.5 landing. Vendors that offer
multi-region deployments (Doubao ``ark.{region}.volces.com``) accept a
``base_url`` override on the factory function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import httpx

from orchestrator.llm.providers.openai import (
    DEFAULT_CHAT_COMPLETIONS_PATH,
    HTTPOpenAIClient,
    OpenAIProvider,
)
from orchestrator.llm.structured_output import StructuredOutputCapability


@dataclass
class OpenAICompatibleProvider(OpenAIProvider):
    """:class:`OpenAIProvider` with the conservative structured-output path.

    Stream RT-1 (RT-ADR-2, § 7.5): ``response_format`` behaviour across
    the OpenAI-compatible vendors is inconsistent (``json_object`` vs
    ``json_schema`` vs unsupported), so providers built around this
    module's factory clients take the ``prompt`` path — the schema is
    injected as a trailing system instruction and the router's
    validate + retry loop is the enforcement. Everything else is
    inherited from :class:`OpenAIProvider` unchanged.
    """

    #: RT-ADR-2 — conservative prompt path for every compat vendor.
    structured_output_capability: ClassVar[StructuredOutputCapability] = "prompt"


# ---------------------------------------------------------------------------
# Per-vendor URL constants
# ---------------------------------------------------------------------------

KIMI_BASE_URL = "https://api.moonshot.cn"

GLM_BASE_URL = "https://open.bigmodel.cn"
GLM_CHAT_COMPLETIONS_PATH = "/api/paas/v4/chat/completions"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode"

DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com"
DOUBAO_CHAT_COMPLETIONS_PATH = "/api/v3/chat/completions"


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def make_kimi_client(
    api_key: str,
    *,
    base_url: str = KIMI_BASE_URL,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """Moonshot AI / Kimi — OpenAI-compatible at ``api.moonshot.cn/v1``."""
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=DEFAULT_CHAT_COMPLETIONS_PATH,
    )


def make_glm_client(
    api_key: str,
    *,
    base_url: str = GLM_BASE_URL,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """Zhipu AI / GLM — OpenAI-compatible at ``open.bigmodel.cn/api/paas/v4``.

    Note the non-default ``chat_completions_path`` — Zhipu's gateway does
    not host the endpoint at ``/v1/chat/completions``.
    """
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=GLM_CHAT_COMPLETIONS_PATH,
    )


def make_deepseek_client(
    api_key: str,
    *,
    base_url: str = DEEPSEEK_BASE_URL,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """DeepSeek — OpenAI-compatible at ``api.deepseek.com/v1``."""
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=DEFAULT_CHAT_COMPLETIONS_PATH,
    )


def make_qwen_client(
    api_key: str,
    *,
    base_url: str = QWEN_BASE_URL,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """Alibaba Qwen via DashScope compatible-mode — OpenAI-format at
    ``dashscope.aliyuncs.com/compatible-mode/v1``.

    The native DashScope endpoint uses a different wire format and
    ``X-DashScope-API-Key`` header; only the compatible-mode path is
    OpenAI-format and is what this factory targets.
    """
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=DEFAULT_CHAT_COMPLETIONS_PATH,
    )


def make_doubao_client(
    api_key: str,
    *,
    base_url: str = DOUBAO_BASE_URL,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """ByteDance Doubao via Volcengine ARK — OpenAI-compatible at
    ``ark.cn-beijing.volces.com/api/v3``.

    ``model`` should be set to the ARK deployment endpoint id
    (``ep-2024xxxx-abcde``) rather than a model family name when
    constructing the :class:`OpenAIProvider`.
    """
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=DOUBAO_CHAT_COMPLETIONS_PATH,
    )


def make_self_hosted_client(
    api_key: str,
    *,
    base_url: str,
    chat_completions_path: str = DEFAULT_CHAT_COMPLETIONS_PATH,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """A self-hosted OpenAI-compatible server (vLLM / Ollama / …).

    Only ``base_url`` differs from stock OpenAI — auth stays
    ``Authorization: Bearer``. ``chat_completions_path`` is overridable
    for servers that don't host the endpoint at ``/v1/chat/completions``.
    """
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=chat_completions_path,
    )


def make_azure_client(
    api_key: str,
    *,
    endpoint: str,
    deployment: str,
    api_version: str,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> HTTPOpenAIClient:
    """Azure OpenAI Service — OpenAI wire format, deployment-style URL.

    The chat-completions endpoint is
    ``{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}``
    and auth is the ``api-key`` header (not ``Authorization: Bearer``).
    """
    path = f"/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    return HTTPOpenAIClient(
        api_key=api_key,
        base_url=endpoint.rstrip("/"),
        timeout_s=timeout_s,
        transport=transport,
        http=http,
        chat_completions_path=path,
        api_key_header="api-key",
        api_key_prefix="",
    )
