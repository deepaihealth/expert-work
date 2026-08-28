"""Pydantic v2 schema tests for :class:`AgentSpec`."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from expert_work.protocol import (
    AgentSpec,
    BuiltinToolSpec,
    HTTPToolSpec,
    MCPToolSpec,
    ModelSpec,
    SubAgentSpec,
    parse_agent_ref,
)

_MINIMAL: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {
        "name": "code-reviewer",
        "version": "1.0.0",
        "tenant": "platform-eng",
    },
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are a reviewer"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _doc() -> dict[str, Any]:
    return deepcopy(_MINIMAL)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_minimal_manifest_validates() -> None:
    spec = AgentSpec.model_validate(_doc())
    assert spec.api_version == "expert_work.io/v1"
    assert spec.metadata.name == "code-reviewer"
    assert spec.spec.model.provider == "anthropic"
    # Defaults should have populated.
    assert spec.spec.workflow.type == "react"
    assert spec.spec.workflow.max_iterations == 30
    assert spec.spec.dynamic_context.inject_memory is True


def test_alias_apiversion_round_trips() -> None:
    spec = AgentSpec.model_validate(_doc())
    dumped = spec.model_dump(by_alias=True)
    assert dumped["apiVersion"] == "expert_work.io/v1"
    assert "api_version" not in dumped


def test_fallback_chain_accepted_when_acyclic() -> None:
    doc = _doc()
    doc["spec"]["model"]["fallback"] = [
        {"provider": "openai", "name": "gpt-4o"},
        {"provider": "anthropic", "name": "claude-haiku-4-5"},
    ]
    spec = AgentSpec.model_validate(doc)
    assert len(spec.spec.model.fallback) == 2


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


def test_missing_model_block_rejected() -> None:
    doc = _doc()
    del doc["spec"]["model"]
    with pytest.raises(ValidationError) as exc_info:
        AgentSpec.model_validate(doc)
    assert any("model" in str(err["loc"]) for err in exc_info.value.errors())


def test_invalid_kind_rejected() -> None:
    doc = _doc()
    doc["kind"] = "Workflow"
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_unknown_extra_field_rejected() -> None:
    doc = _doc()
    doc["spec"]["mystery_field"] = "boom"
    with pytest.raises(ValidationError) as exc_info:
        AgentSpec.model_validate(doc)
    assert any("mystery_field" in str(err["loc"]) for err in exc_info.value.errors())


def test_temperature_out_of_range_rejected() -> None:
    doc = _doc()
    doc["spec"]["model"]["temperature"] = 3.5
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_negative_audit_retention_rejected() -> None:
    doc = _doc()
    doc["spec"]["tenant_config"]["audit_retention_days"] = 0
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# Lint rule #7 — network allowlist != ["*"]
# ---------------------------------------------------------------------------


def test_wildcard_allowlist_rejected() -> None:
    doc = _doc()
    doc["spec"]["sandbox"]["network"]["allowlist"] = ["*"]
    with pytest.raises(ValidationError) as exc_info:
        AgentSpec.model_validate(doc)
    assert "allowlist" in str(exc_info.value)


def test_explicit_wildcard_in_list_allowed() -> None:
    """A literal ``["*.internal", "*"]`` is fine — only the single-element
    ``["*"]`` is the dangerous wildcard. The list-of-one form is the only
    one we lint."""
    doc = _doc()
    doc["spec"]["sandbox"]["network"]["allowlist"] = ["*.internal", "*"]
    spec = AgentSpec.model_validate(doc)
    assert "*" in spec.spec.sandbox.network.allowlist


# ---------------------------------------------------------------------------
# Lint rule #8 — fallback chain must be acyclic
# ---------------------------------------------------------------------------


def test_self_referential_fallback_rejected() -> None:
    """Cycle: primary model recurses to itself in fallback."""
    doc = _doc()
    doc["spec"]["model"]["fallback"] = [
        {
            "provider": "anthropic",
            "name": "claude-sonnet-4-5",  # same as primary → cycle
        }
    ]
    with pytest.raises(ValidationError) as exc_info:
        AgentSpec.model_validate(doc)
    assert "cycle" in str(exc_info.value).lower()


def test_deep_cycle_rejected() -> None:
    """Cycle: A → B → A through nested fallback."""
    doc = _doc()
    doc["spec"]["model"]["fallback"] = [
        {
            "provider": "openai",
            "name": "gpt-4o",
            "fallback": [
                {"provider": "anthropic", "name": "claude-sonnet-4-5"},  # back to root
            ],
        }
    ]
    with pytest.raises(ValidationError) as exc_info:
        AgentSpec.model_validate(doc)
    assert "cycle" in str(exc_info.value).lower()


def test_two_providers_one_name_share_no_cycle() -> None:
    """Identity is (provider, name); same name on different provider is fine."""
    doc = _doc()
    doc["spec"]["model"]["fallback"] = [
        {"provider": "openai", "name": "claude-sonnet-4-5"},
    ]
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.model.fallback[0].provider == "openai"


def test_model_spec_validate_directly() -> None:
    """ModelSpec works in isolation (used by the orchestrator router)."""
    model = ModelSpec.model_validate({"provider": "openai", "name": "gpt-4o"})
    assert model.temperature == 0.2
    assert model.max_tokens == 4096


@pytest.mark.parametrize(
    "provider",
    ["kimi", "glm", "deepseek", "qwen", "doubao"],
)
def test_openai_compatible_providers_accepted(provider: str) -> None:
    """E.11.5 — five domestic OpenAI-compatible vendors must validate."""
    model = ModelSpec.model_validate({"provider": provider, "name": "test-model"})
    assert model.provider == provider


def test_unknown_provider_rejected() -> None:
    """Schema regression guard: unknown providers must still fail validation
    even after the E.11.5 Literal extension."""
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"provider": "bedrock", "name": "claude"})


def test_rate_limit_rpm_default_is_60() -> None:
    """E.12 — ``ModelSpec.rate_limit_rpm`` defaults to a conservative
    60 rpm matching the OpenAI free-tier ballpark."""
    model = ModelSpec.model_validate({"provider": "openai", "name": "gpt-4o"})
    assert model.rate_limit_rpm == 60


def test_rate_limit_rpm_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"provider": "openai", "name": "gpt-4o", "rate_limit_rpm": 0})
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"provider": "openai", "name": "gpt-4o", "rate_limit_rpm": -1})


def test_rate_limit_rpm_custom_value_accepted() -> None:
    model = ModelSpec.model_validate(
        {"provider": "kimi", "name": "moonshot-v1-128k", "rate_limit_rpm": 3}
    )
    assert model.rate_limit_rpm == 3


# ---------------------------------------------------------------------------
# policies.approval — J.8 (Mini-ADR J-24)
# ---------------------------------------------------------------------------


def test_approval_fields_default_empty_and_24h() -> None:
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.policies.approval_required_tools == []
    assert spec.spec.policies.approval_timeout_s == 86400


def test_approval_required_tools_accepted() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {
        "approval_required_tools": ["send_email", "http"],
        "approval_timeout_s": 3600,
    }
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.policies.approval_required_tools == ["send_email", "http"]
    assert spec.spec.policies.approval_timeout_s == 3600


def test_approval_timeout_below_floor_rejected() -> None:
    """``approval_timeout_s`` floor is 60s — a too-short window is a config error."""
    doc = _doc()
    doc["spec"]["policies"] = {"approval_timeout_s": 30}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_approval_timeout_above_ceiling_rejected() -> None:
    """Ceiling is 7 days — anything longer is almost certainly a mistake."""
    doc = _doc()
    doc["spec"]["policies"] = {"approval_timeout_s": 604801}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# tools: discriminated union (Mini-ADR E-14)
# ---------------------------------------------------------------------------


def test_tools_default_is_empty_list() -> None:
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.tools == []


def test_tools_builtin_http_mcp_entries_validate() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [
        {"type": "builtin", "name": "web_search", "config": {"max_results": 5}},
        {"type": "http"},
        {"type": "mcp", "allow_tools": ["read_pr"]},
    ]
    spec = AgentSpec.model_validate(doc)
    builtin, http, mcp = spec.spec.tools
    assert isinstance(builtin, BuiltinToolSpec)
    assert isinstance(http, HTTPToolSpec)
    assert isinstance(mcp, MCPToolSpec)
    assert builtin.name == "web_search"
    assert builtin.config == {"max_results": 5}
    assert mcp.allow_tools == ["read_pr"]


def test_tools_mcp_allow_tools_defaults_empty() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [{"type": "mcp"}]
    spec = AgentSpec.model_validate(doc)
    entry = spec.spec.tools[0]
    assert isinstance(entry, MCPToolSpec)
    assert entry.allow_tools == []


def test_tools_unknown_type_rejected() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [{"type": "python", "name": "x"}]
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_tools_builtin_missing_name_rejected() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [{"type": "builtin"}]
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_tools_entry_extra_field_rejected() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [{"type": "http", "url": "https://x"}]
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_tools_entry_missing_discriminator_rejected() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [{"name": "web_search"}]
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# azure / self-hosted model fields (F-4)
# ---------------------------------------------------------------------------


def test_model_fields_default_to_none() -> None:
    model = ModelSpec.model_validate({"provider": "openai", "name": "gpt-4o"})
    assert model.base_url is None
    assert model.azure_deployment is None
    assert model.azure_api_version is None


def test_self_hosted_model_accepts_base_url() -> None:
    model = ModelSpec.model_validate(
        {
            "provider": "self-hosted",
            "name": "llama-3.1-70b",
            "base_url": "http://vllm.internal:8000",
        }
    )
    assert model.provider == "self-hosted"
    assert model.base_url == "http://vllm.internal:8000"


def test_azure_model_accepts_deployment_fields() -> None:
    model = ModelSpec.model_validate(
        {
            "provider": "azure",
            "name": "gpt-4o",
            "base_url": "https://res.openai.azure.com",
            "azure_deployment": "gpt-4o-deploy",
            "azure_api_version": "2024-10-21",
        }
    )
    assert model.azure_deployment == "gpt-4o-deploy"
    assert model.azure_api_version == "2024-10-21"


# ---------------------------------------------------------------------------
# subagents — agent-as-tool delegation (Stream J.4 / Mini-ADR J-12)
# ---------------------------------------------------------------------------


def test_parse_agent_ref_splits_name_and_version() -> None:
    assert parse_agent_ref("code-reviewer@1.0.0") == ("code-reviewer", "1.0.0")


@pytest.mark.parametrize(
    "bad_ref",
    ["noversion", "name@", "@version", "a@b@c", ""],
)
def test_parse_agent_ref_rejects_malformed(bad_ref: str) -> None:
    with pytest.raises(ValueError, match="agent_ref must be"):
        parse_agent_ref(bad_ref)


def test_subagent_spec_validates() -> None:
    sub = SubAgentSpec.model_validate(
        {
            "name": "research_helper",
            "agent_ref": "researcher@2.1.0",
            "description": "delegates deep research subtasks",
        }
    )
    assert sub.name == "research_helper"
    assert parse_agent_ref(sub.agent_ref) == ("researcher", "2.1.0")


@pytest.mark.parametrize(
    "bad_name",
    ["ResearchHelper", "research-helper", "1research", "_helper", "research helper"],
)
def test_subagent_name_must_be_snake_case(bad_name: str) -> None:
    with pytest.raises(ValidationError, match="snake_case"):
        SubAgentSpec.model_validate(
            {"name": bad_name, "agent_ref": "researcher@1.0.0", "description": "x"}
        )


def test_subagent_rejects_malformed_agent_ref() -> None:
    with pytest.raises(ValidationError, match="agent_ref must be"):
        SubAgentSpec.model_validate(
            {"name": "helper", "agent_ref": "noversion", "description": "x"}
        )


def test_subagent_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        SubAgentSpec.model_validate(
            {
                "name": "helper",
                "agent_ref": "researcher@1.0.0",
                "description": "x",
                "token_budget_fraction": 0.3,
            }
        )


def test_subagents_default_is_empty_list() -> None:
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.subagents == []


def test_subagents_block_validates_in_manifest() -> None:
    doc = _doc()
    doc["spec"]["subagents"] = [
        {"name": "researcher", "agent_ref": "deep-researcher@1.0.0", "description": "research"},
        {"name": "writer", "agent_ref": "doc-writer@2.0.0", "description": "drafting"},
    ]
    spec = AgentSpec.model_validate(doc)
    assert [s.name for s in spec.spec.subagents] == ["researcher", "writer"]


def test_subagent_self_delegation_rejected() -> None:
    """A subagent whose agent_ref points back at this agent is refused."""
    doc = _doc()  # metadata.name == "code-reviewer"
    doc["spec"]["subagents"] = [
        {"name": "myself", "agent_ref": "code-reviewer@2.0.0", "description": "loop"},
    ]
    with pytest.raises(ValidationError, match="self-delegation"):
        AgentSpec.model_validate(doc)


def test_duplicate_subagent_tool_name_rejected() -> None:
    doc = _doc()
    doc["spec"]["subagents"] = [
        {"name": "helper", "agent_ref": "agent-a@1.0.0", "description": "a"},
        {"name": "helper", "agent_ref": "agent-b@1.0.0", "description": "b"},
    ]
    with pytest.raises(ValidationError, match="duplicate subagent"):
        AgentSpec.model_validate(doc)


def test_subagent_name_colliding_with_builtin_tool_rejected() -> None:
    doc = _doc()
    doc["spec"]["tools"] = [{"type": "builtin", "name": "web_search"}]
    doc["spec"]["subagents"] = [
        {"name": "web_search", "agent_ref": "searcher@1.0.0", "description": "x"},
    ]
    with pytest.raises(ValidationError, match="collides with a declared builtin"):
        AgentSpec.model_validate(doc)


@pytest.mark.parametrize(
    "reserved",
    ["spawn_worker", "update_plan", "skill_view", "find_tools", "knowledge_search", "ask_image"],
)
def test_subagent_name_colliding_with_implicit_tool_rejected(reserved: str) -> None:
    """隐式注册的平台工具名(manifest 里从不声明)是保留字。

    没有这道闸,名册条目会在 ToolRegistry.register(重复注册即替换)被
    静默顶掉或顶掉平台工具,赢家取决于装配顺序 —— 例如叫 ``spawn_worker``
    的名册条目会被随后注册的动态 worker 工具无声覆盖。
    """
    doc = _doc()
    doc["spec"]["subagents"] = [
        {"name": reserved, "agent_ref": "helper@1.0.0", "description": "x"},
    ]
    with pytest.raises(ValidationError, match="is reserved"):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# knowledge block — RAG (Stream J.5)
# ---------------------------------------------------------------------------


def test_knowledge_default_is_none() -> None:
    assert AgentSpec.model_validate(_doc()).spec.knowledge is None


def test_knowledge_block_validates_in_manifest() -> None:
    doc = _doc()
    doc["spec"]["knowledge"] = {"knowledge_base_refs": ["hr-policies", "eng-docs"]}
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.knowledge is not None
    assert spec.spec.knowledge.knowledge_base_refs == ["hr-policies", "eng-docs"]


def test_knowledge_empty_refs_rejected() -> None:
    doc = _doc()
    doc["spec"]["knowledge"] = {"knowledge_base_refs": []}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_knowledge_duplicate_refs_rejected() -> None:
    doc = _doc()
    doc["spec"]["knowledge"] = {"knowledge_base_refs": ["kb", "kb"]}
    with pytest.raises(ValidationError, match="duplicate knowledge base ref"):
        AgentSpec.model_validate(doc)


def test_knowledge_blank_ref_rejected() -> None:
    doc = _doc()
    doc["spec"]["knowledge"] = {"knowledge_base_refs": ["   "]}
    with pytest.raises(ValidationError, match="must be non-empty"):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# multimodal input — vision (Stream J.6)
# ---------------------------------------------------------------------------


def test_supports_vision_defaults_to_false() -> None:
    assert AgentSpec.model_validate(_doc()).spec.model.supports_vision is False


def test_supports_vision_accepts_explicit_true() -> None:
    doc = _doc()
    doc["spec"]["model"]["supports_vision"] = True
    assert AgentSpec.model_validate(doc).spec.model.supports_vision is True


def test_vision_block_default_is_none() -> None:
    assert AgentSpec.model_validate(_doc()).spec.vision is None


def test_vision_block_validates_in_manifest() -> None:
    doc = _doc()
    doc["spec"]["vision"] = {"model": {"provider": "qwen", "name": "qwen-vl-max"}}
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.vision is not None
    assert spec.spec.vision.model.provider == "qwen"
    assert spec.spec.vision.model.name == "qwen-vl-max"


def test_vision_block_missing_model_rejected() -> None:
    doc = _doc()
    doc["spec"]["vision"] = {}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_vision_block_extra_field_rejected() -> None:
    doc = _doc()
    doc["spec"]["vision"] = {
        "model": {"provider": "qwen", "name": "qwen-vl-max"},
        "unknown": "x",
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# MCPToolSpec.servers — per-agent server selection (Stream V-E)
# ---------------------------------------------------------------------------


def test_mcp_tool_spec_defaults_empty_servers() -> None:
    spec = MCPToolSpec()
    assert spec.servers == []  # empty = all available servers
    assert spec.allow_tools == []


def test_mcp_tool_spec_accepts_servers() -> None:
    spec = MCPToolSpec(servers=["github", "linear"], allow_tools=["create_issue"])
    assert spec.servers == ["github", "linear"]


def test_mcp_tool_spec_backward_compatible_without_servers() -> None:
    # A manifest dict from before V-E (no "servers" key) must still parse,
    # defaulting servers to [].
    spec = MCPToolSpec.model_validate({"type": "mcp", "allow_tools": ["x"]})
    assert spec.servers == []


def test_mcp_tool_spec_still_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        MCPToolSpec.model_validate({"type": "mcp", "bogus": 1})


# ---------------------------------------------------------------------------
# policies.working_memory — CM-2 (sliding window)
# ---------------------------------------------------------------------------


def test_working_memory_defaults_are_conservative() -> None:
    """CM-2 — defaults must be a no-op for existing manifests: enabled
    with a 0.7 threshold and a generous 20-turn window keeping the first
    turn, so a typical conversation is left untouched."""
    spec = AgentSpec.model_validate(_doc())
    wm = spec.spec.policies.working_memory
    assert wm.enabled is True
    assert wm.threshold_pct == 0.7
    assert wm.max_recent_turns == 20
    assert wm.keep_first_turn is True


def test_working_memory_custom_values_accepted() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {
        "working_memory": {
            "enabled": False,
            "threshold_pct": 0.5,
            "max_recent_turns": 6,
            "keep_first_turn": False,
        }
    }
    wm = AgentSpec.model_validate(doc).spec.policies.working_memory
    assert wm.enabled is False
    assert wm.threshold_pct == 0.5
    assert wm.max_recent_turns == 6
    assert wm.keep_first_turn is False


def test_working_memory_rejects_invalid_bounds() -> None:
    for bad in ({"threshold_pct": 0}, {"threshold_pct": 1.5}, {"max_recent_turns": 0}):
        doc = _doc()
        doc["spec"]["policies"] = {"working_memory": bad}
        with pytest.raises(ValidationError):
            AgentSpec.model_validate(doc)


def test_working_memory_forbids_extra() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {"working_memory": {"bogus": 1}}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# policies.tool_output_budget — Phase 3 (per-agent master switch)
# ---------------------------------------------------------------------------


def test_tool_output_budget_default_enabled() -> None:
    """Phase 3 — default ON so existing manifests keep the feature."""
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.policies.tool_output_budget.enabled is True


def test_tool_output_budget_disable_accepted() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {"tool_output_budget": {"enabled": False}}
    assert AgentSpec.model_validate(doc).spec.policies.tool_output_budget.enabled is False


def test_tool_output_budget_forbids_extra() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {"tool_output_budget": {"bogus": 1}}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# policies.context_compression.flush_before_compaction — CM-3
# ---------------------------------------------------------------------------


def test_flush_before_compaction_defaults_true() -> None:
    """CM-3 — memory-enabled agents flush the discarded middle to long-term
    memory by default; the knob lives on the compression policy."""
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.policies.context_compression.flush_before_compaction is True


def test_flush_before_compaction_can_disable() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {"context_compression": {"flush_before_compaction": False}}
    cc = AgentSpec.model_validate(doc).spec.policies.context_compression
    assert cc.flush_before_compaction is False


def test_context_compression_still_forbids_extra() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {"context_compression": {"bogus": 1}}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# memory.long_term.reconcile_writes — CM-7
# ---------------------------------------------------------------------------


def test_reconcile_writes_defaults_true() -> None:
    """CM-7 — run-end memory writes reconcile against similar existing
    memories by default (Mem0-style ADD/UPDATE/DELETE/NOOP)."""
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {}}
    memory = AgentSpec.model_validate(doc).spec.memory
    assert memory is not None and memory.long_term is not None
    assert memory.long_term.reconcile_writes is True


def test_reconcile_writes_can_disable() -> None:
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {"reconcile_writes": False}}
    memory = AgentSpec.model_validate(doc).spec.memory
    assert memory is not None and memory.long_term is not None
    assert memory.long_term.reconcile_writes is False


def test_long_term_memory_forbids_extra() -> None:
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {"bogus": 1}}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# memory.long_term.rewrite_reads / abstain_threshold — P5a assembly
# ---------------------------------------------------------------------------


def test_rewrite_reads_defaults_false() -> None:
    """P5a — query rewrite is opt-in (it adds one aux LLM call per recall)."""
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {}}
    memory = AgentSpec.model_validate(doc).spec.memory
    assert memory is not None and memory.long_term is not None
    assert memory.long_term.rewrite_reads is False


def test_rewrite_reads_can_enable() -> None:
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {"rewrite_reads": True}}
    memory = AgentSpec.model_validate(doc).spec.memory
    assert memory is not None and memory.long_term is not None
    assert memory.long_term.rewrite_reads is True


def test_abstain_threshold_defaults_zero() -> None:
    """P5a — abstention is observe-only (never abstains) until a tenant opts in."""
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {}}
    memory = AgentSpec.model_validate(doc).spec.memory
    assert memory is not None and memory.long_term is not None
    assert memory.long_term.abstain_threshold == 0.0


def test_abstain_threshold_accepts_unit_interval() -> None:
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {"abstain_threshold": 0.3}}
    memory = AgentSpec.model_validate(doc).spec.memory
    assert memory is not None and memory.long_term is not None
    assert memory.long_term.abstain_threshold == 0.3


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_abstain_threshold_rejects_out_of_range(bad: float) -> None:
    doc = _doc()
    doc["spec"]["memory"] = {"long_term": {"abstain_threshold": bad}}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# model.effort / model.adaptive_thinking — CM-9
# ---------------------------------------------------------------------------


def test_model_compute_controls_default_off() -> None:
    """CM-9 — existing manifests are byte-for-byte unchanged."""
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.model.effort is None
    assert spec.spec.model.adaptive_thinking is False


def test_model_effort_accepts_levels() -> None:
    doc = _doc()
    doc["spec"]["model"]["effort"] = "max"
    doc["spec"]["model"]["adaptive_thinking"] = True
    model = AgentSpec.model_validate(doc).spec.model
    assert model.effort == "max"
    assert model.adaptive_thinking is True


def test_model_effort_rejects_unknown_level() -> None:
    doc = _doc()
    doc["spec"]["model"]["effort"] = "xtreme"
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# Stream HX-1 PR2 — context_window catalog resolution + E.3 trim opt-in
# ---------------------------------------------------------------------------


def test_model_context_window_defaults_none_for_catalog_resolution() -> None:
    """Mini-ADR HX-A4 — ``None`` means the factory resolves the window
    from the model catalog at build time; an explicit value always wins."""
    spec = AgentSpec.model_validate(_MINIMAL)
    assert spec.spec.model.context_window is None


def test_model_context_window_explicit_value_validated() -> None:
    doc = deepcopy(_MINIMAL)
    doc["spec"]["model"]["context_window"] = 1_000_000
    assert AgentSpec.model_validate(doc).spec.model.context_window == 1_000_000
    doc["spec"]["model"]["context_window"] = 0
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_context_compression_trim_caps_default_none() -> None:
    """Mini-ADR HX-A5 — the E.3 view trim is opt-in: both caps default
    ``None`` (middleware not built); setting either opts back in."""
    cc = AgentSpec.model_validate(_MINIMAL).spec.policies.context_compression
    assert cc.max_turns is None
    assert cc.max_tokens is None

    doc = deepcopy(_MINIMAL)
    doc["spec"]["policies"] = {"context_compression": {"max_tokens": 9000}}
    cc = AgentSpec.model_validate(doc).spec.policies.context_compression
    assert cc.max_tokens == 9000
    assert cc.max_turns is None


# ---------------------------------------------------------------------------
# system_prompt — Jinja mode + declared variables (Dynamic-Prompt)
# ---------------------------------------------------------------------------


def test_system_prompt_jinja_defaults_off() -> None:
    sp = AgentSpec.model_validate(_MINIMAL).spec.system_prompt
    assert sp.jinja is False
    assert sp.variables == []


def test_system_prompt_jinja_with_variables_validates() -> None:
    doc = _doc()
    doc["spec"]["system_prompt"] = {
        "template": "你是 {{ persona }},画像:{{ profile }}",
        "jinja": True,
        "variables": [
            {"name": "persona"},
            {"name": "profile", "trusted": False, "required": False},
        ],
    }
    sp = AgentSpec.model_validate(doc).spec.system_prompt
    assert sp.jinja is True
    assert sp.variables[0].name == "persona"
    assert sp.variables[0].trusted is True  # owner default
    assert sp.variables[0].required is True
    assert sp.variables[1].trusted is False
    assert sp.variables[1].required is False


def test_variables_without_jinja_rejected() -> None:
    doc = _doc()
    doc["spec"]["system_prompt"] = {
        "template": "hi {{ x }}",
        "jinja": False,
        "variables": [{"name": "x"}],
    }
    with pytest.raises(ValidationError, match="jinja mode is off"):
        AgentSpec.model_validate(doc)


def test_duplicate_variable_names_rejected() -> None:
    doc = _doc()
    doc["spec"]["system_prompt"] = {
        "template": "{{ x }}",
        "jinja": True,
        "variables": [{"name": "x"}, {"name": "x"}],
    }
    with pytest.raises(ValidationError, match="duplicate names"):
        AgentSpec.model_validate(doc)


def test_invalid_variable_identifier_rejected() -> None:
    doc = _doc()
    doc["spec"]["system_prompt"] = {
        "template": "{{ x }}",
        "jinja": True,
        "variables": [{"name": "2bad-name"}],
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_auto_attach_evolved_skills_defaults_off() -> None:
    """SE-16 (SE-A42) — the evolution auto-attach is an explicit opt-in."""
    assert AgentSpec.model_validate(_MINIMAL).spec.auto_attach_evolved_skills is False
    doc = deepcopy(_MINIMAL)
    doc["spec"]["auto_attach_evolved_skills"] = True
    assert AgentSpec.model_validate(doc).spec.auto_attach_evolved_skills is True


# ---------------------------------------------------------------------------
# output_schema (Stream RT-1 PR-3, RT-ADR-4)
# ---------------------------------------------------------------------------


def test_output_schema_defaults_to_none() -> None:
    assert AgentSpec.model_validate(_MINIMAL).spec.output_schema is None


def test_output_schema_accepted_with_defaults() -> None:
    doc = _doc()
    doc["spec"]["output_schema"] = {
        "json_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    }
    spec = AgentSpec.model_validate(doc).spec.output_schema
    assert spec is not None
    assert spec.name == "final_response"
    assert spec.strict is True
    assert spec.json_schema["required"] == ["answer"]


def test_output_schema_custom_name_and_strict() -> None:
    doc = _doc()
    doc["spec"]["output_schema"] = {
        "name": "review_verdict",
        "json_schema": {"type": "object"},
        "strict": False,
    }
    spec = AgentSpec.model_validate(doc).spec.output_schema
    assert spec is not None
    assert spec.name == "review_verdict"
    assert spec.strict is False


def test_output_schema_missing_json_schema_rejected() -> None:
    doc = _doc()
    doc["spec"]["output_schema"] = {"name": "x"}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_output_schema_empty_json_schema_rejected() -> None:
    doc = _doc()
    doc["spec"]["output_schema"] = {"json_schema": {}}
    with pytest.raises(ValidationError, match="non-empty"):
        AgentSpec.model_validate(doc)


def test_output_schema_non_object_top_level_type_rejected() -> None:
    doc = _doc()
    doc["spec"]["output_schema"] = {"json_schema": {"type": "array"}}
    with pytest.raises(ValidationError, match="must be 'object'"):
        AgentSpec.model_validate(doc)


def test_output_schema_bad_wire_name_rejected() -> None:
    # The name becomes an Anthropic tool name / OpenAI json_schema name and
    # is quoted in prompt-path instructions - constrain it hard.
    doc = _doc()
    doc["spec"]["output_schema"] = {
        "name": "bad name!{ignore instructions}",
        "json_schema": {"type": "object"},
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_output_schema_extra_field_rejected() -> None:
    doc = _doc()
    doc["spec"]["output_schema"] = {"json_schema": {"type": "object"}, "bogus": 1}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# Stream L (P1) — idle_timeout_s / stream_deadline_s reinterpretation
# ---------------------------------------------------------------------------


def test_idle_timeout_field_default() -> None:
    from expert_work.protocol.agent_spec import AgentSpecBody

    assert AgentSpecBody.model_fields["idle_timeout_s"].default == 45
    # stream_deadline_s stays a field (reinterpreted as the first-token budget)
    assert AgentSpecBody.model_fields["stream_deadline_s"].default == 180


# ---------------------------------------------------------------------------
# policies.token_budget — B3 (per-run total token breaker)
# ---------------------------------------------------------------------------


def test_token_budget_round_trips() -> None:
    doc = _doc()
    doc["spec"]["policies"] = {"token_budget": 500_000}
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.policies.token_budget == 500_000
    dumped = spec.model_dump(by_alias=True, exclude_none=True)
    assert dumped["spec"]["policies"]["token_budget"] == 500_000


def test_token_budget_defaults_zero_and_rejects_negative() -> None:
    assert AgentSpec.model_validate(_doc()).spec.policies.token_budget == 0
    doc = _doc()
    doc["spec"]["policies"] = {"token_budget": -1}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_display_name_defaults_to_empty_string() -> None:
    """存量 manifest(没有这个字段)必须照常反序列化,不能抛 ValidationError。

    ``AgentSpecBody`` 是 ``extra="forbid"``,所以新增字段只影响写入侧;
    这条证明读取侧对老数据向后兼容。``_doc()`` 就是这样一份不含
    ``display_name`` 的既有 manifest。
    """
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.display_name == ""


def test_display_name_round_trips() -> None:
    doc = _doc()
    doc["spec"]["display_name"] = "报表助手"
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.display_name == "报表助手"
    assert spec.model_dump(mode="json")["spec"]["display_name"] == "报表助手"


def test_display_name_appears_in_the_generated_json_schema() -> None:
    """``GET /v1/agents/schema`` 由 ``AgentSpec.model_json_schema()`` 生成,
    manifest 编辑器直接吃它。字段进不了 schema,编辑器就永远看不到。"""
    schema = AgentSpec.model_json_schema(by_alias=True)
    body = schema["$defs"]["AgentSpecBody"]["properties"]
    assert "display_name" in body


# ---------------------------------------------------------------------------
# B-35 — workflow.execution_mode (plan_first)
# ---------------------------------------------------------------------------


def test_execution_mode_defaults_to_standard() -> None:
    """存量 manifest(没有这个字段)反序列化后拿到 standard,行为不变。"""
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.workflow.execution_mode == "standard"


def test_plan_first_requires_plan_execute() -> None:
    doc = _doc()
    doc["spec"]["workflow"] = {"type": "react", "execution_mode": "plan_first"}
    with pytest.raises(ValidationError, match="plan_execute"):
        AgentSpec.model_validate(doc)


def test_plan_first_requires_dynamic_workers_enabled() -> None:
    doc = _doc()
    doc["spec"]["workflow"] = {"type": "plan_execute", "execution_mode": "plan_first"}
    doc["spec"]["dynamic_workers"] = {"enabled": False}
    with pytest.raises(ValidationError, match="dynamic_workers"):
        AgentSpec.model_validate(doc)


def test_plan_first_valid_combination_accepted() -> None:
    """plan_execute + dynamic_workers 默认开 → plan_first 合法。"""
    doc = _doc()
    doc["spec"]["workflow"] = {"type": "plan_execute", "execution_mode": "plan_first"}
    spec = AgentSpec.model_validate(doc)
    assert spec.spec.workflow.execution_mode == "plan_first"


def test_execution_mode_unknown_value_rejected() -> None:
    doc = _doc()
    doc["spec"]["workflow"] = {"type": "plan_execute", "execution_mode": "bogus"}
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(doc)


def test_execution_mode_appears_in_the_generated_json_schema() -> None:
    """manifest 编辑器吃 ``AgentSpec.model_json_schema()``,字段必须可见。"""
    schema = AgentSpec.model_json_schema(by_alias=True)
    wf = schema["$defs"]["WorkflowSpec"]["properties"]
    assert "execution_mode" in wf


# ---------------------------------------------------------------------------
# 弹性 worker 预算 — dynamic_workers per-agent budget requests
# ---------------------------------------------------------------------------


def test_dynamic_workers_budget_fields_default_none() -> None:
    """存量 manifest(没有这些字段)反序列化后拿到 None → 平台默认,行为不变。"""
    dw = AgentSpec.model_validate(_doc()).spec.dynamic_workers
    assert dw.max_iterations is None
    assert dw.max_concurrent is None
    assert dw.max_per_run is None


def test_dynamic_workers_budget_fields_accepted() -> None:
    doc = _doc()
    doc["spec"]["dynamic_workers"] = {
        "max_iterations": 48,
        "max_concurrent": 5,
        "max_per_run": 32,
    }
    dw = AgentSpec.model_validate(doc).spec.dynamic_workers
    assert (dw.max_iterations, dw.max_concurrent, dw.max_per_run) == (48, 5, 32)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("max_iterations", 0),
        ("max_iterations", 513),
        ("max_concurrent", 0),
        ("max_concurrent", 65),
        ("max_per_run", 0),
        ("max_per_run", 1025),
    ],
)
def test_dynamic_workers_budget_fields_sanity_bounds(field: str, bad: int) -> None:
    """协议层只做 sanity 闸(真闸=平台 cap clamp);0 与超 sanity 上限都拒。"""
    doc = _doc()
    doc["spec"]["dynamic_workers"] = {field: bad}
    with pytest.raises(ValidationError, match=field):
        AgentSpec.model_validate(doc)


def test_dynamic_workers_budget_fields_in_json_schema() -> None:
    """manifest 编辑器吃 ``AgentSpec.model_json_schema()``,字段必须可见。"""
    schema = AgentSpec.model_json_schema(by_alias=True)
    dw = schema["$defs"]["DynamicWorkersSpec"]["properties"]
    for key in ("max_iterations", "max_concurrent", "max_per_run"):
        assert key in dw
