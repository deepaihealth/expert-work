# 阶段 3 PR-B(3.3 产物视图)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给第三方对接方开出 agent 产出物的列表 / 下载 / 软删三条端点,让「agent 生成了一份报表」这件事在对接方界面上有下文。

**Architecture:** 新建 `external_artifacts.py`,用 `external_workspace.py` 的对外外壳(`APIRouter(prefix="/v1/agents", tags=["external"])` + `reject_nul_path_params` + `external_only()` + 每路由 `require("session", ...)`)包住 `artifacts.py` 已有的三段业务逻辑(list / download / soft-delete)。控制台侧的跨租户与管理员代操身份解析(`ensure_tenant_scope` / `applied_scope` / `resolve_target_user_id`)整套换成 P1 的 `lookup_external_user_id`(**永不 mint**)。控制台侧 `artifacts.py` 一行不改。

**Tech Stack:** FastAPI / pydantic / SQLAlchemy(`ArtifactStore`)/ pytest。

## Global Constraints

以下每条都从 spec `docs/superpowers/specs/2026-08-15-external-api-v1-phase3-design.md` 第四节逐字抄来,**每个任务的要求都隐含包含本节**:

1. **`agent_code` 不参与过滤** —— 产物和工作区一样是 `(tenant_id, user_id)` 维度。路径带它只是为了和其它对外端点形状一致,同 `external_workspace.py` 的既有处理,**模块 docstring 里要写明**。函数体用 `del agent_code  # ...` 显式丢弃。
2. **`name` 走 query 不走 path。** 控制台侧用的是 `{name:path}`;对外用 query 参数,避免产物名含 `/` 时的路径穿越与编码歧义。
3. **DELETE 的 `user_id` 和 `name` 都在 query** —— 与同资源的 GET 一致。(backlog B-6 记的就是同资源两个写操作参数位置不一致坑对接方,这里不重蹈。)
4. **列表不带 `size_bytes`。** `list_for_user` 返回的 `Artifact` 不含版本详情,要带大小得逐行查 latest version —— 一个现成的 N+1。`size_bytes` / `sha256` 本来就是首次下载时才懒回填的,列表里给出来一半是 null,反而误导。
5. **软删的产物不出现**(`list_for_user` 默认 `include_deleted=False`,不要传 `include_deleted=True`)。
6. **下载配额扣减:** `resource_kind="artifact_download"`,`cost=1`。
7. **下载错误映射:** 权限失败(`WorkspacePermissionError`)→ 500 + 固定文案;内容不存在(`SandboxSupervisorError`)→ 404。**两者不能合并成一个 404**(沙箱迁移 W2-BUG-1 的教训),且 `except WorkspacePermissionError` **必须排在** `except SandboxSupervisorError` **之前** —— 前者是后者的子类,顺序反了永远走不到。
8. **下载成功响应是裸文件字节流**,不套 `{success, data, error}`;只有错误路径套信封(同 `workspace/file`)。
9. **删除的权限闸 = `require("session", "write")`,不是 `"delete"`。** `ApiKeyScope` 没有独立 delete 档,挂 `"delete"` 等于只有 `admin` scope 的 key 能删——逼第三方拿一把能改服务账号的钥匙才能删自己的文件,是反向的最小权限。与 `archive_session` 的既有裁决同源(2026-08-13 用户决策)。
10. **删除是软删**(`store.soft_delete`):不存在 / 已软删 / 跨用户 → **统一 404**,不泄露存在性;工作区里的字节不动;agent 重新 save 同名会把它复活。
11. **删除审计:** `AuditAction.ARTIFACT_DELETE`,带 `on_behalf_of=str(end_user_id)`。
12. **不做**:版本历史 / 下载指定版本;改 `kind`(控制台的 PATCH);硬删(`:purge` 永远是 console-only)。这三条**不要顺手实现**。
13. **未知 `user_id` 一律不 mint。** 三条端点都用 `lookup_external_user_id`(返回 `UUID | None`),**不要**用 `resolve_external_user_id`(那个会建行)。
14. 时间字段序列化一律 `x.isoformat() if x else None` —— `JSONResponse` 不能直接序列化 `datetime`(见 `external_runs.py:153`)。

## 文件结构

| 文件 | 责任 | 任务 |
|---|---|---|
| `services/control-plane/src/control_plane/api/external_artifacts.py` | **新建。** 三条对外产物端点。唯一的新生产文件 | 1/2/3 |
| `services/control-plane/src/control_plane/app.py` | 挂载新 router(import + `include_router`) | 1 |
| `services/control-plane/tests/test_console_lockdown.py` | `_EXTERNAL_AGENT_ROUTES` 手工表登记三条新路由 | 1/2/3 |
| `services/control-plane/tests/test_external_artifacts.py` | **新建。** 三条端点的行为测试 | 1/2/3 |
| `apps/admin-ui/docs-site/guide/query.md` | 新增「5.7 产物」一节 | 4 |
| `apps/admin-ui/docs-site/guide/errors.md` | 错误码速查表补产物相关行 | 4 |
| `apps/admin-ui/docs-site/.vitepress/config.mts` | 侧栏加 5.7 | 4 |
| `apps/admin-ui/docs-site/guide/best-practices.md` | 联调自测清单加一行 | 4 |

