"""B-49 —— viewer 是只读旁观者:控制台面的写操作一律 operator+。

背景。182 条 ``console_only()`` 路由做过三身份实证扫描(viewer / operator /
admin 各发真请求)。``console_only()`` 关的是 API key 那一轴,对「哪个员工角色
能做什么」一个字都没说;角色轴大面积存在(84 条 viewer 403),但下面这 18 条写
操作 viewer 全都够得着:建会话、改名、归档、:purge、:pause/:resume/:cancel、
起 run、传附件、删附件、删产物、改产物、覆写计划、删工作区文件、打一次花钱的
检索测试、往晋升队列塞候选。

拍板(2026-09-11):读全保留,写一律 operator+。#1489 先修掉 kill-switch 两条
与 triggers 四条,这里收余下 18 条。

什么算证明。
* **viewer → 403 且 detail.code == FORBIDDEN**:403 必须来自 RBAC 闸本身,不是
  归属闸(404)、user-scope 闸(``USER_SCOPE_FORBIDDEN``)或平面闸。只断言
  「403」会把别的闸的 403 也算成通过。
* **operator → 不是 403**:证明没收过头。archive / :purge 的 key scope 是
  ``delete``,但 ``session:delete`` 只有 ADMIN 持有 —— 角色档要是跟着挂 delete,
  operator 这条会红,正是它要拦的错。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

# Fixed, NOT ``uuid4()`` — these ids are interpolated into the paths below,
# which become the ``parametrize`` ids. A fresh value per collection makes the
# ids non-deterministic and ``pytest-xdist`` refuses to run. Same rule (and
# same rationale) as ``test_console_lockdown.py``. Nothing needs to exist:
# every case asserts the role gate fires *before* resource resolution.
_TID = UUID("00000000-0000-4000-8000-0000000000b4")
_IMG = UUID("00000000-0000-4000-8000-0000000000b5")
_SKILL = UUID("00000000-0000-4000-8000-0000000000b6")

_TENANT = UUID("00000000-0000-4000-8000-0000000000b0")


#: ``(method, path, request kwargs)`` for each of the 18 收窄的写操作.
#: The parametrize id is ``"<METHOD> <path>"`` so a red case names its route.
_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    # ── sessions.py — 会话与其工作区的写(session:write) ──────────────────
    ("POST", "/v1/sessions", {"json": {"agent_name": "ghost", "agent_version": "1.0.0"}}),
    ("PATCH", f"/v1/sessions/{_TID}", {"json": {"title": "renamed"}}),
    ("DELETE", f"/v1/sessions/{_TID}", {}),
    ("POST", f"/v1/sessions/{_TID}:cancel", {"json": {}}),
    ("POST", f"/v1/sessions/{_TID}:pause", {"json": {}}),
    ("POST", f"/v1/sessions/{_TID}:resume", {"json": {}}),
    ("POST", f"/v1/sessions/{_TID}:purge", {"json": {}}),
    ("DELETE", f"/v1/sessions/{_TID}/workspace/file", {"params": {"path": "out.txt"}}),
    ("DELETE", f"/v1/sessions/{_TID}/workspace/artifacts/report.md", {}),
    # ── runs.py / uploads.py — 起 run 与附件(session:write) ──────────────
    ("POST", f"/v1/sessions/{_TID}/runs", {"json": {"input": "hi"}}),
    (
        "POST",
        f"/v1/sessions/{_TID}/uploads",
        {"files": {"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")}},
    ),
    ("DELETE", f"/v1/uploads/{_IMG}", {}),
    # ── artifacts.py / workspace.py — 同一份每用户数据的第二个入口 ────────
    ("PATCH", "/v1/artifacts/report.md", {"json": {"kind": "document"}}),
    ("DELETE", "/v1/artifacts/report.md", {}),
    ("DELETE", "/v1/workspace/file", {"params": {"path": "out.txt"}}),
    # ── plan.py — 覆写计划(session:write) ────────────────────────────────
    (
        "PUT",
        f"/v1/sessions/{_TID}/plan",
        {"json": {"goal": "ship it", "steps": [], "version": 1}},
    ),
    # ── knowledge.py — 会花钱的检索测试(manifest:write) ──────────────────
    ("POST", "/v1/knowledge/bases/kb/test", {"json": {"query": "q"}}),
    # ── skill_evolution.py — 晋升提名,与审批侧对齐(manifest:write) ──────
    (
        "POST",
        f"/v1/skill-evolution/skills/{_SKILL}/promote-requests",
        {"json": {"skill_version": 1}},
    ),
]

_CASES = [
    pytest.param(method, path, kwargs, id=f"{method} {path}")
    for method, path, kwargs in _WRITES
]


def _headers(role: str) -> dict[str, str]:
    """Employee JWT carrying exactly one tenant role."""
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_TENANT, subject=f"emp-{role}", roles=(role,))
    }


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=Settings(
            service_name="control_plane_test",
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        enable_reaper=False,
        enable_scheduler=False,
        enable_curation_worker=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control-plane.test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), _CASES)
async def test_viewer_is_denied_by_the_role_gate(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """A viewer JWT is 403'd — **by the RBAC gate**, before anything resolves."""
    resp = await client.request(method, path, headers=_headers("viewer"), **kwargs)
    assert resp.status_code == 403, f"{method} {path}: {resp.status_code} {resp.text}"
    detail = resp.json()["detail"]
    assert detail["code"] == "FORBIDDEN", f"{method} {path}: {detail}"
    assert detail["message"] == "principal lacks required role", f"{method} {path}: {detail}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kwargs"), _CASES)
async def test_operator_is_not_denied_by_the_role_gate(
    client: AsyncClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """An operator JWT passes the role gate — 没收过头.

    Nothing is seeded, so what comes back is a 404 / 409 / 422 / 503 from the
    resource layer. That is the point: any status *but* 403 proves the gate
    let the operator through. Gate any of these at ``session:delete`` or
    ``manifest:delete`` (neither of which OPERATOR holds) and this goes red.
    """
    resp = await client.request(method, path, headers=_headers("operator"), **kwargs)
    assert resp.status_code != 403, f"{method} {path}: {resp.status_code} {resp.text}"
