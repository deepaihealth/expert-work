"""Idempotency-Key —— 对外 run 端点判定(External-API-v1 P2-a Task 13 queue 模式 +
Task 14 stream 模式重放)。

同键同体(含 agent_code)返回原 run;同键异体、或跨 agent 复用同键,一律 422。
Task 13 曾让 stream 模式带 key 一律 422(裁定 2 —— "支持"是下一个 task 的事);
Task 14 放宽了这条 422 —— stream 模式命中同键同体时,重放原 run 的事件流
(``build_events_response``,与 ``GET .../runs/{id}/events`` 同一份实现),
不再拒绝。并发单赢家的"重查返回赢家"分支用一个确定性触发冲突的
``RunStore`` 包装类覆盖,而不是伪装成真并发的 ``asyncio.gather``——见文件
末尾那段测试前的注释,解释了为什么、以及真并发已经在哪里证过。

Fixture shape mirrors ``test_external_run_inputs.py`` / ``test_external_
sessions.py`` (app + service-account API-key client scoped to one tenant);
``tests.agent_fixtures.stub_agent_runtime`` is enough here — no vision /
jinja capability needed for these tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api import agents as agents_mod
from control_plane.api._idempotency import request_digest as compute_request_digest
from control_plane.api.agents import ExternalRunRequest
from control_plane.api.external_events import build_events_response
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunEventRecord,
    RunIdempotencyConflict,
    RunInfo,
    RunStatus,
)
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_BASE_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "plain-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are a helpful assistant"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec_for(name: str) -> AgentSpec:
    doc = deepcopy(_BASE_SPEC)
    doc["metadata"]["name"] = name
    return AgentSpec.model_validate(doc)


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


def _external_jwt(tenant_id: UUID) -> str:
    # A real third-party caller is a service-account (API-key) principal —
    # matches test_external_sessions.py / test_external_api_contract.py.
    return make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-external-app",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )


@dataclass
class _ExternalCtx:
    app: Any
    tenant_id: UUID
    client: AsyncClient
    run_store: InMemoryRunStore


@pytest.fixture
async def _external_ctx() -> AsyncIterator[_ExternalCtx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    tenant_id = uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {_external_jwt(tenant_id)}"},
    ) as client:
        yield _ExternalCtx(app=app, tenant_id=tenant_id, client=client, run_store=run_store)


@pytest.fixture
def external_client(_external_ctx: _ExternalCtx) -> AsyncClient:
    return _external_ctx.client


@dataclass
class _Agent:
    code: str


@pytest.fixture
async def plain_agent(_external_ctx: _ExternalCtx) -> _Agent:
    await _external_ctx.app.state.agent_spec_repo.create(
        tenant_id=_external_ctx.tenant_id,
        spec=_spec_for("plain-bot"),
        spec_sha256="e" * 64,
        created_by="seed",
    )
    return _Agent(code="plain-bot")


@pytest.fixture
async def other_agent(_external_ctx: _ExternalCtx) -> _Agent:
    """A second, distinct agent in the same tenant — Task 13 裁定 1's pin:
    a key reused across two different agents must never silently return
    either agent's run (see ``test_same_key_different_agent_is_422``)."""
    await _external_ctx.app.state.agent_spec_repo.create(
        tenant_id=_external_ctx.tenant_id,
        spec=_spec_for("other-bot"),
        spec_sha256="f" * 64,
        created_by="seed",
    )
    return _Agent(code="other-bot")


BODY: dict[str, Any] = {"user_id": "u1", "input": "你好", "mode": "queue"}


@pytest.mark.asyncio
async def test_same_key_same_body_returns_same_run(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "order-8899"}
    first = await external_client.post(url, json=BODY, headers=h)
    second = await external_client.post(url, json=BODY, headers=h)
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    # External-API-v1 P2-a Task 15 — the external run endpoint's queue-mode
    # 202 body is now the {success, data, error} envelope (matching every
    # other external endpoint), for BOTH the first-time creation and the
    # idempotency-hit retry. See
    # test_idempotent_retry_envelope_matches_first_request below, which pins
    # that the two shapes are identical, not just that the run_id matches.
    first_body = first.json()
    second_body = second.json()
    assert first_body["success"] is True and first_body["error"] is None
    assert second_body["success"] is True and second_body["error"] is None
    assert first_body["data"]["run_id"] == second_body["data"]["run_id"]


