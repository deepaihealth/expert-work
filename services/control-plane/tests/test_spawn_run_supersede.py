"""P-1 —— spawn_run(supersede=):锁内「supersede → 建行」顺序、regenerated_from、重放输入。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from starlette.requests import Request

from control_plane.api import runs as runs_mod
from control_plane.api.runs import RunRequest, SupersedeRequest, replay_graph_input, spawn_run
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from control_plane.supersede import SupersedeError, SupersedeResult, TurnLocation
from expert_work.common.conversation_channel import SUPERSEDED_BY
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.message_stamp import STAMP_RUN_ID, stamp_message
from expert_work.common.supersede import mark_superseded
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
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


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _build_settings() -> Settings:
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


def _built() -> Any:
    return SimpleNamespace(max_steps=5, max_no_progress=0)


def test_replay_graph_input_fresh_ids_new_stamp_no_attachment_keys() -> None:
    old_run, new_run = uuid4(), uuid4()
    system = SystemMessage(content="sys", id="sys-old")
    human = mark_superseded(
        stamp_message(
            HumanMessage(content="U", id="h-old"),
            run_id=str(old_run),
            now=datetime(2026, 9, 1, tzinfo=UTC),
        ),
        new_run_id=str(new_run),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    out = replay_graph_input(_built(), [system, human], run_id=new_run)
    sys_out, human_out = out["messages"]
    assert sys_out.content == "sys" and sys_out.id not in (None, "sys-old")
    assert human_out.content == "U" and human_out.id not in (None, "h-old")
    assert human_out.additional_kwargs[STAMP_RUN_ID] == str(new_run)
    assert SUPERSEDED_BY not in human_out.additional_kwargs
    assert out["step_count"] == 0 and out["max_steps"] == 5
    # 故意省略:检查点里此刻的值就是被取代轮自己写的那一份 = 同一批附件。
    assert "turn_documents" not in out and "turn_image_refs" not in out


class _SpawnCtx:
    def __init__(self, app: Any, tenant_id: UUID, thread_id: UUID, run_store: Any) -> None:
        self.app = app
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self.run_store = run_store
        self.runtime = app.state.agent_runtime

    async def spawn(self, *, mode: str, supersede: SupersedeRequest | None) -> Any:
        records = await self.app.state.agent_spec_repo.list_by_tenant(
            tenant_id=self.tenant_id, name="support-bot", limit=1
        )
        record = records[0]
        built = await self.runtime.get_agent(
            tenant_id=self.tenant_id,
            name="support-bot",
            version=record.version,
            spec=record.spec,
            user_id=None,
        )
        # queue 分支只读 request.app.state;stream 分支还读 headers /
        # is_disconnected —— 本文件只测 queue。
        request = Request(
            {
                "type": "http",
                "app": self.app,
                "headers": [],
                "method": "POST",
                "path": "/",
                "query_string": b"",
            }
        )
        return await spawn_run(
            runtime=self.runtime,
            audit=self.app.state.audit_logger,
            approvals=self.app.state.approval_store,
            request=request,
            settings=self.app.state.settings,
            built=built,
            record_spec=record.spec,
            thread_id=self.thread_id,
            tenant_id=self.tenant_id,
            actor_id="sa-test",
            effective_user_id=None,
            oauth_subject="sa-test",
            payload=RunRequest(input="U", mode=mode),
            trace_id="0" * 32,
            supersede=supersede,
        )


@pytest.fixture
async def spawn_ctx() -> AsyncIterator[_SpawnCtx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store),
        run_repo=run_store,
    )
    tenant_id = uuid4()
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
    )
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="seed",
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    yield _SpawnCtx(app, tenant_id, thread_id, run_store)


@pytest.mark.asyncio
async def test_spawn_run_supersedes_before_creating_the_row(
    monkeypatch: pytest.MonkeyPatch, spawn_ctx: _SpawnCtx
) -> None:
    """取代必须在建行**之前**跑完:queue worker 只认已存在的 QUEUED 行,
    行在 supersede 全部写完之后才 INSERT,worker 抢不到「标记未落、run 已跑」的窗口。"""
    order: list[str] = []
    target = uuid4()

    async def fake_supersede_run(**kwargs: Any) -> SupersedeResult:
        order.append("supersede")
        assert kwargs["target_run_id"] == target
        assert kwargs["new_run_id"] not in (None, target)
        assert kwargs["require_replay"] is True
        return SupersedeResult(
            location=TurnLocation(start=0, end=2, plan_before=None, chain_run_ids=(target,)),
            replay_messages=(SystemMessage(content="sys"), HumanMessage(content="U-old")),
            superseded_run_ids=(target,),
        )

    real_enqueue = spawn_ctx.runtime.run_manager.enqueue
    seen: dict[str, Any] = {}

    async def spy_enqueue(**kwargs: Any) -> None:
        # 只记不断言 —— 断言留到主体,顺序那条才是第一个红的。
        order.append("enqueue")
        seen.update(kwargs)
        await real_enqueue(**kwargs)

    monkeypatch.setattr(runs_mod, "supersede_run", fake_supersede_run)
    monkeypatch.setattr(spawn_ctx.runtime.run_manager, "enqueue", spy_enqueue)
    resp = await spawn_ctx.spawn(
        mode="queue", supersede=SupersedeRequest(target_run_id=target, replay=True)
    )
    assert resp.status_code == 202
    assert order == ["supersede", "enqueue"]
    # 建行那一刻,取代的产物必须已经在手上。
    assert seen["regenerated_from_run_id"] == target
    assert set(seen["enqueued_input"]) == {"replay_messages"}

    rows = await spawn_ctx.run_store.list_by_thread(
        thread_id=spawn_ctx.thread_id, tenant_id=spawn_ctx.tenant_id
    )
    assert [r.regenerated_from_run_id for r in rows] == [target]


@pytest.mark.asyncio
async def test_spawn_run_propagates_supersede_error_without_creating_a_row(
    monkeypatch: pytest.MonkeyPatch, spawn_ctx: _SpawnCtx
) -> None:
    async def fake_supersede_run(**kwargs: Any) -> SupersedeResult:
        del kwargs
        raise SupersedeError("THREAD_BUSY", "busy", 409)

    monkeypatch.setattr(runs_mod, "supersede_run", fake_supersede_run)
    before = len(
        await spawn_ctx.run_store.list_by_thread(
            thread_id=spawn_ctx.thread_id, tenant_id=spawn_ctx.tenant_id
        )
    )
    with pytest.raises(SupersedeError):
        await spawn_ctx.spawn(
            mode="queue", supersede=SupersedeRequest(target_run_id=uuid4(), replay=False)
        )
    after = len(
        await spawn_ctx.run_store.list_by_thread(
            thread_id=spawn_ctx.thread_id, tenant_id=spawn_ctx.tenant_id
        )
    )
    assert after == before


@pytest.mark.asyncio
async def test_spawn_run_without_supersede_keeps_the_original_enqueued_input(
    spawn_ctx: _SpawnCtx,
) -> None:
    """``supersede=None`` 是原路径:不取锁、不重放,``enqueued_input`` 五个键照旧。"""
    resp = await spawn_ctx.spawn(mode="queue", supersede=None)
    assert resp.status_code == 202
    rows = await spawn_ctx.run_store.list_by_thread(
        thread_id=spawn_ctx.thread_id, tenant_id=spawn_ctx.tenant_id
    )
    assert len(rows) == 1
    assert rows[0].regenerated_from_run_id is None
    assert set(rows[0].enqueued_input or {}) == {
        "input",
        "image_refs",
        "untrusted_content",
        "inputs",
        "document_names",
    }
