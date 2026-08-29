"""Endpoint tests for ``GET /v1/conversations`` (+ ``/{thread_id}``).

The conversation view groups ``agent_run`` rows by ``thread_id`` (the
``thread_meta`` conversation) and joins ``token_usage`` by ``trace_id``.
These exercise the rollup (run/error/pending counts, token sums), the
agent / user filters, and the detail run list against in-memory stores.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypedDict
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.token_usage_store import TokenUsageRecord
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
_USER_B = UUID("bbbbbbbb-0000-0000-0000-000000000002")
_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


def _run(
    *,
    thread_id: UUID,
    user_id: UUID | None,
    status: RunStatus,
    trace_id: str | None,
    created_at: datetime,
) -> RunInfo:
    return RunInfo(
        run_id=uuid4(),
        tenant_id=_TENANT,
        thread_id=thread_id,
        user_id=user_id,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error="boom" if status is RunStatus.ERROR else None,
        created_at=created_at,
        updated_at=created_at,
        finished_at=created_at,
        trace_id=trace_id,
    )


@pytest.fixture
async def client_and_threads() -> AsyncIterator[tuple[AsyncClient, dict[str, UUID]]]:
    """App seeded with 3 conversations + runs + token usage.

    ``convo`` — agent "alpha" / user A: 2 runs (1 success, 1 error), tokens.
    ``other_user`` — agent "alpha" / user B: 1 success run.
    ``other_agent`` — agent "beta" / user A: 1 success + 1 paused run.
    """
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )

    threads = app.state.thread_meta_repo
    runs = app.state.run_store
    tokens = app.state.token_usage_store

    ids = {"convo": uuid4(), "other_user": uuid4(), "other_agent": uuid4()}
    await threads.create(
        thread_id=ids["convo"],
        tenant_id=_TENANT,
        created_by="seed",
        user_id=_USER_A,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    await threads.update_title(ids["convo"], "refund question", tenant_id=_TENANT)
    await threads.create(
        thread_id=ids["other_user"],
        tenant_id=_TENANT,
        created_by="seed",
        user_id=_USER_B,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    await threads.create(
        thread_id=ids["other_agent"],
        tenant_id=_TENANT,
        created_by="seed",
        user_id=_USER_A,
        agent_name="beta",
        agent_version="1.0.0",
    )

    await runs.create(
        _run(
            thread_id=ids["convo"],
            user_id=_USER_A,
            status=RunStatus.SUCCESS,
            trace_id="tr-1",
            created_at=_NOW,
        )
    )
    await runs.create(
        _run(
            thread_id=ids["convo"],
            user_id=_USER_A,
            status=RunStatus.ERROR,
            trace_id="tr-2",
            created_at=_NOW + timedelta(minutes=3),
        )
    )
    await runs.create(
        _run(
            thread_id=ids["other_user"],
            user_id=_USER_B,
            status=RunStatus.SUCCESS,
            trace_id="tr-3",
            created_at=_NOW,
        )
    )
    await runs.create(
        _run(
            thread_id=ids["other_agent"],
            user_id=_USER_A,
            status=RunStatus.SUCCESS,
            trace_id="tr-4",
            created_at=_NOW,
        )
    )
    # A run paused at an approval gate — feeds the has_pending filter.
    await runs.create(
        _run(
            thread_id=ids["other_agent"],
            user_id=_USER_A,
            status=RunStatus.PAUSED,
            trace_id=None,
            created_at=_NOW + timedelta(minutes=2),
        )
    )

    for tid, inp, out in [("tr-1", 100, 20), ("tr-2", 50, 10)]:
        await tokens.insert(
            TokenUsageRecord(
                tenant_id=_TENANT,
                agent_name="alpha",
                agent_version="1.0.0",
                model="claude-sonnet-4-5",
                input_tokens=inp,
                output_tokens=out,
                trace_id=tid,
            )
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        jwt = make_test_jwt(tenant_id=_TENANT, subject=str(uuid4()))
        client.headers["Authorization"] = f"Bearer {jwt}"
        # Content-search tests seed the transcript mirror directly.
        client.app_state = app.state  # type: ignore[attr-defined]
        yield client, ids


@pytest.mark.asyncio
async def test_list_backfills_null_titles_from_the_checkpoint(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """NULL title 的会话在对话页兜底成 checkpoint 首条用户消息(并落库)。

    对外平面早期建的会话没有标题,对话页整页「未命名对话」(2026-08-26
    用户反馈)。sessions 列表早有这层兜底,对话页此前直接吐 ``meta.title``。
    """
    client, ids = client_and_threads

    # _SeedState 的注解名(Annotated/BaseMessage/add_messages)必须可在模块
    # globals 解析 —— ``from __future__ import annotations`` 下 LangGraph 用
    # get_type_hints 按模块命名空间求值注解,函数内 import 会 NameError。
    class _SeedState(TypedDict):
        messages: Annotated[list[BaseMessage], add_messages]

    checkpointer = InMemorySaver()
    graph = StateGraph(_SeedState)
    graph.add_node("n", lambda _state: {"messages": []})
    graph.add_edge(START, "n")
    seeded = graph.compile(checkpointer=checkpointer)
    await seeded.ainvoke(
        {"messages": [HumanMessage("退款流程是什么样的,需要几天?")]},
        config={"configurable": {"thread_id": str(ids["other_user"]), "checkpoint_ns": ""}},
    )
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    app.state.agent_runtime.durable_checkpointer = checkpointer

    resp = await client.get("/v1/conversations")
    assert resp.status_code == 200
    items = {i["thread_id"]: i for i in resp.json()["data"]["items"]}
    assert items[str(ids["other_user"])]["title"] == "退款流程是什么样的,需要几天?"

    # 落库了 —— 再列一次不再依赖 checkpoint(store 直读同值)。
    meta = await app.state.thread_meta_repo.get(ids["other_user"], tenant_id=_TENANT)
    assert meta is not None and meta.title == "退款流程是什么样的,需要几天?"


@pytest.mark.asyncio
async def test_list_rolls_up_runs_and_tokens(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    resp = await client.get("/v1/conversations")
    assert resp.status_code == 200
    items = {i["thread_id"]: i for i in resp.json()["data"]["items"]}

    convo = items[str(ids["convo"])]
    assert convo["run_count"] == 2
    assert convo["error_count"] == 1
    assert convo["pending_count"] == 0
    assert convo["user_id"] == str(_USER_A)
    assert convo["title"] == "refund question"
    # tr-1 (100+20) + tr-2 (50+10) summed across the thread's runs.
    assert convo["tokens"]["input_tokens"] == 150
    assert convo["tokens"]["output_tokens"] == 30
    assert convo["tokens"]["total_tokens"] == 180
    assert convo["tokens"]["llm_calls"] == 2


@pytest.mark.asyncio
async def test_list_filters_by_agent(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    resp = await client.get("/v1/conversations", params={"agent_name": "beta"})
    assert resp.status_code == 200
    got = {i["thread_id"] for i in resp.json()["data"]["items"]}
    assert got == {str(ids["other_agent"])}


@pytest.mark.asyncio
async def test_list_filters_by_user(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    resp = await client.get("/v1/conversations", params={"user_id": str(_USER_B)})
    assert resp.status_code == 200
    got = {i["thread_id"] for i in resp.json()["data"]["items"]}
    assert got == {str(ids["other_user"])}


@pytest.mark.asyncio
async def test_list_version_without_agent_is_422(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, _ = client_and_threads
    resp = await client.get("/v1/conversations", params={"agent_version": "1.0.0"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_detail_returns_run_list_and_summary(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    resp = await client.get(f"/v1/conversations/{ids['convo']}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["run_count"] == 2
    assert data["error_count"] == 1
    assert len(data["runs"]) == 2
    # Runs carry per-run token attribution + the error string.
    errored = [r for r in data["runs"] if r["status"] == "error"]
    assert errored and errored[0]["error"] == "boom"
    assert errored[0]["tokens"]["input_tokens"] == 50


@pytest.mark.asyncio
async def test_detail_runs_carry_the_config_version(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """对话页要标出「这一轮之后配置被改过」,靠的就是这个字段。

    ``agent_version`` 回答不了 —— 配置页是原地编辑,版本号编辑前后一样。
    """
    client, ids = client_and_threads
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    runs = (await client.get(f"/v1/conversations/{ids['convo']}")).json()["data"]["runs"]
    # 种子 run 没经过执行入口,所以这一列是 null —— 字段**存在**才是这里要验的。
    assert all("agent_spec_sha256" in r for r in runs)
    assert all(r["agent_spec_sha256"] is None for r in runs)

    await app.state.run_store.set_agent_spec_sha256(
        run_id=UUID(runs[0]["run_id"]),
        tenant_id=_TENANT,
        agent_spec_sha256="c" * 64,
    )
    refreshed = (await client.get(f"/v1/conversations/{ids['convo']}")).json()["data"]["runs"]
    by_id = {r["run_id"]: r["agent_spec_sha256"] for r in refreshed}
    assert by_id[runs[0]["run_id"]] == "c" * 64
    # 另一轮仍然是 null —— 这一列是逐轮的,不是整个会话一个值。
    assert by_id[runs[1]["run_id"]] is None


@pytest.mark.asyncio
async def test_detail_unknown_thread_is_404(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, _ = client_and_threads
    resp = await client.get(f"/v1/conversations/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_filters_by_has_error(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """has_error narrows to conversations with ≥1 failed run — distinct
    from thread status (an active thread can carry errored runs)."""
    client, ids = client_and_threads
    resp = await client.get("/v1/conversations", params={"has_error": "true"})
    assert resp.status_code == 200
    got = {i["thread_id"] for i in resp.json()["data"]["items"]}
    # Only "convo" has an ERROR run; the other two threads are all-success.
    assert got == {str(ids["convo"])}


@pytest.mark.asyncio
async def test_has_error_composes_with_agent_filter(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, _ = client_and_threads
    # "beta" has conversations but none with errors — empty, not an error.
    resp = await client.get("/v1/conversations", params={"has_error": "true", "agent_name": "beta"})
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_list_filters_by_has_pending(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """has_pending narrows to conversations with ≥1 run paused at an
    approval gate — the "needs a human" queue in conversation context."""
    client, ids = client_and_threads
    resp = await client.get("/v1/conversations", params={"has_pending": "true"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {i["thread_id"] for i in data["items"]} == {str(ids["other_agent"])}
    assert data["total"] == 1

    # Error + pending intersect — no thread carries both, so empty.
    both = await client.get(
        "/v1/conversations", params={"has_pending": "true", "has_error": "true"}
    )
    assert both.json()["data"]["items"] == []
    assert both.json()["data"]["total"] == 0


@pytest.mark.asyncio
async def test_q_matches_title_or_message_content(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """IA M4 — one search box spans the title AND the mirrored transcript."""
    from expert_work.persistence import MessageTurn

    client, ids = client_and_threads
    # Seed the transcript mirror for a conversation whose TITLE doesn't match.
    store = client.app_state.thread_message_store  # type: ignore[attr-defined]
    await store.sync_thread(
        thread_id=ids["other_agent"],
        tenant_id=_TENANT,
        turns=[MessageTurn(seq=0, role="user", content="my invoice was 重复扣费 twice")],
        synced_at=_NOW,
    )

    # Content-only hit.
    resp = await client.get("/v1/conversations", params={"q": "重复扣费"})
    data = resp.json()["data"]
    assert {i["thread_id"] for i in data["items"]} == {str(ids["other_agent"])}
    assert data["total"] == 1

    # Title hit still works ("refund question" on convo) — OR semantics: a
    # term matching one title and another thread's content returns both.
    await store.sync_thread(
        thread_id=ids["other_user"],
        tenant_id=_TENANT,
        turns=[MessageTurn(seq=0, role="assistant", content="your refund is on its way")],
        synced_at=_NOW,
    )
    both = await client.get("/v1/conversations", params={"q": "refund"})
    got = {i["thread_id"] for i in both.json()["data"]["items"]}
    assert got == {str(ids["convo"]), str(ids["other_user"])}
    assert both.json()["data"]["total"] == 2

    # Content search composes with the agent filter.
    scoped = await client.get("/v1/conversations", params={"q": "重复扣费", "agent_name": "alpha"})
    assert scoped.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_total_is_true_count_and_offset_pages(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """``total`` counts every matching conversation, not the page — the
    server-side pager's contract."""
    client, _ = client_and_threads
    resp = await client.get("/v1/conversations", params={"limit": 2})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) == 2
    assert data["total"] == 3

    page2 = await client.get("/v1/conversations", params={"limit": 2, "offset": 2})
    assert page2.status_code == 200
    data2 = page2.json()["data"]
    assert len(data2["items"]) == 1
    assert data2["total"] == 3
    # The two pages are disjoint and cover all three conversations.
    ids1 = {i["thread_id"] for i in data["items"]}
    ids2 = {i["thread_id"] for i in data2["items"]}
    assert not (ids1 & ids2) and len(ids1 | ids2) == 3


