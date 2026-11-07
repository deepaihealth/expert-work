"""B-293 review —— 撤掉「本请求建的新会话」时不能误伤别的请求。

新会话在建好到被撤之间是看得见的(``GET .../sessions`` 不滤空会话),别的请求可以拿它的 id
起 run / 传文件。撤之前在会话的接单锁里确认:没有 run 行、没有别人的登记行;起 run 一侧
(``spawn_run``)在同一把锁里重读会话行,会话已经被撤就回 404 ``SESSION_NOT_FOUND``。

另外几条:撤的顺序(先会话行、再登记行、最后目录,目录删除有时限)、撤掉落下的文件后让
工作区用量重算、建会话之后的意外异常也撤(异常原样抛出)。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from control_plane.api import _new_session_undo as undo_mod
from control_plane.api import agents as agents_mod
from control_plane.api import external_uploads as uploads_mod
from control_plane.api._external_agent_scope import agent_key_for_code
from tests.new_session_fixtures import AGENT, PNG, Ctx, DenyQuota, new_session_ctx, unknown_file


@pytest.fixture
async def ctx() -> AsyncIterator[Ctx]:
    async with new_session_ctx() as c:
        yield c


async def _only_session(ctx: Ctx) -> UUID:
    listed = await ctx.client.get(f"/v1/agents/{AGENT}/sessions", params={"user_id": "u1"})
    assert listed.status_code == 200, listed.text
    sessions = listed.json()["data"]["sessions"]
    assert len(sessions) == 1, sessions
    return UUID(sessions[0]["session_id"])


async def _upload_doc(ctx: Ctx, raw: bytes, session_id: UUID | None = None) -> dict[str, Any]:
    resp = await ctx.upload(
        name="report.pdf", raw=raw, mime="application/pdf", session_id=session_id
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


def _during_admission(monkeypatch: pytest.MonkeyPatch, module: Any, action: Any) -> dict[str, Any]:
    """``module.check_admission`` 第一次被调用时(也就是请求 A 已经建好新会话、还没被拒)先跑
    ``action``(请求 B),再照常做配额判定。回一个字典,``action`` 的结果放在里面。"""
    real = module.check_admission
    seen: dict[str, Any] = {}

    async def _admission(**kwargs: Any) -> Any:
        if not seen:
            seen["started"] = True
            seen["result"] = await action()
        return await real(**kwargs)

    monkeypatch.setattr(module, "check_admission", _admission)
    return seen


# ---------------------------------------------------------------------------
# 别的请求已经在用这条新会话:不撤。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_started_on_the_new_session_keeps_it(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评审复现:A 不带会话起 run、附件不存在;A 在配额判定时,B 列出会话拿到 S、在 S 上排队
    一个 run(202)。A 随后 404 —— S 与 B 的 run 都得留着。"""

    async def _b() -> tuple[UUID, Any]:
        borrowed = await _only_session(ctx)
        return borrowed, await ctx.run(user_id="u1", session_id=str(borrowed))

    seen = _during_admission(monkeypatch, agents_mod, _b)

    resp = await ctx.run(user_id="u1", files=[unknown_file()])

    assert resp.status_code == 404, resp.text
    borrowed, b_resp = seen["result"]
    assert b_resp.status_code == 202, b_resp.text
    assert await ctx.sessions() == [borrowed]
    runs = await ctx.run_store.list_by_thread(thread_id=borrowed, tenant_id=ctx.tenant_id)
    assert [str(r.run_id) for r in runs] == [b_resp.json()["data"]["run_id"]]


