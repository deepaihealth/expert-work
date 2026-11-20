"""B-326 —— 读产物清单时的分享链接(``/items``、run 列表、对外事件续传、排队收尾)。

链接从 run 行里记着的「标记的是哪一版」(``_share``)续,不靠某条链接行活着:

* 链接还能用 → 原样;过期 / 快过期 / 次数用完 → 为同一版本、同一对话再铸一条;公开下载
  从不删行,点过一次过期链接之后,再读清单照样拿到能用的新链接(H1);
* 按智能体**当前**配置:关了分享链接 → 字段去掉、不铸;有效期调短 → 新链接用新有效期(H2);
* 标记的那一版不在了 / 不是这个智能体的 / 不是这段对话登记的 → 字段去掉、不铸(M3);
* ``_share`` 永不出去;没标记的项原样;一页一次查询(M6);
* 对外事件续传的 ``end``、排队收尾的 ``end`` 同样续(M5)。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.api._share_links import RunArtifacts, refresh_share_links_for_runs, share_view
from control_plane.thread_queue import queued_turn_stream
from expert_work.persistence.artifact_share import InMemoryArtifactShareLinkStore
from expert_work.persistence.rls import bypass_rls_session
from expert_work.protocol import AgentSpec, ArtifactVersion
from expert_work.protocol.agent_key import sanitize_agent_key
from expert_work.runtime.runs import RunStatus
from orchestrator.tools import RecordingWorkspaceStore
from orchestrator.tools.artifact_snapshots import InMemoryArtifactSnapshotStore
from orchestrator.tools.share_file import ShareLinkService
from tests.test_external_session_items import _SPEC, _Ctx
from tests.test_external_session_items import ctx as ctx  # the fixture

_BASE = "https://files.example.test"
_AGENT = "support-bot"
_KEY = sanitize_agent_key(_AGENT)
_PDF = b"%PDF-1.7 fake"
_UNMARKED = {"name": "notes.md", "kind": "document", "version": 1, "created_at": "t"}


def _doc(block: dict[str, Any] | None) -> AgentSpec:
    doc = deepcopy(_SPEC)
    if block is not None:
        doc["spec"]["share_links"] = block
    return AgentSpec.model_validate(doc)


async def _seed(ctx: _Ctx, block: dict[str, Any] | None) -> None:
    await ctx.app.state.agent_spec_repo.create(
        tenant_id=ctx.tenant_id, spec=_doc(block), spec_sha256="a" * 64, created_by="seed"
    )


async def _set_policy(ctx: _Ctx, block: dict[str, Any] | None) -> None:
    updated = await ctx.app.state.agent_spec_repo.update_spec(
        tenant_id=ctx.tenant_id,
        name=_AGENT,
        version="1.0.0",
        spec=_doc(block),
        spec_sha256=uuid4().hex * 2,
        updated_by="test",
    )
    assert updated is not None


@dataclass
class _Marked:
    service: ShareLinkService
    session_id: UUID
    run_id: UUID
    user_id: UUID
    version: ArtifactVersion
    link_id: str
    stored: dict[str, Any]

    @property
    def links(self) -> InMemoryArtifactShareLinkStore:
        store = self.service.links
        assert isinstance(store, InMemoryArtifactShareLinkStore)
        return store


def _service(ctx: _Ctx) -> ShareLinkService:
    existing = getattr(ctx.app.state, "share_link_service", None)
    if isinstance(existing, ShareLinkService):
        return existing
    workspace = RecordingWorkspaceStore()
    ctx.app.state.workspace_store = workspace
    ctx.app.state.artifact_snapshots = InMemoryArtifactSnapshotStore(workspace=workspace)
    service = ShareLinkService(
        links=ctx.app.state.artifact_share_link_store,
        artifacts=ctx.app.state.artifact_store,
        public_base_url=_BASE,
    )
    ctx.app.state.share_link_service = service
    return service


async def _save(
    ctx: _Ctx, service: ShareLinkService, user_id: UUID, thread: UUID, name: str = "report.pdf"
) -> ArtifactVersion:
    path = f"agents/{_KEY}/threads/{thread}/work/{name}"
    workspace = ctx.app.state.workspace_store
    workspace.workspace_file_contents[path] = _PDF
    version_id = uuid4()
    digest = await ctx.app.state.artifact_snapshots.capture(
        tenant_id=ctx.tenant_id, user_id=user_id, path=path, snapshot_key=str(version_id)
    )
    return await service.artifacts.save_version(
        tenant_id=ctx.tenant_id,
        user_id=user_id,
        agent_key=_KEY,
        name=name,
        kind="document",
        path_in_workspace=path,
        created_in_thread=str(thread),
        version_id=version_id,
        snapshot_key=str(version_id),
        size_bytes=digest.size_bytes,
        sha256=digest.sha256,
    )


async def _set_artifacts(
    ctx: _Ctx, run_id: UUID, artifacts: list[dict[str, Any]], status: RunStatus | None = None
) -> None:
    """Store ``artifacts`` on the run; ``status`` = None keeps the row's status."""
    row = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert row is not None
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=status or row.status,
        updated_at=row.created_at,
        finished_at=row.created_at + timedelta(seconds=5),
        artifacts=artifacts,
    )


