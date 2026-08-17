# 对外附件模型统一 —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对外 API 的附件只有一种 id(`upl_<uuid>`)、一个回传形状(`files:[{upload_id}]`)、一个下载口(`GET …/uploads/{upload_id}`);删掉顶层 `image_refs`。

**Architecture:** 新登记表 `user_upload` 记录每次对外上传(图片 / 文档都记),对外 id 就是它的主键。上传端点写行,run 端点凭 id 查表分流(图片 → 内部 `image_refs`;文档 → `document_names`),下载端点凭 id 查表再去底层(对象存储 / 工作区)取字节。图片字节的生命周期仍由既有 `image_upload` 表管。

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic;in-memory / SQL 双 store;pytest(+ testcontainers 集成)。

**Spec:** `docs/superpowers/specs/2026-08-17-external-upload-unification-design.md`(下称「spec」)。

## Global Constraints

- **对外 API 尚未上生产,不留任何旧路**:`image_refs` 字段、`files[].type`、`files[].transfer_method`、`uploads/…` 与 `expert_work://image/…` 形态的 `upload_id` 一律**不再接受**。
- 对外 `upload_id` 格式:`upl_` + 小写带连字符 UUID(如 `upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17`)。渲染与解析必须走同一个 helper(`expert_work.protocol.user_upload.render_upload_id` / `parse_upload_id`)。
- 错误码(全部套 `{success:false,data:null,error:{code,message}}` 信封):`INVALID_UPLOAD_ID`(422)、`UPLOAD_NOT_FOUND`(404)、`UPLOAD_CONTENT_UNAVAILABLE`(500)。
- 404 一律**不透露存在性**:未知 `user_id` / 行不存在 / 不属于该用户 / 已软删 / 底层已回收 → 同一个 `UPLOAD_NOT_FOUND`。
- **读端点不铸用户**:一律 `lookup_external_user_id(mint=False)`。
- **不计配额、不改控制台平面**的 `/v1/sessions/{thread_id}/uploads`。
- SQL 与 in-memory 两个 store 的谓词**逐字节同义**。
- 新路由必须登记进两张手工表:`tests/test_console_lockdown.py::_EXTERNAL_AGENT_ROUTES` 与 `tests/test_external_only_gate.py::_EXTERNAL_ROUTES`。
- `WorkspacePermissionError` 的 `except` 必须排在 `SandboxSupervisorError` 之前(子类)。
- 每条新断言先 break→red→restore→green;**变异前把文件复制到 scratchpad,`git diff` 确认变异生效,从副本还原,禁用 `git checkout --`**。
- 集成测试:`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`,前台跑。
- 公开文档红线:不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径。

---

## 文件结构

| 动作 | 路径 | 职责 |
|---|---|---|
| Create | `packages/expert-work-protocol/src/expert_work/protocol/user_upload.py` | `UserUpload` 记录模型 + `UserUploadKind` + `render_upload_id` / `parse_upload_id` |
| Modify | `packages/expert-work-protocol/src/expert_work/protocol/__init__.py` | 导出上面三样 |
| Create | `packages/expert-work-persistence/migrations/versions/0146_user_upload.py` | 建表(照 `0028_image_upload.py`) |
| Create | `packages/expert-work-persistence/src/expert_work/persistence/models/user_upload.py` | `UserUploadRow` ORM |
| Create | `packages/expert-work-persistence/src/expert_work/persistence/user_upload/{__init__,base,memory,sql}.py` | `UserUploadStore` 三件套 |
| Modify | `packages/expert-work-persistence/src/expert_work/persistence/__init__.py` + `models/__init__.py` | 导出 |
| Modify | `services/control-plane/src/control_plane/app.py` | `user_upload_repo` 参数、`sql_stores.user_upload`、`app.state.user_upload_store` |
| Modify | `services/control-plane/src/control_plane/purge/user_purge.py` | 级联删 `user_upload` |
| Modify | `services/control-plane/src/control_plane/api/external_uploads.py` | 上传写行 + 新响应;**新增下载 GET** |
| Modify | `services/control-plane/src/control_plane/api/agents.py` | `ExternalRunRequest` 删 `image_refs`;`ExternalFileRef` 只剩 `upload_id`;解析走 store |
| Modify | `services/control-plane/tests/test_console_lockdown.py`、`tests/test_external_only_gate.py` | 登记新路由 |
| Create/Modify | 各测试文件(见每个 task) | |
| Modify | `apps/admin-ui/docs-site/guide/chat.md`、`errors.md`、`query.md` | 文档(Task 5) |

