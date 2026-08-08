"""Capability Uplift Sprint #3 — Mini-ADR U-15 progressive disclosure
wiring in ``agent_factory``.

Verifies:
- Every loaded skill produces a `<skill name version description files=... />`
  summary entry in ``loaded_skills.skill_summaries``
- ``lazy_load == False`` skills ALSO produce a body fragment (existing
  eager behavior preserved)
- ``lazy_load == True`` skills produce only the summary (no body)
- Mixed lazy + eager skills work together
- ``_assemble_system_prompt`` emits the ``<available-skills>`` block
  even when all skills are lazy

See ``docs/streams/STREAM-UPLIFT-DESIGN.md`` § 4.3.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from expert_work.protocol import SkillVersion
from expert_work.protocol.skill import (
    SkillSupportingFile,
    compute_content_hash,
    supporting_files_to_jsonable,
)
from orchestrator.agent_factory import (
    _assemble_system_prompt,
    _LoadedSkills,
    _render_skill_summary,
)


def _make_version(
    *,
    name: str,
    prompt: str,
    lazy_load: bool,
    supporting_paths: list[str] | None = None,
) -> SkillVersion:
    supporting = {
        path: SkillSupportingFile(content="", size=0, mime="text/plain")
        for path in (supporting_paths or [])
    }
    jsonable = supporting_files_to_jsonable(supporting)
    return SkillVersion(
        id=uuid4(),
        skill_id=uuid4(),
        tenant_id=uuid4(),
        version=1,
        prompt_fragment=prompt,
        tool_names=("http",),
        description=name,
        category="ops",
        required_models=(),
        authored_by="human",
        supporting_files=supporting,
        lazy_load=lazy_load,
        content_hash=compute_content_hash(prompt, jsonable),
        high_risk=False,
        created_at=datetime.now(UTC),
    )


# ─── _render_skill_summary ───────────────────────────────────────────────


def test_summary_lists_skill_md_first_then_supporting_files() -> None:
    version = _make_version(
        name="api-debug",
        prompt="body",
        lazy_load=False,
        supporting_paths=["scripts/diagnose.py", "reference/error_codes.md"],
    )
    summary = _render_skill_summary(name="api-debug", version=version, agent_key="ag-0badf00d")
    assert 'name="api-debug"' in summary
    assert 'version="1"' in summary
    # SKILL.md must come first; then alphabetical
    assert 'files="SKILL.md, reference/error_codes.md, scripts/diagnose.py"' in summary
    # 波 2 终审 Important-1 —— 提示词必须陈述技能文件在沙箱里的绝对目录:
    # 搬到 /opt/skills/<agent_key>/ 之后,agent 既够不到(文件工具锁死在
    # /workspace)、也猜不出(agent_key 带 8 位摘要后缀)。
    assert 'dir="/opt/skills/ag-0badf00d/api-debug"' in summary


def test_summary_escapes_quotes_in_description() -> None:
    version = _make_version(name="x", prompt="body", lazy_load=False)
    v = version.model_copy(update={"description": 'has "quote" inside'})
    summary = _render_skill_summary(name="x", version=v, agent_key="ag-0badf00d")
    assert "&quot;quote&quot;" in summary
    assert '"quote"' not in summary  # raw " would break the attribute


def test_summary_no_supporting_files_lists_only_skill_md() -> None:
    version = _make_version(name="x", prompt="body", lazy_load=False)
    summary = _render_skill_summary(name="x", version=version, agent_key="ag-0badf00d")
    assert 'files="SKILL.md"' in summary


# ─── _assemble_system_prompt — progressive disclosure ────────────────────


def test_eager_skill_renders_summary_plus_body() -> None:
    base = "You are an agent."
    summary = '<skill name="x" version="1" description="x" files="SKILL.md" />'
    body = '<skill name="x" version="1">\nyou know X\n</skill>'
    result = _assemble_system_prompt(base=base, skill_fragments=[body], skill_summaries=[summary])
    assert "<available-skills>" in result
    assert summary in result
    assert body in result
    assert "you know X" in result


def test_lazy_skill_renders_summary_only_no_body() -> None:
    base = "You are an agent."
    summary = '<skill name="x" version="1" description="x" files="SKILL.md" />'
    result = _assemble_system_prompt(base=base, skill_fragments=[], skill_summaries=[summary])
    assert "<available-skills>" in result
    assert summary in result
    # No body section header
    assert "# Skill bodies" not in result


def test_mixed_lazy_and_eager() -> None:
    base = "You are an agent."
    summaries = [
        '<skill name="lazy-one" version="1" description="lazy" files="SKILL.md" />',
        '<skill name="eager-two" version="1" description="eager" files="SKILL.md" />',
    ]
    fragments = ['<skill name="eager-two" version="1">\neager body\n</skill>']
    result = _assemble_system_prompt(
        base=base, skill_fragments=fragments, skill_summaries=summaries
    )
    # Both summaries present
    assert 'name="lazy-one"' in result
    assert 'name="eager-two"' in result
    # Only eager body
    assert "eager body" in result
    # available-skills block + skill-bodies block both present
    assert "<available-skills>" in result
    assert "# Skill bodies" in result


def test_no_skills_returns_base_unchanged() -> None:
    base = "You are an agent."
    assert _assemble_system_prompt(base=base, skill_fragments=[]) == base
    assert _assemble_system_prompt(base=base, skill_fragments=[], skill_summaries=[]) == base


def test_loaded_skills_default_fields_back_compat() -> None:
    """Pre-Sprint #3 callers construct _LoadedSkills with only the first
    3 args; the new ``skill_summaries`` + ``resolved_versions`` fields
    must default to empty containers."""
    loaded = _LoadedSkills(
        prompt_fragments=["frag"],
        skill_tools={"http": "x"},
        activated_skill_names=["x"],
    )
    assert loaded.skill_summaries == []
    assert loaded.resolved_versions == {}
