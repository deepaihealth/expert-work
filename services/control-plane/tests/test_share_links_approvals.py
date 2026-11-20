"""B-326 round 5 M1 —— 审批续跑的直播 ``end``:暂停前的标记接着记,链接按当前配置重给。

续跑接的是暂停前 run 的产物清单(``seed_artifacts``)。暂停前发出的链接不原样再发:第二天才批
的审批拿到的是新链接;暂停期间关掉了分享链接,就不带链接。
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from tests import test_external_approvals
from tests.test_external_approvals import _Ctx, _seed_pending_decision
from tests.test_external_approvals import ctx as ctx  # the fixture
from tests.test_share_links_read_paths import _ON, _PDF, _doc, _end, _link_id, _mark, _path


async def _paused_with_share_links(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> tuple[UUID, UUID]:
    """A pending approval on an agent that has share links on from the start: the
    harness builds its graph from this module's manifest, and changing the manifest
    later would rebuild the stub graph and lose its checkpoint."""
    monkeypatch.setattr(test_external_approvals, "_spec", lambda: _doc(_ON))
    run_id, thread_id, _ = await _seed_pending_decision(ctx)
    return run_id, thread_id


async def _decide(ctx: _Ctx, run_id: UUID) -> list[dict[str, Any]]:
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "approve"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    artifacts: list[dict[str, Any]] = _end(resp.text)["artifacts"]
    return artifacts


@pytest.mark.asyncio
async def test_a_next_day_approval_reports_a_fresh_link(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, thread_id = await _paused_with_share_links(ctx, monkeypatch)
    m = await _mark(cast(Any, ctx), minutes_left=-60, session=(thread_id, run_id), status=None)
    [fresh] = [e for e in await _decide(ctx, run_id) if e["name"] == "report.pdf"]
    assert "_share" not in fresh
    assert _link_id(fresh) != m.link_id
    downloaded = await ctx.client.get(_path(fresh))
    assert downloaded.status_code == 200
    assert downloaded.content == _PDF


@pytest.mark.asyncio
async def test_turned_off_while_paused_reports_no_link(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, thread_id = await _paused_with_share_links(ctx, monkeypatch)
    m = await _mark(cast(Any, ctx), minutes_left=60, session=(thread_id, run_id), status=None)
    # The agent's active version now has share links off (the paused run's own version
    # is what the continuation builds from, so only the policy lookup sees the change).
    repo = ctx.app.state.agent_spec_repo
    list_by_tenant = repo.list_by_tenant

    async def turned_off(**kwargs: Any) -> Any:
        rows = await list_by_tenant(**kwargs)
        return [r.model_copy(update={"spec": _doc({"enabled": False})}) for r in rows]

    monkeypatch.setattr(repo, "list_by_tenant", turned_off)
    [entry] = [e for e in await _decide(ctx, run_id) if e["name"] == "report.pdf"]
    assert "share_url" not in entry
    assert "share_expires_at" not in entry
    assert m.link_id not in str(entry)


@pytest.mark.asyncio
async def test_a_failed_thread_lookup_on_resume_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round-6 L-4 —— 审批续跑时读会话记录出错(裁决已经落库):不让请求 500,
    end 帧的分享字段按「去掉」处理,日志只记错误类型。"""
    from types import SimpleNamespace
    from uuid import uuid4

    from control_plane.api._share_links import strip_view, thread_share_view

    class _BrokenRepo:
        async def get(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("db down secret-ish detail")

    request = cast(
        Any,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(thread_meta_repo=_BrokenRepo()))),
    )
    view = await thread_share_view(request, tenant_id=uuid4(), thread_id=uuid4())

    assert view is strip_view
    marked = {"name": "r.pdf", "share_url": "u", "share_expires_at": "e", "_share": {}}
    assert await view([marked]) == [{"name": "r.pdf"}]
    assert "thread_lookup_failed error=RuntimeError" in caplog.text
    assert "secret-ish" not in caplog.text
