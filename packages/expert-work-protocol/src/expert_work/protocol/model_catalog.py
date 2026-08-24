"""Per-provider model catalog — Stream S PR B (Mini-ADR S-4).

Drives the visual manifest editor's model dropdown: provider → selectable
models + capability flags. ``vision`` gates whether ``ModelSpec.supports_vision``
may be set; ``embeddings`` marks providers usable for long-term memory.

Kept current by hand (small, single source). When extending, verify the
provider's *current* in-sale model names + vision capability against the
provider's official docs — do NOT carry stale names. Mark retired models
``deprecated=True`` so they stay referenceable but drop out of the dropdown
(``models_for_provider``).

Last verified: 2026-08 (glm/qwen additions) against each provider's official
API docs; other providers 2026-07.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from expert_work.protocol.provider_catalog import PROVIDER_CATALOG, Provider


class ModelEntry(BaseModel):
    """One selectable model for a provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    vision: bool = False
    embeddings: bool = False
    # ``rerank`` marks rerank-capable models for the platform rerank config (Stream T).
    rerank: bool = False
    context_window: int | None = None
    deprecated: bool = False
    # Stream CM-9 (Mini-ADR CM-J3) / CM-10 (Mini-ADR CM-L1) — compute-control
    # capability bits. ``thinking`` is the vendor's runtime thinking-depth
    # control shape (vendor params verified 2026-06-10):
    #   "effort" — native multi-level knob (Anthropic ``output_config.effort``;
    #              OpenAI/Azure/DeepSeek ``reasoning_effort``)
    #   "budget" — continuous thinking-token budget (Qwen ``enable_thinking`` +
    #              ``thinking_budget``; Doubao ``thinking.budget_tokens``)
    #   "toggle" — on/off only, no depth (GLM / Kimi K2.5+ ``thinking.type``)
    #   None     — no runtime control (Haiku; always-thinking models like
    #              deepseek-reasoner / kimi-k2-thinking; embeddings)
    # ``sampling`` marks models still accepting ``temperature``/``top_p`` —
    # Anthropic removed sampling params from Opus 4.7+ (sending one is a 400).
    thinking: Literal["effort", "budget", "toggle"] | None = None
    # Stream Thinking-Toggle — the model's DEFAULT thinking state, surfaced to
    # the config UI to seed the per-agent thinking switch. Only meaningful when
    # ``thinking`` is not None (no runtime knob → no switch). Per-model truth
    # (verified per vendor 2026-06): every in-sale thinking-capable flagship
    # currently defaults thinking ON; set ``False`` when a default-off model is
    # added. ``can disable`` is NOT a field — it is derived as
    # ``thinking == "effort" and provider != "anthropic"`` (reasoning_effort has
    # no off level, only ``minimal``).
    thinking_default: bool = False
    sampling: bool = True
    # Stream HX-13 (Mini-ADR HX-J5) — vendor-native tool-disclosure tier:
    #   "native_search"  — Anthropic tool-search beta: deferred tools go to
    #                      the API with ``defer_loading: true`` (server-side
    #                      retrieval; beta header tool-search-tool-2025-10-19).
    #   "allowed_tools"  — OpenAI/Azure ``tool_choice.allowed_tools``: the
    #                      full schema set is frozen on the wire (prompt-cache
    #                      friendly) and promotion drives the allowed SUBSET.
    #   None             — application tier (HX-12 find_tools RAG), the
    #                      semantic floor every provider gets.
    # Declarative (CM-L5): the catalog is the truth, no runtime probing.
    # OpenAI-compatible vendors (kimi/glm/deepseek/qwen/doubao/self-hosted)
    # stay None until their allowed_tools passthrough is individually
    # verified against official docs.
    tool_disclosure: Literal["native_search", "allowed_tools"] | None = None