async def _mark(
    ctx: _Ctx,
    *,
    minutes_left: float = 60,
    ttl_minutes: int = 120,
    session: tuple[UUID, UUID] | None = None,
    status: RunStatus | None = RunStatus.SUCCESS,
) -> _Marked:
    """A finished run whose list has one marked entry (minted ``minutes_left`` before it
    runs out, as ``share_file`` records it) and one unmarked entry."""
    session_id, run_id = session or await ctx.open_session()
    meta = await ctx.app.state.thread_meta_repo.get(session_id, tenant_id=ctx.tenant_id)
    service = _service(ctx)
    version = await _save(ctx, service, meta.user_id, session_id)
    ttl = timedelta(minutes=ttl_minutes)
    link = await service.links.mint_or_reuse(
        tenant_id=ctx.tenant_id,
        user_id=meta.user_id,
        agent_key=_KEY,
        artifact_id=version.artifact_id,
        artifact_version_id=version.id,
        version=version.version,
        thread_id=session_id,
        run_id=run_id,
        file_name="report.pdf",
        ttl=ttl,
        now=datetime.now(UTC) - ttl + timedelta(minutes=minutes_left),
        max_downloads=20,
    )
    stored = {
        "name": "report.pdf",
        "kind": "document",
        "version": version.version,
        "created_at": "t",
        "share_url": service.url_for(link.id, link.file_name),
        "share_expires_at": link.expires_at.isoformat(),
        "_share": {
            "artifact_id": str(version.artifact_id),
            "version_id": str(version.id),
            "agent_key": _KEY,
        },
    }
    await _set_artifacts(ctx, run_id, [stored, dict(_UNMARKED)], status=status)
    return _Marked(service, session_id, run_id, meta.user_id, version, link.id, stored)


def _outward(stored: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stored.items() if k != "_share"}


def _plain(stored: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stored.items() if k not in ("_share", "share_url", "share_expires_at")}


async def _items(ctx: _Ctx, session_id: UUID) -> list[dict[str, Any]]:
    resp = await ctx.items(session_id)
    assert resp.status_code == 200, resp.text
    [run] = resp.json()["data"]["runs"]
    return run["artifacts"]  # type: ignore[no-any-return]


def _link_id(entry: dict[str, Any]) -> str:
    return str(entry["share_url"]).split("/")[-2]


def _path(entry: dict[str, Any]) -> str:
    return str(entry["share_url"]).removeprefix(_BASE)


_ON = {"enabled": True, "ttl_minutes": 120}


# ------------------------------------------------------------------ /items and runs