## 任务依赖与并行

```
Task 1(protocol + 表 + store + 接线 + purge)
  ├─→ Task 2(上传端点)──→ Task 4(下载端点 + 路由表)   [同一 worktree,顺序:都改 external_uploads.py]
  └─→ Task 3(run 请求)                                 [独立 worktree,与 2/4 并行:只改 agents.py + 其测试]
Task 5(文档)在 2/3/4 合并后
```

---

### Task 1: protocol 模型 + `user_upload` 表 + store 三件套 + app 接线 + purge 级联

**Files:**
- Create: `packages/expert-work-protocol/src/expert_work/protocol/user_upload.py`
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/__init__.py`
- Create: `packages/expert-work-persistence/migrations/versions/0146_user_upload.py`
- Create: `packages/expert-work-persistence/src/expert_work/persistence/models/user_upload.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/__init__.py`
- Create: `packages/expert-work-persistence/src/expert_work/persistence/user_upload/__init__.py`、`base.py`、`memory.py`、`sql.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/__init__.py`
- Modify: `services/control-plane/src/control_plane/app.py`(照 `image_upload_repo` 那三处:参数 `:543`、resolved `:655`、`app.state` `:2288`、`SqlStores` 字段 `:2605`、`_build_sql_stores` `:2828`)
- Modify: `services/control-plane/src/control_plane/purge/user_purge.py`(`PurgeStores` 加 `user_uploads: UserUploadStore`;级联加一步 `summary.deleted["user_upload"] = await deps.user_uploads.delete_all_for_user(...)`,照 `:320` 的 image_upload 步)
- Modify: `PurgeStores(...)` 的构造点(`rg -n "PurgeStores\(" services/control-plane/src` 找到,加 `user_uploads=state.user_upload_store`)
- Test: `packages/expert-work-protocol/tests/test_user_upload_id.py`(新)
- Test: `packages/expert-work-persistence/tests/test_in_memory_user_upload_store.py`(新,照 `test_in_memory_image_upload_store.py`)
- Test: `packages/expert-work-persistence/tests/test_sql_user_upload_store.py`(新,照 `test_sql_image_upload_store.py`,`pytestmark = pytest.mark.integration`)
- Test: `services/control-plane/tests/test_user_purge.py`(加一条)

**Interfaces:**
- Produces(后续 task 依赖,签名必须一字不差):

```python
# expert_work/protocol/user_upload.py
from typing import Literal
UserUploadKind = Literal["image", "document"]

class UserUpload(BaseModel):            # frozen
    id: UUID
    tenant_id: UUID
    user_id: UUID
    thread_id: UUID
    kind: UserUploadKind
    ref: str                            # image: expert_work://image/… ; document: uploads/<name>
    mime_type: str
    size_bytes: int
    filename: str
    created_at: datetime
    deleted_at: datetime | None

UPLOAD_ID_PREFIX: Final = "upl_"
def render_upload_id(upload_id: UUID) -> str: ...        # f"upl_{upload_id}"
def parse_upload_id(raw: str) -> UUID | None: ...        # 严格:前缀 + 36 位小写 uuid4 形状;其它一律 None