@pytest.mark.asyncio
async def test_idempotent_retry_envelope_matches_first_request(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    """External-API-v1 P2-a Task 15 裁定 1 —— the idempotency-hit response
    (``_idempotent_run_response``'s queue branch) must be shaped EXACTLY
    like a first-time request's response (``spawn_run``'s queue branch
    called with ``envelope=True``): both the {success, data, error} envelope
    with the same top-level AND ``data`` key sets. A retry that came back
    flat while the first request came back enveloped — same endpoint, same
    202, two different shapes — would land precisely on the path a real
    client is most likely to hit on retry; this is the regression fence for
    that.
    """
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "shape-parity-1"}
    first = await external_client.post(url, json=BODY, headers=h)
    second = await external_client.post(url, json=BODY, headers=h)
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    first_body = first.json()
    second_body = second.json()
    assert set(first_body) == {"success", "data", "error"}
    assert set(second_body) == {"success", "data", "error"}
    assert set(first_body["data"]) == {"run_id", "thread_id", "status"}
    assert set(second_body["data"]) == {"run_id", "thread_id", "status"}
    assert second_body["data"]["run_id"] == first_body["data"]["run_id"]


@pytest.mark.asyncio
async def test_same_key_different_body_is_422(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "order-8899"}
    first = await external_client.post(url, json=BODY, headers=h)
    assert first.status_code == 202, first.text
    resp = await external_client.post(url, json={**BODY, "input": "换了内容"}, headers=h)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_same_key_different_agent_is_422(
    external_client: AsyncClient, plain_agent: _Agent, other_agent: _Agent
) -> None:
    """Task 13 裁定 1 —— agent code 只在 URL path 里,不在请求体里,唯一索引
    又只到 (tenant_id, idempotency_key)。若指纹只看 body,对 agent-A 用某个
    key 发一个 run,再对**完全不同**的 agent-B 用同一个 key + 同样的 body
    发,会被误判成"重复请求",直接把 agent-A 的 run 返回给 agent-B 的调用
    方 —— 第三方以为给 agent-B 派了活,拿回来的是 agent-A 的结果,而且没有
    任何错误信号。这条测试钉住:必须 422,不能返回任何一个 agent 的 run。
    """
    h = {"Idempotency-Key": "order-8899"}
    first = await external_client.post(f"/v1/agents/{plain_agent.code}/runs", json=BODY, headers=h)
    assert first.status_code == 202, first.text
    resp = await external_client.post(f"/v1/agents/{other_agent.code}/runs", json=BODY, headers=h)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # And the agent-A run must never leak into agent-B's response — a bug
    # that turned the 422 into a 202 carrying agent-A's run_id would still
    # be a silent cross-agent mix-up even though the status code differs.
    assert resp.json()["data"] is None


