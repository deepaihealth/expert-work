# 第三方对接 API v1 — P1 契约地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给第三方 app 一组自洽的对外契约(6 个新端点 + 已有 run 端点),并把控制台平面对 API Key 关闭。

**Architecture:** 新端点全部挂 `/v1/agents/{agent_code}/` 命名空间,各自独立文件,共用一个 `_external.py` 归属校验模块(app 自有 `user_id` 字符串 → `tenant_user` → 校验目标资源属于该 (tenant, user, agent),不符一律 404)。外部身份的 `subject_id` 加 `ext:` 前缀,与员工的 Keycloak UUID 隔离。控制台平面(`/v1/sessions/*` 等)对 `service_account` 主体 403。

**Tech Stack:** FastAPI / Pydantic v2 / pytest(`asyncio_mode = "auto"`)/ httpx `ASGITransport`。

## Global Constraints

- **404 而非 403**:归属校验失败一律 404,不区分"不存在"与"不属于你"(隐藏存在性)。已有 `_resolve_session` 就是这个语义。
- **统一信封**:所有 JSON 响应 `{"success": bool, "data": ..., "error": {"code","message"} | None}`。
- **`user_id` 必填**:每个对外端点必收第三方自有标识字符串(1–255 字符),不得回退成"看调用者是谁"。
- **外部身份前缀**:外部 `subject_id` 一律 `ext:{user_id}`;`subject_type` 保持 `"user"` 不变(用户维度运维页 `api/agent_users.py:377` 与删用户链路 `purge/user_purge.py` 都按它取数,换类型会让外部用户从运维页消失且无法删除)。
- **不新增存储路径**:复用既有 store 方法,不为对外端点另开表。
- **测试命令**(仓库根执行,control-plane 单测不需要 Docker):
  `uv run pytest services/control-plane/tests/<file> -q`
- **门禁**:`uv run ruff check <files>` + `uv run ruff format --check <files>` + `uv run mypy <files>`。ruff 跑全库时也要过。
- **变异自证(仓库铁律)**:每条新断言必须 break→red→restore→green 自证,还原一律用反向文本替换,**禁止 `git checkout`**(会吞掉未提交的其他修改,本仓库已踩三次)。
- **`rg -r` 是 replace 不是 recursive**——递归是默认行为,不要加 `-r`。

## 并行波次

预检冲突分析已做(见每个 Task 的 Files 段):

- **Wave 0(串行)**:Task 1 —— 建 `_external.py` + **5 个空壳路由文件** + `api/__init__.py` 导出 + `app.py` 注册 + `ext:` 前缀。**后续所有任务只填充自己那个文件,不再碰 `app.py` / `__init__.py`。**
- **Wave 1(5 个并行)**:Task 2 / 3 / 4 / 5 / 6 —— 各自独占新文件。其中 Task 4 另改 `api/runs.py`(抽共享流式模块)、Task 5 另改 `api/uploads.py`(抽共享落盘函数),两者互不重叠。
- **Wave 2(串行)**:Task 7 —— 控制台收口。**必须排在 Wave 1 之后**:它要给 `runs.py` / `uploads.py` 的路由装饰器加依赖,而这两个文件正是 Task 4 / Task 5 要改的(同文件并行会冲突)。
- **Wave 3(串行)**:Task 8 —— 全链集成测试。

---

### Task 1: 归属校验模块 + 路由骨架 + `ext:` 前缀

**Files:**
- Create: `services/control-plane/src/control_plane/api/_external.py`
- Create(空壳): `services/control-plane/src/control_plane/api/external_runs.py`、`external_events.py`、`external_sessions.py`、`external_uploads.py`、`external_approvals.py`
- Modify: `services/control-plane/src/control_plane/api/__init__.py`(导出 5 个 `build_*_router`)
- Modify: `services/control-plane/src/control_plane/app.py:2495` 之后(注册 5 个 router)+ `app.py:40-96` 导入列表
- Modify: `services/control-plane/src/control_plane/api/agents.py:329`(加前缀)
- Test: `services/control-plane/tests/test_external_scope.py`

**Interfaces:**
- Produces:
  - `EXTERNAL_SUBJECT_PREFIX: str = "ext:"`
  - `external_subject_id(user_id: str) -> str`
  - `class ExternalScopeError(Exception)`,属性 `code: str` / `message: str` / `status_code: int`
  - `external_error(exc: ExternalScopeError) -> JSONResponse`
  - `async resolve_external_user_id(*, tenant_id: UUID, user_id: str, users: TenantUserStore) -> UUID`
  - `async load_owned_session(*, tenant_id: UUID, agent_code: str, user_id: str, session_id: UUID, threads: ThreadMetaStore, users: TenantUserStore) -> ThreadMeta`
  - `async load_owned_run(*, tenant_id: UUID, agent_code: str, user_id: str, run_id: UUID, runs: RunStore, threads: ThreadMetaStore, users: TenantUserStore) -> tuple[RunInfo, ThreadMeta]`
  - 5 个 `build_external_*_router() -> APIRouter`(本任务只建空壳,Wave 1 各自填充)

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_scope.py`

```python
"""External-plane ownership gate — the shared 404 semantics behind every
``/v1/agents/{agent_code}/...`` endpoint a third-party API key can reach."""

from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.api._external import (
    EXTERNAL_SUBJECT_PREFIX,
    ExternalScopeError,
    external_subject_id,
    load_owned_session,
    resolve_external_user_id,
)
from expert_work.persistence.tenant_user import InMemoryTenantUserStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore


def test_external_subject_id_namespaces_the_app_supplied_id() -> None:
    assert external_subject_id("cust-77") == "ext:cust-77"
    assert EXTERNAL_SUBJECT_PREFIX == "ext:"


def test_external_subject_id_cannot_collide_with_a_keycloak_uuid() -> None:
    # An employee's subject_id is a bare Keycloak sub (a UUID). A third party
    # passing that exact UUID must NOT resolve to the employee's row.
    employee_sub = str(uuid4())
    assert external_subject_id(employee_sub) != employee_sub


@pytest.mark.asyncio
async def test_resolve_external_user_id_is_stable_and_prefixed() -> None:
    users = InMemoryTenantUserStore()
    tenant_id = uuid4()
    first = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    again = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    assert first == again  # mint-on-use is idempotent

    stored = await users.get(first, tenant_id=tenant_id)
    assert stored is not None
    assert stored.subject_id == "ext:cust-77"
    assert stored.subject_type == "user"  # ops page + purge pipeline key on this


@pytest.mark.asyncio
async def test_external_user_never_resolves_to_an_employee_row() -> None:
    users = InMemoryTenantUserStore()
    tenant_id = uuid4()
    employee_sub = str(uuid4())
    employee = await users.resolve(
        tenant_id=tenant_id, subject_type="user", subject_id=employee_sub
    )
    impostor = await resolve_external_user_id(
        tenant_id=tenant_id, user_id=employee_sub, users=users
    )
    assert impostor != employee.id