**为什么只有一个新生产文件**:三条端点共享同一套身份解析与错误信封,拆成三个文件会把 `_get_artifact_store` 这类依赖注入助手重复三遍。162 行的 `external_workspace.py` 是同款先例(两条端点一个文件)。预计 ~230 行,在 200-400 的舒适区内。

## 自审表登记(PR-A 的教训)

仓库里有五处「新对外路由必须被发现」的守卫,其中**四处是自动的**、**一处要手工登记**:

| 守卫 | 机制 | PR-B 要做什么 |
|---|---|---|
| `test_external_only_gate.py` | 按 `tags=["external"]` 发现 | 自动 —— router 带 tag 即可 |
| `test_external_path_param_nul_guard.py` | 按 tag 发现 | 自动 |
| `test_external_route_reachability.py` | 按 tag 发现 | 自动 |
| `test_route_plane_partition.py` | **派生**分类(读路由真实依赖图) | 自动 |
| `test_console_lockdown.py::_EXTERNAL_AGENT_ROUTES` | **手工 frozenset** | **必须加三行,否则 CI 红** |

---

### Task 1: 列表端点 `GET /v1/agents/{agent_code}/artifacts`

**Files:**
- Create: `services/control-plane/src/control_plane/api/external_artifacts.py`
- Modify: `services/control-plane/src/control_plane/app.py`(import 块 + `include_router` 块)
- Modify: `services/control-plane/tests/test_console_lockdown.py`(`_EXTERNAL_AGENT_ROUTES`)
- Test: `services/control-plane/tests/test_external_artifacts.py`(新建)

**Interfaces:**
- Consumes:
  - `lookup_external_user_id(*, tenant_id: UUID, user_id: str, users: TenantUserStore) -> UUID | None`(`control_plane.api._external`)
  - `ExternalScopeError` / `external_error(exc) -> JSONResponse`(同上)
  - `reject_nul_path_params`(同上)
  - `external_only()` / `require(resource, action)`(`control_plane.api._authz`)
  - `get_user_repo`(`control_plane.api._user_scope`)
  - `ArtifactStore.list_for_user(*, tenant_id: UUID, user_id: UUID, include_deleted: bool = False) -> list[Artifact]`
  - `Artifact` 字段:`name: str` / `kind: ArtifactKind` / `latest_version: int` / `created_at: datetime | None` / `updated_at: datetime | None`
- Produces:
  - `build_external_artifacts_router() -> APIRouter` —— Task 2/3 在同一个 router 里加路由
  - `_get_artifact_store(request) -> ArtifactStore` / `_get_workspace_store(request) -> WorkspaceStore | None` —— Task 2 复用

- [ ] **Step 1: 写失败测试**

新建 `services/control-plane/tests/test_external_artifacts.py`。参照同目录 `test_external_workspace.py` 的 fixture 搭法(先读它,照抄 app / api-key / tenant 的构造方式,不要自创)。

```python
"""对外产物端点 —— 阶段 3 PR-B。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_returns_active_artifacts(external_client, seed_artifact) -> None:
    """列表返回 name/kind/latest_version/created_at/updated_at 五个字段。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "u-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    items = body["data"]["artifacts"]
    assert len(items) == 1
    assert set(items[0]) == {
        "name",
        "kind",
        "latest_version",
        "created_at",
        "updated_at",
    }, "字段集必须精确 —— 多给 size_bytes 会误导(它只在首次下载后才有值)"
    assert items[0]["name"] == "report.docx"


@pytest.mark.asyncio
async def test_list_hides_soft_deleted(external_client, seed_artifact, soft_delete_artifact) -> None:
    """软删的产物不出现在列表里。"""
    await seed_artifact(user_id="u-1", name="gone.docx", kind="document")
    await soft_delete_artifact(user_id="u-1", name="gone.docx")
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "u-1"}
    )
    assert [a["name"] for a in resp.json()["data"]["artifacts"]] == []


@pytest.mark.asyncio
async def test_unknown_user_gets_empty_list_and_mints_nothing(
    external_client, count_tenant_users
) -> None:
    """未知 user_id 返回空列表,且不建 tenant_user 行。

    这条是 P1 review T3 的不变式:第三方拿任意字符串刷这个端点,不能每刷
    一次就留一行幽灵用户。删掉实现里的 mint=False 语义(改用
    resolve_external_user_id)时这条必须红。
    """
    before = await count_tenant_users()
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "never-seen-before"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["artifacts"] == []
    assert await count_tenant_users() == before


@pytest.mark.asyncio
async def test_agent_code_does_not_filter(external_client, seed_artifact) -> None:
    """agent_code 不参与过滤 —— 换一个(甚至不存在的)agent_code 拿到同一份列表。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    a = await external_client.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "u-1"}
    )
    b = await external_client.get(
        "/v1/agents/no-such-agent-at-all/artifacts", params={"user_id": "u-1"}
    )
    assert a.status_code == b.status_code == 200
    assert a.json()["data"] == b.json()["data"]
```

- [ ] **Step 2: 运行,确认失败**

```bash
cd services/control-plane && uv run pytest tests/test_external_artifacts.py -v
```

预期:FAIL —— 404(路由还不存在)。

- [ ] **Step 3: 写实现**

新建 `services/control-plane/src/control_plane/api/external_artifacts.py`:

```python
"""对外产物端点 —— ``/v1/agents/{agent_code}/artifacts``(阶段 3 PR-B)。

agent 把产出物(报表、导出文件)登记成 artifact,第三方 app 得能列出来、
下下来、删掉 —— 否则「agent 生成了一份周报」这件事在他们界面上就没有下文。

控制台侧 ``api/artifacts.py`` 的五个端点全挂 ``console_only()``。本模块是
其中三个(list / download / soft-delete)的对外镜像:安全处理(MIME 推断 /
active content 强制 attachment / nosniff / 权限失败与不存在分开)全部复用,
只把控制台的身份解析(跨租户 scope + 管理员代操 ``resolve_target_user_id``)
换成 P1 的 ``_external`` 通路。

产物本身是 ``(tenant_id, user_id)`` 维度的,不按 agent 分 —— ``agent_code``
只是外部平面 URL 结构的一部分(与 ``/v1/agents/{agent_code}/sessions`` 等同款
路径形状对齐),**不参与过滤,也不参与权限判定**,和控制台侧 ``/v1/artifacts``
(压根没有 agent_code)语义一致。

``name`` 走 query 而非 path:控制台侧是 ``{name:path}``,对外用 query 参数,
避免产物名含 ``/`` 时的路径穿越与编码歧义。DELETE 的 ``user_id`` / ``name``
同样在 query —— 与同资源的 GET 保持一致(同资源两个写操作参数位置不一致会
坑对接方,session 的 PATCH/DELETE 已经踩过)。

**不镜像**版本历史(``/versions``)、改 ``kind``(PATCH)、硬删(``:purge``)
—— 前两者对第三方界面价值低,硬删与会话侧一致,永远是 console-only。
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    lookup_external_user_id,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from expert_work.persistence import ArtifactStore
from expert_work.persistence.tenant_user import TenantUserStore
from orchestrator.tools import WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.external_artifacts")


def _get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def build_external_artifacts_router() -> APIRouter:
    """Mount the external artifact endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "/{agent_code}/artifacts",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_artifacts(
        agent_code: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> JSONResponse:
        """List an end-user's agent artifacts, most-recently-updated first.

        ``mint=False`` — a read must never mint a ``tenant_user`` row for a
        ``user_id`` this tenant has never seen (External-API-v1 P1 review,
        T3). An unrecognized user simply has no artifacts, so this returns
        an empty list, not 404 — same as ``GET .../workspace/files``.

        No ``size_bytes``: ``list_for_user`` returns the logical rows, not
        version detail — adding it would need a per-row latest-version
        lookup (an N+1), and the digest is only backfilled on first
        download, so most rows would carry ``null`` anyway.
        """
        del agent_code  # artifacts are (tenant, user)-scoped — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return JSONResponse({"success": True, "data": {"artifacts": []}, "error": None})
        artifacts = await store.list_for_user(tenant_id=tenant_id, user_id=end_user_id)
        items = [
            {
                "name": a.name,
                "kind": a.kind,
                "latest_version": a.latest_version,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            }
            for a in artifacts
        ]
        return JSONResponse({"success": True, "data": {"artifacts": items}, "error": None})

    return router
```

- [ ] **Step 4: 挂载 router**

`services/control-plane/src/control_plane/app.py` —— import 块里(第 56-62 行那组 `build_external_*` 里,按字母序插在 `build_external_agent_catalog_router` 之后):

```python
    build_external_artifacts_router,
```

`include_router` 块里(第 2467-2473 行那组的末尾):

```python
    app.include_router(build_external_artifacts_router())
```

- [ ] **Step 5: 登记手工自审表**

`services/control-plane/tests/test_console_lockdown.py` 的 `_EXTERNAL_AGENT_ROUTES` frozenset 里加(与既有条目同块,附一行说明):

```python
        # 阶段 3 PR-B — 对外产物视图(external_artifacts.py)。同样挂在
        # ``APIRouter(prefix="/v1/agents", tags=["external"])`` 上,用
        # ``require("session", ...)`` 而非 ``console_only()``。
        ("GET", "/v1/agents/{agent_code}/artifacts"),
```

- [ ] **Step 6: 跑测试**

```bash
cd services/control-plane && uv run pytest tests/test_external_artifacts.py -v
uv run pytest tests/test_console_lockdown.py tests/test_external_only_gate.py \
    tests/test_external_path_param_nul_guard.py tests/test_external_route_reachability.py \
    tests/test_route_plane_partition.py -q
```

预期:全部 PASS。**测试一律前台跑,不要放后台。**

- [ ] **Step 7: 自证测试真的咬人**

依次做这两个变异,每次确认**有测试变红**,然后**立刻还原**:

1. 把 `lookup_external_user_id` 换成 `resolve_external_user_id`(并把 `if end_user_id is None` 分支删掉)→ `test_unknown_user_gets_empty_list_and_mints_nothing` 必须红。
2. 把 `list_for_user(...)` 改成 `list_for_user(..., include_deleted=True)` → `test_list_hides_soft_deleted` 必须红。

变异前先把文件复制一份到 scratchpad;**不要用 `git checkout --` 还原**(本仓库已四次因此吞掉未提交的真实修改)。改完用 `git diff` 确认变异真的落地了再读结果。

