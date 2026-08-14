"""API-key scope enforcement on the console plane — now defense in depth only.

The ``/v1/sessions`` / ``/v1/approvals`` / ``/v1/runs`` / upload routers
predate ``require(...)`` and enforced no scope for service-account (API-key)
principals: any valid same-tenant key — including one minted with zero
scopes — could read run output, start or resume runs (bypassing the
``require("session", "write")`` gate on ``POST /v1/agents/{agent_code}/runs``),
decide approvals and purge sessions. #1153 pinned a scope gate here.

P1 external-API lockdown (``console_only()``, ``api/_authz.py``) supersedes
that: the console plane is now closed to every service-account (API-key)
principal outright, regardless of scope — third parties use
``/v1/agents/{agent_code}/...`` instead (see
``tests/test_console_lockdown.py`` for the dedicated lockdown coverage,
including a programmatic audit that every console route carries the gate).
``require_key_scope`` stays wired underneath ``console_only()`` as defense
in depth (belt-and-braces against a future regression that narrows or
removes the lockdown), so this file still exercises it — but with the
lockdown active, **any** key scope now 403s on **every** endpoint of this
plane, including the ``read`` / ``write`` / ``admin`` scopes that used to
pass their matching gate:

- zero-scope key → 403 on every endpoint of the plane (unchanged)
- ``read`` key → 403 everywhere (previously: passed read endpoints)
- ``write`` key → 403 everywhere (previously: passed mutations)
- ``admin`` key → 403 everywhere (previously: passed delete-class)
- human JWTs never hit the key gate (their authz on these routers is
  unchanged — the gate keys off ``subject_type == "service_account"``)

"Passes the gate" (still used for the human-JWT test) is asserted as
``status != 403``: the ids below name resources that do not exist, so handlers
typically 404 after the gate — which is exactly the point (the gate must fire
*before* resource resolution so a 403 carries no existence info).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
# Fixed, NOT ``uuid4()`` — these ids get interpolated into the endpoint paths
# below, and the paths become the ``parametrize`` ids. A fresh random value per
# collection makes the test ids non-deterministic, which breaks anything that
# compares two collections: ``pytest-xdist`` refuses to run outright
# ("Different tests were collected between gw0 and gw1"), and ``--lf`` /
# ``-k`` / CI report diffing all silently stop matching. The values themselves
# are arbitrary — every case here asserts a 403 from the scope gate, which is
# reached before any lookup, so these resources need not exist.
_TID = UUID("00000000-0000-4000-8000-000000000001")
_RID = UUID("00000000-0000-4000-8000-000000000002")
_IMG = UUID("00000000-0000-4000-8000-000000000003")

# (method, path, request-kwargs) — every session-plane endpoint, grouped by
# the scope class its route dependency requires.
READ_ENDPOINTS: list[tuple[str, str, dict[str, object]]] = [
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}", {}),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}/trace", {}),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}/trace/raw", {}),
    ("GET", f"/v1/sessions/{_TID}/messages", {}),
    ("GET", f"/v1/sessions/{_TID}/runs", {}),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}/events", {}),
    ("GET", "/v1/runs", {}),
    ("GET", f"/v1/sessions/{_TID}", {}),
    ("GET", f"/v1/sessions/{_TID}/workspace", {}),
    ("GET", f"/v1/sessions/{_TID}/workspace/files", {}),
    ("GET", f"/v1/sessions/{_TID}/workspace/file", {"params": {"path": "out.txt"}}),
    ("GET", f"/v1/sessions/{_TID}/workspace/artifacts/a.txt/download", {}),
    ("GET", "/v1/sessions", {}),
    ("GET", "/v1/approvals", {}),
    ("GET", f"/v1/sessions/{_TID}/plan", {}),
]

WRITE_ENDPOINTS: list[tuple[str, str, dict[str, object]]] = [
    ("POST", f"/v1/sessions/{_TID}/runs", {"json": {"input": "hi"}}),
    ("POST", f"/v1/sessions/{_TID}/runs/{_RID}/resume", {"json": {"decision": "approve"}}),
    ("POST", "/v1/sessions", {"json": {}}),
    ("PATCH", f"/v1/sessions/{_TID}", {"json": {"title": "t"}}),
    ("POST", f"/v1/sessions/{_TID}:pause", {"json": {}}),
    ("POST", f"/v1/sessions/{_TID}:resume", {"json": {}}),
    ("POST", f"/v1/sessions/{_TID}:cancel", {"json": {}}),
    ("DELETE", f"/v1/sessions/{_TID}/workspace/file", {"params": {"path": "out.txt"}}),
    ("DELETE", f"/v1/sessions/{_TID}/workspace/artifacts/a.txt", {}),
    (
        "POST",
        "/v1/approvals:decide",
        {
            "json": {
                "decisions": [{"thread_id": str(_TID), "run_id": str(_RID), "decision": "approve"}]
            }
        },
    ),
    ("PUT", f"/v1/sessions/{_TID}/plan", {"json": {"goal": "g", "steps": []}}),
    ("POST", f"/v1/sessions/{_TID}/feedback", {"json": {"rating": "up"}}),
    (
        "POST",
        f"/v1/sessions/{_TID}/uploads",
        {"files": {"file": ("a.png", b"x", "image/png")}},
    ),
    ("DELETE", f"/v1/uploads/{_IMG}", {}),
]

DELETE_ENDPOINTS: list[tuple[str, str, dict[str, object]]] = [
    ("DELETE", f"/v1/sessions/{_TID}", {}),
    ("POST", f"/v1/sessions/{_TID}:purge", {}),
]

ALL_ENDPOINTS = READ_ENDPOINTS + WRITE_ENDPOINTS + DELETE_ENDPOINTS


def _key_headers(scopes: tuple[str, ...]) -> dict[str, str]:
    """Bearer headers for a service-account (API-key style) principal."""
    token = make_test_jwt(
        tenant_id=_TENANT,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


def _user_headers(roles: tuple[str, ...]) -> dict[str, str]:
    token = make_test_jwt(tenant_id=_TENANT, roles=roles)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as c:
        yield c


def _ids(endpoints: list[tuple[str, str, dict[str, object]]]) -> list[str]:
    return [f"{m} {p}" for m, p, _ in endpoints]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), ALL_ENDPOINTS, ids=_ids(ALL_ENDPOINTS))
async def test_zero_scope_key_403_on_every_endpoint(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, object]
) -> None:
    response = await client.request(method, path, headers=_key_headers(()), **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "FORBIDDEN"
    # Fix round 2 (review Important I4) — pin the message, not just the code.
    # Both require_key_scope and console_only 403 with code="FORBIDDEN", so a
    # bare code check can't tell whether require_key_scope actually ran (it
    # is listed first in every route's dependencies=[...] and so denies a
    # zero-scope key before console_only ever gets a turn) or whether it had
    # been silently deleted/bypassed and console_only alone is carrying the
    # whole plane. This message is unique to require_key_scope's denial —
    # console_only's is a different string (see test_console_lockdown.py) —
    # so this line is a tripwire on require_key_scope specifically.
    assert response.json()["detail"]["message"] == "API key scopes do not cover this operation"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), READ_ENDPOINTS, ids=_ids(READ_ENDPOINTS))
async def test_read_key_403_on_console_plane(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, object]
) -> None:
    """P1 lockdown: a ``read`` key used to pass this gate — now it 403s too."""
    response = await client.request(method, path, headers=_key_headers(("read",)), **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    WRITE_ENDPOINTS + DELETE_ENDPOINTS,
    ids=_ids(WRITE_ENDPOINTS + DELETE_ENDPOINTS),
)
async def test_read_key_403_on_mutations(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, object]
) -> None:
    response = await client.request(method, path, headers=_key_headers(("read",)), **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), WRITE_ENDPOINTS, ids=_ids(WRITE_ENDPOINTS))
async def test_write_key_403_on_console_plane(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, object]
) -> None:
    """P1 lockdown: a ``write`` key used to pass this gate — now it 403s too."""
    response = await client.request(method, path, headers=_key_headers(("write",)), **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), DELETE_ENDPOINTS, ids=_ids(DELETE_ENDPOINTS))
async def test_write_key_403_on_delete_class(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, object]
) -> None:
    response = await client.request(method, path, headers=_key_headers(("write",)), **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), DELETE_ENDPOINTS, ids=_ids(DELETE_ENDPOINTS))
async def test_admin_key_403_on_console_plane(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, object]
) -> None:
    """P1 lockdown: an ``admin`` key used to pass this gate — now it 403s too."""
    response = await client.request(method, path, headers=_key_headers(("admin",)), **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_user_jwt_never_hits_key_gate(client: AsyncClient) -> None:
    """Human principals keep their pre-gate behavior on this plane."""
    headers = _user_headers(("viewer",))
    # A viewer member triggering a run on an unknown thread: 404 (lookup),
    # never the key gate's 403.
    response = await client.post(f"/v1/sessions/{_TID}/runs", headers=headers, json={"input": "hi"})
    assert response.status_code == 404, response.text
    # Same for the delete-class purge.
    response = await client.post(f"/v1/sessions/{_TID}:purge", headers=headers)
    assert response.status_code == 404, response.text