@pytest.mark.asyncio
async def test_load_owned_session_returns_the_session_for_its_owner() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    meta = await load_owned_session(
        tenant_id=tenant_id,
        agent_code="support-bot",
        user_id="cust-77",
        session_id=session_id,
        threads=threads,
        users=users,
    )
    assert meta.thread_id == session_id


@pytest.mark.asyncio
async def test_load_owned_session_404s_for_another_user() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    with pytest.raises(ExternalScopeError) as caught:
        await load_owned_session(
            tenant_id=tenant_id,
            agent_code="support-bot",
            user_id="someone-else",
            session_id=session_id,
            threads=threads,
            users=users,
        )
    assert caught.value.status_code == 404
    assert caught.value.code == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_load_owned_session_404s_for_another_agent() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    with pytest.raises(ExternalScopeError) as caught:
        await load_owned_session(
            tenant_id=tenant_id,
            agent_code="other-bot",
            user_id="cust-77",
            session_id=session_id,
            threads=threads,
            users=users,
        )
    assert caught.value.status_code == 404
```

> **注意**:`InMemoryTenantUserStore` / `InMemoryThreadMetaStore` 的确切导入路径以仓库现状为准 —— 先 `rg -n "class InMemoryTenantUserStore|class InMemoryThreadMetaStore" packages/` 确认再写 import。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_external_scope.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'control_plane.api._external'`

- [ ] **Step 3: 写 `api/_external.py`**

```python
"""Shared resolution + ownership gate for the external (third-party) API plane.

Every ``/v1/agents/{agent_code}/...`` endpoint a third-party API key can reach
goes through here: the app's own ``user_id`` string is resolved to a
``tenant_user`` row, and the addressed resource (session / run) is verified to
belong to that ``(tenant, user, agent)`` triple. A mismatch is 404 — never 403 —
so the response carries no existence information. Mirrors the check
``agents.py:_resolve_session`` already performs for ``session_id``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi.responses import JSONResponse

from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import ThreadMeta
from expert_work.runtime.runs import RunInfo, RunStore

#: Namespace prefix for end-user identities minted from a third-party app's own
#: ``user_id`` string. An employee's ``subject_id`` is a bare Keycloak ``sub``
#: (a UUID), so without this prefix a third party could pass an employee's UUID
#: and reach that employee's console sessions. ``subject_type`` deliberately
#: stays ``"user"``: the user-dimension ops page (``api/agent_users.py``) and the
#: delete-user pipeline (``purge/user_purge.py``) both select on it — a distinct
#: type would hide external users from the former and make them unpurgeable by
#: the latter.
EXTERNAL_SUBJECT_PREFIX = "ext:"


def external_subject_id(user_id: str) -> str:
    """Namespace an app-supplied ``user_id`` for ``tenant_user.subject_id``."""
    return f"{EXTERNAL_SUBJECT_PREFIX}{user_id}"


class ExternalScopeError(Exception):
    """Resolution / ownership failure, converted to an envelope by the endpoint."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def external_error(exc: ExternalScopeError) -> JSONResponse:
    """Render an :class:`ExternalScopeError` as the standard envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": exc.code, "message": exc.message},
        },
    )


async def resolve_external_user_id(
    *, tenant_id: UUID, user_id: str, users: TenantUserStore
) -> UUID:
    """Resolve (mint-on-use) an app-supplied ``user_id`` to ``tenant_user.id``."""
    row = await users.resolve(
        tenant_id=tenant_id,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
    return row.id


async def load_owned_session(
    *,
    tenant_id: UUID,
    agent_code: str,
    user_id: str,
    session_id: UUID,
    threads: ThreadMetaStore,
    users: TenantUserStore,
) -> ThreadMeta:
    """Return the session, or raise 404 unless it belongs to ``(user, agent)``."""
    end_user_id = await resolve_external_user_id(
        tenant_id=tenant_id, user_id=user_id, users=users
    )
    meta = await threads.get(session_id, tenant_id=tenant_id)
    if meta is None or meta.user_id != end_user_id or meta.agent_name != agent_code:
        raise ExternalScopeError(
            "SESSION_NOT_FOUND", "session not found for this user / agent", 404
        )
    return meta


async def load_owned_run(
    *,
    tenant_id: UUID,
    agent_code: str,
    user_id: str,
    run_id: UUID,
    runs: RunStore,
    threads: ThreadMetaStore,
    users: TenantUserStore,
) -> tuple[RunInfo, ThreadMeta]:
    """Return ``(run, its session)``, or raise 404 unless both belong to ``(user, agent)``.

    A run whose session fails the ownership check reports ``RUN_NOT_FOUND`` — not
    the session's code — so the caller cannot tell "this run exists but is
    someone else's" from "no such run".
    """
    run = await runs.get(run_id=run_id, tenant_id=tenant_id)
    if run is None:
        raise ExternalScopeError("RUN_NOT_FOUND", "run not found", 404)
    try:
        meta = await load_owned_session(
            tenant_id=tenant_id,
            agent_code=agent_code,
            user_id=user_id,
            session_id=run.thread_id,
            threads=threads,
            users=users,
        )
    except ExternalScopeError:
        raise ExternalScopeError("RUN_NOT_FOUND", "run not found", 404) from None
    return run, meta
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_external_scope.py -q`
Expected: PASS(7 项)

- [ ] **Step 5: 变异自证**

对 `load_owned_session` 依次做三次变异,每次跑上面的测试文件确认变红后**用反向文本替换还原**(禁止 `git checkout`):

1. 删掉 `meta.user_id != end_user_id` 这一段 → `test_load_owned_session_404s_for_another_user` 必须红
2. 删掉 `meta.agent_name != agent_code` 这一段 → `test_load_owned_session_404s_for_another_agent` 必须红
3. 把 `external_subject_id` 改成 `return user_id`(去掉前缀)→ `test_external_user_never_resolves_to_an_employee_row` 必须红

三次都必须先红再还原为绿,并把结果写进报告。

- [ ] **Step 6: 建 5 个空壳路由文件**

每个文件同一形状,只有名字与 tag 不同。以 `external_runs.py` 为例:

```python
"""External run control for third-party apps — ``/v1/agents/{agent_code}/runs/...``.

Filled in by the external-API P1 plan, Task 2.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_external_runs_router() -> APIRouter:
    """Mount the external run-control endpoints."""
    return APIRouter(prefix="/v1/agents", tags=["external"])
```

同样建:
- `external_events.py` → `build_external_events_router()`
- `external_sessions.py` → `build_external_sessions_router()`
- `external_uploads.py` → `build_external_uploads_router()`
- `external_approvals.py` → `build_external_approvals_router()`

五个都用 `prefix="/v1/agents"`、`tags=["external"]`。

- [ ] **Step 7: 导出与注册**

