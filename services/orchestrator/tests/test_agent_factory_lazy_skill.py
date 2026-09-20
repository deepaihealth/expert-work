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


def test_summary_carries_only_what_choosing_a_skill_needs() -> None:
    """B-84 —— 摘要只留 name + description,三个属性去掉。

    去掉的是 ``files=``(最大的一块,对接方两个 Agent 分别列了 197 / 232 个
    文件名)、``dir=``(改成块头说一次的 ``$EXPERT_WORK_SKILLS_DIR``)、
    ``version=``(选技能不需要版本号,``skill_view`` 也只认 ``skill_name``)。
    这一行每轮 prefill 都重付,所以只放**选**技能用得上的东西;**用**技能才
    需要的文件清单挪到了 ``skill_view(name, "SKILL.md")`` 的返回里
    (见 ``test_skill_view_tool.py`` 里的清单用例)。
    """
    version = _make_version(
        name="api-debug",
        prompt="body",
        lazy_load=False,
        supporting_paths=["scripts/diagnose.py", "reference/error_codes.md"],
    )
    summary = _render_skill_summary(name="api-debug", version=version)
    assert 'name="api-debug"' in summary
    assert "description=" in summary
    # 逐条钉死,别只断言整串相等 —— 哪天有人加回来,红的那条要直接说出是哪个
    assert "files=" not in summary
    assert "dir=" not in summary
    assert "version=" not in summary
    # 文件名一个都不许出现(``files=`` 换个写法回来也算回来)
    assert "diagnose.py" not in summary
    assert "error_codes.md" not in summary


def test_summary_escapes_quotes_in_description() -> None:
    version = _make_version(name="x", prompt="body", lazy_load=False)
    v = version.model_copy(update={"description": 'has "quote" inside'})
    summary = _render_skill_summary(name="x", version=v)
    assert "&quot;quote&quot;" in summary
    assert '"quote"' not in summary  # raw " would break the attribute


def test_summary_is_one_short_line() -> None:
    """瘦身的收益全在这一行的长度上,所以直接钉住它。"""
    version = _make_version(name="x", prompt="body", lazy_load=False)
    summary = _render_skill_summary(name="x", version=version)
    assert summary == '<skill name="x" description="x" />'


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
    # B-85 ③ —— 原来这里断言的是 ``prompt == base``。那只是「自己那个块不出现」
    # 的廉价代理,而平台从此恒追加一段 completion contract(无开关),代理失效。
    # 改成断言真正在乎的那件事:base 原样在最前 + 自己那个块确实不在。
    for prompt in (
        _assemble_system_prompt(base=base, skill_fragments=[]),
        _assemble_system_prompt(base=base, skill_fragments=[], skill_summaries=[]),
    ):
        assert prompt.startswith(base)
        assert "<available-skills>" not in prompt
        assert "<skill>" not in prompt


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