# expert_work/persistence/user_upload/base.py
class UserUploadStore(abc.ABC):
    async def insert(self, *, upload_id: UUID, tenant_id: UUID, user_id: UUID, thread_id: UUID,
                     kind: UserUploadKind, ref: str, mime_type: str, size_bytes: int,
                     filename: str) -> UserUpload: ...
    async def get(self, *, upload_id: UUID, tenant_id: UUID) -> UserUpload | None: ...
        # 只按 (id, tenant) 取,**不过滤 user_id / deleted_at**——调用方比对(与 image_upload.get 同款)
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int: ...
```

- `app.state.user_upload_store: UserUploadStore`

- [ ] **Step 1: protocol 模型 + id helper 的失败测试**

`packages/expert-work-protocol/tests/test_user_upload_id.py`:

```python
from uuid import UUID, uuid4
import pytest
from expert_work.protocol import parse_upload_id, render_upload_id

def test_round_trip():
    u = uuid4()
    assert parse_upload_id(render_upload_id(u)) == u

@pytest.mark.parametrize("bad", [
    "", "upl_", "upl_not-a-uuid", "UPL_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",   # 前缀大小写
    "upl_3F2C9A1E-7B44-4D3E-9C1A-2F6D0E8B5A17",                                    # uuid 大写
    "3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",                                        # 无前缀
    "uploads/report.pdf", "expert_work://image/x/y/z.png",                          # 旧形态
    "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17 ", " upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",
    "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17\x00",
])
def test_rejects(bad):
    assert parse_upload_id(bad) is None

def test_render_is_lowercase_hyphenated():
    assert render_upload_id(UUID("3F2C9A1E-7B44-4D3E-9C1A-2F6D0E8B5A17")) == "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17"
