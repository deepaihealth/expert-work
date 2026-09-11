"""Tests for ``GET /v1/sessions/{thread_id}/runs/{run_id}/trace`` — Batch 4b Task 2.

Mirrors the ``get_run`` fixture pattern in ``test_runs_api.py``: a real
FastAPI app built via ``create_app`` with an injected stub
``AgentRuntime`` (so no real LLM / MCP wiring is needed), exercised over
``httpx.ASGITransport``. The lifespan never runs under ``ASGITransport``
(the ``langfuse_read_client`` it would build lives entirely inside
``lifespan``), so tests inject a fake client directly onto
``app.state.langfuse_read_client`` — exactly how the endpoint reads it
in production (``getattr(request.app.state, "langfuse_read_client",
None)``), and exactly how other tests seed ``app.state.run_store`` /
``app.state.approval_store`` directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langfuse.api import NotFoundError

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_DEFAULT_TENANT = DEFAULT_DEV_TENANT_ID

_AGENT_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


class _FakeTraceApi:
    """Stands in for the real SDK's ``client.api.trace`` resource."""

    def __init__(self, *, trace: Any = None, exc: BaseException | None = None) -> None:
        self._trace = trace
        self._exc = exc
        self.calls = 0

    def get(self, trace_id: str) -> Any:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._trace


class _FakeLangfuseClient:
    """Stands in for a real ``langfuse.Langfuse`` instance — only the
    ``.api.trace.get`` surface ``fetch_and_normalize`` touches."""

    def __init__(self, *, trace: Any = None, exc: BaseException | None = None) -> None:
        self.trace_api = _FakeTraceApi(trace=trace, exc=exc)
        self.api = SimpleNamespace(trace=self.trace_api)


def _fake_trace() -> SimpleNamespace:
    """One GENERATION observation — enough for ``normalize_trace`` to
    produce a single ``ok`` span (see ``test_trace_facade_normalize.py``
    for the full normalization behaviour, already covered there)."""
    obs = SimpleNamespace(
        id="obs-1",
        type="GENERATION",
        name="llm_call",
        parent_observation_id=None,
        latency=1.2,
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        model="glm-4.6",
        prompt_tokens=10,
        completion_tokens=5,
        calculated_total_cost=0.01,
        input="hi",
        output="hello",
    )
    return SimpleNamespace(
        name="expert_work.session.run",
        latency=1.5,
        total_cost=0.01,
        observations=[obs],
    )


@pytest.fixture
async def trace_client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(run_store=run_store),
        run_repo=run_store,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        await client.post(
            "/v1/agents",
            json={"manifest_yaml": _AGENT_YAML},
            headers={"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"},
        )
        yield client


async def _create_session(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["thread_id"])


async def _seed_run(client: AsyncClient, *, thread_id: str, trace_id: str | None) -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC)
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    await app.state.run_store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=_DEFAULT_TENANT,
            thread_id=UUID(thread_id),
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
            trace_id=trace_id,
        )
    )
    return run_id


def _owner_headers(*, roles: tuple[str, ...] = ("admin",)) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="owner", roles=roles)
    }


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_owner_with_client_returns_ok_and_spans(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=_fake_trace())

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["spans"]) == 1
    assert body["spans"][0]["kind"] == "llm"


@pytest.mark.asyncio
async def test_trace_plain_owner_not_admin_still_200(trace_client: AsyncClient) -> None:
    """No system_admin (or even tenant-admin) requirement — a plain
    ``viewer`` who owns the thread gets the trace, same as ``get_run``.

    B-49 —— 建会话现在要 ``session:write``,viewer 建不了。用同一个 subject
    (``owner``)的 operator 身份建、再拿 viewer 身份读:归属关系不变,被证的还是
    「一个只读的 viewer,只要是本人的会话,就能读到 trace」。
    """
    headers = _owner_headers(roles=("viewer",))
    thread_id = await _create_session(trace_client, _owner_headers(roles=("operator",)))
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=_fake_trace())

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# ownership gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_non_owner_returns_404(trace_client: AsyncClient) -> None:
    # B-49 —— owner 的角色从 viewer 改成 operator:它只用来**建**会话,而建会话
    # 现在要 ``session:write``。入侵者仍是 viewer(GET /trace 没有角色闸),所以
    # 拦住它的仍然是归属闸的 404,判据没动。
    owner_headers = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="user-a", roles=("operator",))
    }
    intruder_headers = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="user-b", roles=("viewer",))
    }
    thread_id = await _create_session(trace_client, owner_headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    fake_client = _FakeLangfuseClient(trace=_fake_trace())
    app.state.langfuse_read_client = fake_client

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=intruder_headers
    )
    assert resp.status_code == 404
    # The ownership gate 404s before ever touching the Langfuse client.
    assert fake_client.trace_api.calls == 0


