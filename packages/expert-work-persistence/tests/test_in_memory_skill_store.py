"""Unit tests for :class:`InMemorySkillStore` — Stream J.7a (Mini-ADR J-23)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from expert_work.persistence import (
    DuplicateSkillError,
    InMemorySkillStore,
    SkillNotFoundError,
)
from expert_work.protocol import SkillStatus


def _t() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# create + get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_skill_starts_in_draft_with_zero_version() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(
        skill_id=uuid4(),
        tenant_id=tenant,
        name="foo",
        description="my foo skill",
        category="data",
    )
    assert skill.status == SkillStatus.DRAFT
    assert skill.latest_version == 0
    assert skill.description == "my foo skill"
    assert skill.category == "data"


@pytest.mark.asyncio
async def test_create_skill_rejects_duplicate_tenant_name() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    with pytest.raises(DuplicateSkillError):
        await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")


@pytest.mark.asyncio
async def test_get_skill_cross_tenant_returns_none() -> None:
    store = InMemorySkillStore()
    skill_id = uuid4()
    await store.create_skill(skill_id=skill_id, tenant_id=_t(), name="foo")
    other_tenant = _t()
    assert await store.get_skill(skill_id=skill_id, tenant_id=other_tenant) is None


# ---------------------------------------------------------------------------
# shadowed_skill_names — batch name-shadow lookup. Replaces a per-skill
# get_skill_by_name loop in the merged-view endpoint (avoids an N+1 over the
# platform library).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadowed_skill_names_returns_owned_subset() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="bar")
    got = await store.shadowed_skill_names(tenant_id=tenant, names=["foo", "baz", "bar"])
    assert got == {"foo", "bar"}


@pytest.mark.asyncio
async def test_shadowed_skill_names_is_tenant_scoped() -> None:
    store = InMemorySkillStore()
    mine, other = _t(), _t()
    await store.create_skill(skill_id=uuid4(), tenant_id=other, name="foo")
    # ``foo`` belongs to another tenant — not shadowed for ``mine``.
    assert await store.shadowed_skill_names(tenant_id=mine, names=["foo"]) == set()


@pytest.mark.asyncio
async def test_shadowed_skill_names_empty_input_returns_empty() -> None:
    store = InMemorySkillStore()
    assert await store.shadowed_skill_names(tenant_id=_t(), names=[]) == set()


@pytest.mark.asyncio
async def test_shadowed_skill_names_counts_any_status() -> None:
    """A tenant name shadows regardless of status (a DRAFT shadows too) —
    matches ``get_skill_by_name``, which does not filter on status."""
    store = InMemorySkillStore()
    tenant = _t()
    # A freshly created skill is DRAFT.
    await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    assert await store.shadowed_skill_names(tenant_id=tenant, names=["foo"]) == {"foo"}


# ---------------------------------------------------------------------------
# add_version + resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_version_auto_increments() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    v1 = await store.add_version(
        version_id=uuid4(),
        skill_id=skill.id,
        tenant_id=tenant,
        prompt_fragment="be helpful",
        tool_names=["web_search"],
    )
    v2 = await store.add_version(
        version_id=uuid4(),
        skill_id=skill.id,
        tenant_id=tenant,
        prompt_fragment="be more helpful",
        tool_names=["web_search", "exec_python"],
    )
    assert v1.version == 1
    assert v2.version == 2
    refreshed = await store.get_skill(skill_id=skill.id, tenant_id=tenant)
    assert refreshed is not None and refreshed.latest_version == 2


@pytest.mark.asyncio
async def test_add_version_defaults_to_lazy_load() -> None:
    # RT-ADR-11 — a new version created without an explicit lazy_load defaults
    # to progressive disclosure (lazy), not eager body inlining.
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    v = await store.add_version(
        version_id=uuid4(),
        skill_id=skill.id,
        tenant_id=tenant,
        prompt_fragment="be helpful",
    )
    assert v.lazy_load is True


@pytest.mark.asyncio
async def test_add_platform_version_defaults_to_lazy_load() -> None:
    # RT-ADR-11 — curated platform skills default lazy on creation too.
    store = InMemorySkillStore()
    skill = await store.create_platform_skill(skill_id=uuid4(), name="office")
    v = await store.add_platform_version(
        version_id=uuid4(),
        skill_id=skill.id,
        prompt_fragment="be helpful",
    )
    assert v.lazy_load is True


@pytest.mark.asyncio
async def test_add_version_unknown_skill_raises() -> None:
    store = InMemorySkillStore()
    with pytest.raises(SkillNotFoundError):
        await store.add_version(
            version_id=uuid4(),
            skill_id=uuid4(),
            tenant_id=_t(),
            prompt_fragment="x",
        )


@pytest.mark.asyncio
async def test_resolve_by_name_only_active_skills() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    await store.add_version(
        version_id=uuid4(), skill_id=skill.id, tenant_id=tenant, prompt_fragment="v1"
    )
    # Draft — bare name should not resolve.
    assert await store.resolve_by_name(tenant_id=tenant, name="foo") is None
    # Promote to active.
    await store.set_status(skill_id=skill.id, tenant_id=tenant, status=SkillStatus.ACTIVE)
    resolved = await store.resolve_by_name(tenant_id=tenant, name="foo")
    assert resolved is not None
    assert resolved.version == 1
    # Archive — bare name no longer resolves.
    await store.set_status(skill_id=skill.id, tenant_id=tenant, status=SkillStatus.ARCHIVED)
    assert await store.resolve_by_name(tenant_id=tenant, name="foo") is None


@pytest.mark.asyncio
async def test_resolve_pinned_allows_any_status() -> None:
    """Pinning is the reproducibility escape hatch — works regardless
    of the parent skill's lifecycle state."""
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    await store.add_version(
        version_id=uuid4(), skill_id=skill.id, tenant_id=tenant, prompt_fragment="v1"
    )
    await store.set_status(skill_id=skill.id, tenant_id=tenant, status=SkillStatus.ARCHIVED)
    # Archived but pinned ref still resolves.
    resolved = await store.resolve_pinned(tenant_id=tenant, name="foo", version=1)
    assert resolved is not None
    assert resolved.version == 1


