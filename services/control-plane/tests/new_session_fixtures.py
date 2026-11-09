"""B-293 —— 对外 run / 上传端点「被拒的请求不留新会话」测试共用的 app + 客户端。

内存栈:service-account API key 客户端、一个 ``support-bot`` agent、``RecordingWorkspaceStore``
与内存对象存储(文档 / 图片上传都走得通)。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, CheckResult, QuotaDimension, render_upload_id
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunIdempotencyConflict,
    RunInfo,
)
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import RecordingWorkspaceStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

AGENT = "support-bot"

SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": AGENT, "version": "1.0.0", "tenant": "acme"},
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


def settings() -> Settings:
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


class ConflictOnceRunStore(InMemoryRunStore):
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


def multipart(data: dict[str, str], *, name: str, raw: bytes, mime: str) -> tuple[bytes, str]:
    """手写 multipart:httpx 对空文件名干脆不写 ``filename``(服务端就当成普通表单字段、在进
    端点之前 422),而客户端照样可以发 ``filename=""``(Starlette 收成文件名为空的上传)。"""
    boundary = "b293-boundary"
    head = "".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        for key, value in data.items()
    )
    file_head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    )
    body = (head + file_head).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


@dataclass
class Ctx:
    app: Any
    client: AsyncClient
    tenant_id: UUID
    workspace_store: RecordingWorkspaceStore
    run_store: ConflictOnceRunStore

    async def run(self, **body: Any) -> Any:
        return await self.client.post(
            f"/v1/agents/{AGENT}/runs", json={"input": "x", "mode": "queue"} | body
        )

    async def upload(
        self, *, name: str, raw: bytes, mime: str, session_id: UUID | None = None
    ) -> Any:
        data = {"user_id": "u1"} | ({"session_id": str(session_id)} if session_id else {})
        body, content_type = multipart(data, name=name, raw=raw, mime=mime)
        return await self.client.post(
            f"/v1/agents/{AGENT}/uploads", content=body, headers={"Content-Type": content_type}
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


@asynccontextmanager
async def new_session_ctx() -> AsyncIterator[Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = ConflictOnceRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=settings(),
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
        spec=AgentSpec.model_validate(deepcopy(SPEC)),
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
        yield Ctx(app, client, tenant_id, workspace_store, run_store)


def unknown_file() -> dict[str, str]:
    return {"upload_id": render_upload_id(uuid4())}


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class DenyQuota:
    """结构上满足 ``QuotaService``;``check`` 一律拒(图片配额)。"""

    async def check(self, req: object) -> CheckResult:
        del req
        return CheckResult(
            allowed=False,
            blocked_dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
            retry_after_s=1,
        )