`api/__init__.py`:在既有 `from control_plane.api.xxx import build_xxx_router` 列表里按字母序加 5 行导入,并把 5 个名字加进 `__all__`。

`app.py`:在 `app.py:40-96` 的 `from control_plane.api import (...)` 里按字母序插入 5 个名字;在 `app.py:2495`(`app.include_router(build_platform_quality_config_router())`)之后加:

```python
    # 第三方对接 API v1(P1)—— 对外契约,与控制台平面分开。
    app.include_router(build_external_runs_router())
    app.include_router(build_external_events_router())
    app.include_router(build_external_sessions_router())
    app.include_router(build_external_uploads_router())
    app.include_router(build_external_approvals_router())
```

- [ ] **Step 8: 给 `_resolve_session` 加前缀**

`api/agents.py:329`,把:

```python
    end_user = await users.resolve(tenant_id=tenant_id, subject_type="user", subject_id=user_id)
```

改成:

```python
    end_user = await users.resolve(
        tenant_id=tenant_id,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
```

并在 `agents.py` 顶部导入 `from control_plane.api._external import external_subject_id`。
同时把上方那段注释里"subject type "user" + the app's id is unique per tenant"改成说明前缀的理由(指向 `_external.py` 的常量 docstring)。

> 存量已核实:测试集群 `tenant_user(subject_type='user')` 仅 3 行(2 行 Keycloak UUID + 1 行测试外部用户)、`thread_meta` 仅 2 行;生产无第三方数据。**因此不需要数据迁移**——那 1 行测试数据下次调用会以新前缀重新铸造。

- [ ] **Step 9: 回归 + 门禁**

Run:
```
uv run pytest services/control-plane/tests/test_external_scope.py services/control-plane/tests/test_agents_run_for_user.py services/control-plane/tests/test_agents_bind_session.py -q
uv run ruff check services/control-plane/src/control_plane/api/ services/control-plane/tests/test_external_scope.py
uv run ruff format --check services/control-plane/src/control_plane/api/ services/control-plane/tests/test_external_scope.py
uv run mypy services/control-plane/src/control_plane/api/_external.py
```
Expected: 全部 PASS。`test_agents_run_for_user.py` 里断言 `subject_id="cust-77"` 的地方会因为前缀而失败 —— **改成 `subject_id="ext:cust-77"`**(这是有意变更,不是回归)。

- [ ] **Step 10: Commit**

```bash
git add services/control-plane/src/control_plane/api/ services/control-plane/src/control_plane/app.py services/control-plane/tests/test_external_scope.py
git commit -m "feat(control-plane): 对外 API 归属校验模块 + 路由骨架 + ext: 身份前缀"
```

---

### Task 2: run 级取消端点

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_runs.py`(Task 1 建的空壳,本任务独占)
- Test: `services/control-plane/tests/test_external_runs_cancel.py`

**Interfaces:**
- Consumes: `_external.load_owned_run` / `external_error` / `ExternalScopeError`(签名见 Task 1 的 Produces)
- Produces: `POST /v1/agents/{agent_code}/runs/{run_id}:cancel`,body `{"user_id": str}`,
  响应 `{"success": true, "data": {"run_id": str, "stopped": bool}}`

**背景(实现者必读)**:会话级 `POST /v1/sessions/{id}:cancel` **只改会话状态**,执行引擎全程不读会话状态,正在跑的 run 不受影响。真正停 run 的是两级原语,生产在用(租户停用 / Agent 下线):本副本 `runtime.run_manager.cancel(run_id)` 立即置 `abort_event`;跨副本 `run_store.request_cancel(...)` 写库 CAS,持有方下次租约心跳失败后自停。`agents.py:1257` 与 `tenants.py:178` 是现成调用样板(`await runtime.run_manager.cancel(...) or await run_store.request_cancel(...)`)。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_runs_cancel.py`

装配照抄 `services/control-plane/tests/test_agents_run_for_user.py:50-108` 的 `_build_settings` / `_Ctx` / `ctx` fixture(含 `_SPEC` 常量与 `seed_agent`),然后:

```python
@pytest.mark.asyncio
async def test_cancel_stops_a_queued_run(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["run_id"] == run_id
    assert body["data"]["stopped"] is True

    run = await ctx.run_store.get(run_id=UUID(run_id), tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_cancel_404s_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_404s_for_another_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/other-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cancel_is_idempotent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    first = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert first.status_code == 200
    second = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    # A second cancel must not error — it reports that nothing was still running.
    assert second.status_code == 200, second.text
    assert second.json()["data"]["stopped"] is False
```

需要的 import:`from uuid import UUID` 与 `from expert_work.runtime.runs import RunStatus`(确切导出路径先 `rg -n "class RunStatus" packages/` 核实)。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_external_runs_cancel.py -q`
Expected: FAIL —— 404(路由不存在)

- [ ] **Step 3: 实现 `external_runs.py`**

```python
"""External run control for third-party apps — ``/v1/agents/{agent_code}/runs/...``.

Only run-level cancel lives here. Session-level cancel (``POST
/v1/sessions/{id}:cancel``) is an irreversible close — it flips the thread to
CANCELLED so every later run is refused — and stays a console-only operation.
An end user's "stop" button wants this endpoint: it aborts the current
execution and leaves the conversation usable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
)
from control_plane.api._user_scope import get_user_repo
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import Principal
from expert_work.runtime.runs import RunStore


class ExternalCancelRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}:cancel``.

    ``user_id`` is the app's own end-user identifier and is verified against the
    run's session — an app cannot cancel another end user's run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def build_external_runs_router() -> APIRouter:
    """Mount the external run-control endpoints."""
    router = APIRouter(prefix="/v1/agents", tags=["external"])

    @router.post("/{agent_code}/runs/{run_id}:cancel", response_model=None)
    async def cancel_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalCancelRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
    ) -> JSONResponse:
        """Abort an in-flight run. Works in both ``stream`` and ``queue`` mode.

        Two-level, reusing the primitive the tenant-suspend / agent-kill switches
        already rely on: a run owned by THIS replica is aborted immediately via
        the manager's abort event; a run owned by another replica is CAS-flipped
        to INTERRUPTED in the store, and its owner stops within one lease
        heartbeat. Idempotent — cancelling a finished run reports
        ``stopped: false`` rather than erroring.
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            run, _meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=payload.user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        runtime = request.app.state.agent_runtime
        stopped = await runtime.run_manager.cancel(run.run_id) or await runs.request_cancel(
            run_id=run.run_id, tenant_id=tenant_id, updated_at=datetime.now(UTC)
        )
        return JSONResponse(
            {
                "success": True,
                "data": {"run_id": str(run.run_id), "stopped": bool(stopped)},
                "error": None,
            }
        )

    return router
```

> `request.app.state` 上 thread / run store 的确切属性名以仓库现状为准 —— 先照 `api/runs.py` 里 `_get_thread_repo` / `_get_run_store` 的写法抄。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_external_runs_cancel.py -q`
Expected: PASS(4 项)