@pytest.mark.asyncio
async def test_a_live_link_is_kept_and_nothing_internal_goes_out(ctx: _Ctx) -> None:
    await _seed(ctx, _ON)
    m = await _mark(ctx)
    first = await _items(ctx, m.session_id)
    assert first == [_outward(m.stored), _UNMARKED]
    assert await _items(ctx, m.session_id) == first
    assert len(m.links._rows) == 1
    # The run list (filtered by agent) shows the same.
    ctx.run_store._thread_meta_store = ctx.app.state.thread_meta_repo
    resp = await ctx.client.get(
        f"/v1/agents/{_AGENT}/runs",
        params={"user_id": "u-123", "session_id": str(m.session_id)},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    [run] = resp.json()["data"]["runs"]
    assert run["artifacts"] == first


@pytest.mark.asyncio
async def test_an_expired_link_clicked_then_items_read_gives_a_working_link(ctx: _Ctx) -> None:
    """H1 —— 终端用户先点了过期的旧链接(404,行不删),对接方再读清单拿到能用的新链接。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    old = await ctx.client.get(_path(m.stored))
    assert old.status_code == 404
    async with bypass_rls_session():
        assert await m.links.get_public(link_id=m.link_id) is not None  # not deleted
    [fresh, plain] = await _items(ctx, m.session_id)
    assert plain == _UNMARKED
    assert _link_id(fresh) != m.link_id
    assert fresh["share_url"].endswith("/report.pdf")
    downloaded = await ctx.client.get(_path(fresh))
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == _PDF
    # Read again: the fresh link is reused.
    assert (await _items(ctx, m.session_id))[0] == fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [None, {"enabled": False}])
async def test_turning_share_links_off_stops_new_links(
    ctx: _Ctx, block: dict[str, Any] | None
) -> None:
    """H2 —— 关掉(或删掉)分享链接之后,读清单不出链接、也不铸新的 —— 包括还没过期的。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    await _set_policy(ctx, block)
    assert await _items(ctx, m.session_id) == [_plain(m.stored), _UNMARKED]
    assert len(m.links._rows) == 1


@pytest.mark.asyncio
async def test_a_lowered_lifetime_applies_on_the_next_read(ctx: _Ctx) -> None:
    """H2 —— 有效期从 120 调到 30 分钟:下一次读就给按 30 分钟铸的新链接。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=100)
    await _set_policy(ctx, {"enabled": True, "ttl_minutes": 30})
    before = datetime.now(UTC)
    [fresh, _] = await _items(ctx, m.session_id)
    assert _link_id(fresh) != m.link_id
    link = m.links._rows[_link_id(fresh)]
    assert link.expires_at - link.created_at == timedelta(minutes=30)
    assert link.created_at >= before


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["used_up", "about_to_expire"])
async def test_a_link_that_cannot_serve_is_replaced(ctx: _Ctx, why: str) -> None:
    """M2 —— 次数用完、或只剩不到 5 分钟的链接不再发出去。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=3 if why == "about_to_expire" else 60)
    if why == "used_up":
        m.links.put(replace(m.links._rows[m.link_id], download_count=20))
    [fresh, _] = await _items(ctx, m.session_id)
    assert _link_id(fresh) != m.link_id


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["soft_deleted", "version_pruned", "other_agent", "no_record"])
async def test_a_mark_whose_version_is_gone_loses_its_link(ctx: _Ctx, why: str) -> None:
    """M3 —— 标记的那一版不在了 / 不是这个智能体的 / 没有内部记录:字段去掉,不铸新链接。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    artifacts = m.service.artifacts
    stored = dict(m.stored)
    if why == "soft_deleted":
        await artifacts.soft_delete(
            tenant_id=ctx.tenant_id,
            user_id=m.user_id,
            agent_key=_KEY,
            name="report.pdf",
            now=datetime.now(UTC),
        )
    elif why == "version_pruned":
        await artifacts.delete_versions(version_ids=[m.version.id])
    elif why == "other_agent":
        stored["_share"] = {**stored["_share"], "agent_key": "someone-else"}
    else:
        del stored["_share"]
    await _set_artifacts(ctx, m.run_id, [stored, dict(_UNMARKED)])
    assert await _items(ctx, m.session_id) == [_plain(m.stored), _UNMARKED]
    assert len(m.links._rows) == 1


@pytest.mark.asyncio
async def test_another_conversation_never_gets_a_link_to_this_ones_file(ctx: _Ctx) -> None:
    """同一个平台用户的另一段对话:即使它的清单里抄着这一项(同一版本),也不给链接、不铸。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx)
    session_b, run_b = await ctx.open_session()
    await _set_artifacts(ctx, run_b, [dict(m.stored)])
    [entry] = await _items(ctx, session_b)
    assert entry == _plain(m.stored)
    assert m.link_id not in json.dumps(entry)
    assert len(m.links._rows) == 1