@pytest.mark.asyncio
async def test_resolve_pinned_missing_version_returns_none() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    await store.add_version(
        version_id=uuid4(), skill_id=skill.id, tenant_id=tenant, prompt_fragment="v1"
    )
    assert await store.resolve_pinned(tenant_id=tenant, name="foo", version=99) is None
    assert await store.resolve_pinned(tenant_id=tenant, name="missing", version=1) is None


# ---------------------------------------------------------------------------
# list + paginate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_skills_filters_by_status_and_category() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    a = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="a", category="data")
    b = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="b", category="ops")
    c = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="c", category="data")
    await store.set_status(skill_id=a.id, tenant_id=tenant, status=SkillStatus.ACTIVE)
    await store.set_status(skill_id=c.id, tenant_id=tenant, status=SkillStatus.ACTIVE)
    # Filter by status.
    actives, _ = await store.list_skills(tenant_id=tenant, status=SkillStatus.ACTIVE)
    assert {r.id for r in actives} == {a.id, c.id}
    # Filter by category.
    data_skills, _ = await store.list_skills(tenant_id=tenant, category="data")
    assert {r.id for r in data_skills} == {a.id, c.id}
    # Combined.
    combo, _ = await store.list_skills(tenant_id=tenant, status=SkillStatus.ACTIVE, category="ops")
    assert combo == []
    # Tenant isolation.
    other = await store.list_skills(tenant_id=_t())
    assert other == ([], None)
    # Use the unused 'b' to silence linters and assert it's not in actives.
    _ = b


