"""Per-provider model catalog — Stream S PR B (Mini-ADR S-4).

Drives the visual manifest editor's model dropdown: provider → selectable
models + capability flags. ``vision`` gates whether ``ModelSpec.supports_vision``
may be set; ``embeddings`` marks providers usable for long-term memory.

Kept current by hand (small, single source). When extending, verify the
provider's *current* in-sale model names + vision capability against the
provider's official docs — do NOT carry stale names. Mark retired models
``deprecated=True`` so they stay referenceable but drop out of the dropdown
(``models_for_provider``).

Last verified: 2026-10 (deepseek-flash) / 2026-08 (glm/qwen additions) against
each provider's official API docs; other providers 2026-07.

B-105 上架规矩(2026-09-24 测试集群实调建立)—— 新增或修改带思考 / 视觉能力的模型,
提交前必须在条目上写明:思考形态(``thinking``)、默认开关(``thinking_default`` /
``always_thinking``)、``output_cap_field``、``max_output_tokens``,并用实调探针
核过再合并(开思考 + cap=300、关思考、cap=10,000,000;探针模板见 spec §8)。
``output_cap_field`` / ``max_output_tokens`` 设错在 GLM / DeepSeek 上是**静默的**
(200 正常返回,上限不生效)——厂商文档与既有假设都靠不住,只认实调结果。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from expert_work.protocol.provider_catalog import PROVIDER_CATALOG, Provider

#: B-206 —— 厂商写长度上限用的计数单位(``EmbeddingCaps`` / ``RerankCaps`` 的
#: ``*_tokens`` 字段都按它读)。
#:   "token" —— 按厂商自己的分词器数。平台数不了(B-200:同样 8,000 个 o200k token,
#:              印地文在 text-embedding-v4 那边是 2.6 倍),所以落地时按同数值的 UTF-8
#:              字节截:每个 token 至少一个字节,N 字节必然 ≤ N token。
#:   "char"  —— 按字符数(表 5-B:智谱、腾讯重排),平台能精确数。
#: 默认 "token" 是保守的一侧:按字符的上限被误标成 token,字节截只会更短,不会超限。
LengthUnit = Literal["token", "char"]


class EmbeddingCaps(BaseModel):
    """B-206 K-5 / K-8 —— an embedding model's request capabilities.

    Read by the knowledge-base embedding paths (search queries and ingestion,
    ``orchestrator.llm.embedder.embedding_marker``); long-term memory does not
    use them. Values come from B-206 design §5.5 table 5-A (vendor docs read
    2026-10), corrected where a live measurement disagreed. ``None`` = the
    vendor docs say nothing / not checked yet → callers keep the platform
    default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # How the vendor tells a search query from a document being indexed —
    # table 5-A shows three shapes, plus one found by measurement:
    #   "none"   —— 什么都不发,两侧原样嵌入(OpenAI / 智谱 / 火山 / 自部署 bge-m3 …)。
    #   "param"  —— 枚举请求参数(Cohere ``input_type``、Jina ``task`` …):
    #               ``query_marker`` / ``document_marker`` 写成 ``"name=value"``,两侧都发。
    #   "prefix" —— 平台拼进输入文本的前缀,通常只加在查询侧(bge-zh / nomic / E5 /
    #               自部署 Qwen3):``query_marker`` 是含 ``{query}`` 的模板(可含
    #               ``{task}``,填 ``instruction``);``document_marker`` 可选(nomic 文档侧
    #               也要前缀),是含 ``{document}`` 的模板。
    #   "query_field" —— 只在查询请求里多发一个请求体字段,值是 ``instruction``;文档侧原样
    #               (阿里 text-embedding-v4 的 ``instruct`` 就是这个形态:实测改变查询向量,
    #               但开发集 A/B 没有收益,目录里没开,见该条目注释):``query_marker`` 是字段名。
    # 入库和查询必须用同一套标记(只有 Voyage 承诺带 / 不带互相兼容):文档侧有标记时,它进
    # 段上记的嵌入模型指纹(``knowledge_chunk.embedding_model``),换了不复用旧向量。
    asymmetry: Literal["none", "param", "prefix", "query_field"]
    query_marker: str | None = None
    document_marker: str | None = None
    # Instruction text sent with queries only: the ``{task}`` of a "prefix"
    # template, or the value of a "query_field" field. None = none sent.
    instruction: str | None = None
    # Path of the vendor-native endpoint the markers need, when the vendor's
    # OpenAI-compatible endpoint drops them. None = the markers, if any, work on
    # the endpoint the platform already calls. The platform has no native
    # embedding client yet, so no catalog entry may declare one (a test pins it).
    native_endpoint: str | None = None
    # K-8 —— 每次请求最多几条输入,多了要拆批(超了厂商整次 400)。
    max_batch: int | None = None
    # K-8 —— 单条输入上限(厂商文档值),单位见 ``length_unit``。只作记录,平台不按它截:
    # 文档值不是硬限(B-200 实测 text-embedding-v4 超过 16,000 token 才 400,文档写
    # 8,192),当字节套用又会无据地截短(embedding-3 会从 8,000 字节截到 3,072)。单条上限
    # 仍是部署的 ``embedding_max_input_bytes``。
    max_input_tokens: int | None = None
    length_unit: LengthUnit = "token"

    @model_validator(mode="after")
    def _markers_match_asymmetry(self) -> EmbeddingCaps:
        if self.asymmetry == "none":
            declared = (
                self.query_marker,
                self.document_marker,
                self.instruction,
                self.native_endpoint,
            )
            if any(value is not None for value in declared):
                # Nothing is sent for "none" — a declared marker would be silently dropped.
                msg = "asymmetry='none' takes no markers, instruction or native_endpoint"
                raise ValueError(msg)
        elif self.asymmetry == "param":
            if self.query_marker is None or self.document_marker is None:
                msg = "asymmetry='param' needs query_marker and document_marker"
                raise ValueError(msg)
            for marker in (self.query_marker, self.document_marker):
                name, sep, value = marker.partition("=")
                if not (name and sep and value):
                    msg = f"asymmetry='param' markers are 'name=value', got {marker!r}"
                    raise ValueError(msg)
        elif self.asymmetry == "query_field":
            if self.query_marker is None:
                msg = "asymmetry='query_field' needs query_marker (the field name)"
                raise ValueError(msg)
            if any(char in self.query_marker for char in "={} "):
                msg = (
                    "asymmetry='query_field' query_marker is a bare field name, "
                    f"got {self.query_marker!r}"
                )
                raise ValueError(msg)
            if self.instruction is None:
                msg = "asymmetry='query_field' needs an instruction (the field's value)"
                raise ValueError(msg)
            if self.document_marker is not None:
                msg = "asymmetry='query_field' takes no document_marker: documents are sent as is"
                raise ValueError(msg)
        else:
            if self.query_marker is None or "{query}" not in self.query_marker:
                msg = "asymmetry='prefix' needs a query_marker template containing '{query}'"
                raise ValueError(msg)
            if self.document_marker is not None and "{document}" not in self.document_marker:
                msg = "asymmetry='prefix' document_marker must contain '{document}'"
                raise ValueError(msg)
            if "{task}" in self.query_marker and self.instruction is None:
                msg = "asymmetry='prefix' query_marker '{task}' needs an instruction"
                raise ValueError(msg)
        return self