@pytest.mark.asyncio
async def test_since_filters_by_run_activity(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """``since`` keeps only conversations with ≥1 run at/after the instant —
    the "active in the last N hours" monitoring window."""
    client, ids = client_and_threads
    # Runs after _NOW+1min: convo's ERROR (+3min) and other_agent's
    # PAUSED (+2min); other_user's only run is at _NOW.
    cutoff = (_NOW + timedelta(minutes=1)).isoformat()
    resp = await client.get("/v1/conversations", params={"since": cutoff})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {i["thread_id"] for i in data["items"]} == {
        str(ids["convo"]),
        str(ids["other_agent"]),
    }
    assert data["total"] == 2

    # A cutoff before every run matches all three conversations.
    early = await client.get(
        "/v1/conversations", params={"since": (_NOW - timedelta(hours=1)).isoformat()}
    )
    assert early.json()["data"]["total"] == 3


@pytest.mark.asyncio
async def test_since_composes_with_has_error(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    # "What broke today": convo's ERROR run is at _NOW+3min.
    resp = await client.get(
        "/v1/conversations",
        params={"since": (_NOW + timedelta(minutes=1)).isoformat(), "has_error": "true"},
    )
    assert {i["thread_id"] for i in resp.json()["data"]["items"]} == {str(ids["convo"])}

    # A window after every failed run matches nothing.
    late = await client.get(
        "/v1/conversations",
        params={"since": (_NOW + timedelta(hours=1)).isoformat(), "has_error": "true"},
    )
    assert late.json()["data"]["items"] == []
    assert late.json()["data"]["total"] == 0


# --- 阶段 1.1 员工 RBAC ------------------------------------------------------
#
# ``console_only`` (P2) blocks a third-party API key; it says nothing about
# which *employee* may read. ``GET /v1/conversations`` takes ``user_id`` as a
# query parameter and ``q`` searches transcript content, so before this any
# authenticated employee — including one holding no role — could search any
# end user's conversation text. Ruling (2026-08-14): reads stay open to every
# employee, so the gate is ``session:read`` (viewer/operator/admin all hold it).


def _employee(*roles: str) -> dict[str, str]:
    """Authorization header for an employee JWT carrying exactly ``roles``."""
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, roles=roles)}"}


@pytest.mark.asyncio
async def test_viewer_can_read_conversations(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    listed = await client.get("/v1/conversations", headers=_employee("viewer"))
    assert listed.status_code == 200, listed.text
    detail = await client.get(f"/v1/conversations/{ids['convo']}", headers=_employee("viewer"))
    assert detail.status_code == 200, detail.text


@pytest.mark.asyncio
async def test_roleless_employee_cannot_read_conversations(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """A JWT carrying no role grants nothing. viewer/operator/admin all hold
    ``session:read``, so only a role-less principal can prove the gate is
    actually wired."""
    client, ids = client_and_threads
    for path in ("/v1/conversations", f"/v1/conversations/{ids['convo']}"):
        resp = await client.get(path, headers=_employee())
        assert resp.status_code == 403, f"{path}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "FORBIDDEN", path


@pytest.mark.asyncio
async def test_roleless_employee_cannot_search_transcripts(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """``q`` is full-text over transcript content — the same gate must cover it,
    otherwise a filtered list is worthless: you could still confirm that a given
    end user said a given sentence."""
    client, _ = client_and_threads
    resp = await client.get("/v1/conversations", params={"q": "refund"}, headers=_employee())
    assert resp.status_code == 403, resp.text
