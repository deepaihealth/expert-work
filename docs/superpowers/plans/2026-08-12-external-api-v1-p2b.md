# 第三方对接 API v1 P2-b 实施计划(块 4:客户端补完)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给第三方补上「拿到 agent 产出的文件」与「管理自己的会话」这两类操作,把对外 API 从「能跑 agent」推到「能做一个完整客户端」。

**Architecture:** 四个新端点,全部是对控制台既有实现的**对外镜像** —— 复用同一批安全 helper,只把「控制台身份解析」换成 P1 的 `_external.py` 那套 `user_id` → `ext:` → `tenant_user` 解析。零新存储路径、零新安全原语。

**Tech Stack:** Python 3.13 / FastAPI / Pydantic v2 / pytest

**Spec:** [`docs/superpowers/specs/2026-08-12-external-api-v1-p2-design.md`](../specs/2026-08-12-external-api-v1-p2-design.md) §六

**前置:** 本计划与 [P2-a](./2026-08-12-external-api-v1-p2a.md) **无代码耦合**,可并行或后置。建议独立 PR。

## Global Constraints

- 对外响应一律 `{success, data, error}` 信封;所有 404 隐藏存在性(不区分「不存在」与「不属于你」)。
- 归属解析一律走 `_external.py` 的现成通路,读路径 `mint=False` —— 读操作**绝不**为没见过的 `user_id` 铸 `tenant_user` 行。
- scope 闸:读用 `require("session", "read")`,写用 `require("session", "write")`,归档用 `require("session", "delete")`(`Action` 合法取值见 `auth/rbac.py:50`)。
- 控制台侧的 `console_only()` 端点**一个都不动** —— 本计划只新增对外镜像。
- **不镜像 `DELETE /v1/workspace/file`**:破坏性操作,第三方缺「这文件重不重要」的上下文,需单独拍板(spec §九-4)。
- 本地跑 integration 测试须先 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/control-plane/src/control_plane/api/external_workspace.py` | 对外工作区列表 + 下载 | 建 |
| `services/control-plane/src/control_plane/api/external_sessions.py` | 追加重命名 / 归档 | 改 |
| `services/control-plane/src/control_plane/app.py` | 挂载新 router | 改 |
| `services/control-plane/tests/test_external_workspace.py` | 新端点测试 | 建 |
| `services/control-plane/tests/test_external_sessions.py` | 会话管理测试 | 改 |

---

## Task 1: 对外工作区文件列表

**Files:**
- Create: `services/control-plane/src/control_plane/api/external_workspace.py`
- Modify: `services/control-plane/src/control_plane/app.py`(挂 router)
- Test: `services/control-plane/tests/test_external_workspace.py`(新建)

**Interfaces:**
- Produces: `GET /v1/agents/{agent_code}/workspace/files?user_id=` → `{success, data: {files: [{path, size}]}, error}`
- Produces: `build_external_workspace_router() -> APIRouter`

**镜像来源:** `api/workspace.py:171-217`(`list_workspace_files`)。逐条对照:

| 控制台侧 | 对外侧 |
|---|---|
| `ensure_single_tenant_scope(...)` | 删掉 —— 对外只有单租户语义,`tenant_id` 来自 key |
| `resolve_target_user_id(request, users, requested=user_id)` | `_external.py` 的 `user_id`(字符串)→ `external_subject_id` → `tenant_user`,`mint=False` |
| `console_only()` | `require("session", "read")` |
| 裸 `{"success": True, "data": ...}` | 同款信封(已一致) |
| `WorkspacePermissionError` → 500 | **原样保留** |
| `SandboxSupervisorError` → `[]` | **原样保留** |

> ⚠️ `WorkspacePermissionError` 与 `SandboxSupervisorError` 的 except **顺序不能反** ——
> 前者是后者的子类,反了那一支永远走不到。控制台侧的注释写明了这点,镜像时一并抄过来。
> ⚠️ 权限失败**不能**吞成空列表:用户会看到「工作区是空的」,比报错更坏 —— 连「出错了」
> 都看不到,诊断成本全压到服务端日志。

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_external_workspace.py
"""对外工作区端点(P2-b)。"""

import pytest


@pytest.mark.asyncio
async def test_list_files_returns_envelope(external_client, seeded_workspace) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/files",
        params={"user_id": seeded_workspace.user_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    assert {f["path"] for f in body["data"]["files"]} == {"报表.xlsx"}


@pytest.mark.asyncio
async def test_list_files_unknown_user_returns_empty_not_mint(
    external_client, plain_agent, user_store
) -> None:
    """读路径 mint=False —— 没见过的 user_id 不能铸出 tenant_user 行。"""
    before = await user_store.count_all()
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/files",
        params={"user_id": "从没见过的用户"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["files"] == []
    assert await user_store.count_all() == before


@pytest.mark.asyncio
async def test_list_files_requires_read_scope(external_client_no_scope, plain_agent) -> None:
    resp = await external_client_no_scope.get(
        f"/v1/agents/{plain_agent.code}/workspace/files", params={"user_id": "u1"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_files_permission_error_is_500_not_empty(
    external_client, plain_agent, workspace_store_raising_permission_error
) -> None:
    """权限失败必须冒出来 —— 吞成空列表会让用户以为工作区是空的。"""
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/files", params={"user_id": "u1"}
    )
    assert resp.status_code == 500
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_workspace.py -v`
Expected: FAIL — 404(路由不存在)

