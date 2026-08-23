"""End-to-end tests for the real SSE run trigger (control-plane cutover).

The endpoint runs a real orchestrator graph in-process. To keep the
test off the network the injected :class:`AgentRuntime`'s builder
returns a :class:`BuiltAgent` over a *fake* ``LLMCaller`` — the
control-plane wiring (manifest load → run spawn → SSE drain) is what's
under test, not a real LLM call.

SSE vocabulary is ``metadata`` / ``updates`` / ``end`` (amended ADR B-4).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
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


class _SeedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


async def _seed_thread_messages(
    checkpointer: InMemorySaver, thread_id: str, messages: list[BaseMessage]
) -> None:
    """Write one checkpoint holding ``messages`` for ``thread_id``.

    Mirrors how a real run leaves a thread's durable checkpoint, so the resume
    ``/messages`` endpoint (which reads the checkpointer directly) has something
    to surface.
    """
    graph = StateGraph(_SeedState)
    graph.add_node("n", lambda _state: {"messages": []})
    graph.add_edge(START, "n")
    seeded = graph.compile(checkpointer=checkpointer)
    await seeded.ainvoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
    )


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def runs_client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    # Stream H.3 PR 1 — share one RunStore between the runtime's manager
    # and ``app.state.run_store`` so ``GET /v1/runs`` sees rows created
    # by the SSE worker (test fixture parity with production wiring).
    run_store = InMemoryRunStore()
    # Stream H.3 PR 3 — same parity for the durable SSE event store so
    # the events endpoint's replay path returns rows the worker just
    # wrote.
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        yield client


def _parse_sse(body: str) -> list[tuple[str, object]]:
    """Parse an SSE body into ``(event, data)`` pairs.

    Comment frames (``: heartbeat``) and any frame without a ``data:``
    line are skipped — only real events are returned.
    """
    events: list[tuple[str, object]] = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event_type = ""
        data_payload: str | None = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data_payload = line[len("data: ") :]
        if data_payload is None:
            continue  # comment-only frame, e.g. ": heartbeat"
        events.append((event_type, json.loads(data_payload)))
    return events


async def _create_session(client: AsyncClient) -> str:
    response = await client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert response.status_code == 201
    return str(response.json()["data"]["thread_id"])


# ---------------------------------------------------------------------------
# Playground resume (#6) — thread message history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_messages_404_for_unknown(runs_client: AsyncClient) -> None:
    response = await runs_client.get("/v1/sessions/00000000-0000-0000-0000-0000000000ff/messages")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_thread_messages_empty_for_fresh_thread(runs_client: AsyncClient) -> None:
    # A brand-new thread has no checkpoint history (and best-effort read
    # degrades to empty), so the endpoint returns an empty list, not an error.
    thread_id = await _create_session(runs_client)
    response = await runs_client.get(f"/v1/sessions/{thread_id}/messages")
    assert response.status_code == 200
    assert response.json()["data"]["messages"] == []


@pytest.mark.asyncio
async def test_thread_messages_explicit_home_tenant_ok(runs_client: AsyncClient) -> None:
    """Fast-follow — ``?tenant_id=`` with the caller's own tenant is a no-op
    (the conversation-detail transcript always sends the thread's tenant)."""
    thread_id = await _create_session(runs_client)
    response = await runs_client.get(
        f"/v1/sessions/{thread_id}/messages", params={"tenant_id": str(_DEFAULT_TENANT)}
    )
    assert response.status_code == 200
    assert response.json()["data"]["messages"] == []


@pytest.mark.asyncio
async def test_thread_messages_foreign_tenant_forbidden(runs_client: AsyncClient) -> None:
    """A plain tenant admin asking for another tenant's transcript is
    rejected by the scope gate (only a system_admin may cross)."""
    thread_id = await _create_session(runs_client)
    response = await runs_client.get(
        f"/v1/sessions/{thread_id}/messages",
        params={"tenant_id": str(uuid4())},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_thread_messages_reads_durable_checkpoint_directly() -> None:
    """Regression — resume history reads the durable checkpointer DIRECTLY.

    The old endpoint rebuilt the agent and called ``built.graph.aget_state``; if
    that build bound a different (empty) checkpointer than the durable one, the
    history came back empty even though the checkpoint held the turns. Seed a
    checkpointer, point the runtime at it, and assert the user/assistant turns
    surface while system/tool/empty-AI messages are filtered out.
    """
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    checkpointer = InMemorySaver()
    runtime = stub_agent_runtime(run_store=run_store)
    runtime.durable_checkpointer = checkpointer
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=runtime,
        run_repo=run_store,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        thread_id = await _create_session(client)

        # Seed the durable checkpoint for this thread with a mixed transcript.
        await _seed_thread_messages(
            checkpointer,
            thread_id,
            [
                SystemMessage(content="sys prompt"),
                HumanMessage(content="今天几号"),
                AIMessage(content=""),  # tool-call-only turn, no text
                ToolMessage(content="2026-06-30", tool_call_id="t1"),
                AIMessage(content="今天是 2026年6月30日"),
            ],
        )

        response = await client.get(f"/v1/sessions/{thread_id}/messages")
        assert response.status_code == 200
        assert response.json()["data"]["messages"] == [
            {"role": "user", "content": "今天几号", "channel": None},
            {"role": "assistant", "content": "今天是 2026年6月30日", "channel": "final"},
        ]


@pytest.mark.asyncio
async def test_thread_messages_self_read_hides_scaffolding_but_record_keeps_it() -> None:
    """RT-2 PR-4 (RT-ADR-9), Option A — the same-tenant UI bubble view drops
    orchestrator scaffolding tagged ``expert_work_hide_from_ui`` (the CM-1
    ``<recovery-advisory>``), so it never renders as a user turn. The durable
    checkpoint still holds it (the endpoint reads with ``include_hidden=False``
    only for the bubble view; the search/audit mirror + cross-tenant audit
    drill-in read faithfully) — verified here by asserting the checkpoint row
    survives while the rendered transcript omits it."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    checkpointer = InMemorySaver()
    runtime = stub_agent_runtime(run_store=run_store)
    runtime.durable_checkpointer = checkpointer
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=runtime,
        run_repo=run_store,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        thread_id = await _create_session(client)

        await _seed_thread_messages(
            checkpointer,
            thread_id,
            [
                HumanMessage(content="今天几号"),
                HumanMessage(
                    content="<recovery-advisory>internal</recovery-advisory>",
                    additional_kwargs={"expert_work_hide_from_ui": True},
                ),
                AIMessage(content="今天是 2026年6月30日"),
            ],
        )

        # UI bubble view (same-tenant self-read) omits the scaffolding turn.
        response = await client.get(f"/v1/sessions/{thread_id}/messages")
        assert response.status_code == 200
        assert response.json()["data"]["messages"] == [
            {"role": "user", "content": "今天几号", "channel": None},
            {"role": "assistant", "content": "今天是 2026年6月30日", "channel": "final"},
        ]

        # ...but the faithful record still carries it — the mirror/audit read
        # (``include_hidden=True``, the default) sees all three turns.
        from control_plane.transcript import read_turns

        faithful = await read_turns(checkpointer, UUID(thread_id))
        assert [t.content for t in faithful] == [
            "今天几号",
            "<recovery-advisory>internal</recovery-advisory>",
            "今天是 2026年6月30日",
        ]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_emits_metadata_updates_end(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    response = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs", json={"input": "review the PR"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-expert-work-run-id"]

    events = _parse_sse(response.text)
    types = [e[0] for e in events]
    # Stream opens with metadata, closes with end, an updates in between.
    assert types[0] == "metadata"
    assert types[-1] == "end"
    assert "updates" in types

    metadata = events[0][1]
    assert isinstance(metadata, dict)
    assert metadata["thread_id"] == thread_id
    assert metadata["run_id"] == response.headers["x-expert-work-run-id"]


@pytest.mark.asyncio
async def test_run_streams_the_agent_reply(runs_client: AsyncClient) -> None:
    """The fake LLM's reply reaches the client inside an updates event."""
    thread_id = await _create_session(runs_client)
    response = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hello"})
    assert response.status_code == 200
    body = response.text
    # The fake agent's content is carried in an updates chunk.
    assert "stub agent reply" in body


@pytest.mark.asyncio
async def test_run_stream_keeps_system_prompt_frame(runs_client: AsyncClient) -> None:
    """PR-A.3 Task 8 — the ``system_prompt`` frame is console-only filtering,
    not a global one: ``spawn_run`` doesn't pass ``hide_events`` for the
    console session-run endpoint, so it must still show up here."""
    thread_id = await _create_session(runs_client)
    response = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hello"})
    assert response.status_code == 200
    assert "event: system_prompt" in response.text


@pytest.mark.asyncio
async def test_run_emits_session_write_audit(
    runs_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    thread_id = await _create_session(runs_client)
    response = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hi"})
    assert response.status_code == 200
    _ = response.text  # exhaust the stream

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    run_events = [
        r
        for r in page.entries
        if r.action.value == "session:write"
        and r.resource_id == thread_id
        and r.details.get("stage") == "run.start"
    ]
    assert run_events, "expected a run.start audit row"
    assert run_events[0].details["input_len"] == 2


@pytest.mark.asyncio
async def test_run_emits_run_completed_audit(
    runs_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """The run_agent worker writes a run:completed row at run end (F-3)."""
    thread_id = await _create_session(runs_client)
    response = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hi"})
    assert response.status_code == 200
    _ = response.text  # exhaust the stream — the worker has finished by EOF

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    completed = [r for r in page.entries if r.action.value == "run:completed"]
    assert len(completed) == 1
    entry = completed[0]
    assert entry.resource_id == thread_id
    assert entry.details["status"] == "success"
    assert entry.details["run_id"] == response.headers["x-expert-work-run-id"]


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_against_unknown_session_returns_404(
    runs_client: AsyncClient,
) -> None:
    response = await runs_client.post(
        "/v1/sessions/00000000-0000-0000-0000-000000000099/runs",
        json={},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_run_against_cancelled_session_returns_409(
    runs_client: AsyncClient,
) -> None:
    thread_id = await _create_session(runs_client)
    await runs_client.post(f"/v1/sessions/{thread_id}:cancel", json={})
    response = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_run_against_paused_session_returns_409(
    runs_client: AsyncClient,
) -> None:
    thread_id = await _create_session(runs_client)
    await runs_client.post(f"/v1/sessions/{thread_id}:pause", json={})
    response = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={})
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Stream B + E acceptance: create agent → session → run → real SSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_acceptance_flow(runs_client: AsyncClient) -> None:
    """End-to-end happy path: HTTP run trigger → background graph →
    streamed SSE — the first M0 vertical slice."""
    agents = await runs_client.get("/v1/agents")
    assert agents.status_code == 200
    assert agents.json()["data"]["total"] == 1

    session_response = await runs_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert session_response.status_code == 201
    thread_id = session_response.json()["data"]["thread_id"]

    run_response = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs",
        json={"input": "review the PR"},
    )
    assert run_response.status_code == 200
    events = _parse_sse(run_response.text)
    types = [e[0] for e in events]
    assert types[0] == "metadata"
    assert types[-1] == "end"
    assert "updates" in types


# ---------------------------------------------------------------------------
# per-user isolation — Stream J.14
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_cannot_run_another_users_session(runs_client: AsyncClient) -> None:
    """A run trigger on another user's session is rejected (404)."""
    user_a = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="user-a", roles=("viewer",))
    }
    user_b = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="user-b", roles=("viewer",))
    }
    create = await runs_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        headers=user_a,
    )
    assert create.status_code == 201
    thread_id = create.json()["data"]["thread_id"]

    intruder = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs", json={"input": "hi"}, headers=user_b
    )
    assert intruder.status_code == 404

    owner = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs", json={"input": "hi"}, headers=user_a
    )
    assert owner.status_code == 200