class RerankCaps(BaseModel):
    """B-206 K-7 —— a rerank model's request capabilities.

    Batch 0 only declares them; batch 1 replaces the hard-coded
    ``_MAX_RERANK_CANDIDATES`` / ``RERANK_DOC_MAX_CHARS`` with these. Values
    come from B-206 design §5.5 table 5-B (vendor docs read 2026-10).
    ``None`` = the vendor docs say nothing / not checked yet → callers keep
    the platform default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Request shape:
    #   "dashscope"    —— DashScope 原生重排接口;
    #   "cohere_style" —— ``POST …/rerank`` + ``{model, query, documents, top_n}``(Cohere /
    #                     Jina / Voyage / 智谱 / SiliconFlow / 千帆 / vLLM / TEI …,
    #                     ``top_n`` / ``top_k`` 之类的名字差异归客户端的适配表);
    #   "vertex"       —— Google ``rankingConfigs:rank``(``title`` 独立字段);
    #   "bedrock"      —— AWS agent-runtime ``Rerank``(``numberOfResults``);
    #   "llm"          —— 聊天模型排序,给没有重排接口的厂商。
    rerank_api: Literal["dashscope", "cohere_style", "vertex", "bedrock", "llm"]
    # 每次请求最多几篇文档(厂商上限)。
    max_documents: int | None = None
    # 长度上限,全部按 ``length_unit`` 计:单篇文档 / 整次请求(查询 + 全部文档;阿里超限
    # 整次 400、不截断)/ 查询本身(查询超长拆批也救不了,要单独校验)。
    max_doc_tokens: int | None = None
    max_total_tokens: int | None = None
    query_max_tokens: int | None = None
    length_unit: LengthUnit = "token"
    # 厂商容许的并发请求数。None = 平台默认。
    max_concurrency: int | None = None
    # 接受任务指令(阿里 ``instruct``、Voyage 2.5+、doubao-seed、自部署 Qwen3)。
    # False = 不发(K-14「有则用」;Cohere 文档明说不支持)。
    supports_instruction: bool = False
    # What a score means — decides whether a threshold can carry over:
    #   "normalized" —— 绝对的 0~1;
    #   "sigmoid"    —— sigmoid / yes-no 头给出的 0~1 概率;
    #   "logit"      —— 原始 logit,可为负,批 1 统一过 sigmoid;
    #   "relative"   —— 请求内相对分,不可跨请求比较。
    score_scale: Literal["normalized", "sigmoid", "logit", "relative"] | None = None
    # 厂商限流,每分钟请求数。None = 平台默认。
    rpm: int | None = None


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
    # no off level, only ``minimal``), refined per model by ``always_thinking``.
    thinking_default: bool = False
    # Per-model override of the provider-level "real off" derivation above:
    # the model cannot turn thinking off at all (vendor rejects the disable
    # form) — "off" degrades to the lowest effort tier instead, and the UI
    # shows the not-fully-off hint. First case: glm-5.3-flash (thinking.type
    # 仅支持 enabled per bigmodel docs 2026-08); kimi-k3 predates the field
    # and keeps its provider-branch handling.
    always_thinking: bool = False
    sampling: bool = True
    # B-33 — the model accepts exactly ONE ``temperature`` value (vendor 400
    # on anything else). The agent factory clamps the manifest value to it
    # at build time and logs a warning, so the vendor 400 never becomes the
    # validator (seen live 2026-08-28: an agent switched to kimi-k3 kept its
    # old 0.9 and every run died at the first call). ``None`` = no
    # constraint, manifest value sent as-is. Declared only where the vendor
    # docs say so; first case kimi-k3 (``temperature=1`` only).
    temperature_fixed: float | None = None
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
    # B-105 —— 请求里用哪个字段表示「思考 + 回答合计」的输出上限(2026-09-24 测试集群实调)。
    #   "max_tokens" / "max_completion_tokens" —— 该字段就是合计上限;
    #   "split" —— 没有合计字段(max_tokens 只管回答):开思考时发 thinking_budget=T、
    #              max_tokens=cap-T 拼出合计,关思考时发 max_tokens=cap。
    # **设错在 GLM / DeepSeek 上是静默的**(200 正常返回,上限不生效),改这里必须实调。
    output_cap_field: Literal["max_tokens", "max_completion_tokens", "split"] = "max_tokens"
    # B-105 —— 厂商接受的输出上限最大值(实调:发超大值看报错给的范围)。None = 厂商不报范围,不校验。
    max_output_tokens: int | None = None
    # B-105 —— 支持独立的思考长度硬上限(通义 thinking_budget,实调:设 200 思考停在 200 后照常作答)。
    thinking_cap: bool = False
    # B-105 —— 平台档位 → 厂商档位取值;缺的键按平台档位原样发。None = 全部原样。
    effort_map: dict[str, str] | None = None
    # B-206 批 0(K-5 / K-7 / K-8)—— 嵌入 / 重排模型的请求能力,只能挂在 ``embeddings`` /
    # ``rerank`` 为 True 的条目上。None = 未声明。批 0 只是骨架:运行时还没有任何地方读它们。
    embedding_caps: EmbeddingCaps | None = None
    rerank_caps: RerankCaps | None = None

    @model_validator(mode="after")
    def _caps_match_flags(self) -> ModelEntry:
        if self.embedding_caps is not None and not self.embeddings:
            msg = f"{self.name}: embedding_caps needs embeddings=True"
            raise ValueError(msg)
        if self.rerank_caps is not None and not self.rerank:
            msg = f"{self.name}: rerank_caps needs rerank=True"
            raise ValueError(msg)
        return self


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
            # B-105(按文档,未实调:测试环境没有 Anthropic key)—— ``max_tokens`` 就是
            # 合计输出上限。
            output_cap_field="max_tokens",
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
            # B-105(按文档,未实调:测试环境没有 OpenAI key)—— OpenAI 用 max_completion_tokens
            # 记合计上限。
            output_cap_field="max_completion_tokens",
        ),
        ModelEntry(
            name="gpt-5.5-pro",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            tool_disclosure="allowed_tools",
            output_cap_field="max_completion_tokens",
        ),
        ModelEntry(
            name="gpt-5.4-mini",
            vision=True,
            context_window=400_000,
            thinking="effort",
            thinking_default=True,
            tool_disclosure="allowed_tools",
            output_cap_field="max_completion_tokens",
        ),
        # B-206 §5.5 表 5-A, 2026-10 vendor docs —— 没有查询 / 文档标记;每次请求
        # 2,048 条,单条 8,192 token(另有每请求合计 300k token,目录不记这一项)。
        ModelEntry(
            name="text-embedding-3-large",
            embeddings=True,
            embedding_caps=EmbeddingCaps(asymmetry="none", max_batch=2_048, max_input_tokens=8_192),
        ),
        ModelEntry(name="gpt-4o", vision=True, context_window=128_000, deprecated=True),
        ModelEntry(name="gpt-4o-mini", vision=True, context_window=128_000, deprecated=True),
    ),
    # DeepSeek — api-docs.deepseek.com (2026-10)
    # deepseek-v4-pro (DeepSeek-V4-Pro-0813, text only) / deepseek-flash
    # (DeepSeek-V4.1-Flash, image understanding) are current (1M context, dual
    # mode, thinking on by default). deepseek-v4-flash is the retired V4-Flash
    # name: the vendor still accepts it but serves DeepSeek-V4.1-Flash at the
    # Flash price (2026-10-16 实调:回显 model=deepseek-flash,看图也能答) —
    # deprecated, and vision stays False on it so a new vision setup is made on
    # deepseek-flash rather than on a name the vendor is dropping.
    # deepseek-chat / deepseek-reasoner are the retired V3-era aliases (vendor
    # retirement 2026-07-24) — marked deprecated so they drop out of the
    # dropdown but existing manifests still resolve during the transition. Their
    # published 64K window is the old V3-era value (V4 is 1M); left as-is since
    # they are on the way out.
    "deepseek": (
        ModelEntry(
            name="deepseek-v4-pro",
            vision=False,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            # B-105(2026-09-24 实调)—— max_tokens 合计上限,厂商上限 393_216。
            output_cap_field="max_tokens",
            max_output_tokens=393_216,
        ),
        ModelEntry(
            name="deepseek-flash",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            # B-105(2026-10-16 实调)—— max_tokens 是含思考的合计上限(300 → length,
            # 思考 300);max_completion_tokens 被忽略;厂商上限 393_216;low / medium /
            # high / max 都接受,thinking.type=disabled 真关;看图(image_url)能答。
            output_cap_field="max_tokens",
            max_output_tokens=393_216,
        ),
        ModelEntry(
            name="deepseek-v4-flash",
            vision=False,
            context_window=1_000_000,
            deprecated=True,
            thinking="effort",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=393_216,
        ),
        ModelEntry(name="deepseek-chat", vision=False, context_window=64_000, deprecated=True),
        ModelEntry(name="deepseek-reasoner", vision=False, context_window=64_000, deprecated=True),
    ),
    # Kimi (Moonshot AI) — platform.kimi.com/docs (2026-08)
    # kimi-k3 (2.8T MoE) is the flagship: natively multimodal, 1M context.
    # thinking is "effort" — K3 is ALWAYS thinking (no off switch) with a
    # top-level ``reasoning_effort`` on the max/high/low scale, default max
    # (thinking docs 2026-08; the earlier max-only restriction is lifted).
    # The adapter maps the unified levels (no "medium" on K3), floors "off"
    # at low, and must NOT send the K2.x ``thinking.type`` param (the K3
    # docs forbid it). K3's ``temperature`` is fixed at 1 — any other value
    # is a vendor 400 (confirmed live 2026-08-28, B-33), NOT ignored — so
    # the entry declares ``temperature_fixed=1.0`` and the factory clamps.
    # kimi-k2.6 (2026-04-20) is natively multimodal — text + image + video via
    # the MoonViT encoder — with a 256K context; k2.5 also accepts images and is
    # 256K too (K2.6's gain over K2.5 is stability at length, not window size).
    # The moonshot-v1 series is text-only and being phased out (kept deprecated).
    "kimi": (
        ModelEntry(
            name="kimi-k3",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            temperature_fixed=1.0,
            # B-105(2026-09-24 实调)—— kimi 用 max_completion_tokens 记合计上限;
            # K3 的 reasoning_effort 是 low/high/max 三档(无 medium),medium 顶到 high。
            output_cap_field="max_completion_tokens",
            effort_map={"medium": "high"},
        ),
        ModelEntry(
            name="kimi-k2.6",
            vision=True,
            context_window=256_000,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
        ),
        ModelEntry(
            name="kimi-k2.5",
            vision=True,
            context_window=256_000,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
        ),
        ModelEntry(name="moonshot-v1-128k", vision=False, context_window=128_000, deprecated=True),
        ModelEntry(name="moonshot-v1-32k", vision=False, context_window=32_000, deprecated=True),
    ),
    # Zhipu GLM — open.bigmodel.cn (2026-08)
    # glm-5.3 (released 2026-08-14; same base as 5.2 with extended long-horizon
    # post-training; 1M ctx per the bigmodel pricing page) is the current text
    # flagship; glm-5.2 (1M) stays. GLM-5.2 及以上 support ``reasoning_effort``
    # (max/high/low per the bigmodel core-params page) → shape "effort"; the
    # adapter keeps ``thinking.type`` as the on/off channel and maps the
    # levels (no "medium" on GLM). glm-5.3-flash (2026-08; text params同 5.3,
    # 1M ctx, natively multimodal image/video/file input) is the cheap tier:
    # thinking.type 仅支持 enabled — no off at all → ``always_thinking``;
    # the docs' ``thinking.clear_thinking`` (preserved thinking, GLM 独有
    # param; Kimi's ``thinking.keep`` is the same concept) is deliberately
    # NOT sent — enabling it requires replaying prior reasoning content
    # verbatim, which our multi-turn replay does not do (backlog with B-30).
    # glm-5.3-flashx (2026-09-18) is the same model served faster, see entry.
    # glm-5.1 (200K), glm-4.7 (355B MoE, 200K)
    # and glm-4.6 (200K) are current text models, on/off only. Vision goes
    # through glm-5v-turbo (multimodal Agent/coding base, 200K, thinking.type
    # toggle per the bigmodel VLM docs), glm-4.6v (128K) and glm-4.5v. The
    # older glm-4*-plus line is kept deprecated so existing manifests resolve.
    "glm": (
        ModelEntry(
            name="glm-5.3",
            vision=False,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            # B-105(2026-09-24 实调)—— 关思考(thinking.type=disabled)是 400「该模型
            # 始终思考」,与 5.3-flash 一样是 always_thinking;关思考落到最低档。
            always_thinking=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
            effort_map={"medium": "high"},
        ),
        ModelEntry(
            name="glm-5.3-flash",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            always_thinking=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
            effort_map={"medium": "high"},
        ),
        ModelEntry(
            # 2026-09-18: same model as 5.3-flash (one docs page
            # "GLM-5.3-Flash/FlashX"), served at ~200 vs ~49 tokens/s for 2.5x
            # the price — every capability bit is flash's.
            name="glm-5.3-flashx",
            vision=True,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            always_thinking=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
            effort_map={"medium": "high"},
        ),
        ModelEntry(
            name="glm-5.2",
            vision=False,
            context_window=1_000_000,
            thinking="effort",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
            effort_map={"medium": "high"},
        ),
        ModelEntry(
            name="glm-5.1",
            vision=False,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
        ),
        ModelEntry(
            name="glm-4.7",
            vision=False,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
        ),
        ModelEntry(
            name="glm-4.6",
            vision=False,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
        ),
        ModelEntry(
            name="glm-5v-turbo",
            vision=True,
            context_window=200_000,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=131_072,
        ),
        ModelEntry(
            name="glm-4.6v",
            vision=True,
            context_window=128_000,
            # B-105(2026-09-24 实调)—— 实际默认思考、disabled 有效(与旧注释「无思考」
            # 不符,以实调为准)。
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=32_768,
        ),
        ModelEntry(
            name="glm-4.5v",
            vision=True,
            thinking="toggle",
            thinking_default=True,
            output_cap_field="max_tokens",
            max_output_tokens=16_384,
        ),
        # Platform embedding model (Stream T, user-specified).
        # B-206 §5.5 表 5-A, 2026-10 vendor docs —— 没有查询 / 文档标记;每次请求 64 条,
        # 单条 3,072 token。
        ModelEntry(
            name="embedding-3",
            embeddings=True,
            embedding_caps=EmbeddingCaps(asymmetry="none", max_batch=64, max_input_tokens=3_072),
        ),
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
            # B-105(2026-09-24 实调)—— 通义用 max_completion_tokens 记合计上限,
            # thinking_budget 是独立的思考长度硬上限(设 200 思考停在 200 后照常作答)。
            output_cap_field="max_completion_tokens",
            thinking_cap=True,
        ),
        ModelEntry(
            name="qwen3.7-max",
            vision=False,
            context_window=1_000_000,
            thinking="budget",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
            thinking_cap=True,
        ),
        ModelEntry(
            name="qwen3.6-plus",
            vision=True,
            context_window=1_000_000,
            thinking="budget",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
            thinking_cap=True,
        ),
        ModelEntry(
            name="qwen3.5-plus",
            vision=True,
            thinking="budget",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
            thinking_cap=True,
        ),
        ModelEntry(
            name="qwen3-max",
            vision=False,
            thinking="budget",
            # B-105(2026-09-24 实调)—— 不带思考开关时 reasoning_tokens=null:默认不思考,
            # enable_thinking 是真的开启项(与 qwen3-vl-* 同形)。旧值 True 是错的。
            thinking_default=False,
            # B-105 —— 没有合计字段,拼合计走 "split"(见 ModelEntry.output_cap_field)。
            output_cap_field="split",
            thinking_cap=True,
        ),
        ModelEntry(
            name="qwen3-vl-plus",
            vision=True,
            # B-105(2026-09-24 实调)—— 目录旧结论「没有思考开关」是错的:实际默认不
            # 思考、enable_thinking 有效,与其它通义模型同形。
            thinking="budget",
            thinking_default=False,
            output_cap_field="split",
            thinking_cap=True,
        ),
        ModelEntry(
            name="qwen3-vl-flash",
            vision=True,
            thinking="budget",
            thinking_default=False,
            output_cap_field="split",
            thinking_cap=True,
        ),
        # Platform embedding model (Stream T, user-specified).
        # B-206 K-5 —— 2026-10-21 真接口实测(中文查询),推翻表 5-A 的「``text_type``,只在
        # 原生口生效」:``text_type=query`` / ``document`` / 不发,在兼容口和原生口上向量都
        # **逐位相同**,单发它没有作用;兼容口请求体顶层加 ``"instruct": <任务说明>`` 会改变
        # 查询向量(与不加余弦 0.9185),与原生口 ``text_type=query`` + ``instruct`` 完全相同;
        # ``instruct`` 配 ``text_type=document`` 无作用;放进 ``parameters`` 里兼容口不认。即它
        # 能做的不对称只有「查询请求多发 ``instruct``」(``asymmetry="query_field"``、
        # ``query_marker="instruct"``),文档侧不变。
        # **没有开**:2026-10-22 开发集 A/B(主集 303 题 + 真实问法 172 题,英文平台默认说明)
        # 没有收益 —— 带重排 mrr@10 主集 0.945→0.945、真实问法 0.793→0.795(噪声);只走向量路
        # 主集 mrr@10 0.797→0.793、chk_hit@10 0.908→0.887,真实问法 mrr@10 0.637→0.629、
        # ctx_hit 0.783→0.737。设计判据「向量路单独 mrr 上升」不成立,所以按 "none" 声明,
        # 两侧都原样发。
        # 每次请求 10 条(K-8);单条 8,192 token 是文档值,B-200 真接口实测超过 16,000 token
        # 才整次 400,不是硬限,平台不按它截(见 ``max_input_tokens``)。
        ModelEntry(
            name="text-embedding-v4",
            embeddings=True,
            embedding_caps=EmbeddingCaps(asymmetry="none", max_batch=10, max_input_tokens=8_192),
        ),
        # Platform rerank model (Stream T, user-specified).
        # B-206 §5.5 表 5-B, 2026-10 vendor docs —— DashScope 原生口;文本输入每次 100 篇、
        # 单篇 8,000 token、整次 120,000 token(超限 400 不截断);支持 ``instruct``;
        # 请求内相对分、不可跨请求比较;限流 600 RPM。查询单独上限、并发数文档没写。
        ModelEntry(
            name="qwen3-vl-rerank",
            rerank=True,
            rerank_caps=RerankCaps(
                rerank_api="dashscope",
                max_documents=100,
                max_doc_tokens=8_000,
                max_total_tokens=120_000,
                supports_instruction=True,
                score_scale="relative",
                rpm=600,
            ),
        ),
        ModelEntry(name="qwen-max", vision=False, context_window=32_000, deprecated=True),
        ModelEntry(name="qwen-vl-max", vision=True, context_window=32_000, deprecated=True),
    ),
    # Doubao (ByteDance Volcano Engine) — volcengine.com (2026-06)
    # Seed 2.1 (doubao-seed-2-1-pro-260628, dated model ID) is the current
    # flagship; Seed 2.0 family stays. All tiers support vision and 256K
    # context. Older doubao-*-32k series superseded.
    # B-105(2026-09-24 实调)—— ``thinking.budget_tokens`` 被厂商忽略(200 正常返回,
    # 思考长度不受限),真正生效的档位控制是 ``reasoning_effort`` → shape "effort";
    # 厂商档位 minimal/low/medium/high 实调都有效、没有 max,effort_map 把 max 顶到 high。
    "doubao": (
        ModelEntry(
            name="doubao-seed-2-1-pro-260628",
            vision=True,
            context_window=256_000,
            thinking="effort",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
            max_output_tokens=262_144,
            effort_map={"max": "high"},
        ),
        ModelEntry(
            name="doubao-seed-2.0-pro",
            vision=True,
            context_window=256_000,
            thinking="effort",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
            effort_map={"max": "high"},
        ),
        ModelEntry(
            name="doubao-seed-2.0-lite",
            vision=True,
            context_window=256_000,
            thinking="effort",
            thinking_default=True,
            output_cap_field="max_completion_tokens",
            effort_map={"max": "high"},
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
