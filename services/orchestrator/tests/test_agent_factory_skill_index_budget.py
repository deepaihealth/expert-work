"""B-84 item 8 —— ``<available-skills>`` 索引段的字符预算 + 两档降级。

护栏的三条不变式,每条都有一条测试专门钉:

1. **永不丢弃条目** —— 降级只降到"只留名字"。名字是恢复路径的句柄
   (``skill_view(name)`` 按名字加载正文),丢掉条目是不可恢复的。
2. **判定与渲染只看绑定集合的内容与顺序** —— 不读 user_id / 租户 / 时间 /
   使用频次。索引在系统提示词里是 prompt 前缀,按轮或按用户变化的内容会让
   整段下游缓存作废。
3. **降级时必须明说** —— 块首那一行告知不是装饰,少了它模型会以为这些技能
   本来就只有名字。

外加一条防误伤:今天最大的绑定集合(19 个)必须**逐字**还是改动前的样子。
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.protocol import SkillVersion
from expert_work.protocol.skill import compute_content_hash
from orchestrator.agent_factory import (
    DEGRADE_NOTICE_OVERHEAD,
    MAX_SKILLS_INDEX_CHARS,
    _assemble_system_prompt,
    _render_skill_name_only,
    _render_skill_summary,
    _render_skills_index,
)

# 2026-09-20 测试环境实测:单条索引 288 ~ 471 字符,57 个全绑 = 18,571 字符。
# 下面两个夹具按实测体积造,别随手改小 —— 改小了"超预算"的用例就不再超预算。
_MEASURED_ENTRY_CHARS = 326
_MEASURED_MAX_BOUND_TODAY = 19  # ai-health-plan / ai-health-report
_MEASURED_ALL_ACTIVE_SKILLS = 57


def _make_version(
    *,
    description: str,
    tenant_id: UUID | None = None,
    created_at: datetime | None = None,
) -> SkillVersion:
    prompt = "body"
    return SkillVersion(
        id=uuid4(),
        skill_id=uuid4(),
        tenant_id=tenant_id or uuid4(),
        version=1,
        prompt_fragment=prompt,
        tool_names=(),
        description=description,
        category="ops",
        required_models=(),
        authored_by="human",
        supporting_files={},
        lazy_load=True,
        content_hash=compute_content_hash(prompt, {}),
        high_risk=False,
        created_at=created_at or datetime.now(UTC),
    )


def _summaries(count: int, *, entry_chars: int = _MEASURED_ENTRY_CHARS) -> list[str]:
    """造 ``count`` 条索引,每条约 ``entry_chars`` 字符(按实测体积)。"""
    out: list[str] = []
    for i in range(count):
        name = f"skill-{i:02d}"
        head = _render_skill_summary(name=name, version=_make_version(description="x"))
        pad = max(1, entry_chars - len(head))
        out.append(_render_skill_summary(name=name, version=_make_version(description="d" * pad)))
    return out


def _code_without_docstring(func: Callable[..., object]) -> str:
    """函数体源码,去掉 docstring —— 免得注释里的字眼把扫描测试自己搞红。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    if ast.get_docstring(fn) is not None:
        fn.body = fn.body[1:]
    return ast.unparse(fn)


# ─── 防误伤:今天的最大绑定集合不许被护栏碰到 ──────────────────────────────


def test_todays_largest_binding_set_is_rendered_byte_for_byte_as_before() -> None:
    """19 个技能(今天的最大值)→ 不降级,输出与改动前逐字相同。

    改动前的组装就是 ``"\\n  ".join(summaries)``;这条把它写死在断言里,
    护栏一旦误伤现状就红。
    """
    summaries = _summaries(_MEASURED_MAX_BOUND_TODAY)
    # 实测 ai-health-plan 的索引 5,470 字符,离预算差得远。
    assert len("\n  ".join(summaries)) < MAX_SKILLS_INDEX_CHARS
    assert _render_skills_index(summaries) == "\n  ".join(summaries)

    # 整块也要逐字一样(护栏接在 ``_assemble_system_prompt`` 里)。
    prompt = _assemble_system_prompt(base="b", skill_fragments=[], skill_summaries=summaries)
    assert (
        "\n\n<available-skills>\n  " + "\n  ".join(summaries) + "\n</available-skills>"
    ) in prompt