# ---------------------------------------------------------------------------
# cross-tenant SSE isolation — Stream K.K2 (Mini-ADR K-2)
#
# SSE only flows out of POST /v1/sessions/{thread_id}/runs; there is no
# reconnect endpoint and run_id is server-generated uuid4. The route
# resolves the thread via threads.get(thread_id, tenant_id=jwt_tenant)
# at api/runs.py:191, which 404s when the thread does not belong to
# the caller's tenant. Mini-ADR K-2 commits NOT to add a duplicate
# SSE-layer guard; this test locks the invariant so any new SSE entry
# path must keep it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_cross_tenant_sse_rejected(runs_client: AsyncClient) -> None:
    """A tenant B caller cannot stream a tenant A thread's runs."""
    from uuid import uuid4

    # Create the thread as tenant A (the default fixture tenant).
    create = await runs_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert create.status_code == 201
    tenant_a_thread_id = create.json()["data"]["thread_id"]

    # Mint a fresh tenant B JWT. The signature uses the same dev key
    # the verifier accepts, so the call is authenticated — the
    # rejection has to come from tenant isolation, not auth failure.
    tenant_b_id = uuid4()
    tenant_b_headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=tenant_b_id)}"}

    response = await runs_client.post(
        f"/v1/sessions/{tenant_a_thread_id}/runs",
        json={"input": "trying to peek"},
        headers=tenant_b_headers,
    )

    # 404 from the thread-ownership check — tenant B caller never
    # learns whether the thread exists.
    assert response.status_code == 404, response.text
    # SSE never started — the response body is JSON, not text/event-stream.
    # Regression where the stream opens before tenant check would flip
    # this assertion.
    assert "text/event-stream" not in response.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# GET run — Stream J.8 (Mini-ADR J-24)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_surfaces_pending_approval(runs_client: AsyncClient) -> None:
    """A run with an ``agent_approval`` row reports its pending approval."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from expert_work.protocol import ApprovalRecord

    thread_id = await _create_session(runs_client)
    run_id = uuid4()
    # Seed a pending approval row directly into the in-memory store.
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    await app.state.approval_store.create(
        ApprovalRecord(
            id=uuid4(),
            tenant_id=DEFAULT_DEV_TENANT_ID,
            run_id=run_id,
            thread_id=thread_id,  # type: ignore[arg-type]
            request_id="approval:seed",
            node="tools",
            reason_kind="policy_gate",
            action_summary="approval-gated tool 'send_email'",
            proposed_args={"to": "ops@example.com"},
            requested_at=now,
            timeout_at=now + timedelta(hours=24),
        )
    )

    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    pending = body["pending_approval"]
    assert pending is not None
    assert pending["reason_kind"] == "policy_gate"
    assert pending["action_summary"] == "approval-gated tool 'send_email'"
    assert pending["proposed_args"] == {"to": "ops@example.com"}
    # RT-6 — an unbound approval with no workspace writes reports empty/false.
    assert pending["binding_digest"] == ""
    assert pending["workspace_drift"] is False


@pytest.mark.asyncio
async def test_get_run_pending_surfaces_binding_and_drift(runs_client: AsyncClient) -> None:
    """RT-6 — the pending view carries the Tier A receipt + Tier B drift signal."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from expert_work.protocol import ApprovalRecord, UserWorkspace

    thread_id = await _create_session(runs_client)
    run_id, user_id = uuid4(), uuid4()
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    await app.state.approval_store.create(
        ApprovalRecord(
            id=uuid4(),
            tenant_id=DEFAULT_DEV_TENANT_ID,
            user_id=user_id,
            run_id=run_id,
            thread_id=thread_id,  # type: ignore[arg-type]
            request_id="approval:bind",
            node="tools",
            reason_kind="policy_gate",
            action_summary="approval-gated tool 'bash'",
            proposed_args={"command": "./deploy.sh"},
            requested_at=now,
            timeout_at=now + timedelta(hours=24),
            binding_digest="deadbeefcafef00d",
        )
    )
    # Seed a workspace whose last write lands AFTER the approval request → drift.
    ws_store = app.state.user_workspace_store
    ws_store._rows[(DEFAULT_DEV_TENANT_ID, user_id)] = UserWorkspace(
        id=uuid4(),
        tenant_id=DEFAULT_DEV_TENANT_ID,
        user_id=user_id,
        volume_name="vol",
        last_write_at=now + timedelta(minutes=1),
    )

    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
    assert resp.status_code == 200
    pending = resp.json()["pending_approval"]
    assert pending["binding_digest"] == "deadbeefcafef00d"
    assert pending["workspace_drift"] is True