@pytest.mark.asyncio
async def test_list_skills_paginates_with_cursor() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    ids = []
    for i in range(5):
        skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name=f"s{i}")
        ids.append(skill.id)
    page1, cursor = await store.list_skills(tenant_id=tenant, limit=2)
    assert len(page1) == 2
    assert cursor is not None
    page2, cursor2 = await store.list_skills(tenant_id=tenant, limit=2, cursor=cursor)
    assert len(page2) == 2
    assert cursor2 is not None
    page3, cursor3 = await store.list_skills(tenant_id=tenant, limit=2, cursor=cursor2)
    assert len(page3) == 1
    assert cursor3 is None
    # No duplicates across pages.
    seen = {r.id for r in page1 + page2 + page3}
    assert seen == set(ids)


# ---------------------------------------------------------------------------
# version metadata mirroring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_version_mirrors_description_and_category() -> None:
    """Latest version's description / category lands on the parent skill row
    so admin list responses can render without a JOIN."""
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(
        skill_id=uuid4(), tenant_id=tenant, name="foo", description="old"
    )
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill.id,
        tenant_id=tenant,
        prompt_fragment="x",
        description="new",
        category="research",
    )
    refreshed = await store.get_skill(skill_id=skill.id, tenant_id=tenant)
    assert refreshed is not None
    assert refreshed.description == "new"
    assert refreshed.category == "research"


@pytest.mark.asyncio
async def test_set_status_unknown_skill_raises() -> None:
    store = InMemorySkillStore()
    with pytest.raises(SkillNotFoundError):
        await store.set_status(skill_id=uuid4(), tenant_id=_t(), status=SkillStatus.ACTIVE)


@pytest.mark.asyncio
async def test_list_versions_orders_desc() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    for n in range(3):
        await store.add_version(
            version_id=uuid4(),
            skill_id=skill.id,
            tenant_id=tenant,
            prompt_fragment=f"v{n + 1}",
        )
    versions = await store.list_versions(skill_id=skill.id, tenant_id=tenant)
    assert [v.version for v in versions] == [3, 2, 1]


@pytest.mark.asyncio
async def test_add_version_rejects_invalid_authored_by() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="foo")
    with pytest.raises(ValueError, match="authored_by"):
        await store.add_version(
            version_id=uuid4(),
            skill_id=skill.id,
            tenant_id=tenant,
            prompt_fragment="x",
            authored_by="alien",
        )


# ---------------------------------------------------------------------------
# Stream X — required_tier round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_skill_required_tier_round_trip() -> None:
    from expert_work.protocol.tenant_config import TenantPlan

    store = InMemorySkillStore()
    tenant = _t()
    default_skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="a")
    assert default_skill.required_tier == TenantPlan.FREE
    pro_skill = await store.create_skill(
        skill_id=uuid4(), tenant_id=tenant, name="b", required_tier=TenantPlan.PRO
    )
    assert pro_skill.required_tier == TenantPlan.PRO
    refreshed = await store.get_skill(skill_id=pro_skill.id, tenant_id=tenant)
    assert refreshed is not None and refreshed.required_tier == TenantPlan.PRO


# ---------------------------------------------------------------------------
# Stream X — platform (NULL-tenant) skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_platform_skill_has_null_tenant() -> None:
    from expert_work.protocol.tenant_config import TenantPlan

    store = InMemorySkillStore()
    skill = await store.create_platform_skill(
        skill_id=uuid4(), name="summarize", required_tier=TenantPlan.PRO
    )
    assert skill.tenant_id is None
    assert skill.required_tier == TenantPlan.PRO
    by_id = await store.get_platform_skill(skill_id=skill.id)
    by_name = await store.get_platform_skill_by_name(name="summarize")
    assert by_id is not None and by_id.id == skill.id
    assert by_name is not None and by_name.id == skill.id