# ---------------------------------------------------------------------------
# degrade branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_no_trace_id_skips_client_call(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id=None)
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    fake_client = _FakeLangfuseClient(trace=_fake_trace())
    app.state.langfuse_read_client = fake_client

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "no_trace"}
    assert fake_client.trace_api.calls == 0


@pytest.mark.asyncio
async def test_trace_client_none_returns_unavailable(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    # No app.state.langfuse_read_client set — mirrors production when
    # the lifespan never wired one (missing env) or (in tests) never ran.

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_trace_not_found_returns_not_ready(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(exc=NotFoundError(body="not found"))

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_trace_other_exception_returns_unavailable(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(exc=RuntimeError("boom"))

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_trace_none_latency_returns_not_ready(trace_client: AsyncClient) -> None:
    """``trace.get`` succeeds but Langfuse hasn't finished aggregating the
    trace yet (``latency`` is ``None`` until it closes out) — the common
    just-after-run polling window the debug console hits. Must degrade to
    ``not_ready``, never a 500 (硬约束「降级永不 500」)."""
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    trace = _fake_trace()
    trace.latency = None
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=trace)

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_trace_present_but_no_spans_returns_not_ready(trace_client: AsyncClient) -> None:
    """Langfuse ingestion is NOT atomic under load: a multi-span run's trace
    root closes (``latency`` populated → passes the None-latency guard) BEFORE
    its child observations land, so ``trace.get`` returns a trace whose
    ``observations`` are still empty. That normalizes to zero renderable spans,
    which the waterfall would draw as a bare time-axis with no bars. Degrade to
    ``not_ready`` so the console shows the actionable refresh card instead."""
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    trace = _fake_trace()
    trace.observations = []  # root closed (latency set) but children not ingested yet
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=trace)

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_trace_orphaned_spans_returns_not_ready(trace_client: AsyncClient) -> None:
    """Same non-atomic ingestion, subtler shape: child observations land BEFORE
    their session-root parent. ``trace.get`` returns a trace whose spans all
    point at a ``parent_observation_id`` that isn't in the set (the root hasn't
    ingested yet) → the frontend's root-first tree walk renders nothing (a bare
    axis). Must degrade to ``not_ready``, not report a bogus ``ok`` with an
    empty waterfall."""
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]

    def _child(obs_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=obs_id,
            type="SPAN",
            name="expert_work.orchestrator.tool_call",
            parent_observation_id="root-not-ingested-yet",  # dangles outside the set
            latency=1.0,
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
            model=None,
            prompt_tokens=0,
            completion_tokens=0,
            calculated_total_cost=0.0,
            input=None,
            output=None,
        )

    trace = _fake_trace()
    trace.observations = [_child("child-a"), _child("child-b")]
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=trace)

    resp = await trace_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "not_ready"}


# ---------------------------------------------------------------------------
# fetch_span_raw — facade layer (Task 4: "查看原文")
# ---------------------------------------------------------------------------


def test_fetch_span_raw_returns_full_untruncated() -> None:
    from control_plane.api.trace_facade import fetch_span_raw

    big = [{"type": "system", "content": "y" * 50000}]
    obs = SimpleNamespace(
        id="o9",
        type="GENERATION",
        name="llm_call",
        parent_observation_id=None,
        start_time=None,
        latency=1.0,
        input=big,
        output=None,
    )
    trace = _fake_trace()
    trace.observations = [obs]
    client = _FakeLangfuseClient(trace=trace)

    out = fetch_span_raw(client, "trace-1", "o9", "input")

    assert out is not None
    assert len(out) >= 50000
    assert "…(已截断)" not in out
    assert out.startswith("[system]\n")


