"""Unit tests for :class:`InMemoryAgentSpecStore`."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.agent_spec import (
    DuplicateAgentSpecError,
    InMemoryAgentSpecStore,
)
from expert_work.protocol import AgentSpec, AgentSpecStatus

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

_BASE_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "code-reviewer", "version": "1.0.0", "tenant": "platform-eng"},
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


def _spec(
    *, version: str = "1.0.0", name: str = "code-reviewer", display_name: str = ""
) -> AgentSpec:
    doc = deepcopy(_BASE_SPEC)
    doc["metadata"]["version"] = version
    doc["metadata"]["name"] = name
    doc["spec"]["display_name"] = display_name
    return AgentSpec.model_validate(doc)


def _sha() -> str:
    return "a" * 64


@pytest.fixture
def store() -> InMemoryAgentSpecStore:
    return InMemoryAgentSpecStore()


@pytest.mark.asyncio
async def test_create_then_get_round_trip(store: InMemoryAgentSpecStore) -> None:
    record = await store.create(
        tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="alice"
    )
    assert record.name == "code-reviewer"
    assert record.status is AgentSpecStatus.ACTIVE
    fetched = await store.get(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0")
    assert fetched is not None
    assert fetched.id == record.id


@pytest.mark.asyncio
async def test_duplicate_create_raises(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    with pytest.raises(DuplicateAgentSpecError):
        await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")


@pytest.mark.asyncio
async def test_tenant_isolation_on_get(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    assert await store.get(tenant_id=_TENANT_B, name="code-reviewer", version="1.0.0") is None


@pytest.mark.asyncio
async def test_list_filters(store: InMemoryAgentSpecStore) -> None:
    await store.create(
        tenant_id=_TENANT_A, spec=_spec(version="1.0.0"), spec_sha256=_sha(), created_by="a"
    )
    await store.create(
        tenant_id=_TENANT_A, spec=_spec(version="1.0.1"), spec_sha256=_sha(), created_by="a"
    )
    await store.create(
        tenant_id=_TENANT_A, spec=_spec(name="other"), spec_sha256=_sha(), created_by="a"
    )
    rows = await store.list_by_tenant(tenant_id=_TENANT_A, name="code-reviewer")
    assert len(rows) == 2
    # Newest first ordering.
    assert rows[0].version == "1.0.1"


@pytest.mark.asyncio
async def test_update_spec_round_trip(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    new_doc = deepcopy(_BASE_SPEC)
    new_doc["spec"]["system_prompt"]["template"] = "updated prompt"
    new_spec = AgentSpec.model_validate(new_doc)
    result = await store.update_spec(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        spec=new_spec,
        spec_sha256="b" * 64,
        updated_by="alice",
    )
    assert result is not None
    assert result.record.spec.spec.system_prompt.template == "updated prompt"
    assert result.record.spec_sha256 == "b" * 64
    # Stream HX-5 -- the update appended revision 2 with the actor.
    assert result.revision == 2
    assert result.prev_sha256 == _sha()


@pytest.mark.asyncio
async def test_update_spec_returns_none_when_missing(store: InMemoryAgentSpecStore) -> None:
    result = await store.update_spec(
        tenant_id=_TENANT_A,
        name="none",
        version="9.9.9",
        spec=_spec(),
        spec_sha256=_sha(),
        updated_by="a",
    )
    assert result is None


@pytest.mark.asyncio
async def test_soft_delete_hides_from_get(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    deleted = await store.update_status(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        status=AgentSpecStatus.DELETED,
    )
    assert deleted is not None and deleted.status is AgentSpecStatus.DELETED
    assert await store.get(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0") is None
    # Include-deleted opt-in returns the row.
    fetched = await store.get(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        include_deleted=True,
    )
    assert fetched is not None and fetched.status is AgentSpecStatus.DELETED


@pytest.mark.asyncio
async def test_update_status_unknown_returns_none(store: InMemoryAgentSpecStore) -> None:
    result = await store.update_status(
        tenant_id=_TENANT_A,
        name="missing",
        version="0.0.0",
        status=AgentSpecStatus.DEPRECATED,
    )
    assert result is None


@pytest.mark.asyncio
async def test_record_ids_are_unique() -> None:
    s = InMemoryAgentSpecStore()
    a = await s.create(
        tenant_id=_TENANT_A, spec=_spec(version="1.0.0"), spec_sha256=_sha(), created_by="x"
    )
    b = await s.create(
        tenant_id=_TENANT_A, spec=_spec(version="1.0.1"), spec_sha256=_sha(), created_by="x"
    )
    assert isinstance(a.id, UUID) and isinstance(b.id, UUID)
    assert a.id != b.id and a.id != uuid4()


# ---------------------------------------------------------------------------
# Stream HX-5 -- revision history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_writes_revision_one(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    history = await store.list_revisions(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0")
    assert [r.revision for r in history] == [1]
    assert history[0].actor_id == "a"
    assert history[0].spec_sha256 == _sha()


@pytest.mark.asyncio
async def test_updates_append_revisions_newest_first(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    for i, sha_char in enumerate("bc"):
        doc = deepcopy(_BASE_SPEC)
        doc["spec"]["system_prompt"]["template"] = f"prompt v{i}"
        await store.update_spec(
            tenant_id=_TENANT_A,
            name="code-reviewer",
            version="1.0.0",
            spec=AgentSpec.model_validate(doc),
            spec_sha256=sha_char * 64,
            updated_by="alice",
        )
    history = await store.list_revisions(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0")
    assert [r.revision for r in history] == [3, 2, 1]
    assert history[0].actor_id == "alice"
    one = await store.get_revision(
        tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0", revision=2
    )
    assert one is not None and one.spec_sha256 == "b" * 64
    assert (
        await store.get_revision(
            tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0", revision=9
        )
        is None
    )


@pytest.mark.asyncio
async def test_same_sha_update_is_noop_without_revision(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    result = await store.update_spec(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        spec=_spec(),
        spec_sha256=_sha(),
        updated_by="alice",
    )
    assert result is not None
    assert result.revision is None
    history = await store.list_revisions(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0")
    assert [r.revision for r in history] == [1]


@pytest.mark.asyncio
async def test_revisions_do_not_leak_cross_tenant(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    assert (
        await store.list_revisions(tenant_id=_TENANT_B, name="code-reviewer", version="1.0.0") == []
    )


# ---------------------------------------------------------------------------
# Phase 3 (3.1) C-1 — list_distinct_active_by_tenant / count_distinct_active_by_tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_dedupes_before_paginating(
    store: InMemoryAgentSpecStore,
) -> None:
    """C-1 的核心断言。一个 name 有多个 ACTIVE 版本行是常态(发新版本只
    create,没有代码把旧版本降级)。分页必须打在去重后的结果上 —— 如果先
    分页再去重,``limit=2 offset=0`` 会因为 alpha 占了两个槽位而只剩 1 个
    去重后的结果,客户端按标准的"不足一页即最后一页"循环会静默漏掉
    beta / gamma。"""
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="alpha", version="1.0.0"),
        spec_sha256=_sha(),
        created_by="a",
    )
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="alpha", version="1.0.1"),
        spec_sha256=_sha(),
        created_by="a",
    )
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="beta", version="1.0.0"),
        spec_sha256=_sha(),
        created_by="a",
    )
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="gamma", version="1.0.0"),
        spec_sha256=_sha(),
        created_by="a",
    )

    page0 = await store.list_distinct_active_by_tenant(tenant_id=_TENANT_A, limit=2, offset=0)
    assert [r.name for r in page0] == ["alpha", "beta"]

    page1 = await store.list_distinct_active_by_tenant(tenant_id=_TENANT_A, limit=2, offset=2)
    assert [r.name for r in page1] == ["gamma"]


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_keeps_newest_version_fields(
    store: InMemoryAgentSpecStore,
) -> None:
    """去重保留的必须是 ``created_at`` 最新的那一行 —— 与
    ``agents.py:_resolve_session`` 用 ``limit=1`` 取最新 ACTIVE 选的是同一
    行。用不同 ``display_name`` 标记两个版本,这样"取错版本"也会被逮到。"""
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="alpha", version="1.0.0", display_name="v1 name"),
        spec_sha256=_sha(),
        created_by="a",
    )
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="alpha", version="1.0.1", display_name="v2 name"),
        spec_sha256=_sha(),
        created_by="a",
    )

    rows = await store.list_distinct_active_by_tenant(tenant_id=_TENANT_A)

    assert len(rows) == 1
    assert rows[0].version == "1.0.1"
    assert rows[0].spec.spec.display_name == "v2 name"


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_excludes_non_active_and_other_tenants(
    store: InMemoryAgentSpecStore,
) -> None:
    await store.create(
        tenant_id=_TENANT_A, spec=_spec(name="alpha"), spec_sha256=_sha(), created_by="a"
    )
    await store.create(
        tenant_id=_TENANT_A, spec=_spec(name="beta"), spec_sha256=_sha(), created_by="a"
    )
    await store.update_status(
        tenant_id=_TENANT_A, name="beta", version="1.0.0", status=AgentSpecStatus.DEPRECATED
    )
    await store.create(
        tenant_id=_TENANT_B, spec=_spec(name="gamma"), spec_sha256=_sha(), created_by="a"
    )

    rows = await store.list_distinct_active_by_tenant(tenant_id=_TENANT_A)

    assert [r.name for r in rows] == ["alpha"]


@pytest.mark.asyncio
async def test_count_distinct_active_by_tenant(store: InMemoryAgentSpecStore) -> None:
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="alpha", version="1.0.0"),
        spec_sha256=_sha(),
        created_by="a",
    )
    await store.create(
        tenant_id=_TENANT_A,
        spec=_spec(name="alpha", version="1.0.1"),
        spec_sha256=_sha(),
        created_by="a",
    )
    await store.create(
        tenant_id=_TENANT_A, spec=_spec(name="beta"), spec_sha256=_sha(), created_by="a"
    )

    # 两个 name(alpha 两个版本合一),不是三行。
    assert await store.count_distinct_active_by_tenant(tenant_id=_TENANT_A) == 2


# ---------------------------------------------------------------------------
# 未发布草稿:把「改配置」和「让改动生效」分成两个动作
#
# 这批断言与 test_sql_agent_spec_store 里的同名用例逐条对应 —— 两个实现的
# 谓词必须同义,分裂过一次就再也没人查得出来是哪边错了。
# ---------------------------------------------------------------------------


def _drafted(template: str) -> AgentSpec:
    doc = deepcopy(_BASE_SPEC)
    doc["spec"]["system_prompt"]["template"] = template
    return AgentSpec.model_validate(doc)


@pytest.mark.asyncio
async def test_save_draft_leaves_the_live_spec_alone(store: InMemoryAgentSpecStore) -> None:
    """草稿的全部意义:存了不生效。"""
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    saved = await store.save_draft(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        spec=_drafted("drafted prompt"),
        spec_sha256="d" * 64,
        updated_by="bob",
    )
    assert saved is not None
    assert saved.draft is not None
    assert saved.draft.spec.spec.system_prompt.template == "drafted prompt"
    assert saved.draft.updated_by == "bob"
    # 线上那一版一个字节都没动。
    assert saved.spec.spec.system_prompt.template == "you are a reviewer"
    assert saved.spec_sha256 == _sha()
    # 也没记历史 —— 存草稿不是一次改动。
    assert (
        len(await store.list_revisions(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0"))
        == 1
    )


@pytest.mark.asyncio
async def test_publish_draft_promotes_and_clears(store: InMemoryAgentSpecStore) -> None:
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    await store.save_draft(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        spec=_drafted("drafted prompt"),
        spec_sha256="d" * 64,
        updated_by="bob",
    )
    result = await store.publish_draft(
        tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0", updated_by="bob"
    )
    assert result is not None
    assert result.record.spec.spec.system_prompt.template == "drafted prompt"
    assert result.record.spec_sha256 == "d" * 64
    assert result.prev_sha256 == _sha()
    assert result.revision == 2
    # 发布之后编辑缓冲区必须是空的,否则页面会一直显示「有未发布草稿」。
    assert result.record.draft is None


@pytest.mark.asyncio
async def test_publish_identical_draft_records_no_revision(
    store: InMemoryAgentSpecStore,
) -> None:
    """发布一份内容与线上完全相同的草稿不是一次改动,只是把缓冲区清掉。"""
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    await store.save_draft(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        spec=_spec(),
        spec_sha256=_sha(),
        updated_by="bob",
    )
    result = await store.publish_draft(
        tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0", updated_by="bob"
    )
    assert result is not None
    assert result.revision is None
    assert result.record.draft is None
    assert (
        len(await store.list_revisions(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0"))
        == 1
    )


@pytest.mark.asyncio
async def test_publish_without_a_draft_returns_none(store: InMemoryAgentSpecStore) -> None:
    """「发布什么?」是调用方的错,不是一次静默成功。"""
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    assert (
        await store.publish_draft(
            tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0", updated_by="bob"
        )
        is None
    )


@pytest.mark.asyncio
async def test_discard_draft_is_idempotent(store: InMemoryAgentSpecStore) -> None:
    """没有草稿时丢弃不是错误 —— 调用方要的状态(我这儿没有草稿)已经成立。"""
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    first = await store.discard_draft(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0")
    assert first is not None and first.draft is None
    await store.save_draft(
        tenant_id=_TENANT_A,
        name="code-reviewer",
        version="1.0.0",
        spec=_drafted("x"),
        spec_sha256="d" * 64,
        updated_by="bob",
    )
    second = await store.discard_draft(tenant_id=_TENANT_A, name="code-reviewer", version="1.0.0")
    assert second is not None and second.draft is None


@pytest.mark.asyncio
async def test_draft_methods_are_tenant_scoped(store: InMemoryAgentSpecStore) -> None:
    """跨租户探测返回 None,不泄露这一行存不存在(与其余方法同一契约)。"""
    await store.create(tenant_id=_TENANT_A, spec=_spec(), spec_sha256=_sha(), created_by="a")
    assert (
        await store.save_draft(
            tenant_id=_TENANT_B,
            name="code-reviewer",
            version="1.0.0",
            spec=_drafted("x"),
            spec_sha256="d" * 64,
            updated_by="mallory",
        )
        is None
    )
    assert (
        await store.discard_draft(tenant_id=_TENANT_B, name="code-reviewer", version="1.0.0")
        is None
    )
    assert (
        await store.publish_draft(
            tenant_id=_TENANT_B, name="code-reviewer", version="1.0.0", updated_by="mallory"
        )
        is None
    )