@pytest.mark.asyncio
async def test_a_file_uploaded_into_the_new_session_keeps_it_on_a_rejected_run(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 不带会话起 run 被拒;其间 B 往 S 里传了一份文档 —— S、B 的登记行和文件都留着。"""

    async def _b() -> tuple[UUID, dict[str, Any]]:
        borrowed = await _only_session(ctx)
        return borrowed, await _upload_doc(ctx, b"%PDF-1.4 B", session_id=borrowed)

    seen = _during_admission(monkeypatch, agents_mod, _b)

    resp = await ctx.run(user_id="u1", files=[unknown_file()])

    assert resp.status_code == 404, resp.text
    borrowed, b_upload = seen["result"]
    assert await ctx.sessions() == [borrowed]
    rows = await ctx.app.state.user_upload_store.list_documents_for_thread(
        tenant_id=ctx.tenant_id, thread_id=borrowed
    )
    assert [r.ref for r in rows] == ["uploads/report.pdf"], b_upload
    assert ctx.workspace_store.workspace_tree_deletes == []


@pytest.mark.asyncio
async def test_a_file_uploaded_into_the_new_session_keeps_it_on_a_rejected_upload(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 不带会话传图被配额拒;其间 B 往 S 里传了一份文档 —— S 与 B 的上传都留着。"""

    async def _b() -> tuple[UUID, dict[str, Any]]:
        borrowed = await _only_session(ctx)
        return borrowed, await _upload_doc(ctx, b"%PDF-1.4 B", session_id=borrowed)

    seen = _during_admission(monkeypatch, uploads_mod, _b)
    ctx.app.state.quota_service = DenyQuota()

    resp = await ctx.upload(name="a.png", raw=PNG, mime="image/png")

    assert resp.status_code == 429, resp.text
    borrowed, _b_upload = seen["result"]
    assert await ctx.sessions() == [borrowed]
    rows = await ctx.app.state.user_upload_store.list_documents_for_thread(
        tenant_id=ctx.tenant_id, thread_id=borrowed
    )
    assert len(rows) == 1
    assert ctx.workspace_store.workspace_tree_deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queue", "stream"])
async def test_a_run_on_a_session_undone_meanwhile_is_404(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """B 在接单锁外等的时候,A 把会话撤了(这里:B 在配额判定时会话行没了)。B 进锁后重读会话
    行,回与「会话不存在」同一个 404,不在一条已删的会话上建 run。"""
    bound = await ctx.client.post(f"/v1/agents/{AGENT}/sessions", json={"user_id": "u1"})
    session_id = UUID(bound.json()["data"]["session_id"])

    async def _undo_meanwhile() -> None:
        await ctx.app.state.thread_meta_repo.delete(session_id, tenant_id=ctx.tenant_id)

    _during_admission(monkeypatch, agents_mod, _undo_meanwhile)

    resp = await ctx.run(user_id="u1", session_id=str(session_id), mode=mode)

    assert resp.status_code == 404, resp.text
    assert resp.json() == {
        "success": False,
        "data": None,
        "error": {
            "code": "SESSION_NOT_FOUND",
            "message": "session not found for this user / agent",
        },
    }
    assert await ctx.run_store.list_by_thread(thread_id=session_id, tenant_id=ctx.tenant_id) == []


# ---------------------------------------------------------------------------
# 撤的顺序、目录删除的时限、用量重算。
# ---------------------------------------------------------------------------


async def _partial_copy_run(ctx: Ctx) -> tuple[UUID, Any]:
    """``files[]`` 第一条从别的会话复制进新会话(落文件 + 登记一行),第二条不存在 → 404。"""
    source = await _upload_doc(ctx, b"%PDF-1.4 A")
    resp = await ctx.run(user_id="u1", files=[{"upload_id": source["upload_id"]}, unknown_file()])
    return UUID(source["session_id"]), resp


@pytest.mark.asyncio
async def test_the_session_row_goes_first_then_its_upload_rows_then_its_directory(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同会话彻底删除:会话行先删(之后留存作业的孤儿目录扫描兜得住剩下的),再删登记行,
    最后删目录。反过来的话请求中途被取消会留下一份没有登记行、留存作业看不见的文件。"""
    seen: list[tuple[str, int, int]] = []
    uploads = ctx.app.state.user_upload_store
    real_delete_rows = uploads.delete_for_threads
    real_delete_tree = ctx.workspace_store.delete_tree

    async def _state() -> tuple[int, int]:
        threads = len(await ctx.sessions())
        rows = sum(1 for row in uploads._rows.values())
        return threads, rows

    async def _delete_rows(**kwargs: Any) -> int:
        seen.append(("rows", *await _state()))
        result: int = await real_delete_rows(**kwargs)
        return result

    async def _delete_tree(**kwargs: Any) -> None:
        seen.append(("dir", *await _state()))
        await real_delete_tree(**kwargs)

    monkeypatch.setattr(uploads, "delete_for_threads", _delete_rows)
    monkeypatch.setattr(ctx.workspace_store, "delete_tree", _delete_tree)

    _source, resp = await _partial_copy_run(ctx)

    assert resp.status_code == 404, resp.text
    # (步骤, 当时的会话数, 当时的登记行数):源会话 1 条 + 它的登记行 1 行;新会话与它的
    # 登记行在前面那一步已经没了。
    assert seen == [("rows", 1, 2), ("dir", 1, 1)]


@pytest.mark.asyncio
async def test_a_hanging_directory_delete_does_not_hold_the_rejection(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _hang(**_: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(ctx.workspace_store, "delete_tree", _hang)
    monkeypatch.setattr(undo_mod, "_DISCARD_DIR_TIMEOUT_S", 0.05)

    source, resp = await asyncio.wait_for(_partial_copy_run(ctx), timeout=5)

    assert resp.status_code == 404, resp.text
    assert await ctx.sessions() == [source]


class _RecordingWorkspaceQuota:
    """结构上满足落盘记账用到的那几个方法;记下 ``refresh_soon``。"""

    def __init__(self) -> None:
        self.refreshed: list[tuple[UUID, UUID]] = []

    async def check_upload(self, *, tenant_id: UUID, user_id: UUID, incoming_bytes: int) -> None:
        del tenant_id, user_id, incoming_bytes

    async def note_written(self, *, tenant_id: UUID, user_id: UUID, delta_bytes: int) -> None:
        del tenant_id, user_id, delta_bytes

    def refresh_soon(self, *, tenant_id: UUID, user_id: UUID) -> None:
        self.refreshed.append((tenant_id, user_id))


@pytest.mark.asyncio
async def test_undoing_landed_files_asks_for_a_usage_refresh(ctx: Ctx) -> None:
    """复制进新会话时记过 ``note_written(+字节)``,撤的时候没有对应的减项:让用量重算。"""
    quota = _RecordingWorkspaceQuota()
    ctx.app.state.workspace_quota_service = quota

    source, resp = await _partial_copy_run(ctx)

    assert resp.status_code == 404, resp.text
    owner = (await ctx.app.state.thread_meta_repo.get(source, tenant_id=ctx.tenant_id)).user_id
    assert quota.refreshed == [(ctx.tenant_id, owner)]


@pytest.mark.asyncio
async def test_a_rejection_without_landed_files_asks_for_no_refresh(ctx: Ctx) -> None:
    quota = _RecordingWorkspaceQuota()
    ctx.app.state.workspace_quota_service = quota

    resp = await ctx.run(user_id="u1", untrusted_content=["x" * 9000])

    assert resp.status_code == 422, resp.text
    assert await ctx.sessions() == []
    assert quota.refreshed == []


# ---------------------------------------------------------------------------
# 建会话之后的意外异常:撤掉新会话,异常原样抛出。
# ---------------------------------------------------------------------------


class _BoomError(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["instance_touch", "admission", "build"])
async def test_an_unexpected_error_before_the_run_leaves_no_session(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    boom = _BoomError("boom")

    async def _raise(*_: Any, **__: Any) -> Any:
        raise boom

    if where == "instance_touch":
        monkeypatch.setattr(ctx.app.state.agent_instance_store, "touch", _raise)
    elif where == "admission":
        monkeypatch.setattr(agents_mod, "check_admission", _raise)
    else:
        monkeypatch.setattr(ctx.app.state.agent_runtime, "get_agent", _raise)

    with pytest.raises(_BoomError) as info:
        await ctx.run(user_id="u1")

    assert info.value is boom
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_an_unexpected_error_after_a_document_landed_leaves_no_session(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传:文件已经落进新会话的目录,登记那一步出意外 —— 新会话连同目录撤掉,异常原样抛出。"""
    boom = _BoomError("db down")

    async def _raise(*_: Any, **__: Any) -> Any:
        raise boom

    monkeypatch.setattr(uploads_mod, "register_document_upload", _raise)

    with pytest.raises(_BoomError) as info:
        await ctx.upload(name="report.pdf", raw=b"%PDF-1.4 A", mime="application/pdf")

    assert info.value is boom
    assert await ctx.sessions() == []
    prefix = f"agents/{agent_key_for_code(AGENT)}/threads/"
    landed = [p for p in ctx.workspace_store.created_files if p.startswith(prefix)]
    assert len(landed) == 1, ctx.workspace_store.created_files
    thread_dir = prefix + landed[0].removeprefix(prefix).split("/", 1)[0]
    assert [p for _t, _u, p in ctx.workspace_store.workspace_tree_deletes] == [thread_dir]