def test_fetch_span_raw_missing_span_returns_none() -> None:
    from control_plane.api.trace_facade import fetch_span_raw

    client = _FakeLangfuseClient(trace=_fake_trace())

    assert fetch_span_raw(client, "trace-1", "nope", "input") is None


def test_fetch_span_raw_invalid_field_returns_none() -> None:
    from control_plane.api.trace_facade import fetch_span_raw

    client = _FakeLangfuseClient(trace=_fake_trace())

    assert fetch_span_raw(client, "trace-1", "obs-1", "bogus") is None


def test_fetch_span_raw_unserializable_value_returns_none_not_500() -> None:
    """非 JSON-可序列化字段(input/output 类型是 Any)必须降级 None → 404,
    绝不抛(硬约束「降级永不 500」)。守住 fetch_span_raw 序列化的 try/except。"""
    from control_plane.api.trace_facade import fetch_span_raw

    class _Unserializable:
        pass

    obs = SimpleNamespace(
        id="o9",
        type="GENERATION",
        name="llm_call",
        parent_observation_id=None,
        start_time=None,
        latency=1.0,
        input=_Unserializable(),
        output=None,
    )
    trace = _fake_trace()
    trace.observations = [obs]
    client = _FakeLangfuseClient(trace=trace)

    assert fetch_span_raw(client, "trace-1", "o9", "input") is None


def test_fetch_span_raw_no_client_returns_none() -> None:
    from control_plane.api.trace_facade import fetch_span_raw

    assert fetch_span_raw(None, "trace-1", "obs-1", "input") is None


def test_fetch_span_raw_client_exception_returns_none() -> None:
    from control_plane.api.trace_facade import fetch_span_raw

    client = _FakeLangfuseClient(exc=RuntimeError("boom"))

    assert fetch_span_raw(client, "trace-1", "obs-1", "input") is None


# ---------------------------------------------------------------------------
# GET .../trace/raw — endpoint layer (ownership gate mirrors get_run_trace)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_raw_owner_returns_full_content(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=_fake_trace())

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"span": "obs-1", "field": "output"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"spanId": "obs-1", "field": "output", "content": "hello"}


@pytest.mark.asyncio
async def test_trace_raw_non_owner_returns_404(trace_client: AsyncClient) -> None:
    # B-49 —— owner 的角色从 viewer 改成 operator:它只用来**建**会话,而建会话
    # 现在要 ``session:write``。入侵者仍是 viewer(GET /trace 没有角色闸),所以
    # 拦住它的仍然是归属闸的 404,判据没动。
    owner_headers = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="user-a", roles=("operator",))
    }
    intruder_headers = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="user-b", roles=("viewer",))
    }
    thread_id = await _create_session(trace_client, owner_headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    fake_client = _FakeLangfuseClient(trace=_fake_trace())
    app.state.langfuse_read_client = fake_client

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"span": "obs-1", "field": "output"},
        headers=intruder_headers,
    )
    assert resp.status_code == 404
    # The ownership gate 404s before ever touching the Langfuse client.
    assert fake_client.trace_api.calls == 0


@pytest.mark.asyncio
async def test_trace_raw_no_trace_id_returns_404(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id=None)
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    fake_client = _FakeLangfuseClient(trace=_fake_trace())
    app.state.langfuse_read_client = fake_client

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"span": "obs-1", "field": "output"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert fake_client.trace_api.calls == 0


@pytest.mark.asyncio
async def test_trace_raw_unknown_span_returns_404(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=_fake_trace())

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"span": "nope", "field": "output"},
        headers=headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trace_raw_invalid_field_returns_404(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    app = trace_client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.langfuse_read_client = _FakeLangfuseClient(trace=_fake_trace())

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"span": "obs-1", "field": "bogus"},
        headers=headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trace_raw_client_none_returns_404(trace_client: AsyncClient) -> None:
    headers = _owner_headers()
    thread_id = await _create_session(trace_client, headers)
    run_id = await _seed_run(trace_client, thread_id=thread_id, trace_id="trace-1")
    # No app.state.langfuse_read_client set — mirrors production when the
    # lifespan never wired one (missing env) or (in tests) never ran.

    resp = await trace_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"span": "obs-1", "field": "output"},
        headers=headers,
    )
    assert resp.status_code == 404