@pytest.mark.asyncio
async def test_platform_skill_invisible_to_tenant_methods() -> None:
    store = InMemorySkillStore()
    skill = await store.create_platform_skill(skill_id=uuid4(), name="summarize")
    tenant = _t()
    # Tenant-scoped accessors never see a NULL-tenant row.
    assert await store.get_skill(skill_id=skill.id, tenant_id=tenant) is None
    assert await store.get_skill_by_name(tenant_id=tenant, name="summarize") is None
    tenant_rows, _ = await store.list_skills(tenant_id=tenant)
    assert tenant_rows == []
    # ...and a tenant can have its own skill of the same name (no collision).
    own = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="summarize")
    assert own.tenant_id == tenant


@pytest.mark.asyncio
async def test_platform_skill_duplicate_name_rejected() -> None:
    store = InMemorySkillStore()
    await store.create_platform_skill(skill_id=uuid4(), name="dup")
    with pytest.raises(DuplicateSkillError):
        await store.create_platform_skill(skill_id=uuid4(), name="dup")


@pytest.mark.asyncio
async def test_resolve_platform_by_name_active_vs_draft() -> None:
    store = InMemorySkillStore()
    skill = await store.create_platform_skill(skill_id=uuid4(), name="summarize")
    await store.add_platform_version(
        version_id=uuid4(), skill_id=skill.id, prompt_fragment="summarize text"
    )
    # Still draft → bare-name resolve returns None.
    assert await store.resolve_platform_by_name(name="summarize") is None
    # Activate → resolves to latest version.
    await store.set_platform_status(skill_id=skill.id, status=SkillStatus.ACTIVE)
    resolved = await store.resolve_platform_by_name(name="summarize")
    assert resolved is not None and resolved.version == 1 and resolved.tenant_id is None
    # Pinned resolve works regardless of lifecycle.
    pinned = await store.resolve_platform_pinned(name="summarize", version=1)
    assert pinned is not None and pinned.version == 1


@pytest.mark.asyncio
async def test_platform_versions_listing_and_pin() -> None:
    store = InMemorySkillStore()
    skill = await store.create_platform_skill(skill_id=uuid4(), name="s")
    for n in range(2):
        await store.add_platform_version(
            version_id=uuid4(), skill_id=skill.id, prompt_fragment=f"v{n + 1}"
        )
    versions = await store.list_platform_versions(skill_id=skill.id)
    assert [v.version for v in versions] == [2, 1]
    v1 = await store.get_platform_version_by_number(skill_id=skill.id, version=1)
    assert v1 is not None and v1.version == 1
    pinned = await store.set_platform_pinned(skill_id=skill.id, pinned=True)
    assert pinned.pinned is True


@pytest.mark.asyncio
async def test_platform_category_set_clear_and_distinct_list() -> None:
    store = InMemorySkillStore()
    a = await store.create_platform_skill(skill_id=uuid4(), name="a", category="data")
    b = await store.create_platform_skill(skill_id=uuid4(), name="b", category="writing")
    await store.create_platform_skill(skill_id=uuid4(), name="c")  # uncategorized

    assert await store.list_platform_categories() == ["data", "writing"]

    # Re-label + blank-clears-to-None.
    relabeled = await store.set_platform_category(skill_id=a.id, category="mental-health")
    assert relabeled.category == "mental-health"
    cleared = await store.set_platform_category(skill_id=b.id, category="  ")
    assert cleared.category is None

    assert await store.list_platform_categories() == ["mental-health"]


# ---------------------------------------------------------------------------
# Stream X — curator excludes platform (NULL-tenant) skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_distinct_tenant_ids_excludes_platform() -> None:
    store = InMemorySkillStore()
    tenant = _t()
    tenant_skill = await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="t")
    await store.set_status(skill_id=tenant_skill.id, tenant_id=tenant, status=SkillStatus.ACTIVE)
    plat = await store.create_platform_skill(skill_id=uuid4(), name="p")
    await store.set_platform_status(skill_id=plat.id, status=SkillStatus.ACTIVE)
    ids = await store.curator_distinct_tenant_ids()
    assert ids == [tenant]
    assert None not in ids