@pytest.mark.asyncio
async def test_get_run_unknown_returns_404(runs_client: AsyncClient) -> None:
    """An unknown run id on a known thread — no approval, not in RunManager."""
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_run_cross_tenant_returns_404(runs_client: AsyncClient) -> None:
    """A tenant B caller cannot read a tenant A thread's run."""
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    tenant_b_headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{uuid4()}", headers=tenant_b_headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_run_falls_back_to_durable_run_store(runs_client: AsyncClient) -> None:
    """A finished run dropped from RunManager (5-min TTL / restart) stays
    queryable — GET reads the durable ``agent_run`` row (Mini-ADR J-41)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    run_id = uuid4()
    # Seed a finished run straight into the durable store — it is NOT in
    # the in-memory RunManager, mimicking a run past its 5-minute TTL.
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    await app.state.run_store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=DEFAULT_DEV_TENANT_ID,
            thread_id=UUID(thread_id),
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
        )
    )

    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["pending_approval"] is None


@pytest.mark.asyncio
async def test_get_run_includes_token_summary(runs_client: AsyncClient) -> None:
    """GET run carries the per-run token summary (joined by trace_id)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from expert_work.persistence.token_usage_store import TokenUsageRecord
    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    run_id = uuid4()
    trace = "cafef00dcafef00dcafef00dcafef00d"
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    await app.state.run_store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=DEFAULT_DEV_TENANT_ID,
            thread_id=UUID(thread_id),
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
            trace_id=trace,
        )
    )
    for inp, out in ((100, 40), (50, 10)):
        await app.state.token_usage_store.insert(
            TokenUsageRecord(
                tenant_id=DEFAULT_DEV_TENANT_ID,
                agent_name="code-reviewer",
                agent_version="1.0.0",
                model="claude-sonnet-4-6",
                trace_id=trace,
                input_tokens=inp,
                output_tokens=out,
            )
        )

    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
    assert resp.status_code == 200
    tokens = resp.json()["tokens"]
    assert tokens["input_tokens"] == 150
    assert tokens["output_tokens"] == 50
    assert tokens["total_tokens"] == 200
    assert tokens["llm_calls"] == 2
    assert tokens["models"] == ["claude-sonnet-4-6"]


