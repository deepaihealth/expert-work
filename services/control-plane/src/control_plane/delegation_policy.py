"""委派增强层 3 — 配置页「生成委派策略」草稿(领域判据)。

平台 Agent 配了 dynamic_workers 后模型不主动委派;层 0/2 解决通用判据,
本层解决**领域判据**:每个 Agent 什么活适合下放,只有结合它自己的工具/
技能清单才说得清。做法:辅助 LLM 读该 Agent 的 manifest(描述 / system
prompt 现文 / 工具清单含 MCP 工具名与描述 / 技能列表 / dynamic_workers
配置)起草一段领域化「委派策略」,管理员人审后并入 system prompt ——
端点只返回草稿,**不落库**(采纳与否由前端把文本并进 prompt 编辑器,走
既有保存流程)。

本模块是纯素材层:草稿指令的组装(:func:`build_delegation_policy_prompt`,
纯函数,单测直打)+ 辅助模型调用器工厂(:func:`make_delegation_policy_aux`,
包一层 :class:`~control_plane.aux_model_adapter.LLMRouterAuxModelAdapter`,
凭据走 CredentialsResolver 的平台/租户既有路径)。HTTP 端点在
``api/agents.py``(配置写面同款挂载 + scope)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from expert_work.protocol import AgentSpec

if TYPE_CHECKING:
    from control_plane.credential_value_cache import CredentialValueCache
    from expert_work.common.credentials import CredentialsResolver
    from expert_work.protocol import Provider
    from expert_work.runtime.secret_store import SecretStore

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "MAX_SKILLS",
    "MAX_SYSTEM_PROMPT_CHARS",
    "MAX_TOOLS",
    "MAX_TOOL_DESCRIPTION_CHARS",
    "DelegationPolicyAux",
    "ToolBrief",
    "build_delegation_policy_prompt",
    "make_delegation_policy_aux",
]

# ---------------------------------------------------------------------------
# 输入截断 — manifest 大字段喂给辅助模型前截到有界长度,别爆辅助模型窗口。
# system prompt 只需让模型看出语言/领域/口吻,不需要全文;工具描述同理。
# ---------------------------------------------------------------------------

MAX_SYSTEM_PROMPT_CHARS = 6_000
MAX_DESCRIPTION_CHARS = 1_000
MAX_TOOLS = 60
MAX_TOOL_DESCRIPTION_CHARS = 200
MAX_SKILLS = 50

_TRUNCATION_MARK = "…(truncated)"


@dataclass(frozen=True)
class ToolBrief:
    """One tool as fed to the drafting prompt — the agent's *built* catalog
    entry (so MCP / skill-sourced tools appear under their wire name plus
    description), not the manifest's raw ``tools:`` declaration."""

    name: str
    description: str


class DelegationPolicyAux(Protocol):
    """The endpoint's auxiliary-LLM caller. Tests inject a fake onto
    ``app.state.delegation_policy_aux``; production wiring is
    :func:`make_delegation_policy_aux` (app.py lifespan)."""

    async def __call__(self, *, prompt: str, tenant_id: UUID) -> str:
        """Draft the policy for ``prompt``; returns the draft text."""
        ...


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARK


def build_delegation_policy_prompt(*, spec: AgentSpec, tools: Sequence[ToolBrief]) -> str:
    """The full instruction handed to the auxiliary LLM. Pure — unit-testable.

    要点(功能规格):基于工具/技能清单推断哪些具体活是同构可并行/读取
    提炼类(适合下放)、哪些是写操作/设计判断(不下放);输出五段式;语言
    与该 Agent 的 system prompt 一致;工具名单塞进 prompt 并要求只引用其中
    的名字,禁止编造 manifest 里不存在的工具。
    """
    body = spec.spec
    tool_lines = [
        f"- {t.name}: {_clip(' '.join(t.description.split()), MAX_TOOL_DESCRIPTION_CHARS)}"
        for t in tools[:MAX_TOOLS]
    ]
    if len(tools) > MAX_TOOLS:
        tool_lines.append(f"- ... ({len(tools) - MAX_TOOLS} more tools omitted)")
    skills = list(body.skills)[:MAX_SKILLS]
    skill_lines = [f"- {s}" for s in skills] or ["(none)"]
    worker_model = (
        f"{body.dynamic_workers.model.provider}/{body.dynamic_workers.model.name}"
        if body.dynamic_workers.model is not None
        else "(inherits the agent's own model)"
    )

    instructions = (
        "You are helping the administrator of an AI-agent platform write a "
        "DELEGATION POLICY for one specific agent. At run time this agent can "
        "spawn ephemeral sub-workers through its `spawn_worker` tool, but "
        "today it rarely uses that ability. From the agent's own manifest "
        "below, draft a short, domain-specific policy section that the "
        "administrator will review and merge into the agent's system prompt.\n"
        "\n"
        "Infer from the agent's ACTUAL tools and skills which concrete kinds "
        "of work are homogeneous fan-out or read/extract/distill work (good "
        "to delegate) versus write operations and design/judgment calls (the "
        "main agent must keep). Structure the draft in exactly five parts, "
        "in this order:\n"
        "1. Which concrete tasks SHOULD be delegated to workers — homogeneous "
        "parallelizable batches and reading/searching/summarising/"
        "cross-checking work, named in terms of this agent's real tools and "
        "skills.\n"
        "2. Which tasks must NOT be delegated — write operations, "
        "side-effectful tool calls, and design or judgment decisions the "
        "main agent owns.\n"
        "3. Every worker task must be fully self-contained: the worker sees "
        "none of the conversation, so the task text must carry all context, "
        "inputs, and the expected output shape.\n"
        "4. Spot-check workers' results before relying on them.\n"
        "5. One concrete example sentence of a good delegation in this "
        "agent's own domain.\n"
        "\n"
        "Hard rules:\n"
        "- Write the draft in the SAME LANGUAGE as the agent's system prompt "
        "below.\n"
        "- When naming tools, use ONLY names from the TOOLS list below. "
        "Never invent a tool or skill that is not listed.\n"
        "- Output ONLY the policy text itself: no preamble, no markdown "
        "fences, no commentary about this prompt."
    )

    sections = [
        instructions,
        "\n=== AGENT NAME ===",
        f"{spec.metadata.name} (version {spec.metadata.version})",
        "\n=== DESCRIPTION ===",
        _clip(body.description, MAX_DESCRIPTION_CHARS) or "(none)",
        "\n=== SYSTEM PROMPT (current text) ===",
        _clip(body.system_prompt.template, MAX_SYSTEM_PROMPT_CHARS),
        "\n=== TOOLS (the complete list — reference only these names) ===",
        "\n".join(tool_lines) if tool_lines else "(none)",
        "\n=== SKILLS ===",
        "\n".join(skill_lines),
        "\n=== DYNAMIC WORKERS CONFIG ===",
        f"enabled: {body.dynamic_workers.enabled}; worker model: {worker_model}",
    ]
    return "\n".join(sections)


def make_delegation_policy_aux(
    *,
    resolver: CredentialsResolver,
    secret_store: SecretStore,
    default_provider: Provider,
    default_model: str,
    secret_cache: CredentialValueCache | None = None,
) -> DelegationPolicyAux:
    """Production :class:`DelegationPolicyAux` — a thin text-out wrapper over
    :class:`LLMRouterAuxModelAdapter` (the same aux path + platform default
    provider/model pair the memory consolidator and skill-evolution worker
    use). Built once in app.py's lifespan and parked on
    ``app.state.delegation_policy_aux``; the ``tenant_id`` keyword routes
    each call to the right tenant credentials."""
    # Lazy import — the adapter module pulls in memory_consolidator; keep
    # this module import-light for the prompt-builder unit tests.
    from control_plane.aux_model_adapter import LLMRouterAuxModelAdapter

    adapter = LLMRouterAuxModelAdapter(
        resolver=resolver,
        secret_store=secret_store,
        default_provider=default_provider,
        default_model=default_model,
        secret_cache=secret_cache,
    )

    async def _call(*, prompt: str, tenant_id: UUID) -> str:
        reply = await adapter(prompt=prompt, model=None, tenant_id=tenant_id)
        return reply.text

    return _call