- [ ] **Step 5: 变异自证**

1. 把 `load_owned_run` 调用整段删掉、直接用 `runs.get(...)` 取 run(绕过归属校验)→ `test_cancel_404s_for_another_user` 与 `..._another_agent` 必须红
2. 把 `or await runs.request_cancel(...)` 删掉 → 由于测试用 in-memory manager,`cancel` 会返回 True,此变异**不致红**;因此另加一条断言:mock 掉 `runtime.run_manager.cancel` 让它返回 `False`,断言仍会走 `request_cancel` 且 `stopped is True`。**先补这条断言再做变异**,否则这条回退路径是没有测试保护的。

还原一律用反向文本替换。

- [ ] **Step 6: 门禁 + Commit**

```bash
uv run ruff check services/control-plane/src/control_plane/api/external_runs.py services/control-plane/tests/test_external_runs_cancel.py
uv run ruff format --check services/control-plane/src/control_plane/api/external_runs.py services/control-plane/tests/test_external_runs_cancel.py
uv run mypy services/control-plane/src/control_plane/api/external_runs.py
git add services/control-plane/src/control_plane/api/external_runs.py services/control-plane/tests/test_external_runs_cancel.py
git commit -m "feat(control-plane): 对外 run 级取消端点(stream/queue 双模式 + 归属校验)"
```

---

### Task 3: 会话列表 + 消息历史端点

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_sessions.py`(本任务独占)
- Test: `services/control-plane/tests/test_external_sessions.py`

**Interfaces:**
- Consumes: `_external.load_owned_session` / `resolve_external_user_id` / `external_error` / `ExternalScopeError`
- Produces:
  - `GET /v1/agents/{agent_code}/sessions?user_id=&limit=&offset=` →
    `{"success": true, "data": {"sessions": [{"session_id","title","created_at","updated_at","running"}], "limit", "offset"}}`
  - `GET /v1/agents/{agent_code}/sessions/{session_id}/messages?user_id=&limit=&offset=` →
    `{"success": true, "data": {"messages": [{"role","content","channel"}], "limit", "offset"}}`

**关键约束**:`user_id` 是**必填** query 参数。现有控制台列表端点对机器身份的归属过滤会变成空,返回整租户会话 —— 对外端点绝不允许这个退化。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_sessions.py`

装配同 Task 2(照抄 `test_agents_run_for_user.py` 的 fixture),用例:

```python
@pytest.mark.asyncio
async def test_sessions_list_only_returns_this_users_sessions(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    a = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert a.status_code == 202, a.text
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-99", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["data"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == a.headers["X-Expert-Work-Session-Id"]


@pytest.mark.asyncio
async def test_sessions_list_requires_user_id(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.get("/v1/agents/support-bot/sessions", headers=ctx.headers)
    # Missing the required query param must be rejected, never silently widened
    # to "every session in the tenant".
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_sessions_list_is_scoped_to_the_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    resp = await ctx.client.get(
        "/v1/agents/other-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["sessions"] == []


@pytest.mark.asyncio
async def test_messages_404_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.headers["X-Expert-Work-Session-Id"]
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_messages_returns_envelope_for_its_owner(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.headers["X-Expert-Work-Session-Id"]
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert isinstance(body["data"]["messages"], list)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_external_sessions.py -q`
Expected: FAIL —— 404(路由不存在)

- [ ] **Step 3: 实现 `external_sessions.py`**

要点(逐条照做):

1. `list_sessions` 依赖 `require("session", "read")`;`user_id: Annotated[str, Query(min_length=1, max_length=255)]` **不给默认值**(缺失 → FastAPI 422,这正是 `test_sessions_list_requires_user_id` 要的)。
2. 先 `resolve_external_user_id(...)` 拿 `end_user_id`,再调:

```python
        rows = await threads.list_by_tenant(
            tenant_id,
            user_id=end_user_id,
            agent_name=agent_code,
            include_archived=False,
            limit=limit,
            offset=offset,
        )
```
   注意 `tenant_id` 是**位置参数**,其余全部 keyword-only(签名见 `persistence/thread_meta/base.py:81-97`)。
3. `running` 字段用 `await runtime.run_manager.has_inflight(row.thread_id, tenant_id=tenant_id)`(签名 `manager.py:398`)。
4. 每项输出:

```python
            {
                "session_id": str(row.thread_id),
                "title": row.title,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "running": running,
            }
```
5. `get_messages` 依赖 `require("session", "read")`,先 `load_owned_session(...)`(失败 → `external_error`),再照 `api/runs.py:1393-1410` 的做法读 turns:

```python
        checkpointer = runtime.durable_checkpointer
        if checkpointer is None:
            return JSONResponse({"success": True, "data": {"messages": [], "limit": limit, "offset": offset}, "error": None})
        try:
            turns = await read_turns(checkpointer, session_id, include_hidden=False)
        except Exception:
            logger.warning("external_messages.read_failed", exc_info=True)
            turns = []
        page = turns[offset : offset + limit]
        out = [{"role": t.role, "content": t.content, "channel": t.channel} for t in page]
```
   `read_turns` 来自 `control_plane.transcript`(签名 `transcript.py:35-40`);`include_hidden=False` —— 对外一律不暴露编排脚手架消息。