```

- [ ] **Step 2: 跑,确认 ImportError 红**

`cd packages/expert-work-protocol && uv run pytest tests/test_user_upload_id.py -q`

- [ ] **Step 3: 实现 `protocol/user_upload.py`**

```python
"""对外附件登记记录 + 对外 upload_id 的渲染 / 解析(spec 2026-08-17)。"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Final, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict

UserUploadKind = Literal["image", "document"]
UPLOAD_ID_PREFIX: Final = "upl_"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

class UserUpload(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    tenant_id: UUID
    user_id: UUID
    thread_id: UUID
    kind: UserUploadKind
    ref: str
    mime_type: str
    size_bytes: int
    filename: str
    created_at: datetime
    deleted_at: datetime | None = None

def render_upload_id(upload_id: UUID) -> str:
    return f"{UPLOAD_ID_PREFIX}{upload_id}"

def parse_upload_id(raw: str) -> UUID | None:
    if not raw.startswith(UPLOAD_ID_PREFIX):
        return None
    body = raw[len(UPLOAD_ID_PREFIX):]
    if not _UUID_RE.fullmatch(body):
        return None
    return UUID(body)
```

导出进 `protocol/__init__.py`(`UserUpload`, `UserUploadKind`, `UPLOAD_ID_PREFIX`, `render_upload_id`, `parse_upload_id`)。

- [ ] **Step 4: 跑,绿**

- [ ] **Step 5: 迁移 `0146_user_upload.py`**

照 `0028_image_upload.py` 逐段抄(revision 串用 `0146_user_upload`,`down_revision = "0145_agent_run_idempotency"`),表定义按 spec §二.1:

```python
op.create_table(
    "user_upload",
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
    sa.Column("user_id", UUID(as_uuid=True), nullable=False),
    sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
    sa.Column("kind", sa.Text(), nullable=False),
    sa.Column("ref", sa.Text(), nullable=False),
    sa.Column("mime_type", sa.Text(), nullable=False),
    sa.Column("size_bytes", sa.BigInteger(), nullable=False),
    sa.Column("filename", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('image','document')", name="user_upload_kind_enum"),
    sa.CheckConstraint("size_bytes >= 0", name="user_upload_size_nonneg"),
)
op.create_index("ix_user_upload_tenant_user", "user_upload", ["tenant_id", "user_id"])
op.create_index("ix_user_upload_tenant_thread", "user_upload", ["tenant_id", "thread_id"])
op.execute("ALTER TABLE user_upload ENABLE ROW LEVEL SECURITY")
# policy 语句逐字照 0028 的 image_upload_tenant_isolation,把表名 / policy 名换成 user_upload
```

`downgrade` 逆序:drop policy → drop indexes → drop table。**alembic revision 上限 32 字符**(`0146_user_upload` 没问题,别改长)。

- [ ] **Step 6: ORM `models/user_upload.py`**(照 `models/image_upload.py`,列与迁移一一对应,`__tablename__ = "user_upload"`);在 `models/__init__.py` 导出 `UserUploadRow`。

- [ ] **Step 7: store 三件套 — 先写 in-memory 测试(红)**

`test_in_memory_user_upload_store.py`(照 `test_in_memory_image_upload_store.py` 结构):

```python
async def test_insert_then_get_same_tenant(store):
    row = await store.insert(upload_id=U1, tenant_id=T1, user_id=USER1, thread_id=TH1,
                             kind="document", ref="uploads/report.pdf", mime_type="application/pdf",
                             size_bytes=10, filename="report.pdf")
    assert row.id == U1 and row.kind == "document" and row.deleted_at is None
    assert (await store.get(upload_id=U1, tenant_id=T1)) == row

async def test_get_other_tenant_is_none(store): ...        # 同 id 换 tenant → None
async def test_get_unknown_is_none(store): ...
async def test_get_does_not_filter_user(store):            # 换 user 查仍返回行(调用方比对)
async def test_delete_all_for_user_counts_and_scopes(store):
    # 同 tenant 两个 user 各 2 行 → 删 USER1 返回 2,USER2 仍能 get;跨 tenant 同 user_id 不受影响
```

- [ ] **Step 8: 实现 `user_upload/base.py`(ABC,签名见 Interfaces)、`memory.py`(dict 键 `(tenant_id, id)`)、`sql.py`(照 `image_upload/sql.py` 的 `insert` / `get` / `delete_all_for_user` 三段);`__init__.py` 导出 `UserUploadStore` / `InMemoryUserUploadStore` / `SqlUserUploadStore`;persistence `__init__.py` 再导出一次。**`get` 的 SQL WHERE 只有 `id = :id AND tenant_id = :tenant`,memory 也只比这两项。**

- [ ] **Step 9: in-memory 绿;写 SQL 集成测试(照 `test_sql_image_upload_store.py`:testcontainers 起库 → alembic upgrade head → 三方法各一条 + `downgrade -1` 再 `upgrade head` 不报错)**;`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && cd packages/expert-work-persistence && uv run pytest tests/test_sql_user_upload_store.py -q -m integration` 绿。

- [ ] **Step 10: app 接线**(五处,照 `image_upload_repo` 逐处对位:`create_app(..., user_upload_repo: UserUploadStore | None = None)`、`resolved_user_upload_store`、`app.state.user_upload_store`、`SqlStores.user_upload`、`_build_sql_stores` 里 `user_upload=SqlUserUploadStore(session_factory)`)。跑 `services/control-plane`:`uv run pytest tests/test_app_wiring*.py tests/test_admin_api.py -q`(若无 wiring 测试,`python -c "from control_plane.app import create_app; a=create_app(); assert a.state.user_upload_store"`)。

- [ ] **Step 11: purge 级联**

`user_purge.py`:`PurgeStores` 加 `user_uploads: UserUploadStore`;在 image_upload 那步之后加:

```python
summary.deleted["user_upload"] = await deps.user_uploads.delete_all_for_user(
    tenant_id=tenant_id, user_id=user_id
)
```

包在与其它步同款的 try/except 里(单步失败不阻断)。构造点补 `user_uploads=state.user_upload_store`。`test_user_purge.py` 加一条:seed 两行 → purge → `summary.deleted["user_upload"] == 2` 且 `get` 为 None。跑 `uv run pytest tests/test_user_purge.py -q`。

- [ ] **Step 12: 全量守门**:`cd services/control-plane && uv run pytest tests -q -x -p no:cacheprovider`(前台;约 12 分钟);`uv run ruff check . && uv run ruff format --check .`;仓库根 CI 同款 mypy(`rg -n "mypy" .github/workflows/*.yml` 取命令原样跑)。

- [ ] **Step 13: Commit**

```
feat(persistence): user_upload 登记表 + UserUploadStore 三件套 + 对外 upload_id 渲染/解析 + purge 级联(附件模型统一 Task 1)
```

---

### Task 2: 上传端点写登记行,响应改返 `upl_` id

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_uploads.py`(响应构造两处:文档 `:272` 附近、图片 `:318` 附近;新增依赖 `_get_user_upload_store`)
- Test: `services/control-plane/tests/test_external_uploads.py`

**Interfaces:**
- Consumes: Task 1 的 `UserUploadStore.insert`、`render_upload_id`、`app.state.user_upload_store`
- Produces: 响应 `data.upload_id` 恒为 `upl_<uuid>`;`user_upload` 行 `kind`/`ref`/`filename`:
  - 文档:`kind="document"`,`ref=doc_result.path`(形如 `uploads/report.pdf`),`filename` = `doc_result.path` 的叶子名(`.rsplit("/",1)[-1]`)
  - 图片:`kind="image"`,`ref=image_ref.to_uri()`,`filename=f"{image_ref.image_id}{image_ref.ext}"`
  - `thread_id` = 本次上传绑定的会话;`user_id` = `end_user_id`(tenant_user id);`mime_type=content_type`;`size_bytes` 同响应 `size`

- [ ] **Step 1: 失败测试**——在 `test_external_uploads.py` 找到既有「文档上传成功」「图片上传成功」两条,各加断言:

```python
import re
UPL = re.compile(r"^upl_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
assert UPL.fullmatch(body["data"]["upload_id"])
row = await app.state.user_upload_store.get(upload_id=parse_upload_id(body["data"]["upload_id"]), tenant_id=TENANT)
assert row is not None and row.kind == "document" and row.ref.startswith("uploads/") and row.user_id == <seed 的 tenant_user id>
```

图片那条断 `row.kind == "image"` 且 `row.ref.startswith("expert_work://image/")`。**既有断言 `upload_id == "uploads/…"` / `startswith("expert_work://")` 全部删掉**——旧形态不再对外。

- [ ] **Step 2: 跑,红**(`uv run pytest tests/test_external_uploads.py -q`)

- [ ] **Step 3: 实现**:两处响应构造前各 `await uploads.insert(...)`(`upload_id=uuid4()` 先生成),`"upload_id": render_upload_id(row.id)`。依赖注入照 `_get_image_upload_store` 写 `_get_user_upload_store`。

- [ ] **Step 4: 跑,绿;变异自证**:注释掉 `insert` 调用 → 行查询断言红;`render_upload_id` 换成 `doc_result.path` → 正则断言红。还原(从 scratchpad 副本)。

- [ ] **Step 5: 全量 + ruff + mypy;Commit** `feat(external-api): 上传端点登记 user_upload 行,upload_id 统一为 upl_<uuid>(Task 2)`

---

### Task 3: run 请求 —— 删 `image_refs`,`files[]` 只剩 `upload_id`,凭表分流

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py`(`ExternalFileRef` `:527`–`:540`;`ExternalRunRequest` `:541`–`:575`;解析块 `:1228`–`:1290`;`spawn_run` 调用 `:1330` 附近)
- Test: `services/control-plane/tests/test_external_run_files.py`(重写:26 处 `image_refs` 引用全清)

**Interfaces:**
- Consumes: Task 1 的 `UserUploadStore.get`、`parse_upload_id`、`app.state.user_upload_store`
- Produces:`ExternalFileRef` = `{upload_id: str}`(`extra="forbid"`,`min_length=1`, `max_length=64`——`upl_`+36 恰 40);`ExternalRunRequest` **无** `image_refs`;内部仍以 `image_refs: list[str]` + `document_names: list[str]` 调 `spawn_run`。

- [ ] **Step 1: 重写测试(红)**——保留既有测试的 fixture / seed 方式(**用户必须先 seed 出来**,否则请求在 `end_user_id is None` 短路,永远走不到被测分支——本仓四次踩坑),用例集:

| 用例 | 期望 |
|---|---|
| 请求体带顶层 `image_refs` | 422(pydantic extra forbid;信封 `INVALID_REQUEST` 或既有 422 码,与既有 `extra` 拒绝同码) |
| `files:[{upload_id, type:"image"}]` | 422(extra forbid) |
| `files:[{upload_id:"uploads/report.pdf"}]` | 422 `INVALID_UPLOAD_ID` |
| `files:[{upload_id:"upl_<不存在>"}]` | 404 `UPLOAD_NOT_FOUND` |
| 行属于另一 user | 404 `UPLOAD_NOT_FOUND` |
| 图片行 `thread_id` ≠ 本次会话 | 404 `UPLOAD_NOT_FOUND` |
| 一图一文 | `spawn_run` 收到 `image_refs=[row.ref]`、`document_names=["uploads/report.pdf"]`(monkeypatch spy) |
| 65 项 | 422(既有 max_length) |
| `MAX_RUN_IMAGE_REFS`+1 张图 | 422(既有码,消息不再提 `image_refs`) |
| Agent 不支持视觉 + 图片 | 422(既有码) |

seed 行直接 `await app.state.user_upload_store.insert(...)`(in-memory)。

- [ ] **Step 2: 跑,红**

- [ ] **Step 3: 实现**——`ExternalFileRef` 只留 `upload_id: str = Field(min_length=1, max_length=64)`;删 `image_refs` 字段与 `:1228`–`:1260` 的合并;新解析块(放在原位置):

```python
uploads_store: UserUploadStore = request.app.state.user_upload_store
image_refs: list[str] = []
document_names: list[str] = []
for item in payload.files:
    uid = parse_upload_id(item.upload_id)
    if uid is None:
        raise ExternalScopeError("INVALID_UPLOAD_ID", "upload_id must be the value returned by POST /v1/agents/{agent_code}/uploads", 422)
    row = await uploads_store.get(upload_id=uid, tenant_id=tenant_id)
    if row is None or row.user_id != end_user_id or row.deleted_at is not None:
        raise ExternalScopeError("UPLOAD_NOT_FOUND", "upload not found", 404)
    if row.kind == "image":
        if row.thread_id != thread_id:
            raise ExternalScopeError("UPLOAD_NOT_FOUND", "upload not found", 404)
        image_refs.append(row.ref)
    else:
        document_names.append(_safe_document_name_or_422(row.ref))   # 防御纵深,既有函数
if len(image_refs) > MAX_RUN_IMAGE_REFS:
    raise ExternalScopeError(<既有码>, f"files[] 里的图片不能超过 {MAX_RUN_IMAGE_REFS} 张", 422)
```

`ExternalScopeError` 的构造签名照文件里既有用法;`thread_id` 用解析块处已解析出的本次会话 id(注意顺序:会话解析必须在此块之前——现状已如此,`_validate_image_refs` 需要 thread)。后续 `_validate_image_refs(...)` 与 `spawn_run(image_refs=..., document_names=...)` 调用保持。

- [ ] **Step 4: 跑,绿;三条变异**:①去掉 `row.user_id != end_user_id` → 跨用户用例红;②去掉 `row.thread_id != thread_id` → 跨会话用例红;③把 `document_names.append` 改成 `image_refs.append` → 一图一文 spy 用例红。还原。

- [ ] **Step 5: 全量 + ruff + mypy;Commit** `feat(external-api): run 请求删 image_refs,files[] 只回传 upload_id,凭 user_upload 表分流(Task 3)`

---

### Task 4: 下载端点 `GET /v1/agents/{agent_code}/uploads/{upload_id}` + 两张路由表

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_uploads.py`(在既有 router 上加 GET)
- Modify: `services/control-plane/tests/test_console_lockdown.py::_EXTERNAL_AGENT_ROUTES`、`tests/test_external_only_gate.py::_EXTERNAL_ROUTES`(各加 `("GET", "/v1/agents/{agent_code}/uploads/{upload_id}")`,格式照表内既有条目)
- Test: `services/control-plane/tests/test_external_upload_download.py`(新)

**Interfaces:**
- Consumes: Task 1/2;`external_artifacts.download_artifact`(`:166`–`:270`)为结构模板;`_artifact_mime.infer_content_type(kind=…, path=…)` + `content_disposition_header(filename, disposition=…)`;`parse_image_ref`(`expert_work.protocol.multimodal`);`app.state.image_upload_store.get(image_id=, tenant_id=)`;`app.state.object_store.get(key)`(取法照 `_get_object_store`);`workspace_store.read_file(tenant_id=, user_id=, path=)`。
- Produces:成功裸字节;失败信封;错误码见 Global Constraints。

- [ ] **Step 1: 失败测试** `test_external_upload_download.py`(fixture 照 `test_external_artifacts.py`,seed 用户 + 直接 `insert` 行;文档字节靠既有假 workspace store,图片字节靠既有假 object store——两者在 artifacts / uploads 测试里都有现成 fake,复用):

| 用例 | 期望 |
|---|---|
| 文档 `.txt` | 200,`Content-Type` 起于 `text/plain`,`Content-Disposition` 含 `inline`,body 相等 |
| 文档 `.html` | 200,`attachment`(红线) |
| 图片 `.png` | 200,`image/png`,`inline`,`X-Content-Type-Options: nosniff` |
| `upload_id` 形状错 | 422 `INVALID_UPLOAD_ID` |
| `user_id` 未知 | 404 `UPLOAD_NOT_FOUND`,且 `tenant_user` 计数不变 |
| 行属于别的 user | 404 |
| 图片行在、`image_upload` 无此 id | 404 |
| 文档 `read_file` 抛 `WorkspacePermissionError` | **500** `UPLOAD_CONTENT_UNAVAILABLE` |
| 文档 `read_file` 抛 `SandboxSupervisorError` | 404 |
| 零 scope key | 403(既有 `require("session","read")` 行为,一条即可) |

- [ ] **Step 2: 跑,红**

- [ ] **Step 3: 实现**(照 `download_artifact` 抄结构;`del agent_code`;`lookup_external_user_id(mint=False)`;`store.get` → 三重比对 → 分流;`infer_content_type(kind=("image" if row.kind=="image" else "document"), path=row.filename)`——`ArtifactKind` 若不含 "image" 就传 "other"/按 `_artifact_mime` 现有枚举取最贴近的,**实施者查 `ArtifactKind` 定义后选,并在代码注释里写明理由**;`content_disposition_header(row.filename, disposition=inferred.disposition)`;`except WorkspacePermissionError` 在 `except SandboxSupervisorError` **之前**)。审计:照 `download_artifact` 是否 emit——**先 grep 它,同款**。

- [ ] **Step 4: 跑,绿;两条变异**:①交换两个 except 顺序 → 500 用例红;②去掉 `row.user_id != end_user_id` → 跨用户用例红。还原。

- [ ] **Step 5: 路由表登记**;跑 `uv run pytest tests/test_console_lockdown.py tests/test_external_only_gate.py tests/test_route_plane_partition.py tests/test_external_nul_guard*.py tests/test_route_reachability*.py -q`(文件名以 `ls tests | rg "nul|reachab"` 为准)全绿——**新路由不登记这几条会红,登记了才绿,这本身就是自证**。

- [ ] **Step 6: 全量 + ruff + mypy;Commit** `feat(external-api): 对外附件下载端点 GET …/uploads/{upload_id}(Task 4)`

---

### Task 5: 文档 —— chat.md / errors.md 整章(接口新形状 + 可读性 + 术语 + 代码块标题)

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/chat.md`(§2.3 表、§2.6 整节、「三个容易踩的地方」;**同时整章过可读性**)
- Modify: `apps/admin-ui/docs-site/guide/errors.md`(码表两处 `:31`/`:33` 与 `:164`/`:166`;**同时整章过可读性**)
- Modify: `apps/admin-ui/docs-site/guide/query.md`(若 §5.6 工作区文件一节提到 `uploads/` 可下载,补一句指向新下载端点;没提就不动——线 B Task 3 已留指针的话就不重复)

**Interfaces:** Consumes Task 2/3/4 的最终形状(实施前 `git log` 确认三者已合)。**同时 consumes 线 B**(`docs/superpowers/plans/2026-08-17-external-docs-readability.md`)的三样东西:① Task 0 的代码块标题语法 ```` ```bash [请求] ```` / ```` ```json [响应 200] ````;② Global Constraints 里的可读性四条 + 术语规则(「帧」→「事件」);③ 线 B Task 1 report 里「交线 A」段列出的 chat.md / errors.md 需要同步的 SSE 锚点。**这两份文件线 B 刻意不碰,整章可读性由本 task 负责。**

- [ ] **Step 1: §2.3 请求表**:删 `image_refs` 行;`files` 行改「附件。每项 `{ "upload_id": "…" }`,值来自上传接口。见 2.6」。
- [ ] **Step 2: §2.6 重写**(保留 mermaid 时序图,改文字与载荷):
  - 开头一句:附件三步——上传拿 `upload_id` → 放进 `files[]` 发起对话 →(需要回显时)用同一个 `upload_id` 下载。
  - `files[]` 字段表只剩 `upload_id` 一行。
  - 「第一步:上传」——两个代码组(文档 / 图片),每组请求块标 `[请求]`、响应块标 `[响应 201]`,响应里 `upload_id` 用 `upl_…` 真形状(**用一个固定示例值贯穿全节**,如 `upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17`)。
  - 「第二步:带进对话」——`files:[{"upload_id":"upl_…"},{"upload_id":"upl_…"}]`。
  - 新增「第三步:下载 / 回显」——`GET …/uploads/{upload_id}?user_id=…`,说明返回裸字节、`Content-Type` 与 `Content-Disposition`、图片可直接 `<img src>`(需带 key,所以通常由你的服务端转发)。
  - 「容易踩的地方」只留:① `upload_id` 原样回传别解析;② 图片必须与发起对话的会话是同一段(上传返回的 `session_id` 要传回 `session_id`);③ 未知/别人的 `upload_id` 一律 404 不区分。
- [ ] **Step 3: errors.md**:删 `INVALID_FILE_REF` / `INVALID_IMAGE_REF`(两处表);加 `INVALID_UPLOAD_ID`(422)/ `UPLOAD_NOT_FOUND`(404)/ `UPLOAD_CONTENT_UNAVAILABLE`(500),链接锚点指向 `./chat#_2-6-带图片和文档`(**构建后 grep 产物 HTML 核对 id**)。
- [ ] **Step 4: 构建 + 死链/死锚点检查**:`cd apps/admin-ui && pnpm --filter docs-site build`(命令以 `package.json` 为准),再跑 `docs/superpowers/plans/2026-08-16-phase3-pr-b-artifacts.md` 里那段死链脚本(含同页锚点)。
- [ ] **Step 5: 红线扫描**:`rg -n "aliyuncs|kubeconfig|127\.0\.0\.1|crpi-|expert_work\.|control_plane\.|packages/|services/" apps/admin-ui/docs-site/guide/chat.md apps/admin-ui/docs-site/guide/errors.md` 必须为空(`expert_work://image/` 这个 URI 形态也不能再出现在公开文档里)。
- [ ] **Step 6: Commit** `docs(external-api): 附件章按统一 upload_id 重写 —— 上传/回传/下载三步 + 错误码(Task 5)`