- [ ] **Step 8: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_artifacts.py \
        services/control-plane/src/control_plane/app.py \
        services/control-plane/tests/test_console_lockdown.py \
        services/control-plane/tests/test_external_artifacts.py
git commit -m "feat(external-api): 阶段 3 PR-B — 对外产物列表端点"
```

---

### Task 2: 下载端点 `GET /v1/agents/{agent_code}/artifacts/download`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_artifacts.py`
- Modify: `services/control-plane/tests/test_console_lockdown.py`
- Test: `services/control-plane/tests/test_external_artifacts.py`

**Interfaces:**
- Consumes(Task 1 已建):`build_external_artifacts_router()` / `_get_artifact_store` / `_get_workspace_store`
- Consumes(既有):
  - `ArtifactStore.get_latest_version(*, tenant_id, user_id, name) -> ArtifactVersion | None`(`ArtifactVersion` 有 `id: UUID` / `path_in_workspace: str` / `size_bytes: int | None`)
  - `ArtifactStore.set_version_digest(*, version_id: UUID, size_bytes: int, sha256: str) -> None`
  - `WorkspaceStore.read_file(*, tenant_id, user_id, path) -> bytes`
  - `infer_content_type(*, kind: ArtifactKind, path: str) -> InferredContentType`(有 `.content_type` / `.disposition`)
  - `content_disposition_header(filename: str, *, disposition) -> str`
  - `check_admission(*, quota, audit, tenant_id, actor_id, agent, resource_kind, cost=1) -> JSONResponse | None`
  - `WorkspacePermissionError` / `SandboxSupervisorError`(`orchestrator.tools`;**前者是后者的子类**)
- Produces:`_get_quota` / `_get_audit` 两个依赖注入助手(Task 3 复用 `_get_audit`)

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_external_artifacts.py`:

```python
@pytest.mark.asyncio
async def test_download_returns_raw_bytes_not_envelope(
    external_client, seed_artifact_with_content
) -> None:
    """成功响应是裸字节流,不套 {success, data, error}。"""
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"hello"
    )
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_download_forces_attachment_for_active_content(
    external_client, seed_artifact_with_content
) -> None:
    """HTML/SVG 这类可执行内容强制 attachment —— XSS 防护。"""
    await seed_artifact_with_content(
        user_id="u-1", name="page.html", kind="document", content=b"<script>alert(1)</script>"
    )
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "page.html"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_unknown_name_is_404_enveloped(external_client) -> None:
    """不存在的 name → 404,错误路径套信封。"""
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "nope.txt"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "ARTIFACT_NOT_FOUND"


@pytest.mark.asyncio
async def test_permission_error_is_500_not_404(
    external_client, seed_artifact_with_content, break_workspace_permission
) -> None:
    """权限失败 → 500,不能和「不存在」合并成 404。

    W2-BUG-1 的教训:元数据行在、内容读不动是**服务端配置问题**,报 404 会
    让对接方永远在查自己的 name 拼错了没有。这条测试同时锁住 except 顺序 ——
    WorkspacePermissionError 是 SandboxSupervisorError 的子类,把它写在后面
    就永远走不到,这个断言会红。
    """
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"x"
    )
    break_workspace_permission()
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "ARTIFACT_CONTENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_download_backfills_digest_on_first_read(
    external_client, seed_artifact_with_content, get_latest_version
) -> None:
    """首次下载回填 size_bytes / sha256。"""
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"hello"
    )
    assert (await get_latest_version("u-1", "report.txt")).size_bytes is None
    await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    version = await get_latest_version("u-1", "report.txt")
    assert version.size_bytes == 5
    assert version.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


@pytest.mark.asyncio
async def test_download_deducts_quota(
    external_client, seed_artifact_with_content, quota_calls
) -> None:
    """下载走配额准入 —— resource_kind='artifact_download', cost=1。"""
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"x"
    )
    await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert ("artifact_download", 1) in quota_calls
```

- [ ] **Step 2: 运行,确认失败**

```bash
cd services/control-plane && uv run pytest tests/test_external_artifacts.py -v -k download or permission
```

预期:FAIL —— 404(路由不存在)。

- [ ] **Step 3: 写实现**

`external_artifacts.py` 顶部补 import:

```python
import hashlib

from fastapi.responses import Response