@pytest.mark.asyncio
async def test_a_page_is_read_in_one_query_per_store(ctx: _Ctx) -> None:
    """M6 —— 三轮各标记一个文件:版本一次查询、链接一次查询;都还能用就一次写都没有。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx)
    for _ in range(2):
        run = await ctx.client.post(
            f"/v1/agents/{_AGENT}/runs",
            json={
                "user_id": "u-123",
                "session_id": str(m.session_id),
                "input": "again",
                "mode": "queue",
            },
            headers=ctx.headers,
        )
        assert run.status_code == 202, run.text
        await _mark(ctx, session=(m.session_id, UUID(run.json()["data"]["run_id"])))
    calls: dict[str, int] = {"newest_for": 0, "get_live_versions": 0, "mint_or_reuse": 0}

    def count(obj: Any, name: str) -> None:
        inner: Callable[..., Any] = getattr(obj, name)

        async def wrapped(**kwargs: Any) -> Any:
            calls[name] += 1
            return await inner(**kwargs)

        setattr(obj, name, wrapped)

    count(m.service.links, "newest_for")
    count(m.service.links, "mint_or_reuse")
    count(m.service.artifacts, "get_live_versions")
    resp = await ctx.items(m.session_id)
    assert resp.status_code == 200, resp.text
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 3
    assert all("share_url" in r["artifacts"][0] for r in runs)
    assert calls == {"newest_for": 1, "get_live_versions": 1, "mint_or_reuse": 0}


@pytest.mark.asyncio
async def test_without_a_user_the_fields_are_stripped() -> None:
    """A session without a resolved user: no links, even for an opted-in agent."""

    async def active(**_kw: Any) -> list[Any]:
        return [SimpleNamespace(spec=_doc(_ON))]

    state = SimpleNamespace(
        share_link_service=object(), agent_spec_repo=SimpleNamespace(list_by_tenant=active)
    )
    request: Any = SimpleNamespace(app=SimpleNamespace(state=state))
    stored = {"name": "a.pdf", "version": 1, "share_url": "u", "share_expires_at": "e"}
    [out] = await refresh_share_links_for_runs(
        request,
        [RunArtifacts(run_id=None, thread_id=uuid4(), artifacts=[stored])],
        tenant_id=uuid4(),
        user_id=None,
        agent_name=_AGENT,
    )
    assert out == [{"name": "a.pdf", "version": 1}]


# ------------------------------------------------------------------ event paths (M5)


def _end(body: str) -> dict[str, Any]:
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if "event: end" in lines:
            data = next(line for line in lines if line.startswith("data: "))
            parsed: dict[str, Any] = json.loads(data[6:])
            return parsed
    raise AssertionError(f"no end frame in {body!r}")


@pytest.mark.asyncio
async def test_event_reconnect_end_carries_a_working_link(ctx: _Ctx) -> None:
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    resp = await ctx.client.get(
        f"/v1/agents/{_AGENT}/runs/{m.run_id}/events",
        params={"user_id": "u-123"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    [fresh, plain] = _end(resp.text)["artifacts"]
    assert plain == _UNMARKED
    assert "_share" not in fresh
    assert _link_id(fresh) != m.link_id
    assert (await ctx.client.get(_path(fresh))).status_code == 200


@pytest.mark.asyncio
async def test_idempotent_stream_replay_end_carries_a_working_link(ctx: _Ctx) -> None:
    await _seed(ctx, _ON)
    headers = {**ctx.headers, "Idempotency-Key": "b326-replay-1"}
    body = {"user_id": "u-123", "input": "hi"}
    first = await ctx.client.post(f"/v1/agents/{_AGENT}/runs", json=body, headers=headers)
    assert first.status_code == 200, first.text
    run_id = UUID(first.headers["X-Expert-Work-Run-Id"])
    run = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.status in (RunStatus.SUCCESS, RunStatus.ERROR)
    m = await _mark(ctx, minutes_left=-1, session=(run.thread_id, run_id))
    second = await ctx.client.post(f"/v1/agents/{_AGENT}/runs", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert UUID(second.headers["X-Expert-Work-Run-Id"]) == run_id
    [fresh, _] = _end(second.text)["artifacts"]
    assert "_share" not in fresh
    assert _link_id(fresh) != m.link_id


@pytest.mark.asyncio
async def test_a_failed_refresh_logs_only_the_error_type(
    ctx: _Ctx, caplog: pytest.LogCaptureFixture
) -> None:
    """查库出错:不报错,不出链接(存着的可能已过期),日志里只有错误类型。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx)

    async def boom(**_kw: Any) -> Any:
        raise RuntimeError("postgresql://user:secret@db/x")

    m.service.links.newest_for = boom  # type: ignore[method-assign]
    # Fail closed (round 5 L1): the stored link may have expired — no link at all.
    assert await _items(ctx, m.session_id) == [_plain(m.stored), _UNMARKED]
    assert "secret" not in caplog.text
    assert "refresh_failed error=RuntimeError" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "version"])