6. `limit`/`offset`:`limit: Annotated[int, Query(ge=1, le=200)] = 50`、`offset: Annotated[int, Query(ge=0)] = 0`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_external_sessions.py -q`
Expected: PASS(5 项)

- [ ] **Step 5: 变异自证**

1. 把 `list_by_tenant` 的 `user_id=end_user_id` 删掉 → `test_sessions_list_only_returns_this_users_sessions` 必须红
2. 把 `agent_name=agent_code` 删掉 → `test_sessions_list_is_scoped_to_the_agent` 必须红
3. 把 `get_messages` 里的 `load_owned_session(...)` 整段删掉 → `test_messages_404_for_another_user` 必须红

三次都反向替换还原。

- [ ] **Step 6: 门禁 + Commit**

```bash
uv run ruff check services/control-plane/src/control_plane/api/external_sessions.py services/control-plane/tests/test_external_sessions.py
uv run ruff format --check services/control-plane/src/control_plane/api/external_sessions.py services/control-plane/tests/test_external_sessions.py
uv run mypy services/control-plane/src/control_plane/api/external_sessions.py
git add services/control-plane/src/control_plane/api/external_sessions.py services/control-plane/tests/test_external_sessions.py
git commit -m "feat(control-plane): 对外会话列表 + 消息历史端点(user_id 必填 + 归属校验)"
```

---

### Task 4: 事件回放端点(断线重连)

**Files:**
- Create: `services/control-plane/src/control_plane/api/_run_event_stream.py`(两条路共用的流式生产者)
- Modify: `services/control-plane/src/control_plane/api/external_events.py`(本任务独占)
- Modify: `services/control-plane/src/control_plane/api/runs.py:1541-1580`(改为调用共享模块)
- Test: `services/control-plane/tests/test_external_events.py`

**Interfaces:**
- Consumes: `_external.load_owned_run` / `external_error` / `ExternalScopeError`
- Produces: `GET /v1/agents/{agent_code}/runs/{run_id}/events?user_id=&since_seq=` → `text/event-stream`,
  响应头 `X-Expert-Work-Run-Id` 与 `X-Expert-Work-Stream-Mode: replay|live`

**范围边界(重要)**:本任务**只做对外包壳 + 归属校验**。**seq 错位、live 忽略 `since_seq`、回放分页三个 bug 属于 P3,不在本任务范围**——行为照搬现状,但要在文件 docstring 里写明这三条已知限制并指向 spec §六。

**不许复制粘贴流式逻辑**:`_stream_replay` / `_stream_live` 两个生产者必须**抽成共享模块** `api/_run_event_stream.py`,由控制台端点(`api/runs.py`)与本对外端点**共同调用**。理由:P3 要在这三个 bug 上动刀,两份副本必然只改一处(本仓库已有「同一语义分散多处实现、加约束只加了一处」的事故记录)。抽出的签名:

```python
def build_event_producer(
    *,
    run_id: UUID,
    is_terminal: bool,
    event_store: RunEventStore | None,
    stream_bridge: StreamBridge,
    since_seq: int | None,
    scope: AbstractAsyncContextManager[None] | None = None,
) -> AsyncIterator[bytes]:
```

`scope` 供控制台端点传 `applied_scope(scope)`(它的 DB 读要绑目标租户);对外端点传 `None`。
改完 `api/runs.py` 后必须跑既有回放测试确认零行为变化。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_events.py`

```python
@pytest.mark.asyncio
async def test_events_replays_a_terminal_run(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    # Drive the run to a terminal state so the endpoint takes the replay path.
    await ctx.run_store.set_status(
        run_id=UUID(run_id),
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["X-Expert-Work-Stream-Mode"] == "replay"
    assert "event: end" in resp.text


@pytest.mark.asyncio
async def test_events_404_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    # The gate must fire BEFORE the stream is built, so this is a plain HTTP
    # error — never an SSE frame carrying an error.
    assert resp.status_code == 404, resp.text
    assert not resp.headers["content-type"].startswith("text/event-stream")
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_events_requires_user_id(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events", headers=ctx.headers
    )
    assert resp.status_code == 422, resp.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_external_events.py -q`
Expected: FAIL —— 404(路由不存在)

- [ ] **Step 3: 实现 `external_events.py`**

先把 `api/runs.py:1541-1580` 的 `_stream_replay` / `_stream_live` 原样搬进新模块
`api/_run_event_stream.py` 的 `build_event_producer(...)`(签名见上;`format_sse`、`HEARTBEAT_SENTINEL`、
`END_SENTINEL`、`MAX_LIST_LIMIT` 的用法一字不改),把 `api/runs.py` 改成调用它,跑既有回放测试确认零变化。
然后写对外端点,四处与控制台不同:

1. **归属校验**换成 `load_owned_run(...)`(失败 → `external_error(exc)`,**必须在构造 `StreamingResponse` 之前**,保证 404 是普通 HTTP 错误而不是 SSE 帧)。
2. 去掉 `ensure_single_tenant_scope` / `applied_scope` / `?tenant_id=` 跨租户参数(对外无意义),`tenant_id` 直接取 `request.state.tenant_id`,`build_event_producer(..., scope=None)`。
3. 终态判定沿用 `persisted.status in TERMINAL_RUN_STATUSES`。
4. 依赖 `require("session", "read")`;`user_id` 为必填 query 参数(无默认值)。

文件 docstring 必须写明三条已知限制(P3 修复):

```python
"""External event replay / reconnect — ``GET /v1/agents/{agent_code}/runs/{run_id}/events``.

Wire format and both backends (terminal → replay from the durable store, active
→ live attach to the bridge) are identical to the console endpoint; only the
ownership gate differs. Three known limitations carried over verbatim, all
scheduled for P3 (see docs/superpowers/specs/2026-08-11-external-api-v1-design.md §六):

1. ``token`` frames are live-only — a replay never returns them.
2. ``since_seq`` is honoured on the replay path only; the live path ignores it.
3. Replay is capped at one page and appends ``end`` even when truncated.
"""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_external_events.py -q`
Expected: PASS(3 项)

- [ ] **Step 5: 变异自证**

1. 把 `load_owned_run(...)` 整段删掉、直接 `runs.get(...)` → `test_events_404_for_another_user` 必须红
2. 把归属校验挪到 `StreamingResponse` 构造**之后** → 同一用例里 `assert not ...content-type startswith text/event-stream` 必须红(证明"闸在流之前"这条不变式真的被测到)

反向替换还原。

- [ ] **Step 6: 门禁 + Commit**

```bash
uv run pytest services/control-plane/tests/test_runs_api.py -q   # 既有回放行为零变化
uv run ruff check services/control-plane/src/control_plane/api/external_events.py services/control-plane/src/control_plane/api/_run_event_stream.py services/control-plane/src/control_plane/api/runs.py services/control-plane/tests/test_external_events.py
uv run ruff format --check services/control-plane/src/control_plane/api/external_events.py services/control-plane/src/control_plane/api/_run_event_stream.py services/control-plane/src/control_plane/api/runs.py services/control-plane/tests/test_external_events.py
uv run mypy services/control-plane/src/control_plane/api/external_events.py services/control-plane/src/control_plane/api/_run_event_stream.py
git add services/control-plane/src/control_plane/api/ services/control-plane/tests/test_external_events.py
git commit -m "feat(control-plane): 对外事件回放端点(断线重连 + 闸先于流)"
```

---

### Task 5: 文件上传端点

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_uploads.py`(本任务独占)
- Test: `services/control-plane/tests/test_external_uploads.py`

**Interfaces:**
- Consumes: `_external.resolve_external_user_id` / `load_owned_session` / `external_error` / `ExternalScopeError`;
  `agents.py` 的 `_resolve_session`(通过 import 复用,用于 `session_id` 省略时建会话)
- Produces: `POST /v1/agents/{agent_code}/uploads`(multipart:`file` + `user_id` + 可选 `session_id`)→
  `{"success": true, "data": {"upload_id": str, "session_id": str, "type": "image"|"document", "mime": str, "size": int}}`

**背景(实现者必读)**:图片的引用 URI 与对象存储键里**硬编码了会话 ID**
(`expert_work://image/{tenant_id}/{thread_id}/{image_id}{ext}`,存储键 `{tenant_id}/uploads/{thread_id}/{image_id}{ext}`),
且 `image_upload.thread_id` 非空。所以上传**无法脱离会话**:`session_id` 省略时本端点**顺带建会话**并回传。