@pytest.mark.asyncio
async def test_list_runs_enriches_tokens_and_filters_by_q(runs_client: AsyncClient) -> None:
    """GET /v1/runs carries per-run token totals; ``q`` filters by id fragment."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from expert_work.persistence.token_usage_store import TokenUsageRecord
    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    run_a = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
    run_b = uuid4()
    for rid, trace in ((run_a, "traceaaaa"), (run_b, "tracebbbb")):
        await app.state.run_store.create(
            RunInfo(
                run_id=rid,
                tenant_id=DEFAULT_DEV_TENANT_ID,
                thread_id=UUID(thread_id),
                user_id=None,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=now,
                trace_id=trace,
            )
        )
    # Only run_a has recorded usage.
    await app.state.token_usage_store.insert(
        TokenUsageRecord(
            tenant_id=DEFAULT_DEV_TENANT_ID,
            agent_name="code-reviewer",
            agent_version="1.0.0",
            model="m",
            trace_id="traceaaaa",
            input_tokens=7,
            output_tokens=3,
        )
    )

    resp = await runs_client.get("/v1/runs")
    assert resp.status_code == 200
    items = {i["run_id"]: i for i in resp.json()["data"]["items"]}
    assert items[str(run_a)]["tokens"]["total_tokens"] == 10
    assert items[str(run_b)]["tokens"] is None  # no usage → None, not a crash

    resp2 = await runs_client.get("/v1/runs", params={"q": "aaaaaaaa"})
    assert resp2.status_code == 200
    ids = [i["run_id"] for i in resp2.json()["data"]["items"]]
    assert ids == [str(run_a)]


@pytest.mark.asyncio
async def test_list_runs_filters_by_user_id(runs_client: AsyncClient) -> None:
    """GET /v1/runs?user_id narrows to one end-user's runs."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    user_a = uuid4()
    run_a = uuid4()
    for rid, uid in ((run_a, user_a), (uuid4(), uuid4()), (uuid4(), None)):
        await app.state.run_store.create(
            RunInfo(
                run_id=rid,
                tenant_id=DEFAULT_DEV_TENANT_ID,
                thread_id=UUID(thread_id),
                user_id=uid,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
        )

    resp = await runs_client.get("/v1/runs", params={"user_id": str(user_a)})
    assert resp.status_code == 200
    ids = [i["run_id"] for i in resp.json()["data"]["items"]]
    assert ids == [str(run_a)]


# ---------------------------------------------------------------------------
# POST resume — Stream J.8-step3b (Mini-ADR J-24)
# ---------------------------------------------------------------------------


async def _seed_pending_approval(
    client: AsyncClient,
    thread_id: str,
    *,
    status: str = "pending",
    idempotency_key: str | None = None,
    continuation_run_id: UUID | None = None,
) -> UUID:
    """Seed an ``agent_approval`` row directly into the in-memory store."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from expert_work.protocol import ApprovalRecord, ApprovalStatus

    run_id = uuid4()
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    rec = ApprovalRecord(
        id=uuid4(),
        tenant_id=DEFAULT_DEV_TENANT_ID,
        run_id=run_id,
        thread_id=UUID(thread_id),
        request_id="approval:seed",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'send_email'",
        proposed_args={"to": "ops@example.com"},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
        status=ApprovalStatus(status),
        idempotency_key=idempotency_key,
        continuation_run_id=continuation_run_id,
    )
    await app.state.approval_store.create(rec)
    return run_id


@pytest.mark.asyncio
async def test_resume_unknown_run_returns_404(runs_client: AsyncClient) -> None:
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{uuid4()}/resume",
        json={"decision": "approve"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resume_already_decided_returns_409(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    run_id = await _seed_pending_approval(runs_client, thread_id, status="approved")
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resume_idempotent_replay_returns_continuation(runs_client: AsyncClient) -> None:
    """Stream 13.2 — a retry with the original key replays the same continuation
    run (200 JSON) instead of 409'ing — no agent build / worker spawn needed."""
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    continuation = uuid4()
    run_id = await _seed_pending_approval(
        runs_client,
        thread_id,
        status="approved",
        idempotency_key="resume-key-1",
        continuation_run_id=continuation,
    )
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve", "idempotency_key": "resume-key-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["run_id"] == str(continuation)
    assert body["data"]["idempotent_replay"] is True
    assert resp.headers["X-Expert-Work-Run-Id"] == str(continuation)


@pytest.mark.asyncio
async def test_resume_decided_different_key_returns_409(runs_client: AsyncClient) -> None:
    """A mismatched idempotency key on a decided approval is a real conflict."""
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    run_id = await _seed_pending_approval(
        runs_client,
        thread_id,
        status="approved",
        idempotency_key="resume-key-1",
        continuation_run_id=uuid4(),
    )
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve", "idempotency_key": "different-key"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resume_decided_no_key_still_409(runs_client: AsyncClient) -> None:
    """Backward compat — a keyless retry on a decided approval stays 409."""
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    run_id = await _seed_pending_approval(
        runs_client,
        thread_id,
        status="approved",
        idempotency_key="resume-key-1",
        continuation_run_id=uuid4(),
    )
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resume_modify_without_args_returns_422(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    run_id = await _seed_pending_approval(runs_client, thread_id)
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "modify"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resume_cross_tenant_returns_404(runs_client: AsyncClient) -> None:
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    run_id = await _seed_pending_approval(runs_client, thread_id)
    tenant_b_headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve"},
        headers=tenant_b_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Stream H.3 PR 1 — GET /v1/runs (cross-thread index)
#
# The cross-tenant + system_admin matrix lives in
# ``test_tenant_scope_endpoints.py`` (one row per endpoint covers home /
# "*" forbidden / system_admin "*"). Tests below cover the bits unique to
# this endpoint: status filter, agent_name JOIN, limit cap, audit emit.
# ---------------------------------------------------------------------------


async def _seed_completed_run(runs_client: AsyncClient) -> tuple[str, str]:
    """Trigger one happy-path SSE run and return ``(thread_id, run_id)``.

    Drains the stream so the run hits a terminal status before listing.
    """
    thread_id = await _create_session(runs_client)
    async with runs_client.stream(
        "POST",
        f"/v1/sessions/{thread_id}/runs",
        json={"input": "hello"},
    ) as response:
        assert response.status_code == 200
        body = await response.aread()
    events = _parse_sse(body.decode())
    metadata = next((d for evt, d in events if evt == "metadata"), None)
    assert isinstance(metadata, dict)
    run_id = str(metadata["run_id"])
    return thread_id, run_id


@pytest.mark.asyncio
async def test_list_runs_includes_agent_name_via_thread_join(
    runs_client: AsyncClient,
) -> None:
    """`agent_name` / `agent_version` come from a per-thread JOIN
    (§ 6.5.5 (b))."""
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs")
    assert resp.status_code == 200
    body = resp.json()
    items = body["data"]["items"]
    assert len(items) >= 1
    assert items[0]["agent_name"] == "code-reviewer"
    assert items[0]["agent_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_list_runs_status_filter(runs_client: AsyncClient) -> None:
    """``?status=success`` returns only success rows."""
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs", params={"status": "success"})
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert items
    assert all(item["status"] == "success" for item in items)


@pytest.mark.asyncio
async def test_list_runs_limit_cap_sets_header(runs_client: AsyncClient) -> None:
    """``limit > MAX_LIST_LIMIT`` is silently clamped + ``X-Limit-Capped: true``."""
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs", params={"limit": 9999})
    assert resp.status_code == 200
    assert resp.headers.get("x-limit-capped") == "true"


@pytest.mark.asyncio
async def test_list_runs_emits_audit(
    runs_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """Successful list emits one ``run:list_read`` audit row."""
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs")
    assert resp.status_code == 200

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT, action="run:list_read"))
    assert len(page.entries) >= 1
    row = page.entries[-1]
    assert row.result == "success"
    assert row.resource_type == "run"
    details = row.details or {}
    assert details.get("cross_tenant") is False
    assert details.get("count") >= 1


# ---------------------------------------------------------------------------
# Stream H.3 PR 2 — trace_id 持久化 (Mini-ADR H-9.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_includes_trace_id_field(runs_client: AsyncClient) -> None:
    """Each row exposes a ``trace_id`` field (None when OTel inactive in
    tests; the field's PRESENCE is what the plumbing test validates)."""
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert items
    assert "trace_id" in items[0]
    # Round-trip via RunStore — write a synthetic trace_id and confirm
    # it surfaces unchanged on the next list call.
    from expert_work.runtime.runs import RunStatus

    run_store = runs_client._transport.app.state.run_store  # type: ignore[attr-defined]
    persisted = await run_store.list_for_tenant(tenant_id=_DEFAULT_TENANT)
    assert persisted, "expected at least one persisted run"
    target = next(r for r in persisted if r.status is RunStatus.SUCCESS)
    await run_store.set_trace_id(
        run_id=target.run_id,
        tenant_id=_DEFAULT_TENANT,
        trace_id="cafef00d" * 4,
    )

    resp2 = await runs_client.get("/v1/runs")
    items2 = resp2.json()["data"]["items"]
    row = next(r for r in items2 if r["run_id"] == str(target.run_id))
    assert row["trace_id"] == "cafef00d" * 4


@pytest.mark.asyncio
async def test_get_run_includes_trace_id_field(runs_client: AsyncClient) -> None:
    """``GET /v1/sessions/{thread}/runs/{run}`` exposes the trace_id field."""
    thread_id, run_id = await _seed_completed_run(runs_client)
    # Stamp a known trace_id (in tests OTel is inactive so the
    # handler-captured value is None — directly setting via the store
    # tests the read path end-to-end).
    run_store = runs_client._transport.app.state.run_store  # type: ignore[attr-defined]
    await run_store.set_trace_id(
        run_id=UUID(run_id),
        tenant_id=_DEFAULT_TENANT,
        trace_id="deadbeef" * 4,
    )

    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == "deadbeef" * 4


# ---------------------------------------------------------------------------
# Stream H.3 PR 4 — GET /v1/sessions/{thread}/runs/{run}/events (live + replay)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_endpoint_replays_terminal_run_from_store(
    runs_client: AsyncClient,
) -> None:
    """A terminal run's events are served from the persisted store —
    the bridge cleanup may have already dropped them, but the store has
    everything since the dual-write."""
    thread_id, run_id = await _seed_completed_run(runs_client)

    async with runs_client.stream(
        "GET", f"/v1/sessions/{thread_id}/runs/{run_id}/events"
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-expert-work-stream-mode"] == "replay"
        assert response.headers["x-expert-work-run-id"] == run_id
        body = await response.aread()

    events = _parse_sse(body.decode())
    types = [e[0] for e in events]
    # The fake LLM run emits metadata + at least one updates + (terminal
    # is reached so the producer doesn't emit error). The replay then
    # caps with our own ``end`` frame.
    assert types[0] == "metadata"
    assert "updates" in types
    assert types[-1] == "end"


@pytest.mark.asyncio
async def test_events_endpoint_since_seq_skips_already_received(
    runs_client: AsyncClient,
) -> None:
    """``?since_seq=N`` is Last-Event-ID semantics — events with seq > N."""
    thread_id, run_id = await _seed_completed_run(runs_client)

    # First fetch — get everything to discover the actual seq numbers.
    async with runs_client.stream(
        "GET", f"/v1/sessions/{thread_id}/runs/{run_id}/events"
    ) as response:
        body = await response.aread()
    full = _parse_sse(body.decode())

    # Second fetch — skip the first frame (seq=0 → since_seq=0).
    async with runs_client.stream(
        "GET", f"/v1/sessions/{thread_id}/runs/{run_id}/events?since_seq=0"
    ) as response:
        body2 = await response.aread()
    skipped = _parse_sse(body2.decode())

    # The replay (without including the end frame) should be one shorter.
    full_events = [e for e in full if e[0] != "end"]
    skipped_events = [e for e in skipped if e[0] != "end"]
    assert len(skipped_events) == len(full_events) - 1
    # The skipped one is exactly the first (metadata).
    assert full_events[0][0] == "metadata"
    assert skipped_events[0][0] != "metadata" or len(skipped_events) == 0


@pytest.mark.asyncio
async def test_events_endpoint_cross_tenant_returns_404(
    runs_client: AsyncClient,
) -> None:
    from uuid import uuid4

    thread_id, run_id = await _seed_completed_run(runs_client)
    tenant_b_headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/events", headers=tenant_b_headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_events_endpoint_unknown_run_returns_404(
    runs_client: AsyncClient,
) -> None:
    from uuid import uuid4

    thread_id = await _create_session(runs_client)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{uuid4()}/events")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_events_endpoint_unknown_session_returns_404(
    runs_client: AsyncClient,
) -> None:
    from uuid import uuid4

    resp = await runs_client.get(f"/v1/sessions/{uuid4()}/runs/{uuid4()}/events")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Stream H.6 (Mini-ADR H-10 / H-12) — GET /v1/runs agent filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_agent_filter_happy(runs_client: AsyncClient) -> None:
    """``?agent_name=`` narrows to that agent's threads; cap signal present."""
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs", params={"agent_name": "code-reviewer"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) >= 1
    assert all(item["agent_name"] == "code-reviewer" for item in data["items"])
    assert data["thread_window_capped"] is False