#: Provider → its models. Verify names/capabilities against official docs when
#: editing (Mini-ADR S-4).
MODEL_CATALOG: dict[Provider, tuple[ModelEntry, ...]] = {
    # Anthropic — docs.anthropic.com/en/docs/about-claude/models/overview (2026-07)
    # IDs use dateless format since 4.6 generation. claude-opus-4-8 is flagship.
    # Opus 4.8 / Sonnet 4.6 carry a 1M context window that is GA (on by default,
    # no beta header) since the 4.8 generation — 4.6/4.7 gated it behind the
    # context-1m beta; our client sends no 1M header and still gets 1M. Haiku 4.5
    # stays 200K.
    "anthropic": (
        # CM-9: opus-4-8 dropped sampling params (4.7+ removal); haiku has
        # no effort support — verified against the Anthropic docs 2026-07.
        ModelEntry(
            name="claude-opus-4-8",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            sampling=False,
            tool_disclosure="native_search",
        ),
        ModelEntry(
            name="claude-sonnet-4-6",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            tool_disclosure="native_search",
        ),
        ModelEntry(name="claude-haiku-4-5", vision=True, context_window=200_000),
    ),
    # OpenAI — platform.openai.com/docs/models (2026-07)
    # GPT-5.5 / GPT-5.5 Pro (2026-04-24) are the current production flagships and
    # support vision, both with a 1M API context window (>272K input is
    # long-context priced but the window itself is 1M); gpt-5.4-mini (400K) stays
    # for low-latency/cost. gpt-4o family is retired from the API but kept
    # deprecated so existing manifests resolve.
    "openai": (
        ModelEntry(
            name="gpt-5.5",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            tool_disclosure="allowed_tools",
        ),
        ModelEntry(
            name="gpt-5.5-pro",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            tool_disclosure="allowed_tools",
        ),
        ModelEntry(
            name="gpt-5.4-mini",
            vision=True,
            context_window=400_000,
            thinking="effort",
            thinking_default=True,
            tool_disclosure="allowed_tools",
        ),
        ModelEntry(name="text-embedding-3-large", embeddings=True),
        ModelEntry(name="gpt-4o", vision=True, context_window=128_000, deprecated=True),
        ModelEntry(name="gpt-4o-mini", vision=True, context_window=128_000, deprecated=True),
    ),
    # DeepSeek — api-docs.deepseek.com (2026-07)
    # deepseek-v4-pro / deepseek-v4-flash are current (1M context, dual mode).
    # deepseek-chat / deepseek-reasoner are the retired legacy aliases (map to
    # deepseek-v4-flash non-thinking / thinking; vendor retirement 2026-07-24) —
    # marked deprecated so they drop out of the dropdown but existing manifests
    # still resolve during the transition. Their published 64K window is the old
    # V3-era value (V4 is 1M); left as-is since they are on the way out.
    "deepseek": (
        ModelEntry(
            name="deepseek-v4-pro",
            vision=False,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
        ),
        ModelEntry(
            name="deepseek-v4-flash",
            vision=False,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
        ),
        ModelEntry(name="deepseek-chat", vision=False, context_window=64_000, deprecated=True),
        ModelEntry(name="deepseek-reasoner", vision=False, context_window=64_000, deprecated=True),
    ),
    # Kimi (Moonshot AI) — platform.kimi.com/docs (2026-07)
    # kimi-k3 (2.8T MoE, weights due 2026-07-27) is the new flagship: natively
    # multimodal with a 1M context. thinking is None on purpose — K3 is ALWAYS
    # thinking (no off switch) and today only accepts reasoning_effort=max (its
    # default), so we send no thinking field (the always-thinking → None
    # convention, cf. deepseek-reasoner). None also makes the compat build gate
    # reject a manifest that sets effort / thinking_enabled on K3 — which both
    # avoids the K2.x ``thinking.type`` param the K3 docs forbid and the non-max
    # reasoning_effort levels K3 rejects. Revisit to "effort" once K3 exposes
    # multiple reasoning_effort tiers. (K3's sampling params are fixed; the
    # openai-compat build path does not honor ``sampling=False`` today — only the
    # anthropic path does — so temperature is still sent and K3 ignores it.)
    # kimi-k2.6 (2026-04-20) is natively multimodal — text + image + video via
    # the MoonViT encoder — with a 256K context; k2.5 also accepts images and is
    # 256K too (K2.6's gain over K2.5 is stability at length, not window size).
    # The moonshot-v1 series is text-only and being phased out (kept deprecated).
    "kimi": (
        ModelEntry(
            name="kimi-k3",
            vision=True,
            context_window=1_000_000,
        ),
        ModelEntry(
            name="kimi-k2.6",
            vision=True,
            context_window=256_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(
            name="kimi-k2.5",
            vision=True,
            context_window=256_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(name="moonshot-v1-128k", vision=False, context_window=128_000, deprecated=True),
        ModelEntry(name="moonshot-v1-32k", vision=False, context_window=32_000, deprecated=True),
    ),
    # Zhipu GLM — open.bigmodel.cn (2026-08)
    # glm-5.3 (released 2026-08-14; same base as 5.2 with extended long-horizon
    # post-training; 1M ctx per the bigmodel pricing page) is the current text
    # flagship; glm-5.2 (1M) stays. glm-5.1 (200K), glm-4.7 (355B MoE, 200K)
    # and glm-4.6 (200K) are current text models. Vision goes through
    # glm-5v-turbo (multimodal Agent/coding base, 200K, thinking.type toggle
    # per the bigmodel VLM docs), glm-4.6v (128K) and glm-4.5v. The older
    # glm-4*-plus line is kept deprecated so existing manifests resolve.
    "glm": (
        ModelEntry(
            name="glm-5.3",
            vision=False,
            context_window=1_000_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(
            name="glm-5.2",
            vision=False,
            context_window=1_000_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(
            name="glm-5.1",
            vision=False,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(
            name="glm-4.7",
            vision=False,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(
            name="glm-4.6",
            vision=False,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(
            name="glm-5v-turbo",
            vision=True,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
        ),
        ModelEntry(name="glm-4.6v", vision=True, context_window=128_000),
        ModelEntry(name="glm-4.5v", vision=True),
        # Platform embedding model (Stream T, user-specified).
        ModelEntry(name="embedding-3", embeddings=True),
        ModelEntry(name="glm-4-plus", vision=False, context_window=128_000, deprecated=True),
        ModelEntry(name="glm-4v-plus", vision=True, context_window=8_000, deprecated=True),
        ModelEntry(name="glm-4.1v-thinking", vision=True, context_window=32_000, deprecated=True),
    ),
    # Alibaba Qwen / DashScope (Model Studio / 百炼) — help.aliyun.com/zh/model-studio (2026-08)
    # qwen3.8-max (released 2026-08; 2.4T MoE) is the current flagship — natively
    # multimodal (image/video/text input per the Model Studio product page) with
    # a 1M context; thinking stays the enable_thinking + thinking_budget shape
    # (Model Studio deep-thinking docs list it as supported). qwen3.7-max (text,
    # ~1M) and qwen3.6-plus (multimodal, ~1M) stay. qwen3.5-plus is the prior
    # multimodal tier; qwen3-vl-* are the vision tiers. Context windows left
    # unset where not confirmed against the 百炼 console. Legacy qwen-max /
    # qwen-vl-max kept deprecated.
    "qwen": (
        ModelEntry(
            name="qwen3.8-max",
            vision=True,
            context_window=1_000_000,
            thinking="budget",
            thinking_default=True,
        ),
        ModelEntry(
            name="qwen3.7-max",
            vision=False,
            context_window=1_000_000,
            thinking="budget",
            thinking_default=True,
        ),
        ModelEntry(
            name="qwen3.6-plus",
            vision=True,
            context_window=1_000_000,
            thinking="budget",
            thinking_default=True,
        ),
        ModelEntry(name="qwen3.5-plus", vision=True, thinking="budget", thinking_default=True),
        ModelEntry(name="qwen3-max", vision=False, thinking="budget", thinking_default=True),
        ModelEntry(name="qwen3-vl-plus", vision=True),
        ModelEntry(name="qwen3-vl-flash", vision=True),
        # Platform embedding model (Stream T, user-specified).
        ModelEntry(name="text-embedding-v4", embeddings=True),
        # Platform rerank model (Stream T, user-specified).
        ModelEntry(name="qwen3-vl-rerank", rerank=True),
        ModelEntry(name="qwen-max", vision=False, context_window=32_000, deprecated=True),
        ModelEntry(name="qwen-vl-max", vision=True, context_window=32_000, deprecated=True),
    ),
    # Doubao (ByteDance Volcano Engine) — volcengine.com (2026-06)
    # Seed 2.1 (doubao-seed-2-1-pro-260628, dated model ID) is the current
    # flagship; Seed 2.0 family stays. All tiers support vision and 256K
    # context. Older doubao-*-32k series superseded.
    "doubao": (
        ModelEntry(
            name="doubao-seed-2-1-pro-260628",
            vision=True,
            context_window=256_000,
            thinking="budget",
            thinking_default=True,
        ),
        ModelEntry(
            name="doubao-seed-2.0-pro",
            vision=True,
            context_window=256_000,
            thinking="budget",
            thinking_default=True,
        ),
        ModelEntry(
            name="doubao-seed-2.0-lite",
            vision=True,
            context_window=256_000,
            thinking="budget",
            thinking_default=True,
        ),
        ModelEntry(name="doubao-pro-32k", vision=False, context_window=32_000, deprecated=True),
        ModelEntry(
            name="doubao-vision-pro-32k", vision=True, context_window=32_000, deprecated=True
        ),
    ),
}


def catalog_entry(provider: str, name: str) -> ModelEntry | None:
    """Exact-name catalog lookup — ``None`` for off-catalog models.

    Stream CM-9 — the agent factory gates compute-control parameters
    (``effort`` / sampling) on these capability bits; an off-catalog
    model (custom gateway / self-hosted) is not gated.
    """
    entries: tuple[ModelEntry, ...] = MODEL_CATALOG.get(provider, ())  # type: ignore[call-overload]
    for entry in entries:
        if entry.name == name:
            return entry
    return None


def models_for_provider(provider: str) -> tuple[ModelEntry, ...]:
    """Non-deprecated models for ``provider`` (empty for unknown providers)."""
    if provider not in PROVIDER_CATALOG:
        return ()
    entries = MODEL_CATALOG.get(provider, ())
    return tuple(e for e in entries if not e.deprecated)