**同时修掉一个既有缺陷**:现有 `POST /v1/sessions/{thread_id}/uploads` 的文档分支要求
`caller_user_id is not None`,而 API Key 是机器身份恒为 `None` → 文档上传对 API Key 恒 400
(`uploads.py:145-147`)。本端点的落盘目标改为**请求声明的终端用户**,不是调用者。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_uploads.py`

```python
@pytest.mark.asyncio
async def test_upload_image_without_session_creates_one(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["type"] == "image"
    assert data["upload_id"]
    # Images cannot exist outside a session (the storage key embeds it), so the
    # endpoint mints one and hands it back for the follow-up run call.
    assert data["session_id"]


@pytest.mark.asyncio
async def test_upload_reuses_a_supplied_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    session_id = first.json()["data"]["session_id"]
    second = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77", "session_id": session_id},
        files={"file": ("b.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_upload_404s_for_another_users_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    session_id = first.json()["data"]["session_id"]
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "someone-else", "session_id": session_id},
        files={"file": ("b.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_upload_document_succeeds_for_an_api_key_caller(ctx: _Ctx) -> None:
    """Regression: the console endpoint 400s here because it lands documents in
    the CALLER's workspace and a machine principal has none. The external
    endpoint lands them in the declared end user's workspace instead."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["type"] == "document"


@pytest.mark.asyncio
async def test_upload_rejects_an_unsupported_type(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")},
        headers=ctx.headers,
    )
    assert resp.status_code == 400, resp.text
```

`_PNG_BYTES` 用一个最小合法 PNG(1×1),定义在测试文件顶部:

```python
import base64

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
```

> 文档分支需要 workspace store。若 `stub_agent_runtime` 未接线 workspace,`test_upload_document_*` 会走 503 分支 —— 此时在 fixture 里补一个内存 workspace store 桩,**不要把断言改成 503**(那等于给坏版本发合格证)。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_external_uploads.py -q`
Expected: FAIL —— 404(路由不存在)

- [ ] **Step 3: 实现 `external_uploads.py`**

要点:

1. 表单参数:`file: Annotated[UploadFile, File()]`、`user_id: Annotated[str, Form(min_length=1, max_length=255)]`、`session_id: Annotated[UUID | None, Form()] = None`。
2. `session_id` 为 `None` → 调 `agents._resolve_session(..., session_id=None, ...)` 建会话(它同时做 Agent kill-switch 与存在性校验);不为 `None` → 调 `load_owned_session(...)` 校验归属。两条路都拿到 `thread_id` 与 `end_user_id`。
3. 类型分发与落盘**复用 `api/uploads.py` 现有逻辑**:文档走 `_handle_document_upload` 同款流程但把落盘用户改成 `end_user_id`;图片走 EXIF 清洗 + `ImageRef` + 对象存储 + `image_upload` 登记 + `check_admission` 配额准入(`uploads.py:376-388` 是现成样板)。
   **不要复制粘贴整段** —— 把 `api/uploads.py` 里可复用的部分提取成模块级函数再由两处调用,避免两份实现漂移(本仓库已有"同一语义分散多处、加约束只加了一处"的事故)。
4. `upload_id`:图片用 `ImageRef.to_uri()` 的结果;文档用其工作区相对路径。两者都作为 P2 `files[]` 的 `upload_id` 使用。
5. 依赖 `require("session", "write")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_external_uploads.py -q`
Expected: PASS(5 项)

- [ ] **Step 5: 变异自证**

1. 把 `session_id` 分支里的 `load_owned_session(...)` 删掉 → `test_upload_404s_for_another_users_session` 必须红
2. 把文档落盘用户从 `end_user_id` 改回"调用者" → `test_upload_document_succeeds_for_an_api_key_caller` 必须红
3. 把类型白名单判断删掉 → `test_upload_rejects_an_unsupported_type` 必须红

反向替换还原。

- [ ] **Step 6: 门禁 + Commit**

```bash
uv run ruff check services/control-plane/src/control_plane/api/external_uploads.py services/control-plane/src/control_plane/api/uploads.py services/control-plane/tests/test_external_uploads.py
uv run ruff format --check services/control-plane/src/control_plane/api/external_uploads.py services/control-plane/src/control_plane/api/uploads.py services/control-plane/tests/test_external_uploads.py
uv run mypy services/control-plane/src/control_plane/api/external_uploads.py
uv run pytest services/control-plane/tests/test_uploads.py -q   # 既有上传测试不得回归
git add services/control-plane/src/control_plane/api/ services/control-plane/tests/test_external_uploads.py
git commit -m "feat(control-plane): 对外文件上传端点(无需先建会话 + 修文档上传对 API Key 的 400)"
```

---

### Task 6: 审批决策端点

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_approvals.py`(本任务独占)
- Test: `services/control-plane/tests/test_external_approvals.py`

**Interfaces:**
- Consumes: `_external.load_owned_run` / `external_error` / `ExternalScopeError`;
  `api/runs.py` 的 `resolve_approval_decision(...)`(现成的决策落地函数,`runs.py:507`)
- Produces: `POST /v1/agents/{agent_code}/runs/{run_id}:decide`,body
  `{"user_id": str, "decision": "approve"|"reject"|"modify", "modified_args": dict|None, "reason": str|None, "idempotency_key": str|None}`

**语义**:沿用内部 resume —— `stream` 模式返回**续跑的 SSE 流**;`queue` 模式返回 202 + 续跑 `run_id`。
两种模式下续跑都用**新的 `run_id`**(响应头 `X-Expert-Work-Run-Id`)。`modified_args` 仅在
`decision == "modify"` 时允许,其余组合 422(内部 `ResumeRequest` 已有同款校验,照抄)。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_approvals.py`

```python
@pytest.mark.asyncio
async def test_decide_404s_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "someone-else", "decision": "approve"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_decide_rejects_modified_args_without_modify(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "approve", "modified_args": {"x": 1}},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_decide_requires_modified_args_for_modify(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "modify"},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_decide_404s_when_no_pending_approval(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "approve"},
        headers=ctx.headers,
    )
    # Ownership passes; there is simply nothing waiting on a verdict.
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] != "RUN_NOT_FOUND"
```

> 造"真的挂起在审批上的 run"需要驱动 orchestrator 桩发出 approval —— 若 `stub_agent_runtime` 无此能力,
> 就在 fixture 里直接往 `app.state.approval_repo` 塞一条 pending 记录,再补一条**批准成功**的用例。
> 不要因为造数麻烦就只测否定路径(那样"批准真的生效"这条不变式无人保护)。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_external_approvals.py -q`
Expected: FAIL —— 404(路由不存在)

- [ ] **Step 3: 实现 `external_approvals.py`**

要点:

1. 请求模型照抄 `api/runs.py:171-190` 的 `ResumeRequest`(含 `decision` / `modified_args` / `reason` / `idempotency_key` 与 `modify` 的配对校验),再加 `user_id: str = Field(min_length=1, max_length=255)`。
2. 先 `load_owned_run(...)`(失败 → `external_error`),再复用 `api/runs.py` 的 `resolve_approval_decision(...)`
   传入 `tenant_id` / `actor_id` / `caller_user_id=<end_user_id>` / `oauth_user_id=str(end_user_id)` /
   `thread_id=meta.thread_id` / `run_id` / `idempotency_key`。**参数名与顺序以 `runs.py:507` 的定义为准。**
3. 依赖 `require("session", "write")`。
4. 响应头带 `X-Expert-Work-Run-Id`(续跑的**新** run id)。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_external_approvals.py -q`
Expected: PASS

- [ ] **Step 5: 变异自证**

1. 删掉 `load_owned_run(...)` → `test_decide_404s_for_another_user` 必须红
2. 删掉 `modify` 配对校验 → 两条 422 用例必须红

反向替换还原。

- [ ] **Step 6: 门禁 + Commit**

```bash
uv run ruff check services/control-plane/src/control_plane/api/external_approvals.py services/control-plane/tests/test_external_approvals.py
uv run ruff format --check services/control-plane/src/control_plane/api/external_approvals.py services/control-plane/tests/test_external_approvals.py
uv run mypy services/control-plane/src/control_plane/api/external_approvals.py
git add services/control-plane/src/control_plane/api/external_approvals.py services/control-plane/tests/test_external_approvals.py
git commit -m "feat(control-plane): 对外审批决策端点(归属校验 + 续跑新 run_id)"
```

---

### Task 7: 控制台平面对 API Key 收口

**Files:**
- Modify: `services/control-plane/src/control_plane/api/_authz.py`(加 `console_only()`)
- Modify: `services/control-plane/src/control_plane/api/sessions.py`、`runs.py`、`approvals.py`、`uploads.py`、`plan.py`、`feedback.py`(路由装饰器加依赖)
- Modify: `services/control-plane/tests/test_api_key_scope_gate.py`(期望收紧)
- Test: `services/control-plane/tests/test_console_lockdown.py`

**Interfaces:**
- Produces: `console_only() -> Callable[..., Awaitable[None]]` —— 对 `subject_type == "service_account"` 主体 403 的路由依赖

**为什么不是"给控制台端点加 `user_id` 过滤"**:对外只公开 7 个端点,收口把攻击面从 31 缩到 7,
且第三方不会依赖上内部形状(日后改控制台 API 不打断对接)。参照 Dify「API 无法访问 WebApp 创建的会话」。

**注意**:`test_api_key_scope_gate.py`(#1153)现在断言"read key 通过控制台读闸"。收口后这些期望
**必须改成 403**。这是**有意收紧,不是回归** —— 改测试时要在 docstring 里写明这一点。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_console_lockdown.py`

```python
"""Console plane is closed to API keys — third parties use /v1/agents/... only.

#1153 gave these endpoints a scope gate; P1 closes them to machine principals
outright. A tenant employee's JWT keeps its previous access unchanged.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

_TID = uuid4()
_RID = uuid4()

CONSOLE_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/v1/sessions"),
    ("GET", f"/v1/sessions/{_TID}"),
    ("GET", f"/v1/sessions/{_TID}/messages"),
    ("GET", f"/v1/sessions/{_TID}/runs"),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}"),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}/events"),
    ("GET", "/v1/runs"),
    ("GET", "/v1/approvals"),
    ("GET", f"/v1/sessions/{_TID}/plan"),
    ("GET", f"/v1/sessions/{_TID}/workspace"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), CONSOLE_ENDPOINTS)
async def test_api_key_is_denied_on_the_console_plane(
    ctx: _Ctx, method: str, path: str
) -> None:
    resp = await ctx.client.request(method, path, headers=ctx.key_headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), CONSOLE_ENDPOINTS)