- [ ] **Step 3: 实现**

```python
# services/control-plane/src/control_plane/api/external_workspace.py
"""对外工作区端点 —— ``GET /v1/agents/{agent_code}/workspace/{files,file}``。

agent 把产出物写进终端用户的持久工作区,第三方 app 得能列出来、下下来 ——
否则「agent 生成了一份报表」这件事在他们界面上就没有下文。

控制台侧 ``api/workspace.py`` 的四个端点全挂 ``console_only()``(P1 控制台平面
收口刻意锁的)。本模块是它们的对外镜像:安全处理(MIME 嗅探 / attachment +
nosniff / 路径校验 / 权限失败与不存在分开)全部复用,只把控制台的身份解析换成
P1 的 ``_external`` 通路。

**不镜像 DELETE** —— 破坏性操作,第三方缺上下文,需单独拍板。
"""
```

router 挂 `prefix="/v1/agents"`,两个端点:`GET /{agent_code}/workspace/files` 与
(Task 2)`GET /{agent_code}/workspace/file`。列表端点体照上表逐条对照实现。
`app.py` 里挂载新 router(照 P1 那 5 个 `build_external_*_router()` 的挂法)。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_workspace.py tests/test_external_route_reachability.py -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_workspace.py \
        services/control-plane/src/control_plane/app.py \
        services/control-plane/tests/test_external_workspace.py
git commit -m "feat(control-plane): 对外工作区文件列表端点"
```

---

## Task 2: 对外工作区文件下载

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_workspace.py`
- Test: `services/control-plane/tests/test_external_workspace.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 router 与身份解析
- Produces: `GET /v1/agents/{agent_code}/workspace/file?user_id=&path=` → 文件字节流

**镜像来源:** `api/workspace.py:218-276`(`download_workspace_file`)。必须原样复用的四样:
`_safe_workspace_relpath`(路径校验)、`infer_content_type`、`content_disposition_header`
(活动内容一律 `attachment`)、`X-Content-Type-Options: nosniff`。

> ⚠️ 跨用户 / 文件不存在 / 无 supervisor **一律同一个不透明 404** —— 控制台侧就是这么做的,
> 镜像不能因为「对外要友好」就把它们区分开,那是存在性泄漏。
> ⚠️ 权限失败仍走 500(不是 404),原因同 Task 1。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_external_workspace.py
@pytest.mark.asyncio
async def test_download_returns_bytes_with_safe_headers(external_client, seeded_workspace) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": "报表.xlsx"},
    )
    assert resp.status_code == 200
    assert resp.content == seeded_workspace.expected_bytes
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Disposition"].startswith("attachment")


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "..", ""])
@pytest.mark.asyncio
async def test_download_path_traversal_rejected(external_client, seeded_workspace, bad) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": bad},
    )
    assert resp.status_code == 400, f"{bad!r} 应被拒"


@pytest.mark.asyncio
async def test_download_cross_user_is_opaque_404(external_client, seeded_workspace, other_user) -> None:
    """拿别人的 user_id 下文件 —— 与「文件不存在」返回完全一样的 404。"""
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": other_user.user_id, "path": "报表.xlsx"},
    )
    missing = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": "不存在.txt"},
    )
    assert resp.status_code == missing.status_code == 404
    assert resp.json() == missing.json()


@pytest.mark.asyncio
async def test_download_html_forced_to_attachment(external_client, seeded_workspace_html) -> None:
    """活动内容必须 attachment —— 否则是存储型 XSS。"""
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace_html.agent_code}/workspace/file",
        params={"user_id": seeded_workspace_html.user_id, "path": "x.html"},
    )
    assert resp.headers["Content-Disposition"].startswith("attachment")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_workspace.py -k download -v`