@pytest.mark.asyncio
async def test_list_runs_agent_filter_name_and_version(runs_client: AsyncClient) -> None:
    await _seed_completed_run(runs_client)
    hit = await runs_client.get(
        "/v1/runs", params={"agent_name": "code-reviewer", "agent_version": "1.0.0"}
    )
    assert hit.status_code == 200
    assert len(hit.json()["data"]["items"]) >= 1

    miss = await runs_client.get(
        "/v1/runs", params={"agent_name": "code-reviewer", "agent_version": "9.9.9"}
    )
    assert miss.status_code == 200
    assert miss.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_list_runs_agent_filter_unknown_agent_returns_empty(
    runs_client: AsyncClient,
) -> None:
    await _seed_completed_run(runs_client)
    resp = await runs_client.get("/v1/runs", params={"agent_name": "ghost"})
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_list_runs_bare_agent_version_is_422(runs_client: AsyncClient) -> None:
    """Mini-ADR H-12 — ``agent_version`` without ``agent_name`` fails fast."""
    resp = await runs_client.get("/v1/runs", params={"agent_version": "1.0.0"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_queue_mode_returns_202(runs_client: AsyncClient) -> None:
    """Stream 9.5 — ``mode=queue`` enqueues + returns 202 (non-streaming)."""
    thread_id = await _create_session(runs_client)
    response = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs",
        json={"input": "do it async", "mode": "queue"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["thread_id"] == thread_id
    assert "run_id" in body
    # External-API-v1 P2-a Task 15 — the console call site never opts into
    # the {success, data, error} envelope (admin-ui consumes this flat shape
    # directly), so the body must be EXACTLY these three keys — no wrapping.
    assert set(body) == {"run_id", "thread_id", "status"}
    # Not an SSE stream — a plain JSON envelope.
    assert response.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Session-history uplift — auto-title + purge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_auto_titles_thread(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    # Freshly created — no title yet.
    before = await runs_client.get(f"/v1/sessions/{thread_id}")
    assert before.json()["data"]["title"] is None

    await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "review the PR"})
    after = await runs_client.get(f"/v1/sessions/{thread_id}")
    assert after.json()["data"]["title"] == "review the PR"

    # A second run must NOT overwrite the established title.
    await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "different follow up"})
    still = await runs_client.get(f"/v1/sessions/{thread_id}")
    assert still.json()["data"]["title"] == "review the PR"