def test_index_exactly_at_budget_is_not_degraded() -> None:
    """边界:正好等于预算 → 不降级(判定是 ``>`` 不是 ``>=``)。"""
    sep = "\n  "
    # 先摆到"再加一条就超"的前一格,再用最后一条把总长补到正好等于预算。
    summaries = _summaries(_MEASURED_ALL_ACTIVE_SKILLS)
    while len(sep.join(summaries)) > MAX_SKILLS_INDEX_CHARS:
        summaries.pop()
    summaries.pop()  # 空出一条的余量给下面的定长尾条

    head = sep.join(summaries)
    pad = (
        MAX_SKILLS_INDEX_CHARS - len(head) - len(sep) - len('<skill name="tail" description="" />')
    )
    assert pad >= 0, "夹具没留够余量,这条边界用例造不出"
    tail = _render_skill_summary(name="tail", version=_make_version(description="d" * pad))
    summaries.append(tail)

    assert len(sep.join(summaries)) == MAX_SKILLS_INDEX_CHARS
    assert _render_skills_index(summaries) == sep.join(summaries)


# ─── 不变式 1:永不丢弃条目 ────────────────────────────────────────────────


def test_over_budget_degrades_to_name_only_keeping_every_entry() -> None:
    summaries = _summaries(_MEASURED_ALL_ACTIVE_SKILLS)
    assert len("\n  ".join(summaries)) > MAX_SKILLS_INDEX_CHARS

    rendered = _render_skills_index(summaries)

    # 一条不少 —— 逐个名字点名,包括最后一条(截断丢尾部时它第一个消失)。
    for i in range(_MEASURED_ALL_ACTIVE_SKILLS):
        assert f'<skill name="skill-{i:02d}" />' in rendered
    assert rendered.count("<skill ") == _MEASURED_ALL_ACTIVE_SKILLS
    # 描述真的没了(降级要省到字符上,不能只是换个写法)
    assert "description=" not in rendered
    assert len(rendered) < len("\n  ".join(summaries))


def test_name_only_still_over_budget_is_sent_whole_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """没有档 3:只留名字仍超预算 → 照发 + warning,不截断、不丢。

    57 个只留名字约 1-2 KB,这一档预期永不触发;这里用一个远超现实的绑定集合
    把它逼出来,确认兜底行为是"完整 + 喊一声"而不是"悄悄少给几个"。
    """
    count = 800
    summaries = _summaries(count)
    with caplog.at_level(logging.WARNING, logger="expert_work.orchestrator.agent_factory"):
        rendered = _render_skills_index(summaries)

    assert rendered.count("<skill ") == count
    assert f'<skill name="skill-{count - 1:02d}" />' in rendered
    assert len(rendered) > MAX_SKILLS_INDEX_CHARS
    assert any("still over budget" in r.message for r in caplog.records)


# ─── 不变式 2:只看绑定集合的内容与顺序 ────────────────────────────────────