async def test_employee_jwt_is_unaffected(ctx: _Ctx, method: str, path: str) -> None:
    resp = await ctx.client.request(method, path, headers=ctx.headers)
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_api_key_still_reaches_the_external_plane(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert resp.status_code == 200, resp.text
```

`ctx.key_headers` 用 `make_test_jwt(tenant_id=..., subject="sa-test", sub_type="service_account", roles=(), scopes=("admin",))`
—— **给最宽的 `admin` scope**,这样 403 只可能来自收口闸而不是 scope 不足。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_console_lockdown.py -q`
Expected: FAIL —— 现在返回 200/404 而不是 403

- [ ] **Step 3: 实现 `console_only()`**

在 `api/_authz.py` 里 `require_key_scope` 之后加(照它的结构写:审计行 + 403 信封):

```python
def console_only() -> Callable[..., Awaitable[None]]:
    """Route dependency — 403 a service-account (API-key) principal outright.

    The console plane (``/v1/sessions`` / ``/v1/approvals`` / ``/v1/runs`` /
    uploads / plan / feedback) is shaped for the admin UI: its ownership filter
    resolves to "the calling user", which a machine principal does not have, so
    an API key silently widens to the whole tenant. Third parties use the
    external plane (``/v1/agents/{agent_code}/...``) instead, where every
    endpoint takes an explicit ``user_id`` and verifies it. Employee JWTs and
    mTLS service principals are unaffected.
    """

    async def _dep(
        request: Request,
        principal: Annotated[Principal, Depends(_principal)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        if principal.subject_type != "service_account":
            return
        try:
            await emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.AUTH_LOGIN_FAILED,
                resource_type="user",
                resource_id="console:api_key_denied",
                result=AuditResult.DENIED,
                reason="CONSOLE_PLANE_CLOSED_TO_API_KEYS",
                trace_id=current_trace_id_hex(),
                details={"subject_type": principal.subject_type},
            )
        except Exception:
            logger.exception("authz.deny_audit_emit_failed")
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "console API is not available to API keys; use /v1/agents/{agent_code}/…",
            },
        )

    return _dep
```

> ⚠️ **收窄(External-API-v1 P2-b 安全修复,2026-08-13)**:上面代码块里
> `"""...Employee JWTs and mTLS service principals are unaffected."""` 这句
> 只在 `console_only()` 自身的作用域内成立(它确实不拦员工 JWT / mTLS),**不是**
> "员工 JWT 在整个平台不受影响"的全局承诺 —— 对外平面(`/v1/agents/{agent_code}/...`)
> 当时没有对偶闸,员工 JWT 能以自己的 RBAC 角色打对外端点读写任意终端用户数据
> (真实实测:viewer 读到 SSN、operator 冒充终端用户发 run/裁审批)。已加
> `external_only()`(见 `_authz.py`)堵上这个方向,现存源码的 docstring 已同步
> 收窄;本代码块保留作历史记录,**不要照抄这句话**。

- [ ] **Step 4: 挂到控制台路由上**

给 `sessions.py` / `runs.py` / `approvals.py` / `uploads.py` / `plan.py` / `feedback.py` 里**每一个**
路由装饰器的 `dependencies=[...]` 追加 `Depends(console_only())`(#1153 已经加了
`Depends(require_key_scope(...))`,保留它作纵深防御)。

用这条命令自查漏挂(应无输出):

```bash
rg -n "@router\.(get|post|put|patch|delete)" services/control-plane/src/control_plane/api/{sessions,runs,approvals,uploads,plan,feedback}.py -A 4 | rg -B4 "async def" | rg -v "console_only" | rg "@router\."
```

- [ ] **Step 5: 跑测试确认通过 + 更新 #1153 的期望**

Run: `uv run pytest services/control-plane/tests/test_console_lockdown.py -q`
Expected: PASS

然后 `test_api_key_scope_gate.py`:把针对**控制台端点**的"read key 通过 / write key 通过"断言改成
403,并在模块 docstring 里补一段说明"P1 收口后控制台平面对任何 key 一律 403,scope 闸退居纵深防御"。
`test_user_jwt_never_hits_key_gate` 保持不变。

Run: `uv run pytest services/control-plane/tests/test_api_key_scope_gate.py -q`
Expected: PASS

- [ ] **Step 6: 变异自证**

1. 把 `console_only` 里的 `if principal.subject_type != "service_account": return` 改成无条件 `return`
   → `test_api_key_is_denied_on_the_console_plane` 必须**全部**红
2. 把这一行改成无条件抛 403 → `test_employee_jwt_is_unaffected` 必须红(证明员工不受影响这条真的被测到)

反向替换还原。

- [ ] **Step 7: 门禁 + Commit**

```bash
uv run pytest services/control-plane/tests/ -q
uv run ruff check services/control-plane/src services/control-plane/tests
uv run ruff format --check services/control-plane/src services/control-plane/tests
git add services/control-plane/
git commit -m "feat(control-plane): 控制台平面对 API Key 收口——第三方只走 /v1/agents 对外面"
```

---

### Task 8: 全链集成测试

**Files:**
- Test: `services/control-plane/tests/test_external_api_contract.py`

**Interfaces:**
- Consumes: Task 2–7 的全部端点

**目的**:前面每个任务只验证自己那个端点。本任务验证它们**能串起来**——这是跨任务缝隙的唯一拦截点
(本仓库的历史教训:终审逮到的问题几乎全在任务边界之间)。

- [ ] **Step 1: 写失败测试** — `services/control-plane/tests/test_external_api_contract.py`

```python
"""End-to-end contract walk for the external plane — the seams between tasks.

Each endpoint has its own unit tests; this walks the sequence a real
third-party app performs, so an id produced by one endpoint must be accepted by
the next without translation.
"""


@pytest.mark.asyncio
async def test_third_party_full_chain(ctx: _Ctx) -> None:
    await ctx.seed_agent()

    # 1. Upload a file first — no session exists yet, so the endpoint mints one.
    up = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.key_headers,
    )
    assert up.status_code == 201, up.text
    session_id = up.json()["data"]["session_id"]

    # 2. Run inside that same session.
    run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "session_id": session_id,
            "input": "看下这张图",
            "mode": "queue",
        },
        headers=ctx.key_headers,
    )
    assert run.status_code == 202, run.text
    run_id = run.json()["run_id"]

    # 3. Cancel it.
    cancelled = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    # 4. The session shows up in that user's list — and only that user's.
    listed = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert listed.status_code == 200, listed.text
    ids = [s["session_id"] for s in listed.json()["data"]["sessions"]]
    assert session_id in ids

    other = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-99"},
        headers=ctx.key_headers,
    )
    assert session_id not in [s["session_id"] for s in other.json()["data"]["sessions"]]

    # 5. Message history for that session is readable.
    msgs = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert msgs.status_code == 200, msgs.text

    # 6. Replaying the cancelled run's events works (reconnect path).
    events = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert events.status_code == 200, events.text
    assert "event: end" in events.text

    # 7. The whole chain ran on an API key that CANNOT touch the console plane.
    denied = await ctx.client.get("/v1/sessions", headers=ctx.key_headers)
    assert denied.status_code == 403, denied.text
```

- [ ] **Step 2: 跑测试**

Run: `uv run pytest services/control-plane/tests/test_external_api_contract.py -q`
Expected: PASS。若任何一步的 id 需要"翻译"才能被下一步接受,说明契约有缝 —— **修端点,不要在测试里
转换 id**。

- [ ] **Step 3: 全量回归 + 门禁**

```bash
uv run pytest services/control-plane/tests/ -q
uv run ruff check
uv run ruff format --check
uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/retention-cleanup-job/src services/orchestrator/src
```
Expected: 全部 PASS(mypy 范围与 CI `ci.yml:75` 一致)。

- [ ] **Step 4: Commit**

```bash
git add services/control-plane/tests/test_external_api_contract.py
git commit -m "test(control-plane): 对外 API 全链契约测试——任务边界缝隙拦截"
```

---

## 计划自查

**1. Spec 覆盖(对照 spec §二 的 7 个端点 + §三 的 4 项决策)**

| Spec 条目 | 落到 |
|---|---|
| 端点 1 发起 run(已有) | 不改(P2 才动请求体);Task 1 Step 8 只改了它的身份铸造 |
| 端点 2 取消 run | Task 2 |
| 端点 3 事件回放 | Task 4 |
| 端点 4 会话列表 | Task 3 |
| 端点 5 会话消息 | Task 3 |
| 端点 6 文件上传 | Task 5 |
| 端点 7 审批决策 | Task 6 |
| §三-A 命名空间统一 | Task 1 Step 6-7(5 个 router 全用 `/v1/agents` 前缀) |
| §三-B 控制台收口 | Task 7 |
| §三-C 归属校验复用 | Task 1 Step 3(`load_owned_session` / `load_owned_run`),Task 2-6 消费 |
| §三-D `ext:` 前缀 | Task 1 Step 3 + Step 8;存量已核实无需迁移 |

**2. 占位符扫描**:无 TBD/TODO。三处"以仓库现状为准"(in-memory store 导入路径、`RunStatus` 导出路径、
`app.state` 属性名)均给了确认命令,不是留白。

**3. 类型一致性**:`load_owned_session` 返回 `ThreadMeta`、`load_owned_run` 返回
`tuple[RunInfo, ThreadMeta]`,Task 2/4/5/6 的用法与之一致;`external_error(exc)` 签名在 Task 1 定义、
Task 2-6 按同一形式调用;5 个 `build_external_*_router` 的名字在 Task 1 建壳、Task 7 之外无人重命名。

**4. 范围外(明确不在 P1)**:`files[]` 数组 / 远程 URL 拉取 / `inputs` / 幂等键(P2);
SSE 三个 bug 与帧文档 / 文档站重构(P3)。Task 4 的 docstring 已把三条限制写明并指向 spec。