from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from control_plane.api._quota_admission import check_admission
from control_plane.quota.base import QuotaService
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError
```

补两个依赖注入助手(放在 `_get_workspace_store` 之后):

```python
def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]
```

补一个错误信封助手(放在助手区末尾,Task 3 也用):

```python
def _artifact_error(code: str, message: str, status: int) -> JSONResponse:
    """错误路径的 ``{success, data, error}`` 信封。

    成功路径**不走这里** —— 下载的成功响应是裸文件字节流(信封与「文件不是
    JSON」这个事实冲突,同 ``workspace/file``)。
    """
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )
```

在 `list_artifacts` 之后、`return router` 之前加下载路由:

```python
    @router.get(
        "/{agent_code}/artifacts/download",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def download_artifact(
        agent_code: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        name: Annotated[str, Query(min_length=1, max_length=1024)],
    ) -> Response:
        """Download the latest version of one artifact.

        Success is the raw file body, not the ``{success, data, error}``
        envelope (see module docstring); only error paths render it.

        ``mint=False`` — same rationale as the list endpoint. An
        unrecognized ``user_id`` falls through to the same opaque 404 as a
        cross-user / unknown name: a third party must not be able to tell
        "that user doesn't exist" apart from "that user has no such
        artifact".
        """
        del agent_code  # artifacts are (tenant, user)-scoped — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        version = await store.get_latest_version(
            tenant_id=tenant_id, user_id=end_user_id, name=name
        )
        if version is None:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        artifacts = await store.list_for_user(tenant_id=tenant_id, user_id=end_user_id)
        artifact = next((a for a in artifacts if a.name == name), None)
        if artifact is None:
            # Defensive — a version without its parent row violates a store
            # invariant; stay opaque rather than 500.
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        # 配额准入 —— 第三方比员工更需要这道限制。cost=1 扣 QPS +
        # ARTIFACT_DOWNLOAD_COUNT_30D(租户没有对应维度行时是 no-op)。
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            agent=None,
            resource_kind="artifact_download",
            cost=1,
        )
        if denial is not None:
            return denial
        if workspace_store is None:
            return _artifact_error(
                "ARTIFACT_CONTENT_UNAVAILABLE", "artifact content unavailable", 503
            )
        try:
            data = await workspace_store.read_file(
                tenant_id=tenant_id, user_id=end_user_id, path=version.path_in_workspace
            )
        except WorkspacePermissionError as exc:
            # 元数据行在、内容读不动是权限问题(服务端配置),不是「不存在」——
            # 两者不能合并成一个 404(沙箱迁移 W2-BUG-1 的教训)。这个 except
            # **必须排在** SandboxSupervisorError 之前:它是后者的子类,顺序
            # 反了永远走不到。traceback 只进日志,不进响应体。
            logger.warning(
                "external_artifact.permission_denied version=%s", version.id, exc_info=True
            )
            return _artifact_error(
                "ARTIFACT_CONTENT_UNAVAILABLE", "artifact content unavailable", 500
            )
        except SandboxSupervisorError as exc:
            logger.warning(
                "external_artifact.content_unavailable version=%s reason=%s", version.id, exc
            )
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact content not found", 404)
        # 首次读回填摘要 —— save 时读不到内容(它在工作区卷里),所以那时未知。
        if version.size_bytes is None:
            await store.set_version_digest(
                version_id=version.id,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        # MIME 推断 + XSS 安全 disposition:可执行内容(HTML / SVG 等)一律
        # attachment,未识别扩展名回退 application/octet-stream + attachment。
        inferred = infer_content_type(kind=artifact.kind, path=version.path_in_workspace)
        headers = {
            "Content-Disposition": content_disposition_header(
                artifact.name, disposition=inferred.disposition
            ),
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=data, media_type=inferred.content_type, headers=headers)
```

- [ ] **Step 4: 登记自审表**

`test_console_lockdown.py` 的 `_EXTERNAL_AGENT_ROUTES` 里加:

```python
        ("GET", "/v1/agents/{agent_code}/artifacts/download"),
```

- [ ] **Step 5: 跑测试**

```bash
cd services/control-plane && uv run pytest tests/test_external_artifacts.py -v
uv run pytest tests/test_console_lockdown.py tests/test_external_only_gate.py \
    tests/test_external_path_param_nul_guard.py tests/test_external_route_reachability.py \
    tests/test_route_plane_partition.py -q
```

预期:全部 PASS。前台跑。

- [ ] **Step 6: 自证 except 顺序真的被锁住**

把 `except WorkspacePermissionError` 和 `except SandboxSupervisorError` 两块**对调顺序**,跑 `test_permission_error_is_500_not_404`,**必须红**(权限失败会被父类先接住,变成 404)。确认红了立刻还原。

这条变异是整个 Task 的命门:顺序反了代码照样跑、照样返回响应,只有这条断言能发现。变异前复制文件副本到 scratchpad,不要用 `git checkout --` 还原。

- [ ] **Step 7: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_artifacts.py \
        services/control-plane/tests/test_console_lockdown.py \
        services/control-plane/tests/test_external_artifacts.py
git commit -m "feat(external-api): 阶段 3 PR-B — 对外产物下载端点"
```

---

### Task 3: 删除端点 `DELETE /v1/agents/{agent_code}/artifacts`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_artifacts.py`
- Modify: `services/control-plane/tests/test_console_lockdown.py`
- Test: `services/control-plane/tests/test_external_artifacts.py`

**Interfaces:**
- Consumes(Task 1/2 已建):`build_external_artifacts_router()` / `_get_artifact_store` / `_get_audit` / `_artifact_error`
- Consumes(既有):
  - `ArtifactStore.soft_delete(*, tenant_id: UUID, user_id: UUID, name: str, now: datetime) -> bool`
  - `emit(logger, *, tenant_id, actor_id, action, resource_type, resource_id=None, result=..., trace_id=None, details=None, on_behalf_of=None)`(`control_plane.audit`,按 `external_sessions.py:409` 的用法)
  - `AuditAction.ARTIFACT_DELETE` / `current_trace_id_hex()`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_external_artifacts.py`:

```python
@pytest.mark.asyncio
async def test_delete_soft_deletes_and_hides_from_list(
    external_client, seed_artifact
) -> None:
    """删除命中 → 200 {deleted: name};之后列表里不再出现。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    resp = await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "report.docx"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": "report.docx"}
    listing = await external_client.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "u-1"}
    )
    assert listing.json()["data"]["artifacts"] == []


@pytest.mark.asyncio
async def test_delete_is_idempotent_miss_404(external_client, seed_artifact) -> None:
    """已软删 / 不存在 / 跨用户 → 同一个 404,不泄露存在性。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    first = await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "report.docx"},
    )
    assert first.status_code == 200
    second = await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "report.docx"},
    )
    unknown = await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "never-existed.docx"},
    )
    cross_user = await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-2", "name": "report.docx"},
    )
    assert second.status_code == unknown.status_code == cross_user.status_code == 404
    assert second.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    assert second.json() == unknown.json() == cross_user.json(), (
        "三种情况的响应体必须逐字节相同 —— 任何差异都是存在性预言机"
    )