async def test_an_entry_that_no_longer_matches_its_version_loses_its_link(
    ctx: _Ctx, field: str
) -> None:
    await _seed(ctx, _ON)
    m = await _mark(ctx)
    stored = {**m.stored, field: "other.pdf" if field == "name" else 99}
    await _set_artifacts(ctx, m.run_id, [stored, dict(_UNMARKED)])
    [entry, _] = await _items(ctx, m.session_id)
    assert "share_url" not in entry


@pytest.mark.asyncio
async def test_a_mismatched_entry_sharing_a_valid_entrys_version_gets_no_link(
    ctx: _Ctx,
) -> None:
    """Round-6 L-6 —— 同一 run 里两项指向同一个 ``_share.version_id``,一项对得上、一项名字
    变了:只有对得上的那项拿到链接(到 view 这一步还要再核一次,不能借另一项的链接)。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx)
    tampered = {**m.stored, "name": "other.pdf"}
    await _set_artifacts(ctx, m.run_id, [m.stored, tampered, dict(_UNMARKED)])
    [valid, wrong, _] = await _items(ctx, m.session_id)
    assert "share_url" in valid
    assert "share_url" not in wrong
    assert "_share" not in wrong


@pytest.mark.asyncio
async def test_queue_exit_end_carries_a_working_link(ctx: _Ctx) -> None:
    """排队的这一轮被别的副本跑完了:收尾 end 帧上的链接同样按当前配置续。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    request: Any = SimpleNamespace(app=ctx.app)

    async def never() -> bool:
        return False

    async def no_launch(_: Any) -> AsyncIterator[bytes]:
        raise AssertionError("must not launch")

    frames = [
        f
        async for f in queued_turn_stream(
            store=ctx.run_store,
            run_id=m.run_id,
            thread_id=m.session_id,
            tenant_id=ctx.tenant_id,
            owner="other-replica",
            lease_ttl_s=30,
            ahead=0,
            launch=no_launch,
            is_disconnected=never,
            cancel_on_disconnect=False,
            artifact_view=share_view(
                request,
                tenant_id=ctx.tenant_id,
                user_id=m.user_id,
                thread_id=m.session_id,
                agent_name=_AGENT,
                run_id=m.run_id,
            ),
            sleep=lambda _s: _noop(),
        )
    ]
    [fresh, _] = _end(b"".join(frames).decode())["artifacts"]
    assert "_share" not in fresh
    assert _link_id(fresh) != m.link_id


async def _noop() -> None:
    return None


# ------------------------------------------------------------------ live paths (round 5 M1, M3)


