"""Tests for J.7a skill integration in :func:`build_agent`.

Mini-ADR J-23 § 15.4 + § 15.6 build-time validation:

* Skill resolver wires correctly with bare-name + pinned refs.
* ``<skill>`` XML wrapping lands in the assembled system prompt.
* tool_names conflict between two skills → :class:`SkillConflictError`.
* required_models mismatch → :class:`SkillModelMismatchError`.
* Not-Found / Version-Not-Found / Not-Active → distinct exception classes.
* No-skill manifest still builds (no resolver needed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.skill.memory import InMemorySkillStore
from expert_work.protocol import AgentSpec, SkillVersion
from expert_work.protocol.skill import SkillStatus
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator.agent_factory import _SkillLookupResult, build_agent
from orchestrator.errors import (
    AgentFactoryError,
    SkillConflictError,
    SkillModelMismatchError,
    SkillNotActiveError,
    SkillNotFoundError,
    SkillVersionNotFoundError,
)

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

_ANTHROPIC_KEY_NAME = "anthropic-test"
_MINIMAL_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "test", "version": "1.0.0", "tenant": "test-tenant"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-6",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        "system_prompt": {"template": "you are an agent"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


@pytest.fixture
async def cp() -> AsyncIterator[BaseCheckpointSaver[object]]:
    async with make_checkpointer("memory") as checkpointer:
        yield checkpointer


def _spec_with_skills(skills: list[str], **model_overrides: Any) -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["skills"] = skills
    doc["spec"]["model"].update(model_overrides)
    return AgentSpec.model_validate(doc)


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"})


async def _platform_resolver(provider: str) -> list[str]:
    # Stream Y-2 — agent builds resolve the platform key; these specs are all
    # anthropic, so every provider maps to the seeded anthropic dev key.
    del provider
    return [f"secret://{_ANTHROPIC_KEY_NAME}"]


async def _build(spec: AgentSpec, **kwargs: Any) -> Any:
    """``build_agent`` with the platform key resolver defaulted (Stream Y-2)."""
    kwargs.setdefault("provider_key_resolver", _platform_resolver)
    return await build_agent(spec, **kwargs)


def _make_version(
    *,
    name: str = "foo",
    version: int = 1,
    prompt_fragment: str = "be helpful with X",
    tool_names: tuple[str, ...] = (),
    required_models: tuple[str, ...] = (),
    # These tests exercise EAGER body injection (the ``<skill>`` fragment
    # wrapping). RT-ADR-11 made lazy the default, so pin eager here; the lazy
    # summary path is covered by the skill-reference / compressor tests.
    lazy_load: bool = False,
) -> SkillVersion:
    return SkillVersion(
        id=uuid4(),
        skill_id=uuid4(),
        tenant_id=uuid4(),
        version=version,
        prompt_fragment=prompt_fragment,
        tool_names=tool_names,
        description=f"{name} skill",
        category=None,
        required_models=required_models,
        authored_by="human",
        created_at=datetime.now(UTC),
        lazy_load=lazy_load,
    )


def _make_resolver(rows: dict[tuple[str, int | None], _SkillLookupResult]) -> Any:
    """Build a resolver that returns canned ``_SkillLookupResult`` per
    (name, version) lookup. Missing keys → not_found."""

    def resolver(tenant_id: UUID, name: str, version: int | None) -> _SkillLookupResult:
        del tenant_id
        return rows.get((name, version), _SkillLookupResult.not_found())

    return resolver


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_no_skills_no_resolver_works(cp: BaseCheckpointSaver[object]) -> None:
    """An empty ``spec.skills`` builds cleanly without a resolver."""
    spec = AgentSpec.model_validate(_MINIMAL_SPEC)
    built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    # PI-1: spotlighting on by default appends the untrusted-content clause.
    assert built.system_prompt.startswith("you are an agent")


@pytest.mark.asyncio
async def test_build_agent_bare_skill_ref_resolves_and_wraps_prompt(
    cp: BaseCheckpointSaver[object],
) -> None:
    spec = _spec_with_skills(["foo"])
    version = _make_version(name="foo", prompt_fragment="explain X to the user")
    resolver = _make_resolver({("foo", None): _SkillLookupResult.ok(version)})
    built = await _build(
        spec,
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        tenant_id=uuid4(),
    )
    assert "you are an agent" in built.system_prompt
    assert '<skill name="foo" version="1">' in built.system_prompt
    assert "</skill>" in built.system_prompt
    assert "explain X to the user" in built.system_prompt
    assert "advisory context" in built.system_prompt


@pytest.mark.asyncio
async def test_available_skills_summary_states_the_sandbox_directory(
    cp: BaseCheckpointSaver[object],
) -> None:
    """波 2 终审 Important-1 —— 技能文件搬到
    ``{SANDBOX_SKILLS_ROOT}/<agent_key>/<name>/`` 之后,提示词必须陈述这个
    绝对目录:文件工具 realpath 锁死在 ``/workspace`` 够不到它,而
    ``agent_key`` 带 8 位摘要后缀,agent 猜不出来。``dir=`` 用与 seed 落点
    同一个 :func:`sanitize_agent_key` 算出,这条断言即是两者的漂移闸 ——
    改任一侧的拼法都会在这里红。"""
    from expert_work.persistence import SANDBOX_SKILLS_ROOT
    from orchestrator.tools.skill_seed import sanitize_agent_key

    spec = _spec_with_skills(["foo"])
    version = _make_version(name="foo", prompt_fragment="explain X to the user")
    resolver = _make_resolver({("foo", None): _SkillLookupResult.ok(version)})
    built = await _build(
        spec,
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        tenant_id=uuid4(),
    )

    expected = f"{SANDBOX_SKILLS_ROOT}/{sanitize_agent_key(spec.metadata.name)}/foo"
    assert f'dir="{expected}"' in built.system_prompt


@pytest.mark.asyncio
async def test_build_agent_pinned_skill_ref_resolves(cp: BaseCheckpointSaver[object]) -> None:
    spec = _spec_with_skills(["bar@3"])
    version = _make_version(name="bar", version=3)
    resolver = _make_resolver({("bar", 3): _SkillLookupResult.ok(version)})
    built = await _build(
        spec,
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        tenant_id=uuid4(),
    )
    assert '<skill name="bar" version="3">' in built.system_prompt


@pytest.mark.asyncio
async def test_build_agent_multiple_skills_preserve_declaration_order(
    cp: BaseCheckpointSaver[object],
) -> None:
    spec = _spec_with_skills(["alpha", "beta@2"])
    alpha = _make_version(name="alpha", prompt_fragment="ALPHA-BODY")
    beta = _make_version(name="beta", version=2, prompt_fragment="BETA-BODY")
    resolver = _make_resolver(
        {
            ("alpha", None): _SkillLookupResult.ok(alpha),
            ("beta", 2): _SkillLookupResult.ok(beta),
        }
    )
    built = await _build(
        spec,
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        tenant_id=uuid4(),
    )
    alpha_pos = built.system_prompt.index("ALPHA-BODY")
    beta_pos = built.system_prompt.index("BETA-BODY")
    assert alpha_pos < beta_pos


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_skill_without_resolver_fails(cp: BaseCheckpointSaver[object]) -> None:
    """Manifest declares a skill but build_agent has no resolver wired —
    refuse to silently ignore the skill at run time."""
    spec = _spec_with_skills(["foo"])
    with pytest.raises(AgentFactoryError, match="skill_resolver"):
        await _build(spec, secret_store=_secret_store(), checkpointer=cp)


@pytest.mark.asyncio
async def test_build_agent_skill_not_found_raises(cp: BaseCheckpointSaver[object]) -> None:
    spec = _spec_with_skills(["missing"])
    resolver = _make_resolver({})
    with pytest.raises(SkillNotFoundError):
        await _build(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_build_agent_pinned_version_missing_raises(
    cp: BaseCheckpointSaver[object],
) -> None:
    spec = _spec_with_skills(["foo@99"])
    resolver = _make_resolver({("foo", 99): _SkillLookupResult.version_not_found()})
    with pytest.raises(SkillVersionNotFoundError):
        await _build(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_build_agent_bare_ref_to_inactive_skill_raises(
    cp: BaseCheckpointSaver[object],
) -> None:
    spec = _spec_with_skills(["foo"])
    resolver = _make_resolver({("foo", None): _SkillLookupResult.not_active()})
    with pytest.raises(SkillNotActiveError):
        await _build(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_build_agent_not_entitled_raises_requires_plan_error(
    cp: BaseCheckpointSaver[object],
) -> None:
    """Stream X (Mini-ADR X-4) — a platform skill the tenant's plan tier
    doesn't satisfy surfaces a clear ``requires the {tier} plan`` build error."""
    spec = _spec_with_skills(["foo"])
    resolver = _make_resolver({("foo", None): _SkillLookupResult.not_entitled(required_tier="pro")})
    with pytest.raises(AgentFactoryError, match="requires the pro plan"):
        await _build(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_build_agent_required_models_mismatch_raises(
    cp: BaseCheckpointSaver[object],
) -> None:
    """Skill declares ``required_models`` but agent's primary model isn't in the list."""
    spec = _spec_with_skills(["foo"])
    version = _make_version(name="foo", required_models=("gpt-4o",))
    resolver = _make_resolver({("foo", None): _SkillLookupResult.ok(version)})
    with pytest.raises(SkillModelMismatchError, match="gpt-4o"):
        await _build(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_build_agent_required_models_match_passes(
    cp: BaseCheckpointSaver[object],
) -> None:
    """Empty ``required_models`` skips the check; matching model passes."""
    spec = _spec_with_skills(["foo"])
    version = _make_version(name="foo", required_models=("claude-sonnet-4-6",))
    resolver = _make_resolver({("foo", None): _SkillLookupResult.ok(version)})
    built = await _build(
        spec,
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        tenant_id=uuid4(),
    )
    assert built is not None


@pytest.mark.asyncio
async def test_build_agent_conflicting_tool_names_raises(
    cp: BaseCheckpointSaver[object],
) -> None:
    """Two skills declaring the same tool_name → SkillConflictError."""
    spec = _spec_with_skills(["alpha", "beta"])
    alpha = _make_version(name="alpha", tool_names=("web_search",))
    beta = _make_version(name="beta", tool_names=("web_search",))
    resolver = _make_resolver(
        {
            ("alpha", None): _SkillLookupResult.ok(alpha),
            ("beta", None): _SkillLookupResult.ok(beta),
        }
    )
    with pytest.raises(SkillConflictError, match="web_search"):
        await _build(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=uuid4(),
        )


# ---------------------------------------------------------------------------
# SE-16 (SE-A42) — auto_attach_evolved_skills
# ---------------------------------------------------------------------------


def _spec_auto_attach(skills: list[str] | None = None) -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["auto_attach_evolved_skills"] = True
    if skills:
        doc["spec"]["skills"] = skills
    return AgentSpec.model_validate(doc)


async def _seed_evolved(
    store: InMemorySkillStore,
    *,
    tenant: UUID,
    name: str = "evolved-reporting",
    agent_name: str = "test",
    tool_names: tuple[str, ...] = (),
    origin: str | None = "distilled",
    fragment: str = "distilled how-to body",
) -> None:
    """An ACTIVE agent-authored skill, latest version carrying ``origin``."""
    skill = await store.create_skill(
        skill_id=uuid4(),
        tenant_id=tenant,
        name=name,
        description=f"{name} desc",
        visibility="agent_private",
        created_by_agent_name=agent_name,
    )
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill.id,
        tenant_id=tenant,
        prompt_fragment=fragment,
        tool_names=tool_names,
        description=f"{name} desc",
        authored_by="agent",
        evolution_origin=origin,  # type: ignore[arg-type]
    )
    await store.set_status(skill_id=skill.id, tenant_id=tenant, status=SkillStatus.ACTIVE)


@pytest.mark.asyncio
async def test_auto_attach_binds_distilled_skill_lazily(cp: BaseCheckpointSaver[object]) -> None:
    """Opt-in attaches the agent's own distilled skill: summary + bound for
    rollback, but never an eager prompt fragment (lazy contract)."""
    tenant = uuid4()
    store = InMemorySkillStore()
    await _seed_evolved(store, tenant=tenant)
    built = await _build(
        _spec_auto_attach(),
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_store=store,
        tenant_id=tenant,
    )
    assert 'name="evolved-reporting"' in built.system_prompt  # <available-skills> entry
    assert "distilled how-to body" not in built.system_prompt  # no eager body
    assert [b.skill_version for b in built.bound_distilled_skills] == [1]


@pytest.mark.asyncio
async def test_auto_attach_defaults_off(cp: BaseCheckpointSaver[object]) -> None:
    tenant = uuid4()
    store = InMemorySkillStore()
    await _seed_evolved(store, tenant=tenant)
    built = await _build(
        AgentSpec.model_validate(_MINIMAL_SPEC),
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_store=store,
        tenant_id=tenant,
    )
    assert "evolved-reporting" not in built.system_prompt
    assert built.bound_distilled_skills == ()


@pytest.mark.asyncio
async def test_auto_attach_ignores_non_distilled_and_other_agents(
    cp: BaseCheckpointSaver[object],
) -> None:
    """In-session-authored skills and other agents' distillates stay out."""
    tenant = uuid4()
    store = InMemorySkillStore()
    await _seed_evolved(store, tenant=tenant, name="in-session-one", origin="in_session")
    await _seed_evolved(store, tenant=tenant, name="someone-elses", agent_name="other-agent")
    built = await _build(
        _spec_auto_attach(),
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_store=store,
        tenant_id=tenant,
    )
    assert "in-session-one" not in built.system_prompt
    assert "someone-elses" not in built.system_prompt


@pytest.mark.asyncio
async def test_auto_attach_manifest_declaration_wins(cp: BaseCheckpointSaver[object]) -> None:
    """The same name declared in ``skills:`` resolves through the manifest
    path (eager, hard-fail semantics); the auto-attach duplicate is skipped."""
    tenant = uuid4()
    store = InMemorySkillStore()
    await _seed_evolved(store, tenant=tenant, name="foo", fragment="distilled body")
    manifest_version = _make_version(name="foo", prompt_fragment="manifest body")
    resolver = _make_resolver({("foo", None): _SkillLookupResult.ok(manifest_version)})
    built = await _build(
        _spec_auto_attach(["foo"]),
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        skill_store=store,
        tenant_id=tenant,
    )
    assert "manifest body" in built.system_prompt
    assert "distilled body" not in built.system_prompt


@pytest.mark.asyncio
async def test_auto_attach_tool_conflict_soft_skips(cp: BaseCheckpointSaver[object]) -> None:
    """A distilled skill colliding on a tool name is skipped with a log —
    never a build-breaking SkillConflictError."""
    tenant = uuid4()
    store = InMemorySkillStore()
    await _seed_evolved(store, tenant=tenant, name="clashing", tool_names=("shared_tool",))
    manifest_version = _make_version(name="foo", tool_names=("shared_tool",))
    resolver = _make_resolver({("foo", None): _SkillLookupResult.ok(manifest_version)})
    built = await _build(
        _spec_auto_attach(["foo"]),
        secret_store=_secret_store(),
        checkpointer=cp,
        skill_resolver=resolver,
        skill_store=store,
        tenant_id=tenant,
    )
    assert 'name="foo"' in built.system_prompt
    assert "clashing" not in built.system_prompt