@pytest.mark.asyncio
async def test_no_key_creates_distinct_runs(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    a = await external_client.post(url, json=BODY)
    b = await external_client.post(url, json=BODY)
    assert a.status_code == 202 and b.status_code == 202
    assert a.json()["data"]["run_id"] != b.json()["data"]["run_id"]


@pytest.mark.asyncio
async def test_stream_mode_with_key_is_allowed(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    """Task 14 —— 放宽 Task 13 裁定 2 的 422。这条测试原来钉住"stream + key
    一律 422";现在钉住反面:stream 模式带一个全新 key 的首次请求正常执行
    (200,真正的 SSE 流,不是错误信封),不静默忽略 header,也不拒绝。同一
    条测试改断言而不是删掉,否则"stream + key 走到哪条路径"会失去覆盖。
    """
    url = f"/v1/agents/{plain_agent.code}/runs"
    resp = await external_client.post(
        url,
        json={"user_id": "u1", "input": "hi", "mode": "stream"},
        headers={"Idempotency-Key": "order-8899"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "X-Expert-Work-Run-Id" in resp.headers


@pytest.mark.asyncio
async def test_blank_key_is_422(external_client: AsyncClient, plain_agent: _Agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    resp = await external_client.post(url, json=BODY, headers={"Idempotency-Key": "   "})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"


@pytest.mark.asyncio
async def test_key_too_long_is_422(external_client: AsyncClient, plain_agent: _Agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    resp = await external_client.post(url, json=BODY, headers={"Idempotency-Key": "k" * 256})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"


@pytest.mark.asyncio
async def test_key_at_max_length_is_accepted(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    """255 chars is the documented ceiling (``MAX_IDEMPOTENCY_KEY_LEN``) —
    boundary check alongside the 256-char rejection above."""
    url = f"/v1/agents/{plain_agent.code}/runs"
    resp = await external_client.post(url, json=BODY, headers={"Idempotency-Key": "k" * 255})
    assert resp.status_code == 202, resp.text


# ---------------------------------------------------------------------------
# Concurrent single winner — what this file does NOT (fake-)test, and why.
#
# The brief's illustrative test drives two POSTs through ``asyncio.gather``
# and calls the result "concurrent single winner". Under the in-memory
# backend + a single event loop that label is misleading:
# ``InMemoryRunStore.create`` has no ``await`` between its dup-check and its
# insert, so the two coroutines never actually interleave inside it —
# ``gather`` just runs them one after the other on the same loop. What that
# version of the test actually exercises is "the second call hits the
# pre-check and takes the fast idempotent-return path" — already covered by
# ``test_same_key_same_body_returns_same_run`` above. Keeping a test named
# "concurrent single winner" that is secretly sequential would certify a
# broken implementation exactly as readily as a correct one — see Task 13's
# instructions on why that is not acceptable.
#
# The real "two requests raced past the pre-check and the store's atomic
# insert broke the tie" behaviour lives in ``RunStore.create`` itself, and is
# already proven against a REAL Postgres backend with 8 concurrent asyncpg
# sessions in ``packages/expert-work-runtime/tests/
# test_run_store_idempotency.py::test_concurrent_create_same_key_exactly_one_winner``
# (Task 12). Re-proving store-level atomicity here — at the HTTP layer, with
# an in-memory double that cannot race in the first place — would be
# redundant at best and misleading at worst.
#
# What Task 13 actually adds ON TOP of the store is the endpoint's
# catch-and-requery glue: ``agents.py`` must catch
# ``RunIdempotencyConflict`` raised out of ``spawn_run`` and return the
# *winner's* response instead of 500ing or silently dropping the loser's
# request. That is deterministic application logic, not a race — so it is
# tested deterministically below, with a wrapper store that forces exactly
# one conflict (seeding the "winning" row a concurrent request would have
# created first) and asserts the loser's HTTP call still gets back a clean
# 202 carrying the winner's run_id.
#
# NOTE for the task report: real concurrency at the HTTP/endpoint layer is
# NOT covered by this file — only by the store-layer test cited above.
# ---------------------------------------------------------------------------


class _ConflictOnceRunStore(InMemoryRunStore):
    """Force ``create`` to raise :class:`RunIdempotencyConflict` exactly once
    for ``conflict_key``, seeding ``winner_info`` first — deterministically
    drives the endpoint's catch-and-requery branch without relying on real
    concurrency (see the module comment above)."""

    def __init__(self, *, conflict_key: str, winner_info: RunInfo) -> None:
        super().__init__()
        self._conflict_key = conflict_key
        self._winner_info = winner_info
        self._fired = False

    async def create(self, info: RunInfo) -> None:
        if not self._fired and info.idempotency_key == self._conflict_key:
            self._fired = True
            await super().create(self._winner_info)
            raise RunIdempotencyConflict(
                tenant_id=info.tenant_id, idempotency_key=self._conflict_key
            )
        await super().create(info)


@pytest.mark.asyncio
async def test_conflict_requery_same_digest_returns_winner() -> None:
    """The endpoint catches ``RunIdempotencyConflict`` out of ``spawn_run``
    and returns the WINNER's run — not a 500, not the loser's own run_id —
    when the loser's request genuinely matches the winner's fingerprint.

    Security-review fix (Critical) —— this used to be
    ``test_endpoint_catches_store_conflict_and_returns_winner``, and its
    ``winner_info`` carried a placeholder ``request_digest =
    "irrelevant-to-this-branch"`` that never matched anything — the digest
    genuinely was irrelevant, because the endpoint's requery branch didn't
    check it at all before handing the winner's run back to ANY caller
    holding the same key, regardless of what that caller actually asked for.
    That is the cross-tenant-user data leak the fix closes (see the comment
    at the ``winner.request_digest != digest`` check in ``agents.py``). This
    test is the fix's regression fence in the OTHER direction: a same-digest
    loser — the genuine concurrent-retry case idempotency exists to serve —
    must still get the winner's run, not a wrongly-tightened 422. The digest
    below is computed for real from ``BODY`` (not a placeholder) so this
    fence is honest.
    """
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    tenant_id = uuid4()
    winner_run_id = uuid4()
    winner_thread_id = uuid4()
    now = datetime.now(UTC)
    matching_digest = compute_request_digest(ExternalRunRequest(**BODY), agent_code="plain-bot")
    winner_info = RunInfo(
        run_id=winner_run_id,
        tenant_id=tenant_id,
        thread_id=winner_thread_id,
        user_id=None,
        status=RunStatus.QUEUED,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
        idempotency_key="race-1",
        request_digest=matching_digest,
    )
    run_store = _ConflictOnceRunStore(conflict_key="race-1", winner_info=winner_info)
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id,
        spec=_spec_for("plain-bot"),
        spec_sha256="e" * 64,
        created_by="seed",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {_external_jwt(tenant_id)}"},
    ) as client:
        resp = await client.post(
            "/v1/agents/plain-bot/runs",
            json=BODY,
            headers={"Idempotency-Key": "race-1"},
        )
    assert resp.status_code == 202, resp.text
    # External-API-v1 P2-a Task 15 —— this is the third of the three 202
    # paths that must all envelope identically (裁定 1): the concurrent
    # conflict-loser requery, same ``_idempotent_run_response`` helper as
    # the plain cache-hit path above.
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    assert body["data"]["run_id"] == str(winner_run_id)
    assert body["data"]["thread_id"] == str(winner_thread_id)


@pytest.mark.asyncio
async def test_conflict_requery_digest_mismatch_is_422_queue_mode() -> None:
    """Security-review fix (Critical) —— cross-tenant-user data leak.

    An attacker (any same-tenant ``session:write`` key holder) guesses a
    victim's ``Idempotency-Key`` and races a request under it. If the
    attacker's request LOSES the race (the victim's insert wins), the
    endpoint used to hand the loser — the attacker — the winner's
    (the victim's) ``run_id`` / ``thread_id`` unconditionally: no digest
    comparison at all on this requery branch, unlike the pre-``spawn_run``
    cache-hit branch a few lines above it, which already 422s on mismatch.
    Queue mode leaks the run/session identifiers themselves (an attacker can
    then poll ``GET .../runs/{id}/events`` under its own key — session
    ownership is keyed on ``(tenant, agent, user_id)``, and the endpoint
    trusts the caller's own ``user_id``... but the run_id/thread_id
    themselves are already a target-identification leak on their own,
    handed to a caller who authored neither). The fix mirrors the cache-hit
    branch's check exactly: a digest mismatch means this key was reused for
    a genuinely different request, so it must 422, not disclose the
    winner's identifiers.
    """
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    tenant_id = uuid4()
    winner_run_id = uuid4()
    winner_thread_id = uuid4()
    now = datetime.now(UTC)
    winner_info = RunInfo(
        run_id=winner_run_id,
        tenant_id=tenant_id,
        thread_id=winner_thread_id,
        user_id=None,
        status=RunStatus.QUEUED,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
        idempotency_key="race-2",
        # A sha256 hex digest is 64 hex chars — this literal can never
        # collide with one, so it is guaranteed to mismatch whatever the
        # attacker's own (structurally valid) request body hashes to.
        request_digest="irrelevant-to-this-branch",
    )
    run_store = _ConflictOnceRunStore(conflict_key="race-2", winner_info=winner_info)
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id,
        spec=_spec_for("plain-bot"),
        spec_sha256="e" * 64,
        created_by="seed",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {_external_jwt(tenant_id)}"},
    ) as client:
        resp = await client.post(
            "/v1/agents/plain-bot/runs",
            json=BODY,
            headers={"Idempotency-Key": "race-2"},
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # The whole point of the fix: no trace of the winner's (victim's)
    # identifiers anywhere in the response body — a 422 that still echoed
    # them back "for debugging" would be the same leak in a different shape.
    assert str(winner_run_id) not in resp.text
    assert str(winner_thread_id) not in resp.text


@pytest.mark.asyncio
async def test_conflict_requery_digest_mismatch_is_422_stream_mode() -> None:
    """Security-review fix (Critical) —— the stream-mode leak is the
    severe one: a queue-mode leak hands the attacker identifiers it could
    maybe use to poll for more; a stream-mode leak hands the attacker the
    victim run's ENTIRE SSE event history in the same response — no further
    action needed. This test seeds the winner run's event store with a
    frame carrying an obvious secret marker and proves a digest-mismatched
    loser's 422 response contains neither that secret nor any winner
    identifier — the fix must stop ``_idempotent_run_response`` (and
    therefore ``build_events_response``'s replay) from ever being reached
    on a mismatch, not just filter its output after the fact.
    """
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    tenant_id = uuid4()
    winner_run_id = uuid4()
    winner_thread_id = uuid4()
    now = datetime.now(UTC)
    winner_info = RunInfo(
        run_id=winner_run_id,
        tenant_id=tenant_id,
        thread_id=winner_thread_id,
        user_id=None,
        status=RunStatus.SUCCESS,  # terminal → replay path, deterministic body
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=now,
        idempotency_key="race-3",
        request_digest="irrelevant-to-this-branch",
    )
    run_store = _ConflictOnceRunStore(conflict_key="race-3", winner_info=winner_info)
    run_event_store = InMemoryRunEventStore()
    secret = "VICTIM SECRET: bank balance is 12345"
    await run_event_store.append(
        RunEventRecord(
            run_id=winner_run_id,
            seq=1,
            event_name="updates",
            data={"content": secret},
            created_at_ms=int(now.timestamp() * 1000),
            created_at=now,
        )
    )
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id,
        spec=_spec_for("plain-bot"),
        spec_sha256="e" * 64,
        created_by="seed",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {_external_jwt(tenant_id)}"},
    ) as client:
        resp = await client.post(
            "/v1/agents/plain-bot/runs",
            json={"user_id": "u1", "input": "你好", "mode": "stream"},
            headers={"Idempotency-Key": "race-3"},
        )
    assert resp.status_code == 422, resp.text
    assert not resp.headers["content-type"].startswith("text/event-stream")
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert secret not in resp.text
    assert str(winner_run_id) not in resp.text
    assert str(winner_thread_id) not in resp.text


# ---------------------------------------------------------------------------
# Task 14 —— stream 模式重放。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_replay_attaches_to_original_run(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    """Brief Step 1 给定的场景——同键同体的第二次 stream 请求拿回同一个
    run_id,而不是新建一个。``X-Expert-Work-Stream-Mode`` 接受 replay 或
    live 两者之一:stub LLM 几乎瞬时执行完,第一次请求的响应体被 httpx 排空
    时 run 是否已转终态取决于调度时序,两种结果都是"重放成功"的合法证据。
    """
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "stream-1"}
    body = {"user_id": "u1", "input": "你好", "mode": "stream"}
    first = await external_client.post(url, json=body, headers=h)
    assert first.status_code == 200, first.text
    original = first.headers["X-Expert-Work-Run-Id"]
    original_session = first.headers["X-Expert-Work-Session-Id"]
    second = await external_client.post(url, json=body, headers=h)
    assert second.status_code == 200, second.text
    assert second.headers["X-Expert-Work-Run-Id"] == original
    assert second.headers["X-Expert-Work-Stream-Mode"] in {"replay", "live"}
    # External-API-v1 P2-a security-review fix (Important) —— the replay
    # response used to drop this header entirely (``build_events_response``
    # only set Run-Id / Stream-Mode); every docs-site page describing stream
    # mode tells the caller to read it to continue the conversation, so a
    # caller that only reads headers (not the SSE body) would get it on the
    # first response and never again on a retry.
    assert second.headers["X-Expert-Work-Session-Id"] == original_session


@pytest.mark.asyncio
async def test_stream_first_request_persists_idempotency_key(
    _external_ctx: _ExternalCtx, plain_agent: _Agent
) -> None:
    """裁定 1 的自证——stream 模式的第一次请求必须真的把
    ``idempotency_key`` / ``request_digest`` 落到 run 行上,不能只靠"第二次
    请求返回同一个 run_id"这种间接证据(那也可能是别的 bug 凑巧撞对)。直接
    查 ``run_store``,而不是看 HTTP 响应。
    """
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "stream-persist-1"}
    body = {"user_id": "u1", "input": "你好", "mode": "stream"}
    resp = await _external_ctx.client.post(url, json=body, headers=h)
    assert resp.status_code == 200, resp.text
    run_id = UUID(resp.headers["X-Expert-Work-Run-Id"])
    stored = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert stored is not None
    assert stored.idempotency_key == "stream-persist-1"
    assert stored.request_digest is not None


@pytest.mark.asyncio
async def test_stream_replay_without_event_store_still_streams(
    _external_ctx: _ExternalCtx, plain_agent: _Agent
) -> None:
    """裁定 4 —— 核实过 ``build_event_producer`` 对 ``event_store=None`` 早已
    优雅退化(终态 run 只吐一个 end 帧,见 ``_run_event_stream.py`` 的
    ``_stream_replay`` 文档字符串),不抛异常,所以不需要 brief 设想的那套
    JSON 降级信封 / ``stream_unavailable`` 字段(那是没人要的复杂度)。这里
    显式拔掉 ``app.state.run_event_store`` 复现"未配"的部署场景,证明重放
    路径不会 500,而是仍然拿到一个合法的 200 SSE 响应,run_id 与首次请求一致。
    """
    _external_ctx.app.state.run_event_store = None
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "stream-no-store-1"}
    body = {"user_id": "u1", "input": "你好", "mode": "stream"}
    first = await _external_ctx.client.post(url, json=body, headers=h)
    assert first.status_code == 200, first.text
    second = await _external_ctx.client.post(url, json=body, headers=h)
    assert second.status_code == 200, second.text
    assert second.headers["content-type"].startswith("text/event-stream")
    assert second.headers["X-Expert-Work-Run-Id"] == first.headers["X-Expert-Work-Run-Id"]


@pytest.mark.asyncio
async def test_build_events_response_is_terminal_reflects_run_status() -> None:
    """裁定 5 自证(自证要求变异 3)——``build_events_response`` 里的
    ``is_terminal`` 必须从传入 ``run.status`` 派生,不能写死成常量:写死为
    ``True`` 会让非终态 run 也走 replay 分支(截断一条本该继续的直播流);
    写死为 ``False`` 会让 ``test_external_events.py::
    test_events_replays_a_terminal_run`` 的 ``X-Expert-Work-Stream-Mode ==
    "replay"`` 断言翻红。直接调用函数、只看响应头,不排空 body —— 排空
    live 分支的 body 会挂起等待一个永远不会发布事件的 run_id。
    """
    now = datetime.now(UTC)
    running_run = RunInfo(
        run_id=uuid4(),
        tenant_id=uuid4(),
        thread_id=uuid4(),
        user_id=None,
        status=RunStatus.RUNNING,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
    )
    live_resp = await build_events_response(
        run=running_run,
        event_store=InMemoryRunEventStore(),
        stream_bridge=InMemoryStreamBridge(),
    )
    assert live_resp.headers["X-Expert-Work-Stream-Mode"] == "live"
    assert live_resp.headers["X-Expert-Work-Run-Id"] == str(running_run.run_id)

    terminal_run = replace(running_run, status=RunStatus.SUCCESS)
    replay_resp = await build_events_response(
        run=terminal_run,
        event_store=InMemoryRunEventStore(),
        stream_bridge=InMemoryStreamBridge(),
    )
    assert replay_resp.headers["X-Expert-Work-Stream-Mode"] == "replay"


# ---------------------------------------------------------------------------
# 对话条目 program PR3 —— stream_format 的两个入口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_format_reaches_spawn_run(
    external_client: AsyncClient, plain_agent: _Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发起对话时选的流形态必须一路传到 SSE 出口。

    ``ExternalRunRequest`` 是 ``extra="forbid"``,所以这条测试还顺带证明这个字段
    真的被声明了 —— 没声明的话请求在到达 ``spawn_run`` 之前就 422 了。
    """
    real = agents_mod.spawn_run
    seen: list[Any] = []

    async def spy(**kwargs: Any) -> Any:
        seen.append(kwargs.get("stream_format"))
        return await real(**kwargs)

    monkeypatch.setattr(agents_mod, "spawn_run", spy)

    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "input": "你好", "mode": "queue", "stream_format": "items"},
    )

    assert resp.status_code == 202, resp.text
    assert seen == ["items"]


@pytest.mark.asyncio
async def test_stream_format_defaults_to_legacy_on_spawn(
    external_client: AsyncClient, plain_agent: _Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传时是 legacy —— 已在对接的第三方零感知。"""
    real = agents_mod.spawn_run
    seen: list[Any] = []

    async def spy(**kwargs: Any) -> Any:
        seen.append(kwargs.get("stream_format"))
        return await real(**kwargs)

    monkeypatch.setattr(agents_mod, "spawn_run", spy)

    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs", json={"user_id": "u1", "mode": "queue"}
    )

    assert resp.status_code == 202, resp.text
    assert seen == ["legacy"]


@pytest.mark.asyncio
async def test_stream_format_survives_an_idempotent_replay(
    external_client: AsyncClient, plain_agent: _Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Idempotency-Key`` 命中时的重放也要给同一种流形态。

    这是四个入口里最容易漏的一个:重放走的是另一个函数,漏接线时同一个客户端的
    重试会拿回 legacy,而它第一次拿到的是条目。
    """
    real = agents_mod._idempotent_run_response
    seen: list[Any] = []

    async def spy(run: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("stream_format"))
        return await real(run, **kwargs)

    monkeypatch.setattr(agents_mod, "_idempotent_run_response", spy)

    body = {"user_id": "u1", "input": "你好", "mode": "queue", "stream_format": "items"}
    headers = {"Idempotency-Key": "items-replay-1"}
    first = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs", json=body, headers=headers
    )
    second = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs", json=body, headers=headers
    )

    assert first.status_code == 202 and second.status_code == 202, second.text
    # 先证兄弟事件在:第一次没走重放,第二次才走 —— 所以这条不是空转。
    assert seen == ["items"]


@pytest.mark.asyncio
async def test_changing_only_stream_format_reuses_the_key(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    """幂等指纹是整个请求体的哈希,所以同一个 key 只改流形态会被拒。

    这是要写进文档的一条副作用:同一个 ``Idempotency-Key`` 必须对应同一个请求。
    """
    headers = {"Idempotency-Key": "items-switch-1"}
    first = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "input": "你好", "mode": "queue"},
        headers=headers,
    )
    second = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "input": "你好", "mode": "queue", "stream_format": "items"},
        headers=headers,
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_unknown_stream_format_is_422(
    external_client: AsyncClient, plain_agent: _Agent
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "mode": "queue", "stream_format": "conversation"},
    )
    assert resp.status_code == 422, resp.text
