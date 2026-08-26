"""LLM caller + provider routing subsystem — Stream E.6 + E.11.

Public surface:

- :class:`LLMCaller` (E.6) — the abstract callable the ReAct graph
  consumes.
- :class:`LLMRouter` (E.11) — multi-provider fallback router that
  implements :class:`LLMCaller`.
- :class:`ProviderHandle` / :class:`LLMProvider` /
  :class:`AllProvidersExhaustedError` (E.11) — building blocks.
- Concrete adapters (:class:`AnthropicProvider`, :class:`OpenAIProvider`)
  and their recording / HTTP client variants for production + tests.
"""

from orchestrator.llm.caller import LLMCaller as LLMCaller
from orchestrator.llm.embedder import DEFAULT_EMBEDDINGS_PATH as DEFAULT_EMBEDDINGS_PATH
from orchestrator.llm.embedder import QWEN_EMBEDDING_BASE_URL as QWEN_EMBEDDING_BASE_URL
from orchestrator.llm.embedder import Embedder as Embedder
from orchestrator.llm.embedder import EmbeddingClient as EmbeddingClient
from orchestrator.llm.embedder import FakeEmbedder as FakeEmbedder
from orchestrator.llm.embedder import HTTPEmbeddingClient as HTTPEmbeddingClient
from orchestrator.llm.embedder import OpenAICompatibleEmbedder as OpenAICompatibleEmbedder
from orchestrator.llm.oauth_provider import (
    OAuthCapableProvider as OAuthCapableProvider,
)
from orchestrator.llm.providers import (
    DEEPSEEK_BASE_URL as DEEPSEEK_BASE_URL,
)
from orchestrator.llm.providers import (
    DEFAULT_CHAT_COMPLETIONS_PATH as DEFAULT_CHAT_COMPLETIONS_PATH,
)
from orchestrator.llm.providers import (
    DEFAULT_MAX_TOKENS as DEFAULT_MAX_TOKENS,
)
from orchestrator.llm.providers import (
    DOUBAO_BASE_URL as DOUBAO_BASE_URL,
)
from orchestrator.llm.providers import (
    DOUBAO_CHAT_COMPLETIONS_PATH as DOUBAO_CHAT_COMPLETIONS_PATH,
)
from orchestrator.llm.providers import (
    GLM_BASE_URL as GLM_BASE_URL,
)
from orchestrator.llm.providers import (
    GLM_CHAT_COMPLETIONS_PATH as GLM_CHAT_COMPLETIONS_PATH,
)
from orchestrator.llm.providers import (
    KIMI_BASE_URL as KIMI_BASE_URL,
)
from orchestrator.llm.providers import (
    QWEN_BASE_URL as QWEN_BASE_URL,
)
from orchestrator.llm.providers import (
    AnthropicClient as AnthropicClient,
)
from orchestrator.llm.providers import (
    AnthropicProvider as AnthropicProvider,
)
from orchestrator.llm.providers import (
    HTTPAnthropicClient as HTTPAnthropicClient,
)
from orchestrator.llm.providers import (
    HTTPOpenAIClient as HTTPOpenAIClient,
)
from orchestrator.llm.providers import (
    OpenAIClient as OpenAIClient,
)
from orchestrator.llm.providers import (
    OpenAICompatibleProvider as OpenAICompatibleProvider,
)
from orchestrator.llm.providers import (
    OpenAIProvider as OpenAIProvider,
)
from orchestrator.llm.providers import (
    RecordingAnthropicClient as RecordingAnthropicClient,
)
from orchestrator.llm.providers import (
    RecordingOpenAIClient as RecordingOpenAIClient,
)
from orchestrator.llm.providers import (
    make_azure_client as make_azure_client,
)
from orchestrator.llm.providers import (
    make_deepseek_client as make_deepseek_client,
)
from orchestrator.llm.providers import (
    make_doubao_client as make_doubao_client,
)
from orchestrator.llm.providers import (
    make_glm_client as make_glm_client,
)
from orchestrator.llm.providers import (
    make_kimi_client as make_kimi_client,
)
from orchestrator.llm.providers import (
    make_qwen_client as make_qwen_client,
)
from orchestrator.llm.providers import (
    make_self_hosted_client as make_self_hosted_client,
)
from orchestrator.llm.rate_limit import (
    DEFAULT_TIME_PERIOD_S as DEFAULT_TIME_PERIOD_S,
)
from orchestrator.llm.rate_limit import (
    RateLimitedProvider as RateLimitedProvider,
)
from orchestrator.llm.rate_limit import (
    effective_rpm as effective_rpm,
)
from orchestrator.llm.rerank import (
    DASHSCOPE_RERANK_URL as DASHSCOPE_RERANK_URL,
)
from orchestrator.llm.rerank import (
    DashScopeReranker as DashScopeReranker,
)
from orchestrator.llm.rerank import (
    HTTPDashScopeRerankClient as HTTPDashScopeRerankClient,
)
from orchestrator.llm.rerank import (
    RerankClient as RerankClient,
)
from orchestrator.llm.router import (
    AllProvidersExhaustedError as AllProvidersExhaustedError,
)
from orchestrator.llm.router import (
    LLMProvider as LLMProvider,
)
from orchestrator.llm.router import (
    LLMRouter as LLMRouter,
)
from orchestrator.llm.router import (
    ProviderHandle as ProviderHandle,
)

__all__ = [
    "DASHSCOPE_RERANK_URL",
    "DEEPSEEK_BASE_URL",
    "DEFAULT_CHAT_COMPLETIONS_PATH",
    "DEFAULT_EMBEDDINGS_PATH",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIME_PERIOD_S",
    "DOUBAO_BASE_URL",
    "DOUBAO_CHAT_COMPLETIONS_PATH",
    "GLM_BASE_URL",
    "GLM_CHAT_COMPLETIONS_PATH",
    "KIMI_BASE_URL",
    "QWEN_BASE_URL",
    "QWEN_EMBEDDING_BASE_URL",
    "AllProvidersExhaustedError",
    "AnthropicClient",
    "AnthropicProvider",
    "DashScopeReranker",
    "Embedder",
    "EmbeddingClient",
    "FakeEmbedder",
    "HTTPAnthropicClient",
    "HTTPDashScopeRerankClient",
    "HTTPEmbeddingClient",
    "HTTPOpenAIClient",
    "LLMCaller",
    "LLMProvider",
    "LLMRouter",
    "OAuthCapableProvider",
    "OpenAIClient",
    "OpenAICompatibleEmbedder",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ProviderHandle",
    "RateLimitedProvider",
    "RecordingAnthropicClient",
    "RecordingOpenAIClient",
    "RerankClient",
    "effective_rpm",
    "make_azure_client",
    "make_deepseek_client",
    "make_doubao_client",
    "make_glm_client",
    "make_kimi_client",
    "make_qwen_client",
    "make_self_hosted_client",
]
