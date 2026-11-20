"""B-326 round 5 L3 —— 清除用户、彻底删除智能体时一并删产物分享链接(删了立刻失效)。

会话清除已经删(``test_session_purge_children*``)。平台没有「清除租户」的流程:停用的租户
的链接一律 404(``test_public_files``)。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.purge.agent_purge import purge_agent
from control_plane.purge.user_purge import purge_user
from control_plane.settings import Settings
from expert_work.persistence.artifact_share import InMemoryArtifactShareLinkStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol.agent_key import sanitize_agent_key
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier
from tests.test_agent_purge import _X, _Y, _world
from tests.test_user_purge import _LogWorld


async def _mint(store: InMemoryArtifactShareLinkStore, tenant: UUID, user: UUID, agent: str) -> str:
    link = await store.mint_or_reuse(
        tenant_id=tenant,
        user_id=user,
        agent_key=agent,
        artifact_id=uuid4(),
        artifact_version_id=uuid4(),
        version=1,
        thread_id=uuid4(),
        run_id=None,
        file_name="r.pdf",
        ttl=timedelta(hours=1),
        now=datetime.now(UTC),
        max_downloads=20,
    )
    return link.id


@pytest.mark.asyncio
async def test_purging_a_user_deletes_their_links_only() -> None:
    world = _LogWorld()
    a = await world.seed()
    links = InMemoryArtifactShareLinkStore()
    mine = await _mint(links, world.t1, a, "k")
    theirs = await _mint(links, world.t1, uuid4(), "k")
    deps = replace(world.deps(), share_links=links)

    summary = await purge_user(
        tenant_id=world.t1, user_id=a, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    assert summary.deleted["artifact_share_links"] == 1
    assert set(links._rows) == {theirs}
    assert mine not in links._rows


@pytest.mark.asyncio
async def test_purging_an_agent_deletes_its_links_only() -> None:
    world, _ = await _world()
    links = InMemoryArtifactShareLinkStore()
    doomed = await _mint(links, world.t1, uuid4(), sanitize_agent_key(_X))
    sibling = await _mint(links, world.t1, uuid4(), sanitize_agent_key(_Y))
    other_tenant = await _mint(links, world.t2, uuid4(), sanitize_agent_key(_X))
    deps = replace(world.deps(), share_links=links)

    summary = await purge_agent(deps, tenant_id=world.t1, name=_X)

    assert summary.deleted["artifact_share_links"] == 1
    assert set(links._rows) == {sibling, other_tenant}
    assert doomed not in links._rows


def _app() -> object:
    return create_app(
        settings=Settings(
            env="dev", auth_mode="dev", oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE]
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )


def test_both_purge_endpoints_wire_the_link_store() -> None:
    from control_plane.api.agent_lifecycle import build_agent_purge_deps
    from control_plane.api.agent_users import _build_purge_deps

    app = _app()
    request = SimpleNamespace(app=app)
    store = app.state.artifact_share_link_store  # type: ignore[attr-defined]
    assert _build_purge_deps(request).share_links is store  # type: ignore[arg-type]
    assert build_agent_purge_deps(request).share_links is store  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_purging_an_agent_keeps_links_of_users_whose_files_were_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-6 L-5 —— 某个用户的 NAS 目录没删掉(``keep_users``):他的产物这次留着,
    分享链接也留着;别的用户照删。"""
    import control_plane.purge.agent_purge as agent_purge_mod

    world, _ = await _world()
    links = InMemoryArtifactShareLinkStore()
    kept_user, gone_user = uuid4(), uuid4()
    kept = await _mint(links, world.t1, kept_user, sanitize_agent_key(_X))
    gone = await _mint(links, world.t1, gone_user, sanitize_agent_key(_X))
    deps = replace(world.deps(), share_links=links)

    async def _nas_failed_for(*_args: object, **_kwargs: object) -> set[UUID]:
        return {kept_user}

    monkeypatch.setattr(agent_purge_mod, "purge_nas", _nas_failed_for)
    summary = await purge_agent(deps, tenant_id=world.t1, name=_X)

    assert summary.deleted["artifact_share_links"] == 1
    assert kept in links._rows
    assert gone not in links._rows