Expected: FAIL — 404(路由不存在)

- [ ] **Step 3: 实现**

照 `api/workspace.py:219-276` 逐段镜像,替换身份解析,except 顺序与 404 语义原样保留。

- [ ] **Step 4: 变异自验**

把 `_safe_workspace_relpath` 的调用临时改成直接用原始 `path`,重跑参数化的路径穿越测试,
确认 **FAIL**(至少 `../../etc/passwd` 一例)。恢复后再跑绿。

- [ ] **Step 5: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_workspace.py tests/test_external_hardening.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_workspace.py services/control-plane/tests/test_external_workspace.py
git commit -m "feat(control-plane): 对外工作区文件下载端点"
```

---

## Task 3: 对外会话重命名与归档

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_sessions.py`
- Test: `services/control-plane/tests/test_external_sessions.py`(追加)

**Interfaces:**
- Produces:
  - `PATCH /v1/agents/{agent_code}/sessions/{session_id}` body `{user_id, title}` → 信封
  - `DELETE /v1/agents/{agent_code}/sessions/{session_id}?user_id=` → 信封(软删/归档)
- Consumes: 既有 `load_owned_session`、`ThreadMetaStore.update_title` / `update_status`

**镜像来源:** `api/sessions.py:879-913`(rename)、`:914-943`(archive)。

> ⚠️ 归属校验走 `load_owned_session(..., mint=False)` —— 这是**写**操作,但操作的是
> 已存在的会话,不该为没见过的 `user_id` 铸行(与 P1 `load_owned_session` 文档里
> 「是否创建它所寻址的会话」那条分界线一致)。
> ⚠️ 归档是**软删**:检查点 / runs / 工作区都不动,只是从默认列表里隐藏。文档必须写明 ——
> 别让第三方以为调了就是彻底删除。硬删(`:purge`)不对外暴露。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_external_sessions.py
@pytest.mark.asyncio
async def test_rename_session(external_client, seeded_session) -> None:
    resp = await external_client.patch(
        f"/v1/agents/{seeded_session.agent_code}/sessions/{seeded_session.session_id}",
        json={"user_id": seeded_session.user_id, "title": "改过的标题"},
    )
    assert resp.status_code == 200 and resp.json()["success"] is True
    listed = await external_client.get(
        f"/v1/agents/{seeded_session.agent_code}/sessions",
        params={"user_id": seeded_session.user_id},
    )
    assert listed.json()["data"]["sessions"][0]["title"] == "改过的标题"


