"""B-293 —— ``POST /v1/agents/{code}/runs`` 不带 ``session_id`` 时,请求在起 run 之前被拒,
不能留下一条新的空会话。

现场(测试环境 2026-11-06):``files[]`` 引用了一个不存在的上传,接口回 404,但那位用户名下
多了一条空会话;对接方每重试一次就多一条,出现在用户的会话列表里。原因是端点先建会话
(``_resolve_session``)、再做配额 / 构建 / ``files[]`` / 上限这些可能拒绝的检查。

带 ``session_id`` 续一条已有会话的请求被拒时,那条会话原样留着。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from control_plane.api import agents as agents_mod
from control_plane.api._external import external_subject_id
from control_plane.api._external_agent_scope import agent_key_for_code
from control_plane.api._idempotency import request_digest as compute_request_digest
from control_plane.api.agents import ExternalRunRequest
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, render_upload_id
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunIdempotencyConflict,
    RunInfo,
    RunStatus,
)
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator import AgentFactoryError
from orchestrator.tools import RecordingWorkspaceStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_AGENT = "support-bot"

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": _AGENT, "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _ConflictOnceRunStore(InMemoryRunStore):
    """``create`` 对 ``conflict_key`` 先插入「赢家」再抛一次 :class:`RunIdempotencyConflict`
    —— 确定性地走到端点的「并发输家」分支(同 ``test_external_idempotency.py``)。"""

    def __init__(self) -> None:
        super().__init__()
        self.winner: RunInfo | None = None

    async def create(self, info: RunInfo) -> None:
        winner = self.winner
        key = info.idempotency_key
        if winner is not None and key is not None and key == winner.idempotency_key:
            self.winner = None
            await super().create(winner)
            raise RunIdempotencyConflict(tenant_id=info.tenant_id, idempotency_key=key)
        await super().create(info)


@dataclass
class _Ctx:
    app: Any
    client: AsyncClient
    tenant_id: UUID
    workspace_store: RecordingWorkspaceStore
    run_store: _ConflictOnceRunStore

    async def run(self, **body: Any) -> Any:
        return await self.client.post(
            f"/v1/agents/{_AGENT}/runs", json={"input": "x", "mode": "queue"} | body
        )

    async def sessions(self) -> list[UUID]:
        rows = await self.app.state.thread_meta_repo.list_by_tenant(
            self.tenant_id, include_archived=True, limit=MAX_LIST_LIMIT
        )
        return [row.thread_id for row in rows]

    async def has_user(self, user_id: str) -> bool:
        rows = await self.app.state.tenant_user_repo.list_by_tenant(
            self.tenant_id, subject_type="user", limit=MAX_LIST_LIMIT
        )
        return any(row.subject_id == external_subject_id(user_id) for row in rows)


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = _ConflictOnceRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    app.state.object_store = InMemoryObjectStore()
    workspace_store = RecordingWorkspaceStore()
    app.state.workspace_store = workspace_store
    tenant_id = uuid4()
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id,
        spec=AgentSpec.model_validate(deepcopy(_SPEC)),
        spec_sha256="a" * 64,
        created_by="seed",
    )
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-external-app",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {jwt}"},
    ) as client:
        yield _Ctx(app, client, tenant_id, workspace_store, run_store)


def _unknown_file() -> dict[str, str]:
    return {"upload_id": render_upload_id(uuid4())}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queue", "stream"])
async def test_unknown_upload_on_a_new_session_leaves_no_session(ctx: _Ctx, mode: str) -> None:
    """现场那一例:``files[]`` 指向不存在的上传,对接方重试两次 —— 回包不变,会话一条不多。"""
    for _ in range(2):
        resp = await ctx.run(user_id="u1", mode=mode, files=[_unknown_file()])
        assert resp.status_code == 404, resp.text
        assert resp.json() == {
            "success": False,
            "data": None,
            "error": {"code": "UPLOAD_NOT_FOUND", "message": "upload not found"},
        }

    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_a_rejected_run_on_an_existing_session_keeps_that_session(ctx: _Ctx) -> None:
    bound = await ctx.client.post(f"/v1/agents/{_AGENT}/sessions", json={"user_id": "u1"})
    assert bound.status_code == 201, bound.text
    session_id = UUID(bound.json()["data"]["session_id"])

    resp = await ctx.run(user_id="u1", session_id=str(session_id), files=[_unknown_file()])

    assert resp.status_code == 404, resp.text
    assert await ctx.sessions() == [session_id]


@pytest.mark.asyncio
async def test_a_quota_denial_leaves_no_session(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    denial = JSONResponse(status_code=429, content={"success": False})

    async def _deny(**_: Any) -> JSONResponse:
        return denial

    monkeypatch.setattr(agents_mod, "check_admission", _deny)

    resp = await ctx.run(user_id="u1")

    assert resp.status_code == 429, resp.text
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_a_build_failure_leaves_no_session(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail(**_: Any) -> Any:
        raise AgentFactoryError("no model key")

    monkeypatch.setattr(ctx.app.state.agent_runtime, "get_agent", _fail)

    resp = await ctx.run(user_id="u1")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "AGENT_BUILD_FAILED"
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_an_over_limit_request_is_rejected_before_anything_is_written(ctx: _Ctx) -> None:
    """上限预检不依赖会话,挪到建会话之前:连终端用户都不铸。"""
    resp = await ctx.run(user_id="u1", untrusted_content=["x" * 9000])

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UNTRUSTED_CONTENT_BLOCK_TOO_LONG"
    assert await ctx.sessions() == []
    assert not await ctx.has_user("u1")


@pytest.mark.asyncio
async def test_a_rejection_inside_spawn_run_leaves_no_session(ctx: _Ctx) -> None:
    """``spawn_run`` 建 run 之前的同步校验(这里:agent 没声明变量却传了 ``inputs``)。"""
    resp = await ctx.run(user_id="u1", inputs={"name": "x"})

    assert resp.status_code == 422, resp.text
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_an_idempotency_race_loser_leaves_no_session(ctx: _Ctx) -> None:
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
            ExternalRunRequest.model_validate(body), agent_code=_AGENT
        ),
    )

    resp = await ctx.client.post(
        f"/v1/agents/{_AGENT}/runs", json=body, headers={"Idempotency-Key": "race-1"}
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["data"]["thread_id"] == str(winner_thread)
    assert await ctx.sessions() == []


@pytest.mark.asyncio
async def test_files_copied_in_before_the_rejection_go_with_the_session(ctx: _Ctx) -> None:
    """``files[]`` 第一条是别的会话的文档(复制进新会话、登记一行),第二条不存在 → 404。
    复制进来的登记行与新会话的目录都随新会话一起撤掉;源会话与它的上传不动。"""
    uploaded = await ctx.client.post(
        f"/v1/agents/{_AGENT}/uploads",
        data={"user_id": "u1"},
        files={"file": ("report.pdf", b"%PDF-1.4 A", "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    source = UUID(uploaded.json()["data"]["session_id"])
    upload_id = uploaded.json()["data"]["upload_id"]

    resp = await ctx.run(user_id="u1", files=[{"upload_id": upload_id}, _unknown_file()])

    assert resp.status_code == 404, resp.text
    assert await ctx.sessions() == [source]
    key = agent_key_for_code(_AGENT)
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