@pytest.mark.asyncio
async def test_list_skills_filters_by_created_by_agent_name() -> None:
    """Stream H.6 (Mini-ADR H-11) — agent-authored slice for the Skills tab."""
    store = InMemorySkillStore()
    tenant = _t()
    await store.create_skill(
        skill_id=uuid4(),
        tenant_id=tenant,
        name="authored-by-reporter",
        created_by_agent_name="reporter",
    )
    await store.create_skill(
        skill_id=uuid4(),
        tenant_id=tenant,
        name="authored-by-scribe",
        created_by_agent_name="scribe",
    )
    # Human-authored skill has no agent provenance.
    await store.create_skill(skill_id=uuid4(), tenant_id=tenant, name="human-made")

    rows, _ = await store.list_skills(tenant_id=tenant, created_by_agent_name="reporter")
    assert [s.name for s in rows] == ["authored-by-reporter"]

    none_rows, _ = await store.list_skills(tenant_id=tenant, created_by_agent_name="ghost")
    assert none_rows == []

    # No filter → all three (regression).
    all_rows, _ = await store.list_skills(tenant_id=tenant)
    assert len(all_rows) == 3

    # Cross-tenant variant honours the same filter.
    cross, _ = await store.list_skills_all_tenants(created_by_agent_name="scribe")
    assert [s.name for s in cross] == ["authored-by-scribe"]


# ---------------------------------------------------------------------------
# platform list — offset pagination + search (C+)
# ---------------------------------------------------------------------------


async def _seed_platform(store: InMemorySkillStore, name: str, **kw: object) -> UUID:
    sid = uuid4()
    await store.create_platform_skill(skill_id=sid, name=name, **kw)  # type: ignore[arg-type]
    return sid


@pytest.mark.asyncio
async def test_platform_list_offset_pagination_and_total() -> None:
    store = InMemorySkillStore()
    for i in range(5):
        await _seed_platform(store, f"plat-{i}")
    page1, total = await store.list_platform_skills(offset=0, limit=2)
    page2, total2 = await store.list_platform_skills(offset=2, limit=2)
    assert total == 5 and total2 == 5
    assert len(page1) == 2 and len(page2) == 2
    # Distinct pages, no overlap.
    assert {s.id for s in page1}.isdisjoint({s.id for s in page2})


@pytest.mark.asyncio
async def test_platform_list_q_searches_name_and_description() -> None:
    store = InMemorySkillStore()
    await _seed_platform(store, "pptx-maker", description="build slides")
    await _seed_platform(store, "docx-tool", description="generate PPTX decks too")
    await _seed_platform(store, "unrelated", description="nothing")
    rows, total = await store.list_platform_skills(q="pptx")
    # Matches the name of one and the description of the other (case-insensitive).
    assert total == 2
    assert {s.name for s in rows} == {"pptx-maker", "docx-tool"}


@pytest.mark.asyncio
async def test_bulk_update_by_ids_sets_pinned() -> None:
    store = InMemorySkillStore()
    a = await _seed_platform(store, "a")
    b = await _seed_platform(store, "b")
    c = await _seed_platform(store, "c")
    n = await store.bulk_update_platform_skills(ids=[a, b], set_pinned=True)
    assert n == 2
    rows, _ = await store.list_platform_skills()
    pinned = {s.id for s in rows if s.pinned}
    assert pinned == {a, b} and c not in pinned


@pytest.mark.asyncio
async def test_bulk_update_by_filter_sets_status_and_bumps_state_changed() -> None:
    store = InMemorySkillStore()
    keep = await _seed_platform(store, "report-weekly")
    other = await _seed_platform(store, "misc")
    before = await store.get_platform_skill(skill_id=keep)
    assert before is not None
    n = await store.bulk_update_platform_skills(filter_q="report", set_status=SkillStatus.ARCHIVED)
    assert n == 1
    after = await store.get_platform_skill(skill_id=keep)
    assert after is not None and after.status == SkillStatus.ARCHIVED
    assert after.state_changed_at is not None and before.state_changed_at is not None
    assert after.state_changed_at >= before.state_changed_at
    # The non-matching skill is untouched.
    untouched = await store.get_platform_skill(skill_id=other)
    assert untouched is not None and untouched.status != SkillStatus.ARCHIVED