@pytest.mark.asyncio
async def test_run_does_not_clobber_manual_title(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    renamed = await runs_client.patch(f"/v1/sessions/{thread_id}", json={"title": "my custom name"})
    assert renamed.status_code == 200

    await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "review the PR"})
    after = await runs_client.get(f"/v1/sessions/{thread_id}")
    assert after.json()["data"]["title"] == "my custom name"


@pytest.mark.asyncio
async def test_purge_deletes_thread_and_runs(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hi"})

    purge = await runs_client.post(f"/v1/sessions/{thread_id}:purge")
    assert purge.status_code == 200
    data = purge.json()["data"]
    assert data["purged"] == thread_id
    assert data["runs"] >= 1  # the run row we created was removed

    # The thread is gone — a follow-up read 404s.
    gone = await runs_client.get(f"/v1/sessions/{thread_id}")
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_purge_404_for_unknown(runs_client: AsyncClient) -> None:
    resp = await runs_client.post("/v1/sessions/00000000-0000-0000-0000-000000000099:purge")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Playground history reconstruction — thread run history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_runs_404_for_unknown(runs_client: AsyncClient) -> None:
    resp = await runs_client.get("/v1/sessions/00000000-0000-0000-0000-0000000000ff/runs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_thread_runs_empty_for_fresh_thread(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs")
    assert resp.status_code == 200
    assert resp.json()["data"]["runs"] == []


@pytest.mark.asyncio
async def test_thread_runs_lists_oldest_first(runs_client: AsyncClient) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    base = datetime.now(UTC)
    older, newer = uuid4(), uuid4()
    # Seed newer first to prove the endpoint sorts by created_at, not insert order.
    for rid, created, is_resume in (
        (newer, base + timedelta(seconds=61), True),
        (older, base, False),
    ):
        await app.state.run_store.create(
            RunInfo(
                run_id=rid,
                tenant_id=DEFAULT_DEV_TENANT_ID,
                thread_id=UUID(thread_id),
                user_id=None,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=is_resume,
                error=None,
                created_at=created,
                updated_at=created,
                finished_at=created,
            )
        )
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert [r["run_id"] for r in runs] == [str(older), str(newer)]
    assert [r["is_resume"] for r in runs] == [False, True]
    assert runs[0]["status"] == "success"
    assert "created_at" in runs[0]


@pytest.mark.asyncio
async def test_thread_runs_carry_per_run_tokens(runs_client: AsyncClient) -> None:
    """Debug-console redesign PR-A — the thread's run list carries each run's
    token rollup (joined by trace_id, same source as ``GET .../runs/{run_id}``);
    a run without usage carries ``tokens: null``."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from expert_work.persistence.token_usage_store import TokenUsageRecord
    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    with_usage, without_usage = uuid4(), uuid4()
    trace = "feedfacefeedfacefeedfacefeedface"
    for rid, created, tid in (
        (with_usage, now, trace),
        (without_usage, now + timedelta(seconds=5), None),
    ):
        await app.state.run_store.create(
            RunInfo(
                run_id=rid,
                tenant_id=DEFAULT_DEV_TENANT_ID,
                thread_id=UUID(thread_id),
                user_id=None,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=created,
                updated_at=created,
                finished_at=created,
                trace_id=tid,
            )
        )
    await app.state.token_usage_store.insert(
        TokenUsageRecord(
            tenant_id=DEFAULT_DEV_TENANT_ID,
            agent_name="code-reviewer",
            agent_version="1.0.0",
            model="claude-sonnet-4-6",
            trace_id=trace,
            input_tokens=120,
            output_tokens=30,
        )
    )
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert [r["run_id"] for r in runs] == [str(with_usage), str(without_usage)]
    assert runs[0]["tokens"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 150,
        "llm_calls": 1,
        "models": ["claude-sonnet-4-6"],
    }
    assert runs[1]["tokens"] is None


@pytest.mark.asyncio
async def test_thread_runs_token_lookup_failure_degrades_tokens_only(
    runs_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Debug-console redesign PR-A fix round 1 — a ``token_usage`` lookup
    failure must not empty the whole run list (only the run-store failing
    should do that, per ``test_thread_runs_store_failure_degrades_to_empty``).
    The console's history reconstruction pairs message count to run count;
    silently collapsing to ``[]`` here would wrongly degrade a resumed
    thread to flat text instead of merely losing the token badge."""
    from datetime import UTC, datetime

    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    run_id = uuid4()
    await app.state.run_store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=DEFAULT_DEV_TENANT_ID,
            thread_id=UUID(thread_id),
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
            trace_id="deadbeefdeadbeefdeadbeefdeadbeef",
        )
    )

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.token_usage_store, "totals_by_trace_ids", _boom)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == str(run_id)
    assert runs[0]["tokens"] is None