@pytest.mark.asyncio
async def test_delete_requires_write_not_delete_scope(
    external_client_factory, seed_artifact
) -> None:
    """write 档的 key 就能删 —— 不是 delete 档。

    ApiKeyScope 没有独立 delete 档,挂 "delete" 等于只有 admin key 能删,
    逼第三方拿一把能改服务账号的钥匙才能删自己的文件。把实现里的
    require("session", "write") 改成 "delete" 时这条必须红。
    """
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    client = await external_client_factory(scopes=["write"])
    resp = await client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "report.docx"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_emits_audit_with_on_behalf_of(
    external_client, seed_artifact, audit_entries
) -> None:
    """审计写 ARTIFACT_DELETE,带 on_behalf_of=终端用户。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "report.docx"},
    )
    entry = next(e for e in audit_entries() if e.action.value == "artifact:delete")
    assert entry.resource_id == "report.docx"
    assert entry.on_behalf_of is not None


@pytest.mark.asyncio
async def test_delete_miss_writes_no_audit(external_client, audit_entries) -> None:
    """未命中不写审计 —— 否则第三方能用审计流水反推名字存不存在。"""
    await external_client.delete(
        "/v1/agents/test-agent/artifacts",
        params={"user_id": "u-1", "name": "never-existed.docx"},
    )
    assert [e for e in audit_entries() if e.action.value == "artifact:delete"] == []
```

- [ ] **Step 2: 运行,确认失败**

```bash
cd services/control-plane && uv run pytest tests/test_external_artifacts.py -v -k delete
```

预期:FAIL —— 405 或 404(DELETE 路由不存在)。

- [ ] **Step 3: 写实现**

`external_artifacts.py` 顶部补 import:

```python
from datetime import UTC, datetime

from control_plane.audit import emit as audit_emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import AuditAction
```

在下载路由之后、`return router` 之前:

```python
    @router.delete(
        "/{agent_code}/artifacts",
        response_model=None,
        dependencies=[Depends(require("session", "write"))],
    )
    async def delete_artifact(
        agent_code: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        name: Annotated[str, Query(min_length=1, max_length=1024)],
    ) -> JSONResponse:
        """Soft-delete one artifact (metadata only).

        ``require("session", "write")`` — **not** ``"delete"``:
        ``ApiKeyScope`` has no standalone delete tier, so gating on it would
        mean only an ``admin``-scope key could delete, forcing a third party
        to hold a key that can also rewrite service accounts just to remove
        its own file. Same ruling as ``archive_session``.

        The workspace bytes are untouched — the retention sweep hard-deletes
        later, and an agent re-saving the same name un-deletes the row.

        Unknown / already-deleted / cross-user all collapse to one 404 so the
        response never reveals whether the name exists.
        """
        del agent_code  # artifacts are (tenant, user)-scoped — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        hit = await store.soft_delete(
            tenant_id=tenant_id, user_id=end_user_id, name=name, now=datetime.now(UTC)
        )
        if not hit:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.ARTIFACT_DELETE,
            resource_type="artifact",
            resource_id=name,
            trace_id=current_trace_id_hex(),
            details={"op": "soft_delete"},
            on_behalf_of=str(end_user_id),
        )
        return JSONResponse({"success": True, "data": {"deleted": name}, "error": None})
```

- [ ] **Step 4: 登记自审表**

`test_console_lockdown.py` 的 `_EXTERNAL_AGENT_ROUTES` 里加:

```python
        ("DELETE", "/v1/agents/{agent_code}/artifacts"),
```

- [ ] **Step 5: 跑测试**

```bash
cd services/control-plane && uv run pytest tests/test_external_artifacts.py -v
uv run pytest tests/test_console_lockdown.py tests/test_external_only_gate.py \
    tests/test_external_path_param_nul_guard.py tests/test_external_route_reachability.py \
    tests/test_route_plane_partition.py tests/test_external_scope.py -q
```

预期:全部 PASS。前台跑。

- [ ] **Step 6: 自证权限档位被锁住**

把 `require("session", "write")` 改成 `require("session", "delete")`,跑 `test_delete_requires_write_not_delete_scope`,**必须红**。确认后还原(复制副本,不用 `git checkout --`)。

- [ ] **Step 7: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_artifacts.py \
        services/control-plane/tests/test_console_lockdown.py \
        services/control-plane/tests/test_external_artifacts.py
git commit -m "feat(external-api): 阶段 3 PR-B — 对外产物软删端点"
```

---

### Task 4: 文档

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/query.md`(新增「5.7 产物」)
- Modify: `apps/admin-ui/docs-site/guide/errors.md`(8.1 速查表补行)
- Modify: `apps/admin-ui/docs-site/guide/best-practices.md`(9.6 清单补行)
- Modify: `apps/admin-ui/docs-site/.vitepress/config.mts`(侧栏)

**Interfaces:**
- Consumes:Task 1-3 的三条端点最终形状(路径、query 参数、响应字段、错误码 `ARTIFACT_NOT_FOUND` / `ARTIFACT_CONTENT_UNAVAILABLE`)

**写作要求**(文档站 2026-08-16 刚重构过,新内容必须与它一致):
- **不造术语。** 不写「闸」「铸造」「不透明 404」「终局状态」;用「校验 / 限制」「自动创建」「统一的 404」「最终状态」。
- 正文标点用全角(代码块与行内代码内不动)。
- 每节开头一句话说明「这个接口解决什么问题」。
- 长句拆短,注意事项用表格而非大段散文。

- [ ] **Step 1: 写 5.7 节**

在 `query.md` 的「5.6 工作区文件」之后追加:

````markdown
## 5.7 产物

Agent 在执行过程中可以把一份成果**登记成产物**(比如一份周报、一份导出数据)。产物和工作区文件的区别:工作区是 Agent 的原始文件系统,产物是 Agent 主动挑出来、给人看的那些,带名字、类型和版本号。

这三条接口的 `agent_code` **不参与过滤**——产物按 (租户, 终端用户) 维度存,与 [5.6 工作区文件](#_5-6-工作区文件) 同一条规则。

### 列出产物

```
GET /v1/agents/{agent_code}/artifacts
```

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/artifacts?user_id=u-123" \
  -H "Authorization: Bearer <key>"
```

```json
{
  "success": true,
  "data": {
    "artifacts": [
      {
        "name": "2026-08 周报.docx",
        "kind": "document",
        "latest_version": 3,
        "created_at": "2026-08-12T10:00:00+00:00",
        "updated_at": "2026-08-14T09:30:00+00:00"
      }
    ]
  },
  "error": null
}
```

| 字段 | 说明 |
|---|---|
| `name` | 产物名,在同一个终端用户下唯一。下载和删除都用它 |
| `kind` | `document` / `code` / `data` / `other`,由 Agent 保存时声明 |
| `latest_version` | 版本号。Agent 每次用同名保存一次就 +1 |
| `created_at` / `updated_at` | 首次创建 / 最近一次更新时间 |

**列表里没有文件大小。** 大小和校验和是**首次下载时才记录**的,列表里给出来大部分是 `null`,反而误导。

已删除的产物不出现在这个列表里。`user_id` 是这个租户从没见过的值时返回空列表,不是 404。

### 下载产物

```
GET /v1/agents/{agent_code}/artifacts/download
```

下载的永远是**最新版本**,当前不提供按版本号下载。

| 查询参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | |
| `name` | 是 | 原样回传列表里的 `name`,不要自己拼 |

```bash
curl "https://<your-domain>/v1/agents/{agent_code}/artifacts/download?user_id=u-123&name=2026-08%20周报.docx" \
  -H "Authorization: Bearer <key>" \
  -o report.docx
```

成功响应是文件字节流本身,**不是** `{success, data, error}` 形状——那个形状只包裹错误响应。`Content-Disposition` 的规则与 [5.6 工作区文件](#_5-6-工作区文件) 完全一致:HTML / SVG 这类可执行内容强制 `attachment`,响应始终带 `X-Content-Type-Options: nosniff`。

**这条接口计入配额。** 每次下载扣 1 次 `artifact_download` 额度,超限时返回 429,见 [7.6 限流与配额](./conventions#_7-6-限流与配额)。

错误响应:

| 状态码 | `error.code` | 含义 |
|---|---|---|
| 404 | `ARTIFACT_NOT_FOUND` | 产物不存在、已删除、或不属于这个 `user_id`——**三种情况统一返回这一个 404**,不区分 |
| 500 | `ARTIFACT_CONTENT_UNAVAILABLE` | 产物记录在,但服务端读不到内容(存储配置问题)。**这不是"不存在"**,重试没用,联系你的租户管理员 |

### 删除产物

```
DELETE /v1/agents/{agent_code}/artifacts
```

要求 `write` 权限(与归档会话一样,对外 API 没有单独的删除权限档位)。

| 查询参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | |
| `name` | 是 | |

```bash
curl -X DELETE "https://<your-domain>/v1/agents/{agent_code}/artifacts?user_id=u-123&name=2026-08%20周报.docx" \
  -H "Authorization: Bearer <key>"
```

```json
{ "success": true, "data": { "deleted": "2026-08 周报.docx" }, "error": null }
```

::: warning 这是软删除
产物从列表里消失、也下载不到了,但**工作区里的文件字节没有删**。到保留期后由后台清理任务真正删除。

如果 Agent 之后又用同一个 `name` 保存了一次,这个产物会**恢复**(版本号接着往上加)。

当前 API 没有对外的撤销删除操作。
:::

产物不存在、已经删过、或不属于这个 `user_id`,都返回同一个 404 `ARTIFACT_NOT_FOUND`,不区分。
````

- [ ] **Step 2: 补错误码速查表**

`errors.md` 的 8.1 速查表里,按状态码顺序插入两行(格式与相邻行一致):

```markdown
| `ARTIFACT_NOT_FOUND` | 404 | 产物不存在、已删除,或不属于这个 `user_id` | 核对 `user_id` / `name`;三种情况不区分 |
| `ARTIFACT_CONTENT_UNAVAILABLE` | 500 | 产物记录在,服务端读不到内容 | 服务端存储配置问题,重试无效,联系租户管理员 |
```

- [ ] **Step 3: 补侧栏与自测清单**

`.vitepress/config.mts` 的「5 查询与管理」items 末尾加:

```typescript
                { text: "5.7 产物", link: "/guide/query#_5-7-产物" },
```

`best-practices.md` 的 9.6 清单里,在工作区那行之后加:

```markdown
- [ ] 列出、下载、删除一个 Agent 产物 —— [5.7](./query#_5-7-产物)
```

- [ ] **Step 4: 构建并验证零死链**

```bash
cd apps/admin-ui/docs-site && pnpm build
```

预期:`build complete`。然后跑死链校验(从渲染产物提取全部 `id`,核对每条站内链接的锚点存在):

```bash
cd apps/admin-ui/docs-site && python3 - <<'PYEOF'
import re, os, urllib.parse
DIST='.vitepress/dist'; pages, files = {}, {}
for root,_,names in os.walk(DIST):
    for n in names:
        if not n.endswith('.html'): continue
        fp=os.path.join(root,n); html=open(fp,encoding='utf-8').read()
        url='/docs/'+os.path.relpath(fp,DIST).replace('index.html','').replace('.html','')
        url=url.rstrip('/') or '/docs'
        pages[url]={urllib.parse.unquote(i) for i in re.findall(r'\sid="([^"]+)"',html)}
        files[url]=html
SKIP=re.compile(r'^/docs/assets/|\.(css|js|woff2?|png|svg|ico)$')
bad,total=[],0
for url,html in files.items():
    for href in re.findall(r'href="(/docs/[^"]*)"',html):
        if SKIP.search(href.split('#')[0]): continue
        total+=1
        path,_,frag=href.partition('#'); path=path.rstrip('/') or '/docs'
        if path.endswith('.html'): path=path[:-5]
        if path not in pages: bad.append((url,href,'PAGE')); continue
        if frag and urllib.parse.unquote(frag) not in pages[path]: bad.append((url,href,'锚点'))
    # 同页锚点 —— 渲染成 href="#..."(没有 /docs/ 前缀),上面那条正则匹配不到。
    # 2026-08-16 的文档重构给标题加编号后,31 条同页锚点全失效而脚本报「零死链」,
    # 这一段就是补那个盲区的,别删。
    for href in re.findall(r'href="(#[^"]+)"',html):
        total+=1
        if urllib.parse.unquote(href[1:]) not in pages[url]: bad.append((url,href,'同页锚点'))
print(f'{total} 条站内链接 / {len(pages)} 页')
for s,h,w in bad: print(f'  ❌ [{w}] {h} ← {s}')
print('✅ 无死链' if not bad else f'{len(bad)} 条死链')
PYEOF
```

预期:`✅ 无死链`。**VitePress 只检文件级死链,不检锚点**,这个脚本是唯一能发现 `#_5-7-产物` 这类锚点写错的手段。

- [ ] **Step 5: 提交**

```bash
git add apps/admin-ui/docs-site
git commit -m "docs(external-api): 阶段 3 PR-B — 产物视图三条端点文档"
```

---

## 自审

**1. spec 覆盖**:第四节的每条都有归属 —— 端点三条(Task 1/2/3)、`agent_code` 不过滤(三个任务的 docstring + Task 1 的 `test_agent_code_does_not_filter`)、`name` 走 query(Task 2/3 签名)、DELETE 参数位置(Task 3)、列表字段与无 `size_bytes`(Task 1 的字段集断言)、软删不出现(Task 1)、下载全套安全处理(Task 2)、配额扣减(Task 2)、错误映射与顺序(Task 2 Step 6 变异)、裸字节流(Task 2)、软删语义(Task 3)、`write` 而非 `delete`(Task 3 Step 6 变异)、审计(Task 3)、三条「不做」(Global Constraints 12,无对应任务=正确)。文档(Task 4)是 spec 之外的既有交付惯例。

**2. 占位符扫描**:无 TBD / TODO;每个代码步骤都给了可直接落地的完整代码;测试步骤都给了完整断言。

**3. 类型一致性**:`build_external_artifacts_router()` 在三个任务里同名;`_get_artifact_store` / `_get_workspace_store`(Task 1 定义)→ Task 2 消费;`_get_audit`(Task 2 定义)→ Task 3 消费;`_artifact_error(code, message, status)`(Task 2 定义)→ Task 3 消费;错误码 `ARTIFACT_NOT_FOUND` / `ARTIFACT_CONTENT_UNAVAILABLE` 在 Task 2/3/4 三处一致。

**一处需要实现者当场确认的事实**:Task 1 的测试用了 `external_client` / `seed_artifact` / `count_tenant_users` 等 fixture 名。**这些 fixture 未必存在** —— 实现者第一步必须先读 `tests/test_external_workspace.py` 与 `tests/conftest.py`,用那里已有的 fixture 搭法,名字对不上就改成既有的,不要新造一套平行的测试基建。