@pytest.mark.parametrize(
    "count",
    [_MEASURED_MAX_BOUND_TODAY, _MEASURED_ALL_ACTIVE_SKILLS],
    ids=["tier-1-full", "tier-2-name-only"],
)
def test_same_binding_set_renders_identically_across_tenants_and_users(count: int) -> None:
    """同一绑定集合、不同租户/不同用户/不同写入时间 → 渲染结果逐字相同。

    **两档都要验**:只验降级那一档会漏掉 bug —— 降级本来就把描述压掉了,
    条目里混进租户/时间也会一并被压没,测试照绿。档 1 才是它藏身的地方。
    """

    def build(*, tenant_id: UUID, created_at: datetime) -> list[str]:
        return [
            _render_skill_summary(
                name=f"skill-{i:02d}",
                version=_make_version(
                    description="d" * 300, tenant_id=tenant_id, created_at=created_at
                ),
            )
            for i in range(count)
        ]

    tenant_a = build(tenant_id=uuid4(), created_at=datetime(2024, 1, 1, tzinfo=UTC))
    tenant_b = build(tenant_id=uuid4(), created_at=datetime(2026, 9, 20, tzinfo=UTC))

    assert _render_skills_index(tenant_a) == _render_skills_index(tenant_b)
    # 同一输入重复渲染也必须逐字相同(前缀缓存靠的就是这个)。
    assert _render_skills_index(tenant_a) == _render_skills_index(tenant_a)


def test_index_render_reads_nothing_but_the_binding_set() -> None:
    """判定与渲染不许碰 user/租户/时间/频次 —— 这是缓存前缀的硬约束。

    签名只收 ``summaries``,函数体里不出现任何按轮/按用户变化的来源。谁把
    判定改成依赖 ``user_id`` 或 ``datetime.now()``,这条当场红。
    """
    assert list(inspect.signature(_render_skills_index).parameters) == ["summaries"]

    forbidden = ("user_id", "tenant", "datetime", "now(", "time.", "random", "last_used")
    for func in (_render_skills_index, _render_skill_name_only):
        code = _code_without_docstring(func)
        for token in forbidden:
            assert token not in code, f"{func.__name__} 碰了 {token!r}"


# ─── 不变式 3:降级时必须明说 ──────────────────────────────────────────────


def test_degraded_index_tells_the_model_what_happened() -> None:
    summaries = _summaries(_MEASURED_ALL_ACTIVE_SKILLS)
    rendered = _render_skills_index(summaries)

    first_line = rendered.split("\n")[0]
    assert first_line.startswith("⚠️")
    assert str(_MEASURED_ALL_ACTIVE_SKILLS) in first_line
    assert "name only" in first_line
    # 恢复路径必须点名 —— 只说"降级了"而不说怎么拿回正文等于没说。
    assert "skill_view(name)" in first_line


def test_degrade_notice_fits_the_reserved_overhead() -> None:
    """告知行的长度算进预算 —— ``DEGRADE_NOTICE_OVERHEAD`` 不是摆设。"""
    summaries = _summaries(_MEASURED_ALL_ACTIVE_SKILLS)
    rendered = _render_skills_index(summaries)
    notice = rendered.split("\n")[0]
    assert len(notice) <= DEGRADE_NOTICE_OVERHEAD
    # 名字段 + 预留的告知开销,一起仍在预算内。
    body_without_notice = rendered[len(notice) + len("\n  ") :]
    assert len(body_without_notice) <= MAX_SKILLS_INDEX_CHARS - DEGRADE_NOTICE_OVERHEAD


# ─── 渲染器与解析器是一对 ─────────────────────────────────────────────────


def test_name_only_round_trips_the_rendered_summary() -> None:
    """``_render_skill_name_only`` 读的就是 ``_render_skill_summary`` 的产物。

    两个函数改一个不改另一个,降级会悄悄退化成"原样输出"(不丢条目,但一个
    字符也省不下来)。这条钉住它们成对。
    """
    summary = _render_skill_summary(
        name="ai-health-plan", version=_make_version(description='has "quote" and 中文')
    )
    assert _render_skill_name_only(summary) == '<skill name="ai-health-plan" />'


def test_unparseable_entry_is_kept_verbatim_never_dropped() -> None:
    """取不到名字也不许丢 —— 省字符是目的,丢条目不是手段。"""
    weird = "<!-- not a skill entry -->"
    assert _render_skill_name_only(weird) == weird
