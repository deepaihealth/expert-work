"""内容面员工 RBAC —— 阶段 1.5。

The content plane (skills / knowledge / eval-runs / quality / eval-datasets)
carried `console_only()` after P2, which blocks a third-party API key and says
nothing about which *employee* may do what. Route-level authorization was
empty: any employee, `viewer` included, could create and delete knowledge
bases, start eval runs, author and publish skills.

Ruling (2026-08-14): reuse the existing `manifest` resource — read for every
employee, write for operator+, delete for admin only. Zero change to
`rbac.py`; the 38 routes only gained a `Depends`.

    ADMIN     {read, write, delete, sign, approve}
    OPERATOR  {read, write}
    VIEWER    {read}

**What actually proves the gate.** For reads, only a role-less principal can:
viewer, operator and admin all hold `manifest:read`, so a passing viewer
proves nothing. For writes, `viewer` is the prover. For deletes, `operator`.
Each case below picks the weakest role that can fail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


def _headers(*roles: str) -> dict[str, str]:
    """Employee JWT carrying exactly ``roles`` (none at all when empty)."""
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_TENANT, subject=f"emp-{'-'.join(roles) or 'none'}", roles=roles)
    }


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        knowledge_repo=InMemoryKnowledgeStore(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as ac:
        yield ac


def _assert_role_denied(resp: object) -> None:
    """The 403 must come from the RBAC gate, not from something downstream
    that happens to also refuse — otherwise the case passes for the wrong
    reason the day the gate is removed."""
    assert getattr(resp, "status_code", None) == 403, getattr(resp, "text", resp)
    detail = resp.json()["detail"]  # type: ignore[attr-defined]
    assert detail["code"] == "FORBIDDEN", detail
    assert detail["message"] == "principal lacks required role", detail


# --- 读:唯一能证明闸存在的是零角色 principal --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/skills",
        "/v1/knowledge/bases",
        "/v1/eval-runs",
        "/v1/eval-datasets",
        "/v1/quality/scores",
        "/v1/quality/drift-alerts",
    ],
)
async def test_roleless_employee_cannot_read_the_content_plane(
    client: AsyncClient, path: str
) -> None:
    resp = await client.get(path, headers=_headers())
    _assert_role_denied(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/v1/skills", "/v1/knowledge/bases", "/v1/eval-runs", "/v1/eval-datasets"],
)
async def test_viewer_still_reads_the_content_plane(client: AsyncClient, path: str) -> None:
    """The ruling kept reads open to every employee. This is the regression
    sentinel for over-tightening: gate these reads at ``write`` by mistake and
    this goes red."""
    resp = await client.get(path, headers=_headers("viewer"))
    assert resp.status_code == 200, resp.text


# --- 写:viewer 是判据 --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/skills", {"name": "s", "description": "d"}),
        ("POST", "/v1/knowledge/bases", {"name": "kb", "description": "d"}),
        ("POST", "/v1/eval-runs", {"agent_name": "a"}),
        ("POST", "/v1/eval-datasets", {"agent_name": "a", "name": "n", "input": {}}),
        ("PATCH", f"/v1/skills/{uuid4()}", {"status": "active"}),
        ("PATCH", f"/v1/knowledge/bases/{uuid4()}", {"description": "x"}),
    ],
    ids=["skill_create", "kb_create", "eval_run", "dataset_create", "skill_patch", "kb_patch"],
)
async def test_viewer_cannot_write_the_content_plane(
    client: AsyncClient, method: str, path: str, body: dict[str, object]
) -> None:
    """A ``viewer`` writing was the actual gap: before this, any employee could
    create a knowledge base or publish a skill. The paths with a random id
    would 404 *if the gate let them through* — the 403 has to arrive first,
    which is why ``_assert_role_denied`` pins the message."""
    resp = await client.request(method, path, json=body, headers=_headers("viewer"))
    _assert_role_denied(resp)


@pytest.mark.asyncio
async def test_operator_can_write_the_content_plane(client: AsyncClient) -> None:
    """Operator+ keeps write. Without this, gating everything at ``delete``
    would look just as green as the correct matrix."""
    resp = await client.post(
        "/v1/knowledge/bases",
        json={"name": "kb-operator", "description": "d"},
        headers=_headers("operator"),
    )
    assert resp.status_code in (200, 201), resp.text


# --- 删:operator 是判据 ------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_cannot_delete_a_knowledge_base(client: AsyncClient) -> None:
    """Deleting an ingested corpus is irreversible — admin only. ``operator``
    is the prover here: it holds ``manifest:write``, so a 403 can only be the
    ``delete`` action being required."""
    created = await client.post(
        "/v1/knowledge/bases",
        json={"name": "kb-to-delete", "description": "d"},
        headers=_headers("admin"),
    )
    assert created.status_code in (200, 201), created.text

    resp = await client.delete("/v1/knowledge/bases/kb-to-delete", headers=_headers("operator"))
    _assert_role_denied(resp)

    survived = await client.get("/v1/knowledge/bases/kb-to-delete", headers=_headers("admin"))
    assert survived.status_code == 200, "the 403 must be a gate, not a cosmetic status code"


@pytest.mark.asyncio
async def test_admin_can_delete_a_knowledge_base(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/knowledge/bases",
        json={"name": "kb-admin-delete", "description": "d"},
        headers=_headers("admin"),
    )
    assert created.status_code in (200, 201), created.text
    resp = await client.delete("/v1/knowledge/bases/kb-admin-delete", headers=_headers("admin"))
    assert resp.status_code == 204, resp.text


# --- 两处刻意偏离动词映射的地方 ----------------------------------------------


@pytest.mark.asyncio
async def test_unsubscribe_is_a_write_not_a_delete(client: AsyncClient) -> None:
    """``DELETE /v1/skills/{id}/subscribe`` is gated at ``write``, not
    ``delete``.

    Subscribing and unsubscribing are one reversible pair, and skills.py's own
    inline gate already rules that pair operator-level. Mapping the verb
    blindly would have made unsubscribing admin-only while subscribing stayed
    operator — an asymmetry nothing asked for.
    """
    resp = await client.delete(f"/v1/skills/{uuid4()}/subscribe", headers=_headers("operator"))
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_knowledge_base_test_query_is_a_read(client: AsyncClient) -> None:
    """``POST /v1/knowledge/bases/{name}/test`` runs a retrieval query. The
    verb is POST only because the query travels in a body; nothing is
    mutated, so it is gated at ``read`` and a viewer may run it."""
    resp = await client.post(
        "/v1/knowledge/bases/nonexistent/test",
        json={"query": "q"},
        headers=_headers("viewer"),
    )
    assert resp.status_code != 403, resp.text