@pytest.mark.asyncio
async def test_a_run_outliving_its_link_ends_with_a_valid_one(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直播 ``end``(``spawn_run`` → ``sse_consumer``)同样按当前配置给链接:``share_file``
    之后又跑了比有效期还久的 run,收尾不发一条已过期的链接。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    bridge = ctx.app.state.agent_runtime.stream_bridge
    publish_end = bridge.publish_end

    async def with_the_mark(run_id: UUID, **kwargs: Any) -> None:
        kwargs["artifacts"] = [*(kwargs.get("artifacts") or []), dict(m.stored)]
        await publish_end(run_id, **kwargs)

    monkeypatch.setattr(bridge, "publish_end", with_the_mark)
    resp = await ctx.client.post(
        f"/v1/agents/{_AGENT}/runs",
        json={"user_id": "u-123", "session_id": str(m.session_id), "input": "more"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    [fresh] = [e for e in _end(resp.text)["artifacts"] if e["name"] == "report.pdf"]
    assert "_share" not in fresh
    assert _link_id(fresh) != m.link_id
    assert (await ctx.client.get(_path(fresh))).status_code == 200


@pytest.mark.asyncio
async def test_a_queued_turn_finished_elsewhere_ends_with_a_valid_link(ctx: _Ctx) -> None:
    """M3 —— ``spawn_run`` 的排队分支:这一轮排在后面、被别的副本跑完,收尾 ``end`` 的链接
    同样按当前配置给(HTTP 层,走真实的装配)。"""
    import asyncio

    await _seed(ctx, _ON)
    session_id, first = await ctx.open_session(finish=False)  # stays queued: the session is busy
    m = await _mark(ctx, minutes_left=-1, session=(session_id, first), status=None)

    async def finish_the_queued_turn_elsewhere() -> None:
        while True:
            rows = await ctx.run_store.list_for_tenant(
                tenant_id=ctx.tenant_id, thread_ids=[session_id], limit=10
            )
            queued = [r for r in rows if r.run_id != first and r.status is RunStatus.QUEUED]
            if queued:
                await _set_artifacts(
                    ctx, queued[0].run_id, [dict(m.stored)], status=RunStatus.SUCCESS
                )
                return
            await asyncio.sleep(0.01)

    finisher = asyncio.create_task(finish_the_queued_turn_elsewhere())
    resp = await ctx.client.post(
        f"/v1/agents/{_AGENT}/runs",
        json={"user_id": "u-123", "session_id": str(session_id), "input": "next"},
        headers=ctx.headers,
    )
    await asyncio.gather(finisher)
    assert resp.status_code == 200, resp.text
    assert "event: queued" in resp.text
    [fresh] = _end(resp.text)["artifacts"]
    assert _link_id(fresh) != m.link_id
    assert (await ctx.client.get(_path(fresh))).status_code == 200


@pytest.mark.asyncio
async def test_a_disabled_agent_reports_no_links(ctx: _Ctx) -> None:
    """Round 5 L4 —— 停用的智能体(kill switch)和关掉分享链接一样:不出链接、不铸。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    await ctx.app.state.agent_disable_repo.set_disabled(
        tenant_id=ctx.tenant_id, agent_name=_AGENT, disabled=True, reason="r", disabled_by="u"
    )
    ctx.app.state.agent_disable_service.invalidate(ctx.tenant_id, _AGENT)
    assert await _items(ctx, m.session_id) == [_plain(m.stored), _UNMARKED]
    assert len(m.links._rows) == 1


@pytest.mark.asyncio
async def test_a_mark_pointing_at_another_artifact_loses_its_link(ctx: _Ctx) -> None:
    """Round 5 M3 —— ``_share.artifact_id`` 指向同一用户的另一个活着的产物(版本 id 对得上的
    不是它):不出链接、不铸。"""
    await _seed(ctx, _ON)
    m = await _mark(ctx, minutes_left=-1)
    other = await _save(ctx, m.service, m.user_id, m.session_id, name="other.pdf")
    stored = {**m.stored, "_share": {**m.stored["_share"], "artifact_id": str(other.artifact_id)}}
    await _set_artifacts(ctx, m.run_id, [stored, dict(_UNMARKED)])
    assert await _items(ctx, m.session_id) == [_plain(m.stored), _UNMARKED]
    assert len(m.links._rows) == 1


@pytest.mark.asyncio
async def test_a_conversation_without_an_agent_gets_no_links(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def active(**_kw: Any) -> list[Any]:
        return [SimpleNamespace(spec=_doc(_ON))]

    state = SimpleNamespace(
        share_link_service=object(), agent_spec_repo=SimpleNamespace(list_by_tenant=active)
    )
    request: Any = SimpleNamespace(app=SimpleNamespace(state=state))
    stored = {"name": "a.pdf", "version": 1, "share_url": "u", "share_expires_at": "e"}
    [out] = await refresh_share_links_for_runs(
        request,
        [RunArtifacts(run_id=None, thread_id=uuid4(), artifacts=[stored])],
        tenant_id=uuid4(),
        user_id=uuid4(),
        agent_name=None,
    )
    assert out == [{"name": "a.pdf", "version": 1}]
    assert "refresh_failed" not in caplog.text  # stripped by rule, not by an error
