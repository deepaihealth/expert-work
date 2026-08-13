"""NUL-byte hardening for the external (third-party) API plane —
External-API-v1 P2-b.

PostgreSQL ``text`` / ``jsonb`` columns reject an embedded NUL byte
(``\\x00``) — asyncpg raises ``CharacterNotInRepertoireError``, and
``app.py`` registers no fallback ``@app.exception_handler(Exception)``, so an
uncaught instance of that error used to escape as Starlette's bare-text
"Internal Server Error", breaking the external plane's ``{success, data,
error}`` envelope contract. Every external-plane field that can reach such a
column now routes through the ONE shared guard in ``api/_external.py``
(``reject_nul`` / ``reject_nul_deep``, plus ``external_subject_id`` for
``user_id``) — this file proves each of those call sites is actually wired
up, one test per field, and that the guard does NOT reject legitimate
content (``\\n`` / ``\\t`` / unicode) along the way.

Fixture shape mirrors ``test_external_hardening.py`` / ``test_external_approvals.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, Role
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

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


class _Ctx:
    def __init__(self, client: AsyncClient, app: Any, tenant_id: UUID, headers: dict[str, str]):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def bind_session(self, user_id: str = "cust-77") -> str:
        resp = await self.client.post(
            "/v1/agents/support-bot/sessions",
            json={"user_id": user_id},
            headers=self.headers,
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["data"]["session_id"])


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
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
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers)


def _assert_envelope_422(resp: Any, *, code: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" not in body
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == code
    assert body["error"]["message"]


# ---------------------------------------------------------------------------
# Reject — every one of these routes through the ONE shared guard
# (``reject_nul`` / ``reject_nul_deep`` in ``_external.py``, or
# ``external_subject_id`` for ``user_id``). Mutation 1 (gutting that guard's
# NUL check) must turn every single test below red at once — that is the
# actual proof "handle it in one shared place" held, not "some field somewhere
# is covered".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_id_query_param_with_nul_is_422(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-\x00-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_USER_ID")


@pytest.mark.asyncio
async def test_user_id_body_field_with_nul_is_422(ctx: _Ctx) -> None:
    """The mint (write) path — distinct code path inside ``_resolve_session``
    from the query-param lookup (read) path above; both funnel through
    ``external_subject_id``, but only a test on each proves it."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-\x00-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_USER_ID")


@pytest.mark.asyncio
async def test_title_with_nul_is_422(ctx: _Ctx) -> None:
    """The concretely-reproduced crash: ``PATCH .../sessions/{id}`` body
    ``{"title": "a\\x00b"}`` used to reach ``thread_meta.title`` and 500."""
    await ctx.seed_agent()
    session_id = await ctx.bind_session()
    resp = await ctx.client.patch(
        f"/v1/agents/support-bot/sessions/{session_id}",
        json={"user_id": "cust-77", "title": "a\x00b"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_idempotency_key_header_with_nul_is_422(ctx: _Ctx) -> None:
    """``Idempotency-Key`` is a header, not a pydantic field — the one call
    site with no ``field_validator`` to attach to; guarded by a direct call
    in ``run_agent_for_user`` instead. Lands in ``agent_run.idempotency_key``
    (``Text``) for both ``stream`` and ``queue`` mode."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers={**ctx.headers, "Idempotency-Key": "key-\x00-1"},
    )
    _assert_envelope_422(resp, code="INVALID_IDEMPOTENCY_KEY")


@pytest.mark.asyncio
async def test_input_with_nul_is_422(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi\x00there", "mode": "queue"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_untrusted_content_with_nul_is_422(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "input": "hi",
            "mode": "queue",
            "untrusted_content": ["clean block", "dirty\x00block"],
        },
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_inputs_value_with_nul_is_422(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "input": "hi",
            "mode": "queue",
            "inputs": {"lang": "en\x00us"},
        },
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_inputs_key_with_nul_is_422(ctx: _Ctx) -> None:
    """``inputs`` values aren't the only place a NUL can hide — a JSON object
    key is user-controlled input here too, and JSONB rejects a NUL in a key
    exactly like it does in a value. ``reject_nul_deep`` checks both;
    covering only values would leave this half of the guard unverified."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "input": "hi",
            "mode": "queue",
            "inputs": {"la\x00ng": "en"},
        },
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_decide_modified_args_with_nul_is_422(ctx: _Ctx) -> None:
    """Field-level validation fires during request-body parsing, before the
    handler ever looks up the run/approval — so this 422s the same way
    whether or not ``run_id`` addresses a real pending approval."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{uuid4()}:decide",
        json={
            "user_id": "cust-77",
            "decision": "modify",
            "modified_args": {"path": "a\x00b"},
        },
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_decide_idempotency_key_body_field_with_nul_is_422(ctx: _Ctx) -> None:
    """Distinct from the run-creation endpoint's ``Idempotency-Key`` HEADER
    test above — this is a BODY field on ``:decide`` feeding a different
    column (``agent_approval.idempotency_key``, via the same
    ``mark_decided`` CAS write ``modified_args`` uses)."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{uuid4()}:decide",
        json={
            "user_id": "cust-77",
            "decision": "approve",
            "idempotency_key": "key-\x00-1",
        },
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


@pytest.mark.asyncio
async def test_disable_reason_with_nul_is_422(ctx: _Ctx) -> None:
    """Bonus finding, not in the original field list: ``POST
    /{name}/disable|enable`` are not ``console_only()`` — a ``write``-scope
    API key maps to OPERATOR, which is granted ``manifest: {read, write}``
    (``rbac.py``), so this is reachable by the same external caller class as
    every other field above. ``reason`` lands in ``agent_disable.reason``
    (``Text``) verbatim."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/disable",
        json={"reason": "a\x00b"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp, code="INVALID_REQUEST")


# ---------------------------------------------------------------------------
# Non-regression — legitimate content must NOT be rejected. Every field a
# third party is documented to carry free text in (an email body, a support
# ticket) routinely contains "\n" / "\t" / "\r" and non-ASCII — blocking
# those would be a functional regression, not a security fix. Mutation 3
# (broadening the guard to also reject "\n") must turn every test below red.
# ---------------------------------------------------------------------------

_LEGIT_TEXT = "line one\nline two\ttabbed\r\nCRLF too 你好世界 🎉"


@pytest.mark.asyncio
async def test_title_with_newline_tab_and_unicode_is_accepted(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    session_id = await ctx.bind_session()
    resp = await ctx.client.patch(
        f"/v1/agents/support-bot/sessions/{session_id}",
        json={"user_id": "cust-77", "title": _LEGIT_TEXT},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["title"] == _LEGIT_TEXT


@pytest.mark.asyncio
async def test_input_with_newline_tab_and_unicode_is_accepted(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": _LEGIT_TEXT, "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_untrusted_content_with_newline_tab_and_unicode_is_accepted(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "input": "hi",
            "mode": "queue",
            "untrusted_content": [_LEGIT_TEXT],
        },
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text
