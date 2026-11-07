"""B-293 —— ``POST /v1/agents/{code}/runs`` 不带 ``session_id`` 时,请求在起 run 之前被拒,
不能留下一条新的空会话。

现场(测试环境 2026-11-06):``files[]`` 引用了一个不存在的上传,接口回 404,但那位用户名下
多了一条空会话;对接方每重试一次就多一条,出现在用户的会话列表里。原因是端点先建会话
(``_resolve_session``)、再做配额 / 构建 / ``files[]`` / 上限这些可能拒绝的检查。

带 ``session_id`` 续一条已有会话的请求被拒时,那条会话原样留着。

``POST /v1/agents/{code}/uploads`` 不带 ``session_id`` 时同样先建会话,再校验文件名 / 类型 /
图片配额 / 落盘;被拒时同一个规矩。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.responses import JSONResponse

from control_plane.api import agents as agents_mod
from control_plane.api._external_agent_scope import agent_key_for_code
from control_plane.api._idempotency import request_digest as compute_request_digest
from control_plane.api.agents import ExternalRunRequest
from expert_work.protocol import (
    parse_upload_id,
)
from expert_work.runtime.runs import (
    DisconnectMode,
    RunInfo,
    RunStatus,
)
from orchestrator import AgentFactoryError
from orchestrator.tools import SandboxSupervisorError
from tests.new_session_fixtures import (
    AGENT,
    PNG,
    Ctx,
    DenyQuota,
    new_session_ctx,
    unknown_file,
)


@pytest.fixture
async def ctx() -> AsyncIterator[Ctx]:
    async with new_session_ctx() as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queue", "stream"])
async def test_unknown_upload_on_a_new_session_leaves_no_session(ctx: Ctx, mode: str) -> None:
    """现场那一例:``files[]`` 指向不存在的上传,对接方重试两次 —— 回包不变,会话一条不多。"""
    for _ in range(2):
        resp = await ctx.run(user_id="u1", mode=mode, files=[unknown_file()])
        assert resp.status_code == 404, resp.text
        assert resp.json() == {
            "success": False,
            "data": None,
            "error": {"code": "UPLOAD_NOT_FOUND", "message": "upload not found"},
        }

    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_a_rejected_run_on_an_existing_session_keeps_that_session(ctx: Ctx) -> None:
    bound = await ctx.client.post(f"/v1/agents/{AGENT}/sessions", json={"user_id": "u1"})
    assert bound.status_code == 201, bound.text
    session_id = UUID(bound.json()["data"]["session_id"])

    resp = await ctx.run(user_id="u1", session_id=str(session_id), files=[unknown_file()])

    assert resp.status_code == 404, resp.text
    assert await ctx.sessions() == [session_id]


@pytest.mark.asyncio
async def test_a_quota_denial_leaves_no_session(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    denial = JSONResponse(status_code=429, content={"success": False})

    async def _deny(**_: Any) -> JSONResponse:
        return denial

    monkeypatch.setattr(agents_mod, "check_admission", _deny)

    resp = await ctx.run(user_id="u1")

    assert resp.status_code == 429, resp.text
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_a_build_failure_leaves_no_session(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(**_: Any) -> Any:
        raise AgentFactoryError("no model key")

    monkeypatch.setattr(ctx.app.state.agent_runtime, "get_agent", _fail)

    resp = await ctx.run(user_id="u1")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "AGENT_BUILD_FAILED"
    assert await ctx.sessions() == []


_OVER_LIMIT = ["x" * 9000]


@pytest.mark.asyncio
async def test_an_over_limit_request_leaves_no_session(ctx: Ctx) -> None:
    resp = await ctx.run(user_id="u1", untrusted_content=_OVER_LIMIT)

    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "success": False,
        "data": None,
        "error": {
            "code": "UNTRUSTED_CONTENT_BLOCK_TOO_LONG",
            "message": "untrusted_content[0] 超过 8192 字符",
        },
    }
    assert await ctx.sessions() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "status", "error"),
    [
        (
            "unknown_agent",
            404,
            {"code": "AGENT_NOT_FOUND", "message": "no active agent 'nope' for this tenant"},
        ),
        (
            "unknown_session",
            404,
            {"code": "SESSION_NOT_FOUND", "message": "session not found for this user / agent"},
        ),
        (
            "disabled_agent",
            403,
            {"code": "AGENT_DISABLED", "message": f"agent {AGENT!r} is disabled"},
        ),
    ],
)
async def test_an_over_limit_request_reports_session_errors_first(
    ctx: Ctx,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    status: int,
    error: dict[str, str],
) -> None:
    """先后不变:会话解析的拒绝先于上限预检(一个请求同时撞上两者,回包与改动前一样)。"""
    agent, body = AGENT, {"user_id": "u1", "untrusted_content": _OVER_LIMIT}
    if case == "unknown_agent":
        agent = "nope"
    elif case == "unknown_session":
        body["session_id"] = str(uuid4())
    else:

        async def _disabled(*_: Any) -> bool:
            return True

        monkeypatch.setattr(ctx.app.state.agent_disable_service, "is_disabled", _disabled)

    resp = await ctx.client.post(
        f"/v1/agents/{agent}/runs", json={"input": "x", "mode": "queue"} | body
    )

    assert resp.status_code == status, resp.text
    assert resp.json() == {"success": False, "data": None, "error": error}


@pytest.mark.asyncio
async def test_a_rejection_inside_spawn_run_leaves_no_session(ctx: Ctx) -> None:
    """``spawn_run`` 建 run 之前的同步校验(这里:agent 没声明变量却传了 ``inputs``)。"""
    resp = await ctx.run(user_id="u1", inputs={"name": "x"})

    assert resp.status_code == 422, resp.text
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_an_idempotency_race_loser_leaves_no_session(ctx: Ctx) -> None:
    """并发同 key 的输家拿到赢家的 run;它自己刚建的会话没人会用,不留。"""
    body = {"user_id": "u1", "input": "x", "mode": "queue"}
    now = datetime.now(UTC)
    winner_thread = uuid4()
    ctx.run_store.winner = RunInfo(
        run_id=uuid4(),
        tenant_id=ctx.tenant_id,
        thread_id=winner_thread,
        user_id=None,
        status=RunStatus.QUEUED,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
        idempotency_key="race-1",
        request_digest=compute_request_digest(
            ExternalRunRequest.model_validate(body), agent_code=AGENT
        ),
    )

    resp = await ctx.client.post(
        f"/v1/agents/{AGENT}/runs", json=body, headers={"Idempotency-Key": "race-1"}
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["data"]["thread_id"] == str(winner_thread)
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_files_copied_in_before_the_rejection_go_with_the_session(ctx: Ctx) -> None:
    """``files[]`` 第一条是别的会话的文档(复制进新会话、登记一行),第二条不存在 → 404。
    复制进来的登记行与新会话的目录都随新会话一起撤掉;源会话与它的上传不动。"""
    uploaded = await ctx.client.post(
        f"/v1/agents/{AGENT}/uploads",
        data={"user_id": "u1"},
        files={"file": ("report.pdf", b"%PDF-1.4 A", "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    source = UUID(uploaded.json()["data"]["session_id"])
    upload_id = uploaded.json()["data"]["upload_id"]

    resp = await ctx.run(user_id="u1", files=[{"upload_id": upload_id}, unknown_file()])

    assert resp.status_code == 404, resp.text
    assert await ctx.sessions() == [source]
    key = agent_key_for_code(AGENT)
    prefix = f"agents/{key}/threads/"
    landed = [
        path
        for path in ctx.workspace_store.created_files
        if path.startswith(prefix) and not path.startswith(f"{prefix}{source}/")
    ]
    assert len(landed) == 1, ctx.workspace_store.created_files
    discarded = UUID(landed[0].removeprefix(prefix).split("/", 1)[0])
    assert (ctx.tenant_id, discarded) in {
        (t, UUID(p.removeprefix(prefix))) for t, _u, p in ctx.workspace_store.workspace_tree_deletes
    }
    uploads = ctx.app.state.user_upload_store
    assert (
        await uploads.list_documents_for_thread(tenant_id=ctx.tenant_id, thread_id=discarded) == []
    )
    assert (
        len(await uploads.list_documents_for_thread(tenant_id=ctx.tenant_id, thread_id=source)) == 1
    )


# ---------------------------------------------------------------------------
# ``POST /v1/agents/{code}/uploads`` —— 不带 ``session_id`` 时建的会话,被拒就撤掉。
# ---------------------------------------------------------------------------


def _reject_upload(ctx: Ctx, case: str) -> tuple[str, bytes, str]:
    """把 ``ctx`` 摆成 ``case`` 那种拒绝,回要上传的 ``(文件名, 字节, 类型)``。"""
    if case == "quota":
        ctx.app.state.quota_service = DenyQuota()
        return "a.png", PNG, "image/png"
    if case == "landing":
        ctx.workspace_store.workspace_write_error = SandboxSupervisorError("boom")
        return "report.pdf", b"%PDF-1.4 A", "application/pdf"
    if case == "no_filename":
        return "", b"%PDF-1.4 A", "application/pdf"
    return "x.bin", b"\x00\x01", "application/octet-stream"


_UPLOAD_REJECTIONS = [
    ("no_filename", 400, "uploaded file has no filename"),
    ("unsupported", 400, "unsupported content type: 'application/octet-stream'"),
    ("quota", 429, None),
    ("landing", 502, "workspace write failed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "status", "message"), _UPLOAD_REJECTIONS)
async def test_a_rejected_upload_on_a_new_session_leaves_no_session(
    ctx: Ctx, case: str, status: int, message: str | None
) -> None:
    name, raw, mime = _reject_upload(ctx, case)

    resp = await ctx.upload(name=name, raw=raw, mime=mime)

    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["success"] is False and body["data"] is None
    if message is not None:
        assert body["error"]["message"] == message
    assert await ctx.sessions() == []
    # 内存登记表的全部行(登记表没有按租户列全部的接口)。
    assert ctx.app.state.user_upload_store._rows == {}


@pytest.mark.asyncio
async def test_a_landing_error_on_a_new_session_removes_its_directory(ctx: Ctx) -> None:
    """落盘失败时目录里可能已经留下半份文件(NAS 上写了一半):新会话的目录随会话一起删。"""
    name, raw, mime = _reject_upload(ctx, "landing")

    resp = await ctx.upload(name=name, raw=raw, mime=mime)

    assert resp.status_code == 502, resp.text
    prefix = f"agents/{agent_key_for_code(AGENT)}/threads/"
    deleted = [p for _t, _u, p in ctx.workspace_store.workspace_tree_deletes]
    assert len(deleted) == 1 and deleted[0].startswith(prefix), deleted
    thread_id = UUID(deleted[0].removeprefix(prefix))
    assert await ctx.app.state.thread_meta_repo.get(thread_id, tenant_id=ctx.tenant_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "status", "message"), _UPLOAD_REJECTIONS)
async def test_a_rejected_upload_into_an_existing_session_keeps_it(
    ctx: Ctx, case: str, status: int, message: str | None
) -> None:
    del message
    first = await ctx.upload(name="report.pdf", raw=b"%PDF-1.4 first", mime="application/pdf")
    assert first.status_code == 201, first.text
    session_id = UUID(first.json()["data"]["session_id"])
    name, raw, mime = _reject_upload(ctx, case)

    resp = await ctx.upload(name=name, raw=raw, mime=mime, session_id=session_id)

    assert resp.status_code == status, resp.text
    assert await ctx.sessions() == [session_id]
    uploads = ctx.app.state.user_upload_store
    assert (
        len(await uploads.list_documents_for_thread(tenant_id=ctx.tenant_id, thread_id=session_id))
        == 1
    )
    assert ctx.workspace_store.workspace_tree_deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "mime", "kind"),
    [
        ("report.pdf", b"%PDF-1.4 A", "application/pdf", "document"),
        ("a.png", PNG, "image/png", "image"),
    ],
)
async def test_a_successful_upload_on_a_new_session_keeps_it(
    ctx: Ctx, name: str, raw: bytes, mime: str, kind: str
) -> None:
    resp = await ctx.upload(name=name, raw=raw, mime=mime)

    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["type"] == kind
    session_id = UUID(data["session_id"])
    assert await ctx.sessions() == [session_id]
    upload_id = parse_upload_id(data["upload_id"])
    assert upload_id is not None
    row = await ctx.app.state.user_upload_store.get(upload_id=upload_id, tenant_id=ctx.tenant_id)
    assert row is not None and row.thread_id == session_id
    assert ctx.workspace_store.workspace_tree_deletes == []