@pytest.mark.asyncio
async def test_thread_runs_foreign_tenant_forbidden(runs_client: AsyncClient) -> None:
    """A plain tenant admin asking for another tenant's runs is rejected by
    the scope gate (only a system_admin may cross) — same as
    ``test_thread_messages_foreign_tenant_forbidden``; the tenant-scope gate
    runs before the per-thread ownership check that returns 404."""
    thread_id = await _create_session(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs",
        params={"tenant_id": "11111111-1111-1111-1111-111111111111"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_thread_runs_store_failure_degrades_to_empty(
    runs_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: store 异常 → best-effort 返回 {"runs": []}(不 500),与 /messages 一致。
    The ownership gate already passed (own thread) — only the store read fails."""
    thread_id = await _create_session(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.run_store, "list_by_thread", _boom)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs")
    assert resp.status_code == 200
    assert resp.json()["data"]["runs"] == []


# ---------------------------------------------------------------------------
# Deletion hygiene PR4 — a session bound to a soft-DELETED agent refuses new
# runs / approval resumes with 410 AGENT_DELETED; a never-registered agent
# stays a plain 404.
# ---------------------------------------------------------------------------


async def _seed_ghost_thread(client: AsyncClient) -> UUID:
    """A thread bound to an agent name that was never registered."""
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id,
        tenant_id=_DEFAULT_TENANT,
        created_by="test",
        agent_name="ghost",
        agent_version="1.0.0",
    )
    return thread_id


@pytest.mark.asyncio
async def test_run_on_deleted_agent_returns_410(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    deleted = await runs_client.delete("/v1/agents/code-reviewer/1.0.0")
    assert deleted.status_code == 204, deleted.text

    resp = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hi"})
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_DELETED"


@pytest.mark.asyncio
async def test_run_on_never_registered_agent_still_404(runs_client: AsyncClient) -> None:
    thread_id = await _seed_ghost_thread(runs_client)
    resp = await runs_client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hi"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_resume_on_deleted_agent_returns_410(runs_client: AsyncClient) -> None:
    thread_id = await _create_session(runs_client)
    run_id = await _seed_pending_approval(runs_client, thread_id)
    deleted = await runs_client.delete("/v1/agents/code-reviewer/1.0.0")
    assert deleted.status_code == 204, deleted.text

    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve"},
    )
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_DELETED"


@pytest.mark.asyncio
async def test_resume_on_never_registered_agent_still_404(runs_client: AsyncClient) -> None:
    thread_id = await _seed_ghost_thread(runs_client)
    run_id = await _seed_pending_approval(runs_client, str(thread_id))
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve"},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# W2 — run 详情读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id → 200;普通租户
# 用户带他租户 tenant_id → 403 TENANT_NOT_ALLOWED;tenant_id=* → 400
# SCOPE_ALL_NOT_SUPPORTED(详情端点只认单租户)。
# ---------------------------------------------------------------------------


def _foreign_tenant_headers() -> dict[str, str]:
    """A plain tenant user homed in a random other tenant."""
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}


#: (name, path-suffix under /v1/sessions/{thread}/runs/{run}, extra query params)
_RUN_SCOPE_ENDPOINTS: list[tuple[str, str, dict[str, str]]] = [
    ("get_run", "", {}),
    ("trace", "/trace", {}),
    ("trace_raw", "/trace/raw", {"span": "s1", "field": "input"}),
    ("events", "/events", {}),
]


class _StubTraceApi:
    def __init__(self, trace: Any) -> None:
        self._trace = trace

    def get(self, trace_id: str) -> Any:
        return self._trace


class _StubLangfuseClient:
    """Minimal read client: one trace holding one span with a raw input."""

    def __init__(self) -> None:
        span = SimpleNamespace(id="s1", input="raw span input", output=None)
        self.api = SimpleNamespace(trace=_StubTraceApi(SimpleNamespace(observations=[span])))


@pytest.mark.asyncio
async def test_get_run_system_admin_target_tenant_200(runs_client: AsyncClient) -> None:
    thread_id, run_id = await _seed_completed_run(runs_client)
    headers = await grant_system_admin(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["status"] == "success"


@pytest.mark.asyncio
async def test_get_run_trace_system_admin_target_tenant_200(runs_client: AsyncClient) -> None:
    thread_id, run_id = await _seed_completed_run(runs_client)
    headers = await grant_system_admin(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    # 命中目标租户的 run 行(而非跨租户 404)。具体 status 取决于整个套件的
    # OTel 状态:单跑时无 tracer → run 无 trace_id → "no_trace";全量套件里
    # 其他用例装过全局 tracer → run 带 trace_id,但本 app 无 Langfuse client
    # → "unavailable"。两者都证明已越过租户 gate 读到了目标租户的 run 行。
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in {"no_trace", "unavailable"}


@pytest.mark.asyncio
async def test_get_run_trace_raw_system_admin_target_tenant_200(
    runs_client: AsyncClient,
) -> None:
    thread_id, run_id = await _seed_completed_run(runs_client)
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    await app.state.run_store.set_trace_id(
        run_id=UUID(run_id), tenant_id=_DEFAULT_TENANT, trace_id="cafef00d" * 4
    )
    app.state.langfuse_read_client = _StubLangfuseClient()
    headers = await grant_system_admin(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
        params={"tenant_id": str(_DEFAULT_TENANT), "span": "s1", "field": "input"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "raw span input"


@pytest.mark.asyncio
async def test_run_events_system_admin_target_tenant_200(runs_client: AsyncClient) -> None:
    thread_id, run_id = await _seed_completed_run(runs_client)
    headers = await grant_system_admin(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}/events",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-expert-work-stream-mode"] == "replay"
    events = _parse_sse(resp.text)
    assert events, "expected replayed SSE frames"
    assert events[0][0] == "metadata"


@pytest.mark.parametrize("name,suffix,extra", _RUN_SCOPE_ENDPOINTS)
@pytest.mark.asyncio
async def test_run_detail_foreign_tenant_user_403(
    runs_client: AsyncClient, name: str, suffix: str, extra: dict[str, str]
) -> None:
    thread_id, run_id = await _seed_completed_run(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}{suffix}",
        params={"tenant_id": str(_DEFAULT_TENANT), **extra},
        headers=_foreign_tenant_headers(),
    )
    assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.parametrize("name,suffix,extra", _RUN_SCOPE_ENDPOINTS)
@pytest.mark.asyncio
async def test_run_detail_tenant_id_star_400(
    runs_client: AsyncClient, name: str, suffix: str, extra: dict[str, str]
) -> None:
    thread_id, run_id = await _seed_completed_run(runs_client)
    resp = await runs_client.get(
        f"/v1/sessions/{thread_id}/runs/{run_id}{suffix}",
        params={"tenant_id": "*", **extra},
    )
    assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name


# ---------------------------------------------------------------------------
# D-6 — POST /v1/sessions/{thread_id}/runs/{run_id}:cancel
# ---------------------------------------------------------------------------


async def _seed_run_row(client: AsyncClient, thread_id: str, *, status: object) -> UUID:
    from datetime import UTC, datetime
    from uuid import uuid4 as _uuid4

    from expert_work.runtime.runs import DisconnectMode, RunInfo

    run_id = _uuid4()
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    now = datetime.now(UTC)
    await app.state.run_store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=DEFAULT_DEV_TENANT_ID,
            thread_id=UUID(thread_id),
            user_id=None,
            status=status,  # type: ignore[arg-type]
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )
    )
    return run_id


@pytest.mark.asyncio
async def test_cancel_run_interrupts_a_running_row(runs_client: AsyncClient) -> None:
    """A peer-owned running run cancels via the store CAS → interrupted."""
    from expert_work.runtime.runs import RunStatus

    thread_id = await _create_session(runs_client)
    run_id = await _seed_run_row(runs_client, thread_id, status=RunStatus.RUNNING)

    resp = await runs_client.post(f"/v1/sessions/{thread_id}/runs/{run_id}:cancel", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"cancelled": True}

    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    row = await app.state.run_store.get(run_id=run_id, tenant_id=DEFAULT_DEV_TENANT_ID)
    assert row is not None
    assert row.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_cancel_run_terminal_row_is_409(runs_client: AsyncClient) -> None:
    from expert_work.runtime.runs import RunStatus

    thread_id = await _create_session(runs_client)
    run_id = await _seed_run_row(runs_client, thread_id, status=RunStatus.SUCCESS)

    resp = await runs_client.post(f"/v1/sessions/{thread_id}/runs/{run_id}:cancel", json={})
    assert resp.status_code == 409, resp.text
    # The row is untouched — a finished run is never clobbered.
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    row = await app.state.run_store.get(run_id=run_id, tenant_id=DEFAULT_DEV_TENANT_ID)
    assert row is not None
    assert row.status is RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_cancel_run_terminal_with_local_manager_record_is_409(
    runs_client: AsyncClient,
) -> None:
    """终审 C-1/I-7 — the 409 must hold even when THIS instance still holds
    the run's in-memory RunManager record: ``RunManager.cancel()`` returns
    True for any known record regardless of status, so without the terminal
    pre-filter this would 200 + set the record's abort_event."""
    from datetime import UTC, datetime
    from uuid import uuid4 as _uuid4

    from expert_work.runtime.runs import RunStatus

    thread_id = await _create_session(runs_client)
    run_id = _uuid4()
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    runtime = app.state.agent_runtime
    record = await runtime.run_manager.create(
        run_id=run_id,
        thread_id=UUID(thread_id),
        tenant_id=DEFAULT_DEV_TENANT_ID,
        user_id=None,
    )
    await app.state.run_store.set_status(
        run_id=run_id,
        tenant_id=DEFAULT_DEV_TENANT_ID,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
    )

    resp = await runs_client.post(f"/v1/sessions/{thread_id}/runs/{run_id}:cancel", json={})
    assert resp.status_code == 409, resp.text
    # The in-memory record was never touched — no phantom abort.
    assert not record.abort_event.is_set()


@pytest.mark.asyncio
async def test_cancel_run_paused_row_is_409_decide_instead(runs_client: AsyncClient) -> None:
    """A paused run is decided through the approval path, never cancelled."""
    from expert_work.runtime.runs import RunStatus

    thread_id = await _create_session(runs_client)
    run_id = await _seed_run_row(runs_client, thread_id, status=RunStatus.PAUSED)

    resp = await runs_client.post(f"/v1/sessions/{thread_id}/runs/{run_id}:cancel", json={})
    assert resp.status_code == 409, resp.text
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    row = await app.state.run_store.get(run_id=run_id, tenant_id=DEFAULT_DEV_TENANT_ID)
    assert row is not None
    assert row.status is RunStatus.PAUSED


@pytest.mark.asyncio
async def test_cancel_run_wrong_thread_is_404(runs_client: AsyncClient) -> None:
    from expert_work.runtime.runs import RunStatus

    thread_a = await _create_session(runs_client)
    thread_b = await _create_session(runs_client)
    run_in_b = await _seed_run_row(runs_client, thread_b, status=RunStatus.RUNNING)

    resp = await runs_client.post(f"/v1/sessions/{thread_a}/runs/{run_in_b}:cancel", json={})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cancel_run_requires_operator_role(runs_client: AsyncClient) -> None:
    """D-6 role gate — a viewer JWT cannot cancel runs (403)."""
    from expert_work.runtime.runs import RunStatus

    thread_id = await _create_session(runs_client)
    run_id = await _seed_run_row(runs_client, thread_id, status=RunStatus.RUNNING)

    viewer = {
        "Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT, roles=('viewer',))}"
    }
    resp = await runs_client.post(
        f"/v1/sessions/{thread_id}/runs/{run_id}:cancel", json={}, headers=viewer
    )
    assert resp.status_code == 403, resp.text
    # Untouched — the gate fired before the CAS.
    app = runs_client._transport.app  # type: ignore[attr-defined,union-attr]
    row = await app.state.run_store.get(run_id=run_id, tenant_id=DEFAULT_DEV_TENANT_ID)
    assert row is not None
    assert row.status is RunStatus.RUNNING