@pytest.mark.asyncio
async def test_bulk_update_requires_selector_and_patch() -> None:
    store = InMemorySkillStore()
    with pytest.raises(ValueError):
        await store.bulk_update_platform_skills(set_pinned=True)  # no selector
    with pytest.raises(ValueError):
        await store.bulk_update_platform_skills(ids=[uuid4()])  # no patch


@pytest.mark.asyncio
async def test_bulk_set_category_by_ids() -> None:
    store = InMemorySkillStore()
    a = await _seed_platform(store, "a")
    b = await _seed_platform(store, "b")
    c = await _seed_platform(store, "c", category="old")
    n = await store.bulk_update_platform_skills(
        ids=[a, b], update_category=True, new_category="研发"
    )
    assert n == 2
    rows, _ = await store.list_platform_skills()
    by_id = {s.id: s.category for s in rows}
    assert by_id[a] == "研发" and by_id[b] == "研发"
    assert by_id[c] == "old"  # untouched


@pytest.mark.asyncio
async def test_bulk_clear_category_writes_none() -> None:
    store = InMemorySkillStore()
    a = await _seed_platform(store, "a", category="研发")
    n = await store.bulk_update_platform_skills(ids=[a], update_category=True, new_category=None)
    assert n == 1
    rows, _ = await store.list_platform_skills()
    assert rows[0].category is None


@pytest.mark.asyncio
async def test_bulk_category_only_needs_no_status_or_pinned() -> None:
    # update_category alone satisfies the "at least one action" guard.
    store = InMemorySkillStore()
    a = await _seed_platform(store, "a")
    n = await store.bulk_update_platform_skills(ids=[a], update_category=True, new_category="设计")
    assert n == 1


@pytest.mark.asyncio
async def test_bulk_no_action_raises() -> None:
    store = InMemorySkillStore()
    a = await _seed_platform(store, "a")
    with pytest.raises(ValueError):
        await store.bulk_update_platform_skills(ids=[a])  # no set_* and update_category=False


# ---------------------------------------------------------------------------
# platform list — keyset pagination (B-23, feeds the merged ``GET /v1/skills``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_list_keyset_walks_whole_library_without_overlap() -> None:
    store = InMemorySkillStore()
    seeded = {await _seed_platform(store, f"plat-{i}") for i in range(5)}
    pages: list[list[UUID]] = []
    cursor: UUID | None = None
    for _ in range(10):
        rows, cursor = await store.list_platform_skills_keyset(cursor=cursor, limit=2)
        pages.append([s.id for s in rows])
        if cursor is None:
            break
    assert [len(p) for p in pages] == [2, 2, 1]
    walked = [sid for p in pages for sid in p]
    assert len(walked) == len(set(walked)) == 5
    assert set(walked) == seeded
    # Same order as the offset listing (created_at DESC, id) — the two
    # platform listings must agree on what "next" means.
    offset_rows, _ = await store.list_platform_skills(limit=10)
    assert walked == [s.id for s in offset_rows]


@pytest.mark.asyncio
async def test_platform_list_keyset_filters_status_and_skips_tenant_rows() -> None:
    store = InMemorySkillStore()
    active = await _seed_platform(store, "active-one")
    await store.set_platform_status(skill_id=active, status=SkillStatus.ACTIVE)
    await _seed_platform(store, "still-draft")
    await store.create_skill(skill_id=uuid4(), tenant_id=uuid4(), name="tenant-owned")
    rows, next_cursor = await store.list_platform_skills_keyset(status=SkillStatus.ACTIVE, limit=50)
    assert [s.id for s in rows] == [active]
    assert next_cursor is None
