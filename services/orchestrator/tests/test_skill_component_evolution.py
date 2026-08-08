"""Stream SE — SE-10 text-class harness component evolution.

Covers the in-session authoring builtins for the three text components
(``note_behavior_patch`` / ``clarify_tool_usage`` / ``remember``) and the
``_load_skills`` → ``_assemble_system_prompt`` render dispatch that turns
each component_type into its own advisory prompt block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from expert_work.persistence.skill import InMemorySkillStore
from expert_work.protocol import Skill, SkillStatus, SkillVersion
from orchestrator.agent_factory import (
    _assemble_system_prompt,
    _load_skills,
    _SkillLookupResult,
)
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.skill_authoring import (
    ClarifyToolUsageTool,
    NoteBehaviorPatchTool,
    RememberTool,
)

AGENT = "researcher"


def _ctx(tid, uid):
    return ToolContext(tenant_id=tid, user_id=uid, run_id=uuid4())


# ── in-session authoring builtins ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_behavior_patch_creates_system_prompt_component() -> None:
    store = InMemorySkillStore()
    tid, uid = uuid4(), uuid4()
    res = await NoteBehaviorPatchTool(store=store, agent_name=AGENT).call(
        {"name": "checklist-first", "prompt_fragment": "List the checklist first."},
        ctx=_ctx(tid, uid),
    )
    assert res.meta["result"] == "ok"
    assert res.meta["component_type"] == "system_prompt"
    skill = await store.get_skill_by_name(tenant_id=tid, name="checklist-first")
    assert skill is not None
    assert skill.component_type == "system_prompt"
    assert skill.target_tool_name is None
    assert skill.visibility == "agent_private"
    assert skill.status == SkillStatus.DRAFT


@pytest.mark.asyncio
async def test_clarify_tool_usage_creates_tool_description_component() -> None:
    store = InMemorySkillStore()
    tid, uid = uuid4(), uuid4()
    res = await ClarifyToolUsageTool(store=store, agent_name=AGENT).call(
        {
            "name": "search-caveat",
            "target_tool_name": "web_search",
            "prompt_fragment": "Prefer recent sources.",
        },
        ctx=_ctx(tid, uid),
    )
    assert res.meta["component_type"] == "tool_description"
    skill = await store.get_skill_by_name(tenant_id=tid, name="search-caveat")
    assert skill is not None
    assert skill.component_type == "tool_description"
    assert skill.target_tool_name == "web_search"


@pytest.mark.asyncio
async def test_clarify_tool_usage_requires_target() -> None:
    store = InMemorySkillStore()
    with pytest.raises(ValueError, match="target_tool_name"):
        await ClarifyToolUsageTool(store=store, agent_name=AGENT).call(
            {"name": "x", "target_tool_name": "", "prompt_fragment": "y"},
            ctx=_ctx(uuid4(), uuid4()),
        )


@pytest.mark.asyncio
async def test_remember_creates_memory_entry_component() -> None:
    store = InMemorySkillStore()
    tid, uid = uuid4(), uuid4()
    res = await RememberTool(store=store, agent_name=AGENT).call(
        {"name": "report-period", "prompt_fragment": "This user uses calendar months."},
        ctx=_ctx(tid, uid),
    )
    assert res.meta["component_type"] == "memory_entry"
    skill = await store.get_skill_by_name(tenant_id=tid, name="report-period")
    assert skill is not None
    assert skill.component_type == "memory_entry"


# ── render dispatch ───────────────────────────────────────────────────────


def _version(prompt: str) -> SkillVersion:
    now = datetime.now(UTC)
    return SkillVersion(
        id=uuid4(),
        skill_id=uuid4(),
        tenant_id=uuid4(),
        version=1,
        prompt_fragment=prompt,
        created_at=now,
        # Eager so the "skill" component renders its ``<skill>`` body fragment
        # (RT-ADR-11 made lazy the default). Text-class components render by
        # component_type regardless of this flag.
        lazy_load=False,
    )


def _skill(component_type: str, target: str | None = None) -> Skill:
    now = datetime.now(UTC)
    return Skill(
        id=uuid4(),
        tenant_id=uuid4(),
        name="c",
        status=SkillStatus.ACTIVE,
        latest_version=1,
        component_type=component_type,  # type: ignore[arg-type]
        target_tool_name=target,
        created_at=now,
        updated_at=now,
    )


def _make_spec(skill_names: list[str]):
    from expert_work.protocol.agent_spec import AgentSpec

    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "a", "version": "1", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
                "system_prompt": {"template": "BASE"},
                "sandbox": {
                    "resources": {"cpu": "1", "memory": "1Gi"},
                    "network": {},
                    "filesystem": {},
                },
                "skills": skill_names,
            },
        }
    )


@pytest.mark.asyncio
async def test_component_types_render_distinct_prompt_blocks() -> None:
    versions = {
        "patch": (_version("PATCH-BODY"), _skill("system_prompt")),
        "note": (_version("NOTE-BODY"), _skill("tool_description", target="web_search")),
        "mem": (_version("MEM-BODY"), _skill("memory_entry")),
        "real": (_version("SKILL-BODY"), _skill("skill")),
    }

    def resolver(_tenant, name, _version):
        ver, sk = versions[name]
        return _SkillLookupResult.ok(ver, skill=sk)

    spec = _make_spec(["patch", "note", "mem", "real"])
    loaded = await _load_skills(
        spec=spec, skill_resolver=resolver, tenant_id=uuid4(), registry=None, agent_key="ag-test"
    )

    # Each text component routed to its own bucket; only the real skill is an
    # activated, summarised, skill_view-able skill.
    assert len(loaded.behavior_patches) == 1
    assert len(loaded.tool_notes) == 1
    assert len(loaded.memory_blocks) == 1
    assert loaded.activated_skill_names == ["real"]
    assert len(loaded.prompt_fragments) == 1
    # But all four are tracked for rollback coverage.
    assert set(loaded.resolved_versions) == {"patch", "note", "mem", "real"}

    prompt = _assemble_system_prompt(
        base="BASE",
        skill_fragments=loaded.prompt_fragments,
        skill_summaries=loaded.skill_summaries,
        behavior_patches=loaded.behavior_patches,
        tool_notes=loaded.tool_notes,
        memory_blocks=loaded.memory_blocks,
    )
    assert "<behavior-patch" in prompt and "PATCH-BODY" in prompt
    assert '<tool-note tool="web_search"' in prompt and "NOTE-BODY" in prompt
    assert "<long-term-memory" in prompt and "MEM-BODY" in prompt
    assert "<skill name=" in prompt and "SKILL-BODY" in prompt


@pytest.mark.asyncio
async def test_resolver_without_skill_defaults_to_plain_skill() -> None:
    """Older resolvers that don't populate ``skill`` stay backward-compatible."""

    def resolver(_tenant, _name, _ver):
        return _SkillLookupResult.ok(_version("BODY"))

    spec = _make_spec(["legacy"])
    loaded = await _load_skills(
        spec=spec, skill_resolver=resolver, tenant_id=uuid4(), registry=None, agent_key="ag-test"
    )
    assert loaded.activated_skill_names == ["legacy"]
    assert loaded.behavior_patches == []