@pytest.mark.asyncio
async def test_rename_blank_title_is_422(external_client, seeded_session) -> None:
    resp = await external_client.patch(
        f"/v1/agents/{seeded_session.agent_code}/sessions/{seeded_session.session_id}",
        json={"user_id": seeded_session.user_id, "title": "   "},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rename_foreign_session_is_404(external_client, seeded_session, other_user) -> None:
    resp = await external_client.patch(
        f"/v1/agents/{seeded_session.agent_code}/sessions/{seeded_session.session_id}",
        json={"user_id": other_user.user_id, "title": "偷改"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_session_hides_from_list(external_client, seeded_session) -> None:
    resp = await external_client.delete(
        f"/v1/agents/{seeded_session.agent_code}/sessions/{seeded_session.session_id}",
        params={"user_id": seeded_session.user_id},
    )
    assert resp.status_code == 200 and resp.json()["success"] is True
    listed = await external_client.get(
        f"/v1/agents/{seeded_session.agent_code}/sessions",
        params={"user_id": seeded_session.user_id},
    )
    assert listed.json()["data"]["sessions"] == []


@pytest.mark.asyncio
async def test_archive_requires_delete_scope(external_client_write_only, seeded_session) -> None:
    """write 档不够 —— 归档要 delete 档(与控制台侧同款)。"""
    resp = await external_client_write_only.delete(
        f"/v1/agents/{seeded_session.agent_code}/sessions/{seeded_session.session_id}",
        params={"user_id": seeded_session.user_id},
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_sessions.py -k "rename or archive" -v`
Expected: FAIL — 405(方法不存在)

- [ ] **Step 3: 实现**

在 `external_sessions.py` 的 `build_external_sessions_router()` 里追加两个端点:

```python
class ExternalRenameRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=200)


@router.patch(
    "/{agent_code}/sessions/{session_id}",
    response_model=None,
    dependencies=[Depends(require("session", "write"))],
)
async def rename_session(...) -> JSONResponse:
    """改会话标题(覆盖自动标题)。不属于 ``(user, agent)`` 一律 404。"""


@router.delete(
    "/{agent_code}/sessions/{session_id}",
    response_model=None,
    dependencies=[Depends(require("session", "delete"))],
)
async def archive_session(...) -> JSONResponse:
    """归档会话 —— **软删**:从默认列表隐藏,可逆;检查点 / runs / 工作区都不动。
    不可逆的硬删(``:purge``)不对外暴露。"""
```

两者都先 `load_owned_session(..., mint=False)`,再调 `update_title` / `update_status`,
`False` 返回值转 404,并 `emit` 审计(照控制台侧那两处的审计参数)。
标题 `strip()` 后为空 → 422。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_sessions.py tests/test_external_scope.py -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_sessions.py services/control-plane/tests/test_external_sessions.py
git commit -m "feat(control-plane): 对外会话重命名 / 归档端点"
```

---

## Task 4: 文档

**Files:**
- Modify: 文档站对外 API 章节(新增「工作区」与「会话管理」两节)

- [ ] **Step 1: 定位文档源文件**

Run: `rg -l "/v1/agents/.*sessions" docs/ --glob '!**/specs/**' --glob '!**/plans/**'`

- [ ] **Step 2: 写工作区两个端点**

字段表 + curl 示例。必须写明:
- 列表返回的是**该终端用户**工作区的全部文件,与会话无关
- 下载一律 `attachment`(浏览器不会内联渲染),这是有意的 XSS 防护
- 跨用户与文件不存在返回**同一个** 404,不要据此推断文件是否存在

- [ ] **Step 3: 写会话管理两个端点**

必须写明:**`DELETE` 是归档(软删),不是彻底删除** —— 检查点 / runs / 工作区都保留,
只是从默认列表隐藏。硬删不对外提供。

- [ ] **Step 4: 本地起文档站核对**

Run: `pnpm -C docs dev`
逐页核对渲染与示例可复制。

- [ ] **Step 5: 提交**

```bash
git add docs/
git commit -m "docs: 对外 API 补工作区与会话管理两节"
```

---

## 收尾：全量校验

- [ ] **CI 同款全量跑**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest
```

- [ ] **真栈验收**(spec §八 第 10–13 条)

发测试集群后逐条跑通,重点是第 11 条(跨用户下载返回不透明 404)与第 12 条(路径穿越 400)。

---

## 自查记录

**Spec 覆盖**：spec §六A(工作区列表 + 下载)→ Task 1、2;§六B(会话重命名 / 删除)→ Task 3;文档 → Task 4。§六A 明说「不镜像 DELETE /file」,已在 Global Constraints 与 Task 1 docstring 两处记明,无任务实现它 —— 这是**有意的**不覆盖。

**类型一致性**：Task 1 建立的 router 与身份解析被 Task 2 复用(同一模块);Task 3 复用既有 `load_owned_session` / `update_title` / `update_status`,签名见 `api/sessions.py:879-943` 与 `thread_meta/base.py:204-226`。

**scope 取值已核实**：`Action` 的合法取值定义在 `services/control-plane/src/control_plane/auth/rbac.py:50`,含 `read` / `write` / `delete`。对外归档用 `delete` 与控制台侧 `sessions.py:914` 的 `require_key_scope("delete")` 对齐。

**已知需实现者补齐的**：测试 fixture(`external_client`、`external_client_no_scope`、`external_client_write_only`、`seeded_workspace`、`seeded_session`、`other_user`、`user_store`)—— 先查 `services/control-plane/tests/conftest.py` 里 P1 是否已建同名或等价 fixture,**优先复用,不新造一套桩**。
