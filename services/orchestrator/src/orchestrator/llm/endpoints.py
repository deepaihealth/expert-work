"""Where each provider's API lives — one table for every kind of call (B-308).

Each provider has one **official root address** (it ends with the API version
segment) and one **path per use** appended to it. Every HTTP client that talks
to a provider takes its address from here, so a provider's address is written
down once.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

#: What a call is for; each use has its own path under the provider's root.
EndpointUse = Literal["chat", "embeddings", "rerank", "layout_parsing"]

#: provider → official root address, version segment included.
OFFICIAL_BASE_URLS: Mapping[str, str] = MappingProxyType(
    {
        "anthropic": "https://api.anthropic.com/v1",
        "openai": "https://api.openai.com/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "glm": "https://open.bigmodel.cn/api/paas/v4",
        "deepseek": "https://api.deepseek.com/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    }
)

#: provider → use → path appended to the root. A use that is missing is one the
#: provider does not offer through this platform.
_PATHS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "anthropic": {"chat": "/messages"},
        "openai": {"chat": "/chat/completions", "embeddings": "/embeddings"},
        "kimi": {"chat": "/chat/completions"},
        "glm": {
            "chat": "/chat/completions",
            "embeddings": "/embeddings",
            "rerank": "/rerank",
            "layout_parsing": "/layout_parsing",
        },
        "deepseek": {"chat": "/chat/completions"},
        "qwen": {"chat": "/chat/completions", "embeddings": "/embeddings"},
        "doubao": {"chat": "/chat/completions"},
    }
)

#: provider → what a request address must end with (its version segment). A
#: provider missing here takes no request address on its credential.
VERSION_SUFFIXES: Mapping[str, str] = MappingProxyType(
    {
        "anthropic": "/v1",
        "openai": "/v1",
        "kimi": "/v1",
        "glm": "/v4",
        "deepseek": "/v1",
        "qwen": "/compatible-mode/v1",
        "doubao": "/v3",
    }
)

#: Providers whose own documentation gives the API address without the version
#: segment — a form that asks for it has to say so.
DOCS_OMIT_VERSION_SEGMENT: frozenset[str] = frozenset({"anthropic", "deepseek"})

#: Every path the platform appends to a root; a request address must not
#: already end with one.
API_PATHS: tuple[str, ...] = tuple(
    sorted({path for paths in _PATHS.values() for path in paths.values()})
)

#: qwen's native rerank API is not under its ``/compatible-mode/v1`` root: the
#: root's last segment is replaced by this path.
QWEN_ROOT_SUFFIX = "/compatible-mode/v1"
_QWEN_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"


class EndpointNotSupportedError(ValueError):
    """The provider has no address for this use."""


@dataclass(frozen=True)
class ProviderKey:
    """One platform credential as a caller needs it: which secret to read and
    where to send it.

    ``base_url`` is the request address saved on the credential row (API root,
    version segment included); ``None`` = the provider's official address. The
    two travel together so that no caller can pick up the key and lose the
    address it was configured for.
    """

    secret_ref: str
    base_url: str | None = None


def fixed_provider_key_resolver(
    provider: str, key: ProviderKey
) -> Callable[[str], Awaitable[list[ProviderKey]]]:
    """A key resolver that answers with ``key`` — for ``provider`` only.

    For a caller that has already resolved the one platform credential it will
    use (judges, auxiliary models, the chat-model rerank branch) and hands it
    to the model router, so the key keeps its request address on the way. Asked
    for any other provider it raises: the key belongs to ``provider`` and must
    not be sent to another vendor's address.
    """

    async def _resolve(requested: str) -> list[ProviderKey]:
        if requested != provider:
            msg = f"this key is pinned for provider {provider!r}, not {requested!r}"
            raise ValueError(msg)
        return [key]

    return _resolve


@dataclass(frozen=True)
class Endpoint:
    """One resolved address: ``root`` + ``path``.

    ``custom`` is true when the root came from a credential's request address —
    the HTTP layer then sends the request through the address-checking client
    (see :func:`orchestrator.llm.providers._http.client_for`).
    """

    root: str
    path: str
    custom: bool = False

    @property
    def url(self) -> str:
        return f"{self.root}{self.path}"


def _endpoint(provider: str, use: EndpointUse, root: str, *, custom: bool) -> Endpoint:
    if provider == "qwen" and use == "rerank":
        return Endpoint(
            root=root.removesuffix(QWEN_ROOT_SUFFIX), path=_QWEN_RERANK_PATH, custom=custom
        )
    path = _PATHS.get(provider, {}).get(use)
    if path is None:
        msg = f"provider {provider!r} has no {use} endpoint"
        raise EndpointNotSupportedError(msg)
    return Endpoint(root=root, path=path, custom=custom)


def official_endpoint(provider: str, use: EndpointUse) -> Endpoint:
    """The provider's official address for ``use``.

    :raises EndpointNotSupportedError: the provider does not offer ``use``.
    """
    return endpoint_for(provider, use)


def endpoint_for(provider: str, use: EndpointUse, base_url: str | None = None) -> Endpoint:
    """The address for ``use`` on ``provider``: under the credential's request
    address ``base_url`` when it has one, else under the official root.

    A custom root gets exactly the path the official one gets (a trailing ``/``
    on it is dropped), so the two differ only in where they point.

    :raises EndpointNotSupportedError: the provider does not offer ``use`` (or
        takes its address from the agent manifest, not from a credential).
    """
    official = OFFICIAL_BASE_URLS.get(provider)
    if official is None:
        msg = f"provider {provider!r} has no {use} endpoint"
        raise EndpointNotSupportedError(msg)
    if base_url is None:
        return _endpoint(provider, use, official, custom=False)
    return _endpoint(provider, use, base_url.rstrip("/"), custom=True)


def endpoint_host(url: str) -> str:
    """``host[:port]`` of ``url`` — what logs, errors and audit rows may show of
    a request address. The path can carry a token and is never shown."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


def redact_endpoint(text: str, endpoint: Endpoint) -> str:
    """``text`` with a custom endpoint's address cut down to its origin.

    Error text that reaches users and logs must not carry the path of a custom
    request address. Text about an official address is returned unchanged.
    """
    if not endpoint.custom:
        return text
    parts = urlsplit(endpoint.root)
    origin = f"{parts.scheme}://{parts.netloc}"
    return re.sub(re.escape(origin) + r"/[^\s'\"<>)]*", origin, text)
