# 阶段 3 PR-A(agent 目录 + run 列表)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给第三方两个只读列表端点 —— 「这个租户有哪些 agent 能调」和「这个用户在这个 agent 上跑过哪些 run」。

**Architecture:** 两个新端点都走既有的对外平面通路(`external_only()` + `require(...)` + `_external.py` 的 owner 校验),不新造机制。`GET /v1/agent-catalog` 是新前缀(`/v1/agents` 已被控制台面占死);`GET /v1/agents/{agent_code}/runs` 挂在已有的 `external_runs.py` 路由上。两个 store 各加一个批量方法消除 N+1 / 分页失准。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / pytest;前端 React + antd + vitest;文档站 VitePress。

**规格来源:** `docs/superpowers/specs/2026-08-15-external-api-v1-phase3-design.md`

## Global Constraints

以下每条都隐含在每个 task 的要求里,不再逐条重复:

1. **对外端点三件套缺一不可**:路由必须挂 `dependencies=[Depends(reject_nul_path_params), Depends(external_only())]`,每条路由再挂自己的 `Depends(require(资源, 动作))`。
2. **`tags=["external"]`** —— 三个自审测试靠 tag 发现路由(Task 4 之后)。
3. **`user_id` 必填、无默认值**。漏传必须是 422,永远不能降级成「列出整个租户」。
4. **读路径一律 `mint=False`** —— 用 `lookup_external_user_id`,绝不用 `resolve_external_user_id`。陌生 `user_id` 返回空结果,不建 `tenant_user` 行。
5. **越权一律 404,永不用 403**,响应体不携带存在性信息。
6. **响应信封** `{"success": bool, "data": ..., "error": null | {"code", "message"}}`。错误一律经 `external_error(ExternalScopeError(...))` 渲染。
7. **SQL 与 in-memory 两个后端的谓词必须字节级同义**。任何加到一个后端的过滤条件,另一个必须同语义,且要有跨后端等价性测试。
8. **每条新断言必须变异自证**:break → 跑出 red → restore → green。只会绿的断言等于没有断言。
9. **变异前先把文件复制一份到 scratchpad**,不要用 `git checkout --` 还原(本仓已四次因此吞掉未提交的真实修改)。
10. **admin-ui 类型检查必须用 `pnpm typecheck`**(`tsc -b`),裸 `tsc --noEmit` 在本仓恒绿、一个文件都不检查。
11. **本地跑 integration 测试需要** `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。
12. **公开文档站机密红线**:不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径。
13. **不要动 `.vitepress/dist/`**(产物目录,不入库)。
14. 句末不留尾随空白。

---

## 文件结构

| 文件 | 责任 | Task |
|---|---|---|
| `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py` | `AgentSpecBody` 加 `display_name` | 1 |
| `packages/expert-work-persistence/src/expert_work/persistence/agent_disable/{base,memory,sql}.py` | 批量查禁用集 | 2 |
| `services/control-plane/src/control_plane/api/external_agent_catalog.py` | **新建** —— agent 目录端点 | 3 |
| `services/control-plane/src/control_plane/app.py` | 挂载新 router | 3 |
| `services/control-plane/tests/test_external_{only_gate,path_param_nul_guard,route_reachability}.py` | 发现器改 tag 驱动 | 4 |
| `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py` | `list_for_tenant` 加 `agent_name` | 5 |
| `services/control-plane/src/control_plane/api/external_runs.py` | run 列表端点 | 6 |
| `apps/admin-ui/src/components/manifest-editor/{form_model.ts,FormView.tsx}` + `i18n/locales/{en,zh-CN}.ts` | 显示名输入框 | 7 |
| `apps/admin-ui/docs-site/guide/{run-agent.md}` + `.vitepress/config.mts` | 对外文档 | 8 |

---

## Task 1:manifest 加 `display_name` 字段

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:1145-1160`(`AgentSpecBody`)
- Test: `packages/expert-work-protocol/tests/test_agent_spec.py`

**Interfaces:**
- Consumes: 无
- Produces: `AgentSpecBody.display_name: str`(默认 `""`)。Task 3 读 `record.spec.spec.display_name`;Task 7 前端读写 `spec.display_name`。

**背景**:manifest 现在只有 `metadata.name`(机器标识,= `agent_code`)和 `spec.description`。客户端界面直接显示 `agent_code` 很难看。新字段可选、有默认值,存量 manifest 反序列化不受影响,**不需要数据迁移**。

- [ ] **Step 1: 写失败的测试**

在 `packages/expert-work-protocol/tests/test_agent_spec.py` 末尾追加:

```python
def test_display_name_defaults_to_empty_string() -> None:
    """存量 manifest(没有这个字段)必须照常反序列化,不能抛 ValidationError。

    ``AgentSpecBody`` 是 ``extra="forbid"``,所以新增字段只影响写入侧;
    这条证明读取侧对老数据向后兼容。
    """
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "a", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
                "system_prompt": {"template": "x"},
            },
        }
    )
    assert spec.spec.display_name == ""


def test_display_name_round_trips() -> None:
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "a", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "display_name": "报表助手",
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
                "system_prompt": {"template": "x"},
            },
        }
    )
    assert spec.spec.display_name == "报表助手"
    assert spec.model_dump(mode="json")["spec"]["display_name"] == "报表助手"


def test_display_name_appears_in_the_generated_json_schema() -> None:
    """``GET /v1/agents/schema`` 由 ``AgentSpec.model_json_schema()`` 生成,
    manifest 编辑器直接吃它。字段进不了 schema,编辑器就永远看不到。"""
    schema = AgentSpec.model_json_schema(by_alias=True)
    body = schema["$defs"]["AgentSpecBody"]["properties"]
    assert "display_name" in body
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
cd /Users/mac/src/github/jone_qian/expert-work
uv run pytest packages/expert-work-protocol/tests/test_agent_spec.py -k display_name -v
```

预期:三条全 FAIL。前两条报 `AttributeError: 'AgentSpecBody' object has no attribute 'display_name'`;`test_display_name_round_trips` 也可能先报 `ValidationError: Extra inputs are not permitted`(`extra="forbid"`)。

- [ ] **Step 3: 加字段**

在 `agent_spec.py` 的 `AgentSpecBody` 里,把 `description` 那一行改成两行:

```python
    description: str = ""
    #: 阶段 3 (3.1) — 给终端用户看的名字。``metadata.name``(= 对外的
    #: ``agent_code``)是机器标识,直接显示在第三方界面上很难看。
    #: 可选:为空时 ``GET /v1/agent-catalog`` 回落到 ``agent_code``,
    #: 所以对外响应里这个字段永远非空,客户端不用做空判断。
    display_name: str = ""
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest packages/expert-work-protocol/tests/test_agent_spec.py -k display_name -v
```

预期:3 passed。

- [ ] **Step 5: 变异自证**

把默认值临时改成 `display_name: str = "X"`,重跑,确认 `test_display_name_defaults_to_empty_string` 变红;改回来,确认变绿。

**改之前先 `cp agent_spec.py /private/tmp/claude-501/.../scratchpad/`,改回来用副本,不要用 `git checkout --`。**

- [ ] **Step 6: 跑全量 protocol 测试确认没回归**

```bash
uv run pytest packages/expert-work-protocol/tests/ -q
```

- [ ] **Step 7: 提交**

```bash
git add packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py \
        packages/expert-work-protocol/tests/test_agent_spec.py
git commit -m "feat(protocol): AgentSpecBody 加可选 display_name——给终端用户看的 agent 名字"
```

---

## Task 2:`AgentDisableStore` 加批量查禁用集

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/agent_disable/base.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/agent_disable/memory.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/agent_disable/sql.py`
- Test: `packages/expert-work-persistence/tests/test_in_memory_agent_disable_store.py`
- Test: `packages/expert-work-persistence/tests/test_sql_agent_disable_store.py`

**Interfaces:**
- Consumes: 无
- Produces: `AgentDisableStore.list_disabled_names(*, tenant_id: UUID) -> set[str]`。Task 3 用它一次性拿到该租户全部被禁用的 agent 名。

**为什么需要**:目录端点要给每个 agent 算 `available`。现有的只有 per-agent 的 `get(tenant_id, agent_name)` —— 列 50 个 agent 就是 50 次点查(`AgentDisableService` 的 TTL 缓存首次全 miss)。这是标准的 N+1,而本仓专门扫过这类问题(`docs/research/` 的 N+1 编目)。

禁用集本身很小 —— `agent_disable_status.py` 自己的模块 docstring 就写着 "the disabled set is small" —— 所以一次查该租户全部禁用行,比 N 次点查便宜得多。

- [ ] **Step 1: 写 in-memory 的失败测试**

在 `packages/expert-work-persistence/tests/test_in_memory_agent_disable_store.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_list_disabled_names_returns_only_disabled_and_only_this_tenant() -> None:
    store = InMemoryAgentDisableStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.set_disabled(
        tenant_id=tenant_a, agent_name="off-1", disabled=True, reason="r", disabled_by="admin"
    )
    await store.set_disabled(
        tenant_id=tenant_a, agent_name="off-2", disabled=True, reason=None, disabled_by=None
    )
    # 一条 disabled=False 的行 —— enable 过的 agent 会留下这样的行,
    # 它绝不能出现在结果里(否则目录端点会把正常 agent 标成不可用)。
    await store.set_disabled(
        tenant_id=tenant_a, agent_name="back-on", disabled=False, reason=None, disabled_by=None
    )
    # 另一个租户的禁用行 —— 跨租户泄漏会让 A 租户看到 B 租户的 agent 名。
    await store.set_disabled(
        tenant_id=tenant_b, agent_name="other-tenant", disabled=True, reason=None, disabled_by=None
    )

    assert await store.list_disabled_names(tenant_id=tenant_a) == {"off-1", "off-2"}


@pytest.mark.asyncio
async def test_list_disabled_names_is_empty_when_nothing_is_disabled() -> None:
    store = InMemoryAgentDisableStore()
    assert await store.list_disabled_names(tenant_id=uuid4()) == set()
```

文件顶部如果还没有,补上 `from uuid import uuid4`。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest packages/expert-work-persistence/tests/test_in_memory_agent_disable_store.py -k list_disabled -v
```

预期:FAIL,`AttributeError: 'InMemoryAgentDisableStore' object has no attribute 'list_disabled_names'`。

- [ ] **Step 3: 加抽象方法**

在 `base.py` 的 `AgentDisableStore` 类里,`get` 之后加:

```python
    @abc.abstractmethod
    async def list_disabled_names(self, *, tenant_id: UUID) -> set[str]:
        """这个租户当前被禁用的 agent 名字集合。

        阶段 3 (3.1) —— ``GET /v1/agent-catalog`` 要给列表里每个 agent 算
        ``available``,逐个调 :meth:`get` 就是一次 N+1。禁用集很小(kill
        switch 是罕见的管理动作),一次查完整个租户比 N 次点查便宜。

        只返回 ``disabled=True`` 的行。enable 过的 agent 会留下一条
        ``disabled=False`` 的行,它**必须**不在结果里 —— 否则目录端点会把
        一个正常 agent 标成不可用。
        """
```

- [ ] **Step 4: in-memory 实现**

在 `memory.py` 的 `get` 之后加:

```python
    async def list_disabled_names(self, *, tenant_id: UUID) -> set[str]:
        async with self._lock:
            return {
                name
                for (row_tenant, name), record in self._rows.items()
                if row_tenant == tenant_id and record.disabled
            }
```

- [ ] **Step 5: SQL 实现**

在 `sql.py` 的 `get` 之后加(文件顶部补 `from sqlalchemy import select`):

```python
    async def list_disabled_names(self, *, tenant_id: UUID) -> set[str]:
        # 只 SELECT 名字这一列 —— 调用方只要名字集合,不需要整行。
        # ``disabled.is_(True)`` 而不是 ``== True``:enable 过的行仍在表里
        # (disabled=False),必须被过滤掉。
        stmt = select(AgentDisableRow.agent_name).where(
            AgentDisableRow.tenant_id == tenant_id,
            AgentDisableRow.disabled.is_(True),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return set(rows)
```

- [ ] **Step 6: 跑 in-memory 测试确认通过**

```bash
uv run pytest packages/expert-work-persistence/tests/test_in_memory_agent_disable_store.py -k list_disabled -v
```

预期:2 passed。

- [ ] **Step 7: 写 SQL 的等价性测试**

在 `packages/expert-work-persistence/tests/test_sql_agent_disable_store.py` 末尾追加(照该文件既有测试的 fixture 名与 marker 写;这个文件已有真容器 fixture,照抄同文件里其它测试的签名):

```python
@pytest.mark.asyncio
async def test_list_disabled_names_matches_the_in_memory_store(
    sql_store: SqlAgentDisableStore,
) -> None:
    """SQL 与 in-memory 的谓词必须同义 —— 两个后端各写一遍过滤条件,
    是本仓反复出问题的地方(SQL 用 ``== True`` / 内存用 ``is True``
    这类差异不会被单后端测试发现)。同一组输入喂两个 store,断言输出相等。
    """
    from expert_work.persistence.agent_disable.memory import InMemoryAgentDisableStore

    mem_store = InMemoryAgentDisableStore()
    tenant_id = uuid4()
    fixtures = [("off-1", True), ("off-2", True), ("back-on", False)]
    for name, disabled in fixtures:
        for store in (sql_store, mem_store):
            await store.set_disabled(
                tenant_id=tenant_id,
                agent_name=name,
                disabled=disabled,
                reason=None,
                disabled_by=None,
            )

    sql_result = await sql_store.list_disabled_names(tenant_id=tenant_id)
    mem_result = await mem_store.list_disabled_names(tenant_id=tenant_id)
    assert sql_result == mem_result == {"off-1", "off-2"}


@pytest.mark.asyncio
async def test_list_disabled_names_does_not_leak_across_tenants(
    sql_store: SqlAgentDisableStore,
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    await sql_store.set_disabled(
        tenant_id=tenant_a, agent_name="mine", disabled=True, reason=None, disabled_by=None
    )
    await sql_store.set_disabled(
        tenant_id=tenant_b, agent_name="theirs", disabled=True, reason=None, disabled_by=None
    )
    assert await sql_store.list_disabled_names(tenant_id=tenant_a) == {"mine"}
```

- [ ] **Step 8: 跑 SQL 测试(真容器)**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests/test_sql_agent_disable_store.py -k list_disabled -v
```

预期:2 passed。**这一步必须真跑,不能只跑 in-memory** —— `disabled.is_(True)` 这类 SQL 谓词在 in-memory 后端根本不存在。

- [ ] **Step 9: 变异自证**

把 SQL 实现的 `.is_(True)` 临时删掉(变成只按 tenant 过滤),重跑 Step 8,确认 `test_list_disabled_names_matches_the_in_memory_store` 变红(`back-on` 会混进来)。

**改之前先复制副本**;还原用副本覆盖,不用 `git checkout`。

改完后 **`git diff` 确认变异真的落地了**再读结果 —— 本仓有过两次变异因为格式化后替换串失配而根本没改到文件,差点让人误判测试空转。

- [ ] **Step 10: 提交**

```bash
git add packages/expert-work-persistence/src/expert_work/persistence/agent_disable/ \
        packages/expert-work-persistence/tests/test_in_memory_agent_disable_store.py \
        packages/expert-work-persistence/tests/test_sql_agent_disable_store.py
git commit -m "feat(persistence): AgentDisableStore 加 list_disabled_names——目录端点批量判可用性,免 N+1"
```

---

## Task 3:`GET /v1/agent-catalog` 端点

**Files:**
- Create: `services/control-plane/src/control_plane/api/external_agent_catalog.py`
- Modify: `services/control-plane/src/control_plane/app.py`(import + `include_router`)
- Test: `services/control-plane/tests/test_external_agent_catalog.py`(新建)

**Interfaces:**
- Consumes: `AgentSpecBody.display_name`(Task 1);`AgentDisableStore.list_disabled_names`(Task 2)
- Produces: `build_external_agent_catalog_router() -> APIRouter`。Task 4 的自审要能发现这个 router 挂出来的路由;Task 8 写它的文档。

**契约**

```
GET /v1/agent-catalog?limit=50&offset=0
```

```json
{
  "success": true,
  "data": {
    "agents": [
      {"agent_code": "report-writer", "display_name": "报表助手",
       "description": "根据数据生成周报", "available": true}
    ],
    "limit": 50, "offset": 0
  },
  "error": null
}
```

**`available` 必须与 run 端点同判据**。`api/agents.py:_resolve_session:428-436` 的两道闸是:

```python
if await disable_service.is_disabled(tenant_id, agent_code):   # → 403 AGENT_DISABLED
active = await repo.list_by_tenant(status=AgentSpecStatus.ACTIVE, name=agent_code, limit=1)
if not active:                                                  # → 404 AGENT_NOT_FOUND
```

目录端点用同一对判据。列出来一个「点了就 403」的 agent,是客户端最难排查的那类不一致。

- [ ] **Step 1: 写失败的测试**

新建 `services/control-plane/tests/test_external_agent_catalog.py`。fixture 照抄 `tests/test_external_sessions.py:84-146`(同一套 `_build_settings` / `_Ctx` / `ctx` 形状),把 `_SPEC` 换成下面这份带 `display_name` 的:

```python
"""``GET /v1/agent-catalog`` —— 阶段 3 (3.1) 的对外 agent 目录。

对外平面新前缀:``/v1/agents`` 已被控制台面占死(``85abdb39`` 给它下面
9 条路由补了 ``console_only()``),它吐完整 manifest(系统提示词 / 工具
清单 / 模型配置),不能给第三方。这里只给四个字段。
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
from expert_work.protocol import AgentSpec, AgentSpecStatus
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


def _spec_dict(name: str, *, display_name: str = "", description: str = "") -> dict[str, Any]:
    return {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "1.0.0", "tenant": "acme"},
        "spec": {
            "display_name": display_name,
            "description": description,
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
            "system_prompt": {"template": "you are support"},
            # ``sandbox`` 在 ``AgentSpecBody`` 上**没有默认值**(必填)——
            # 漏了它 ``model_validate`` 会因为 ``spec.sandbox`` 缺失而炸,
            # 报的错跟 display_name 毫无关系,很浪费排查时间。这一块照抄
            # ``tests/test_external_sessions.py`` 的 ``_SPEC``。
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }


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

    async def seed_agent(
        self, name: str, *, display_name: str = "", description: str = ""
    ) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id,
            spec=AgentSpec.model_validate(
                deepcopy(_spec_dict(name, display_name=display_name, description=description))
            ),
            spec_sha256="a" * 64,
            created_by="seed",
        )

    async def disable_agent(self, name: str) -> None:
        await self.app.state.agent_disable_repo.set_disabled(
            tenant_id=self.tenant_id,
            agent_name=name,
            disabled=True,
            reason="kill switch",
            disabled_by="admin",
        )


def _ctx_factory(scopes: tuple[str, ...]) -> Any:
    async def _make() -> AsyncIterator[_Ctx]:
        lifecycle = Lifecycle()
        lifecycle.mark_ready()
        app = create_app(
            settings=_build_settings(),
            lifecycle=lifecycle,
            jwt_verifier=build_test_jwt_verifier(),
            audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
            agent_runtime=stub_agent_runtime(),
        )
        tenant_id = uuid4()
        jwt = make_test_jwt(
            tenant_id=tenant_id,
            subject="sa-catalog",
            sub_type="service_account",
            roles=(),
            scopes=scopes,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
            yield _Ctx(client, app, tenant_id, {"Authorization": f"Bearer {jwt}"})

    return _make


ctx = pytest.fixture(_ctx_factory(("read",)))
ctx_no_scope = pytest.fixture(_ctx_factory(()))


@pytest.mark.asyncio
async def test_catalog_returns_the_four_fields(ctx: _Ctx) -> None:
    await ctx.seed_agent("report-writer", display_name="报表助手", description="生成周报")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    agents = body["data"]["agents"]
    assert agents == [
        {
            "agent_code": "report-writer",
            "display_name": "报表助手",
            "description": "生成周报",
            "available": True,
        }
    ]


@pytest.mark.asyncio
async def test_display_name_falls_back_to_agent_code_when_blank(ctx: _Ctx) -> None:
    """对外响应里 ``display_name`` 永远非空 —— 客户端不用做空判断。"""
    await ctx.seed_agent("no-display-name")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    assert resp.json()["data"]["agents"][0]["display_name"] == "no-display-name"


@pytest.mark.asyncio
async def test_disabled_agent_is_listed_but_unavailable(ctx: _Ctx) -> None:
    """禁用的 agent 出现在列表里、``available: false`` —— 客户端界面上
    置灰比「凭空消失」好排查。"""
    await ctx.seed_agent("killed")
    await ctx.disable_agent("killed")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    agents = {a["agent_code"]: a for a in resp.json()["data"]["agents"]}
    assert agents["killed"]["available"] is False


@pytest.mark.asyncio
async def test_available_matches_what_the_run_endpoint_actually_does(ctx: _Ctx) -> None:
    """**这条是本 task 的核心断言。**

    目录说 ``available: false``,那么直接发 run 就必须真的被拒;目录说
    ``true``,run 就必须被接受。两处判据各自漂移,会让客户端列出一个
    「点了就 403」的 agent —— 最难排查的那类不一致。

    所以这里不是分别测两个端点,而是在**同一个测试**里把两边对起来。
    """
    await ctx.seed_agent("healthy")
    await ctx.seed_agent("killed")
    await ctx.disable_agent("killed")

    catalog = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)
    by_code = {a["agent_code"]: a["available"] for a in catalog.json()["data"]["agents"]}

    for code, available in by_code.items():
        run = await ctx.client.post(
            f"/v1/agents/{code}/runs",
            json={"user_id": "cust-1", "input": "hi", "mode": "queue"},
            headers=ctx.headers,
        )
        accepted = run.status_code == 202
        assert accepted == available, (
            f"{code}: 目录说 available={available},但 run 端点回 {run.status_code} "
            f"({run.text[:200]})"
        )


@pytest.mark.asyncio
async def test_agent_with_no_active_version_is_absent_from_the_catalog(ctx: _Ctx) -> None:
    """目录 = 「能调什么」。一个 code 只剩 DEPRECATED 版本时没有任何可调版本
    (run 端点会 404 AGENT_NOT_FOUND),所以它**不出现在目录里** —— 而不是
    以 ``available: false`` 出现。

    与 kill-switch 禁用**刻意不同**:那是可逆的临时状态,置灰等它回来是对的;
    只剩 deprecated 是终态,列一个永远 false 的条目对客户端是噪音,而且
    deprecated 属于租户内部的版本管理状态,不该对第三方暴露。
    """
    await ctx.seed_agent("healthy")
    await ctx.seed_agent("deprecated-only")
    await ctx.app.state.agent_spec_repo.update_status(
        tenant_id=ctx.tenant_id,
        name="deprecated-only",
        version="1.0.0",
        status=AgentSpecStatus.DEPRECATED,
    )

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    codes = {a["agent_code"] for a in resp.json()["data"]["agents"]}
    # 断言 ``== {"healthy"}`` 而不是 ``"deprecated-only" not in codes`` ——
    # 后者在「端点坏了返回空列表」时也会绿,是个假绿陷阱。
    assert codes == {"healthy"}, f"deprecated-only 不该出现在目录里,实际: {codes}"


@pytest.mark.asyncio
async def test_pagination(ctx: _Ctx) -> None:
    for i in range(3):
        await ctx.seed_agent(f"agent-{i}")

    page = await ctx.client.get(
        "/v1/agent-catalog", params={"limit": 2, "offset": 0}, headers=ctx.headers
    )
    assert len(page.json()["data"]["agents"]) == 2
    assert page.json()["data"]["limit"] == 2
    assert page.json()["data"]["offset"] == 0

    rest = await ctx.client.get(
        "/v1/agent-catalog", params={"limit": 2, "offset": 2}, headers=ctx.headers
    )
    assert len(rest.json()["data"]["agents"]) == 1


@pytest.mark.asyncio
async def test_zero_scope_key_is_denied(ctx_no_scope: _Ctx) -> None:
    """零 scope 的 key 读不到目录 —— #1153 那轮堵的就是零权限 key 的读写绕行。"""
    resp = await ctx_no_scope.client.get("/v1/agent-catalog", headers=ctx_no_scope.headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_employee_jwt_is_denied(ctx: _Ctx) -> None:
    """对外平面必须拒绝人类 —— ``external_only()`` 的既有铁律。"""
    employee = make_test_jwt(tenant_id=ctx.tenant_id, subject=str(uuid4()), roles=("admin",))
    resp = await ctx.client.get(
        "/v1/agent-catalog", headers={"Authorization": f"Bearer {employee}"}
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_catalog_never_leaks_the_manifest(ctx: _Ctx) -> None:
    """响应里只能有那四个字段 —— 系统提示词 / 工具清单 / 模型配置属于
    控制台面,``85abdb39`` 刻意对第三方关死的。逐字段白名单断言,
    这样将来往 DTO 里多塞一个字段会立刻失败。"""
    await ctx.seed_agent("report-writer", display_name="x", description="y")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    for agent in resp.json()["data"]["agents"]:
        assert set(agent.keys()) == {"agent_code", "display_name", "description", "available"}
    assert "claude-sonnet" not in resp.text
    assert "you are support" not in resp.text
```

已核对的签名(直接用,不用再查):`AgentSpecStore.update_status(*, tenant_id: UUID, name: str, version: str, status: AgentSpecStatus) -> AgentSpecRecord | None`(`agent_spec/base.py:146`)。注意方法名是 `update_status` 不是 `set_status`。`app.state` 上的属性名是 `agent_spec_repo` / `agent_disable_repo`(`app.py:2257` / `:2337`)。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest services/control-plane/tests/test_external_agent_catalog.py -v
```

预期:全部 404(路由还不存在)。

- [ ] **Step 3: 写端点**

新建 `services/control-plane/src/control_plane/api/external_agent_catalog.py`:

```python
"""对外 agent 目录 —— ``GET /v1/agent-catalog``。

阶段 3 (3.1)。第三方对接时得先知道「这个租户有哪些 agent 能调」,否则只能
人工问一遍 agent_code 写死在客户端里,agent 上下线客户端也不知道。

**为什么是新前缀而不是 ``/v1/agents``**:那个前缀已经被控制台面占死 ——
``85abdb39`` 给它下面 9 条路由补了 ``console_only()``,因为一把 write scope
的 key 曾能穿过 RBAC 摸到整个 manifest 面(建 / 改 / 删 / 禁用 agent)。
控制台的 ``GET /v1/agents`` 吐完整 manifest(系统提示词、工具清单、模型
配置),那是第三方永远不该看到的东西。这里只给四个字段。

新前缀的代价:三个 external 自审(``test_external_only_gate`` /
``test_external_path_param_nul_guard`` / ``test_external_route_reachability``)
原本靠 ``path.startswith("/v1/agents/")`` 发现路由,会漏掉这里。阶段 3 PR-A
Task 4 把那三个发现器改成纯 tag 驱动,所以本模块的 ``tags=["external"]``
是**必需**的,不是装饰。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import external_only, require
from control_plane.api._external import reject_nul_path_params
from expert_work.persistence.agent_disable.base import AgentDisableStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import AgentSpecStatus


def _get_agent_spec_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_agent_disable_repo(request: Request) -> AgentDisableStore:
    return request.app.state.agent_disable_repo  # type: ignore[no-any-return]


def build_external_agent_catalog_router() -> APIRouter:
    """挂载对外 agent 目录端点。"""
    router = APIRouter(
        prefix="/v1/agent-catalog",
        tags=["external"],
        # 这个前缀下没有路径参数,但守卫仍然挂上:它是 router 级的,
        # 挂在构造函数里意味着以后往这个 router 加带路径参数的路由时
        # 自动被覆盖 —— 而不是等着某个人记得补(``_external.py`` 里
        # ``reject_nul_path_params`` 的 docstring 讲的就是这条)。
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "",
        response_model=None,
        # ``manifest:read``——``read`` scope 的 key 映射成 VIEWER 角色,
        # VIEWER 的矩阵里 manifest 是 {read},通过;零 scope 的 key 一个
        # 角色都没有,挡住(#1153 堵的就是零权限 key 的绕行)。
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_agent_catalog(
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_agent_spec_repo)],
        disable_repo: Annotated[AgentDisableStore, Depends(_get_agent_disable_repo)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """这个租户有哪些 agent 可以调。

        ``available`` 与 ``agents.py:_resolve_session`` 用**同一对判据**:
        没被 kill switch 禁用,且存在 ``status=ACTIVE`` 的版本。两处判据
        各自漂移会让目录列出一个「点了就 403」的 agent,是客户端最难排查
        的那类不一致 —— 所以测试里把两边对在同一个断言下。

        禁用的 agent **仍然列出**,只是 ``available: false``:客户端界面上
        置灰比「凭空消失」好排查。
        """
        tenant_id: UUID = request.state.tenant_id

        # ACTIVE 版本决定「这个 code 能不能调」。同一个 name 可能有多个
        # 版本行,按 name 去重 —— 第三方不选版本,平台自动用 ACTIVE 的那个。
        active = await repo.list_by_tenant(
            tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, limit=limit, offset=offset
        )
        # 一次拿全租户的禁用集,而不是每个 agent 查一次(N+1)。
        disabled = await disable_repo.list_disabled_names(tenant_id=tenant_id)

        seen: set[str] = set()
        agents: list[dict[str, object]] = []
        for record in active:
            if record.name in seen:
                continue
            seen.add(record.name)
            body = record.spec.spec
            agents.append(
                {
                    "agent_code": record.name,
                    # 空显示名回落到 code —— 对外响应里这个字段永远非空。
                    "display_name": body.display_name or record.name,
                    "description": body.description,
                    "available": record.name not in disabled,
                }
            )
        return JSONResponse(
            {
                "success": True,
                "data": {"agents": agents, "limit": limit, "offset": offset},
                "error": None,
            }
        )

    return router
```

**注意 `AgentSpecStore` 的 import 路径**:先 `rg -n "^from|^import" services/control-plane/src/control_plane/api/agents.py | head -30` 确认它实际怎么 import 的,照抄。

- [ ] **Step 4: 挂载 router**

`services/control-plane/src/control_plane/app.py`:在 import 块里加 `build_external_agent_catalog_router`(照第 59-61 行既有 external router 的 import 形状),并在 `app.include_router(build_external_workspace_router())`(约 2471 行)之后加一行:

```python
    app.include_router(build_external_agent_catalog_router())
```

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest services/control-plane/tests/test_external_agent_catalog.py -v
```

预期:全部 PASS。

`test_agent_with_no_active_version_is_unavailable` 如果因为 store 签名不对而失败,是测试写错了不是实现错了 —— 修测试。

- [ ] **Step 6: 变异自证(三条,逐条来)**

1. 把 `record.name not in disabled` 改成 `True` → `test_disabled_agent_is_listed_but_unavailable` 和 `test_available_matches_what_the_run_endpoint_actually_does` 必须变红
2. 把 `body.display_name or record.name` 改成 `body.display_name` → `test_display_name_falls_back_to_agent_code_when_blank` 必须变红
3. 往 DTO 里临时加一个 `"model": body.model.name` → `test_catalog_never_leaks_the_manifest` 必须变红

每条改完 `git diff` 确认落地,跑完还原(用 scratchpad 副本,不用 `git checkout`)。

- [ ] **Step 7: 跑平面分区自审**

```bash
uv run pytest services/control-plane/tests/test_route_plane_partition.py -v
```

预期:全绿。这个测试是**派生式**的(读路由真实的依赖图),新路由挂了 `external_only()` 就自动归 external 平面,不需要改它。如果它红了,说明闸没挂上。

- [ ] **Step 8: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_agent_catalog.py \
        services/control-plane/src/control_plane/app.py \
        services/control-plane/tests/test_external_agent_catalog.py
git commit -m "feat(control-plane): GET /v1/agent-catalog——对外 agent 目录(code/显示名/描述/可用)"
```

---

## Task 4:三个 external 自审的发现器改成 tag 驱动

**Files:**
- Modify: `services/control-plane/tests/test_external_only_gate.py:366-372`
- Modify: `services/control-plane/tests/test_external_path_param_nul_guard.py:420-426`
- Modify: `services/control-plane/tests/test_external_route_reachability.py:94-101`

**Interfaces:**
- Consumes: `/v1/agent-catalog` 路由(Task 3)
- Produces: 无(纯测试基建)

**为什么必须做**:三个自审各有一个发现器,形状都是:

```python
route.path.startswith("/v1/agents/") and "external" in (route.tags or [])
```

`/v1/agent-catalog` **不以 `/v1/agents/` 开头**,所以它会从这三个自审里全部漏掉 —— 而它们的注释还写着「a new route mounted on any of the six `external_*.py` routers is picked up automatically」,这句话从 Task 3 落地那一刻起就不再成立。

**路径前缀这个条件本来就是多余的**:全仓只有 `external_*.py` 用 `tags=["external"]`,tag 已经足够精确。去掉前缀条件是纯收紧(覆盖变宽),而且让任何未来的新前缀自动被覆盖 —— 不需要有人记得维护一张前缀表。

> 三处一起改。这正是本仓反复出问题的形状:「修复只落在发现问题的那个位置,结构相同的兄弟位置不会被自动带上」。

**不要动**这两处 —— 它们是另一回事(专门盯 `agents.py` 自己那个 router 的非-external 路由,前缀条件是对的):
- `test_external_only_gate.py:518`
- `test_external_path_param_nul_guard.py:514`

- [ ] **Step 1: 先写「证明新路由被覆盖」的断言**

三个文件的发现器函数名与 app 构建方式**已核对**,各不相同,分别是:

| 文件 | 发现器 | 拿路由的方式 |
|---|---|---|
| `test_external_only_gate.py` | `_external_agents_routes(app)`(`:362`) | app 由 `create_app(...)` 在 fixture 里建(`:167`) |
| `test_external_path_param_nul_guard.py` | `_external_agents_routes(app)`(`:416`) | 同上(`:141`) |
| `test_external_route_reachability.py` | `_external_routes(routes)`(`:94`) | `_build_routes()`(`:47`)返回 `list[BaseRoute]` |

前两个文件各追加(app 从该文件既有 fixture 拿,照同文件其它测试的写法):

```python
def test_the_discovery_is_not_tied_to_the_agents_path_prefix(ctx: _Ctx) -> None:
    """发现器必须靠 ``tags=["external"]`` 而不是路径前缀。

    ``GET /v1/agent-catalog``(阶段 3)是第一条不在 ``/v1/agents/`` 下的
    对外路由。发现器要是还带着前缀条件,它就从这个自审里整条漏掉 —— 而
    自审本身照样全绿。这是最坏的失败模式:看起来在保护,实际不覆盖。
    """
    paths = {r.path for r in _external_agents_routes(ctx.app)}
    assert "/v1/agent-catalog" in paths, (
        "对外路由 /v1/agent-catalog 没被发现器捡到 —— 发现器还在按路径前缀过滤"
    )
```

`test_external_route_reachability.py` 追加(它没有 fixture,用模块级的 `_build_routes()`):

```python
def test_the_discovery_is_not_tied_to_the_agents_path_prefix() -> None:
    """见 ``test_external_only_gate.py`` 同名测试的注释。"""
    paths = {r.path for r in _external_routes(_build_routes())}
    assert "/v1/agent-catalog" in paths, (
        "对外路由 /v1/agent-catalog 没被发现器捡到 —— 发现器还在按路径前缀过滤"
    )
```

前两个文件的 fixture 参数名(上面写的 `ctx: _Ctx`)以各自文件实际为准 —— 照该文件里其它同类测试的签名抄。

- [ ] **Step 2: 跑三个文件确认新断言失败**

```bash
uv run pytest services/control-plane/tests/test_external_only_gate.py \
              services/control-plane/tests/test_external_path_param_nul_guard.py \
              services/control-plane/tests/test_external_route_reachability.py \
              -k not_tied_to_the_agents_path_prefix -v
```

预期:3 FAIL,消息是「没被发现器捡到」。

- [ ] **Step 3: 改三个发现器**

三处都把前缀条件删掉,并把注释改成实话。`test_external_only_gate.py:366-372` 与 `test_external_path_param_nul_guard.py:420-426` 改成:

```python
    """对外路由从活的 app 里发现,不是手维护的清单。

    判据是 ``tags=["external"]`` **单独一条** —— 不带路径前缀。全仓只有
    ``external_*.py`` 打这个 tag,所以 tag 本身已经足够精确;而路径前缀
    条件会让任何新前缀整条漏掉这个自审(阶段 3 的 ``/v1/agent-catalog``
    就是第一个不在 ``/v1/agents/`` 下的对外路由),自审却照样全绿。
    """
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and "external" in (route.tags or [])
    ]
```

`test_external_route_reachability.py:94-101` 同理(注意它用的是 starlette 的 `Route` 而不是 `APIRoute`,`tags` 要用 `getattr(route, "tags", None)`):

```python
def _external_routes(routes: list[BaseRoute]) -> list[Route]:
    """判据是 ``tags=["external"]`` 单独一条 —— 见 ``test_external_only_gate.py``
    同名函数的注释:路径前缀条件会让新前缀整条漏掉自审。"""
    return [
        route
        for route in routes
        if isinstance(route, Route) and "external" in (getattr(route, "tags", None) or [])
    ]
```

- [ ] **Step 4: 跑三个文件的全量测试**

```bash
uv run pytest services/control-plane/tests/test_external_only_gate.py \
              services/control-plane/tests/test_external_path_param_nul_guard.py \
              services/control-plane/tests/test_external_route_reachability.py -v
```

预期:全绿,**且新断言通过**。

如果某条既有断言现在红了 —— 那是发现器变宽后逮到的**真问题**(某条对外路由确实缺守卫),不是回归。修那条路由,不要把发现器改回去。

- [ ] **Step 5: 变异自证**

把 `/v1/agent-catalog` router 的 `tags=["external"]` 临时改成 `tags=["catalog"]`,重跑 Step 4,确认三条新断言全红。改回来。

这一步同时证明了另一件事:**Task 3 里那个 tag 是必需的,不是装饰**。

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/tests/test_external_only_gate.py \
        services/control-plane/tests/test_external_path_param_nul_guard.py \
        services/control-plane/tests/test_external_route_reachability.py
git commit -m "test(control-plane): 三个 external 自审的发现器改纯 tag 驱动——新前缀不再整条漏掉"
```

---

## Task 5:`RunStore.list_for_tenant` 加 `agent_name` 过滤

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:266-285`(抽象签名)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:653-679`(in-memory)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:1141-1178`(SQL)
- Test: `packages/expert-work-runtime/tests/test_run_store.py`(in-memory)
- Test: 该包里跑真容器的 run store 集成测试文件(先 `rg -l "SqlRunStore" packages/expert-work-runtime/tests/` 确认文件名)

**Interfaces:**
- Consumes: 无
- Produces: `list_for_tenant(..., agent_name: str | None = None, ...)`。Task 6 用它按 agent 过滤 run。

**为什么不在 API 层做**:API 层先查 `(user, agent)` 的全部 thread_ids 再传 `thread_ids=`,**分页会失准** —— thread 数量无上限,先取一页 thread 再过滤 run,`offset` 的语义就错了。`list_running_for_agent`(同文件 `:1180-1203`)已经有现成的 join 可以照抄。

**in-memory 后端的注意点**:agent 绑定在 `thread_meta` 上,不在 run 行上。in-memory 的 `list_running_for_agent` 靠 `self._thread_meta_store` 逐个查;`_thread_meta_store is None` 时返回 `[]`。新参数必须同款处理,并写进抽象方法的 docstring。

- [ ] **Step 1: 写 in-memory 的失败测试**

**新建** `packages/expert-work-runtime/tests/test_run_store_list_for_tenant_agent.py`。

不要往 `test_run_store.py` 里塞 —— 隔壁的 `test_run_store_list_running_for_agent.py` 就是「agent_name join」这件事的既有测试文件,本 task 的测试是它的兄弟,**两个 helper 直接照抄它的**(已核对,可原样用):

```python
"""``RunStore.list_for_tenant(agent_name=...)`` —— 阶段 3 (3.2)。

run 行上没有 agent,绑定在 ``thread_meta`` 上,所以这是个 join ——
和同目录 ``test_run_store_list_running_for_agent.py`` 测的是同一层机制,
helper 也照抄那份。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus

_BASE = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _info(
    *,
    run_id: UUID,
    tenant_id: UUID,
    thread_id: UUID,
    user_id: UUID | None = None,
    status: RunStatus = RunStatus.SUCCESS,
) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=user_id,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=_BASE,
        updated_at=_BASE,
        finished_at=None,
    )


async def _seed_thread(
    threads: InMemoryThreadMetaStore,
    *,
    thread_id: UUID,
    tenant_id: UUID,
    agent_name: str,
) -> None:
    # ``created_by`` 是必填的(thread_meta/base.py:48)。
    await threads.create(
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="seed",
        agent_name=agent_name,
    )


@pytest.mark.asyncio
async def test_filters_by_agent_name() -> None:
    """这个过滤必须穿过 thread_meta 那一层,不然对外的 run 列表只能靠
    API 层先查 thread 再过滤 —— 分页会失准。"""
    threads = InMemoryThreadMetaStore()
    store = InMemoryRunStore(thread_meta_store=threads)
    tenant_id = uuid4()
    t_alpha, t_beta = uuid4(), uuid4()
    await _seed_thread(threads, thread_id=t_alpha, tenant_id=tenant_id, agent_name="alpha")
    await _seed_thread(threads, thread_id=t_beta, tenant_id=tenant_id, agent_name="beta")

    run_alpha = uuid4()
    await store.create(_info(run_id=run_alpha, tenant_id=tenant_id, thread_id=t_alpha))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=t_beta))

    rows = await store.list_for_tenant(tenant_id=tenant_id, agent_name="alpha")

    assert [r.run_id for r in rows] == [run_alpha]


@pytest.mark.asyncio
async def test_agent_name_none_keeps_every_agent() -> None:
    """不传 ``agent_name`` 的既有调用方行为必须一个字不变。"""
    threads = InMemoryThreadMetaStore()
    store = InMemoryRunStore(thread_meta_store=threads)
    tenant_id = uuid4()
    t_alpha, t_beta = uuid4(), uuid4()
    await _seed_thread(threads, thread_id=t_alpha, tenant_id=tenant_id, agent_name="alpha")
    await _seed_thread(threads, thread_id=t_beta, tenant_id=tenant_id, agent_name="beta")
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=t_alpha))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=t_beta))

    assert len(await store.list_for_tenant(tenant_id=tenant_id)) == 2


@pytest.mark.asyncio
async def test_agent_name_without_thread_store_returns_empty() -> None:
    """没接 ``thread_meta_store`` 时,agent 过滤无从判断 —— 返回空,
    而不是**静默忽略过滤条件**把全部 run 都吐出来。后者会让一个配错的
    实例把别的 agent 的 run 漏给第三方。同 ``list_running_for_agent``
    的既有处理(它在同样情况下返回 ``[]``)。"""
    store = InMemoryRunStore()  # 没有 thread_meta_store
    tenant_id, thread_id = uuid4(), uuid4()
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_id))

    assert await store.list_for_tenant(tenant_id=tenant_id, agent_name="alpha") == []
    # 不带过滤时照常返回 —— 证明上面那条空结果来自过滤,不是 store 坏了
    assert len(await store.list_for_tenant(tenant_id=tenant_id)) == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest packages/expert-work-runtime/tests/test_run_store_list_for_tenant_agent.py -v
```

预期:FAIL,`TypeError: list_for_tenant() got an unexpected keyword argument 'agent_name'`。

- [ ] **Step 3: 改抽象签名 + docstring**

`store.py:266-285`:

```python
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunInfo]:
        """Return runs for ``tenant_id``, newest first; paginated.

        Stream H.3 PR 1 — feeds the cross-thread ``GET /v1/runs`` index.
        ``limit`` is clamped to ``MAX_LIST_LIMIT`` (Mini-ADR H-7 D).
        ``thread_ids`` narrows to runs of those threads (Stream H.6
        Mini-ADR H-10 — the API layer resolves an agent to its thread
        window via ``ThreadMetaStore`` and passes the ids here; an empty
        collection returns no rows).

        ``agent_name`` (阶段 3, 3.2) narrows to runs whose thread is bound
        to that agent. Runs carry no agent — the binding lives on
        ``thread_meta`` — so this is a join, exactly like
        :meth:`list_running_for_agent`. Doing it in the API layer instead
        (fetch the thread ids first, pass ``thread_ids=``) breaks
        pagination: a tenant's thread count is unbounded, so taking one
        page of threads and then filtering runs makes ``offset`` mean
        something else entirely.

        An in-memory store with no ``thread_meta_store`` wired returns
        ``[]`` for any ``agent_name`` query rather than silently ignoring
        the filter — same rule as :meth:`list_running_for_agent`. Silently
        dropping the predicate would leak other agents' runs.
        """
```

- [ ] **Step 4: in-memory 实现**

`store.py:653` 的 `list_for_tenant`,在 `thread_ids` 过滤之后、`q` 过滤之前插入:

```python
        if agent_name is not None:
            if self._thread_meta_store is None:
                return []
            kept: list[RunInfo] = []
            for r in rows:
                meta = await self._thread_meta_store.get(r.thread_id, tenant_id=tenant_id)
                if meta is not None and meta.agent_name == agent_name:
                    kept.append(r)
            rows = kept
```

并在方法签名里加 `agent_name: str | None = None`(位置与抽象签名一致:`user_id` 之后、`q` 之前)。

- [ ] **Step 5: SQL 实现**

`store.py:1141` 的 `list_for_tenant`:签名同样加 `agent_name: str | None = None`,并在 `thread_ids` 那个 `if` 之后加:

```python
        if agent_name is not None:
            # agent 绑定在 thread_meta 上,不在 agent_run 上 —— 与
            # ``list_running_for_agent`` 同一个 join。注意这个 join 必须
            # 只在需要时加:无条件 join 会把没有 thread_meta 行的 run
            # (理论上不该有,但历史数据里可能存在)从默认列表里静默剔掉。
            stmt = stmt.join(
                ThreadMetaRow, ThreadMetaRow.thread_id == AgentRunRow.thread_id
            ).where(ThreadMetaRow.agent_name == agent_name)
```

- [ ] **Step 6: 跑 in-memory 测试确认通过**

```bash
uv run pytest packages/expert-work-runtime/tests/test_run_store_list_for_tenant_agent.py -v
```

预期:3 passed。

- [ ] **Step 7: 写 SQL ↔ in-memory 等价性测试**

加进 `packages/expert-work-runtime/tests/test_sql_run_store.py`(真容器文件,`pytestmark = pytest.mark.integration`)。

**已核对的现状**:run store 的 fixture 叫 `run_store`(`:49`);该文件已有 `_info` helper(`:74`,签名 `_info(*, run_id, tenant_id, thread_id=None, user_id=None, status=RunStatus.PENDING, created_at=None)`)——直接用,不要另写。

**但它没有 thread store fixture**,而 agent 过滤要 join `thread_meta`。照同文件 `run_event_store`(`:61`,「同一个库上的第二个 store」)的形状加一个:

```python
@pytest.fixture
def thread_meta_store(postgres_container: PostgresContainer) -> Iterator[SqlThreadMetaStore]:
    """``SqlThreadMetaStore`` on the same database as ``run_store`` —— agent
    过滤 join 的是 ``thread_meta``,所以两个 store 必须落在同一个库上。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    yield SqlThreadMetaStore(create_async_session_factory(engine))
```

文件顶部补 import(`SqlThreadMetaStore` 与 `InMemoryThreadMetaStore` 都从 `expert_work.persistence.thread_meta` 导出,已核对):

```python
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore, SqlThreadMetaStore
from expert_work.runtime.runs import InMemoryRunStore
```

等价性测试本体:

```python
@pytest.mark.asyncio
async def test_agent_name_filter_matches_the_in_memory_store(
    run_store: SqlRunStore, thread_meta_store: SqlThreadMetaStore
) -> None:
    """两个后端各写一遍谓词,是本仓反复出问题的地方(SQL 的 join 语义与
    内存的逐行比较不是自动等价的)。同一组输入喂两边,断言结果集相同。

    覆盖四种输入:两个命中的、一个不命中的、``None``(不过滤)。只测命中
    的话,「SQL 把过滤条件写反了」和「写对了」会给出同样的绿。
    """
    mem_threads = InMemoryThreadMetaStore()
    mem_runs = InMemoryRunStore(thread_meta_store=mem_threads)
    tenant_id = uuid4()
    layout = [("alpha", uuid4()), ("beta", uuid4()), ("alpha", uuid4())]

    for agent_name, thread_id in layout:
        for threads in (thread_meta_store, mem_threads):
            await threads.create(
                thread_id=thread_id,
                tenant_id=tenant_id,
                created_by="seed",
                agent_name=agent_name,
            )
        run_id = uuid4()
        for runs in (run_store, mem_runs):
            await runs.create(_info(run_id=run_id, tenant_id=tenant_id, thread_id=thread_id))

    for probe in ("alpha", "beta", "nonexistent", None):
        sql_rows = await run_store.list_for_tenant(tenant_id=tenant_id, agent_name=probe)
        mem_rows = await mem_runs.list_for_tenant(tenant_id=tenant_id, agent_name=probe)
        assert {r.run_id for r in sql_rows} == {r.run_id for r in mem_rows}, (
            f"agent_name={probe!r} 两个后端结果不一致"
        )
```

**两个 store 必须用同一批 `run_id`**(上面的 `run_id = uuid4()` 提到循环外正是为此)—— 各自生成 id 的话结果集永远不相等,测试会以一种看起来像真失败的方式红,浪费一轮排查。

- [ ] **Step 8: 跑 SQL 集成测试(真容器)**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-runtime/tests/test_sql_run_store.py -k agent_name -v
```

- [ ] **Step 9: 变异自证**

把 SQL 的 `ThreadMetaRow.agent_name == agent_name` 改成 `!= agent_name`,重跑 Step 8,确认等价性测试变红。改回来(用副本)。

- [ ] **Step 10: 跑既有调用方的回归**

```bash
uv run pytest packages/expert-work-runtime/tests/ -q
uv run pytest services/control-plane/tests/ -q -k "runs or conversation"
```

新参数有默认值 `None`,既有调用方一行都不用改 —— 这一步是证明这句话,不是走形式。

- [ ] **Step 11: 提交**

```bash
git add packages/expert-work-runtime/src/expert_work/runtime/runs/store.py \
        packages/expert-work-runtime/tests/
git commit -m "feat(runtime): RunStore.list_for_tenant 加 agent_name 过滤——join thread_meta,分页才准"
```

---

## Task 6:`GET /v1/agents/{agent_code}/runs` 端点

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_runs.py`
- Test: `services/control-plane/tests/test_external_runs_list.py`(新建)

**Interfaces:**
- Consumes: `RunStore.list_for_tenant(agent_name=...)`(Task 5)
- Produces: `GET /v1/agents/{agent_code}/runs`。Task 8 写它的文档。

**契约**

```
GET /v1/agents/{agent_code}/runs?user_id=u_123&session_id=<uuid>&status=success&limit=50&offset=0
```

```json
{
  "success": true,
  "data": {
    "runs": [
      {"run_id": "...", "session_id": "...", "status": "success",
       "created_at": "...", "finished_at": "...", "error": null}
    ],
    "limit": 50, "offset": 0
  },
  "error": null
}
```

**`error` 字段为什么可以给**:`agent_run.error` 存的是 `str(exc)`(`orchestrator/sse.py:745` 与 `:779`),而同一次 run 的 SSE `error` 帧发的是 `{"message": str(exc), "name": type(exc).__name__}`(同两处)——**同一个字符串**。第三方在实时流里已经收到过它,列表里再给一次是零增量。owner 校验也一致。

- [ ] **Step 1: 写失败的测试**

新建 `services/control-plane/tests/test_external_runs_list.py`,fixture 照抄 `tests/test_external_sessions.py:84-146`(它已经带 `run_store`,正是这里需要的):

```python
"""``GET /v1/agents/{agent_code}/runs`` —— 阶段 3 (3.2) 的对外 run 列表。

现在第三方只能按 ``run_id`` 拿事件,列不出「这个用户在这个 agent 上跑过
哪些任务」,客户端要做「我的任务」列表只能自己在本地记 run_id。
"""
```

测试用例(全部用该文件的 `ctx` fixture):

```python
@pytest.mark.asyncio
async def test_missing_user_id_is_422(ctx: _Ctx) -> None:
    """``user_id`` 必填、无默认 —— 漏传必须是 422,绝不能降级成
    「列出整个租户的 run」。与 ``GET .../sessions`` 同一条铁律。"""
    await ctx.seed_agent()
    resp = await ctx.client.get("/v1/agents/support-bot/runs", headers=ctx.headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_lists_only_this_users_runs_on_this_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    mine = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert mine.status_code == 202, mine.text
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-99", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    assert resp.status_code == 200, resp.text
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == mine.json()["data"]["run_id"]
    assert runs[0]["session_id"] == mine.json()["data"]["thread_id"]


@pytest.mark.asyncio
async def test_unknown_user_returns_empty_and_mints_no_row(ctx: _Ctx) -> None:
    """读路径 ``mint=False``:一个这个租户从没见过的 ``user_id`` 返回空列表,
    **且不留下一行 ``tenant_user``**。第三方喷任意 user_id 不该在用户维度
    运维页上攒出一堆幽灵行(P1 复审 T3)。"""
    await ctx.seed_agent()
    before = await ctx.app.state.tenant_user_repo.list_by_tenant(ctx.tenant_id, limit=500)

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "never-seen"}, headers=ctx.headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["runs"] == []
    after = await ctx.app.state.tenant_user_repo.list_by_tenant(ctx.tenant_id, limit=500)
    assert len(after) == len(before), "读路径不该 mint tenant_user 行"


@pytest.mark.asyncio
async def test_runs_of_another_agent_are_not_listed(ctx: _Ctx) -> None:
    """同一个用户在**别的 agent** 上的 run 不能出现在这里 —— 这是
    ``agent_name`` 过滤(Task 5 那个 join)真的生效的证明。"""
    await ctx.seed_agent()
    await ctx.seed_agent_named("other-bot")
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    await ctx.client.post(
        "/v1/agents/other-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    assert len(resp.json()["data"]["runs"]) == 1


@pytest.mark.asyncio
async def test_session_id_filter_404s_on_someone_elses_session(ctx: _Ctx) -> None:
    """``session_id`` 指向别人的会话 → 404(不是 403、不是空列表)——
    响应不能携带存在性信息。"""
    await ctx.seed_agent()
    theirs = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-99", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    their_session = theirs.json()["data"]["thread_id"]

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "session_id": their_session},
        headers=ctx.headers,
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_status_filter(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    r = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = UUID(r.json()["data"]["run_id"])
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        error=None,
        finished_at=datetime.now(UTC),
    )

    hit = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "status": "success"},
        headers=ctx.headers,
    )
    miss = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "status": "error"},
        headers=ctx.headers,
    )

    assert len(hit.json()["data"]["runs"]) == 1
    assert miss.json()["data"]["runs"] == []


@pytest.mark.asyncio
async def test_error_is_returned_verbatim_without_enrichment(ctx: _Ctx) -> None:
    """列表里的 ``error`` 必须是 ``agent_run.error`` 的**原样**,不加工。

    **这条断言的范围要说清楚**:它证明的是「端点没在存的东西之外再添
    内容」,**不是**「列表 error 与 SSE error 帧同源」。后者是代码层面的
    论证 —— 两边都是 ``str(exc)``(``orchestrator/sse.py:745`` 与
    ``:779``),同两处赋值 —— 这个 stub 测试环境不跑真 ``sse.py``,
    抓不到真实的 error 帧,所以那一半在这里**无法被断言**,只能靠代码
    阅读和真栈验收。

    它仍然有价值:哪天有人给列表里的 error 拼上堆栈、内部路径或
    ``claimed_by`` 实例 id,这条就红。
    """
    await ctx.seed_agent()
    r = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = UUID(r.json()["data"]["run_id"])
    failure = "boom: upstream refused"
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=RunStatus.ERROR,
        updated_at=datetime.now(UTC),
        error=failure,
        finished_at=datetime.now(UTC),
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    assert resp.json()["data"]["runs"][0]["error"] == failure


@pytest.mark.asyncio
async def test_pagination(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    for _ in range(3):
        await ctx.client.post(
            "/v1/agents/support-bot/runs",
            json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
            headers=ctx.headers,
        )

    page = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "limit": 2, "offset": 0},
        headers=ctx.headers,
    )
    rest = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "limit": 2, "offset": 2},
        headers=ctx.headers,
    )

    assert len(page.json()["data"]["runs"]) == 2
    assert len(rest.json()["data"]["runs"]) == 1


@pytest.mark.asyncio
async def test_response_shape_is_a_whitelist(ctx: _Ctx) -> None:
    """逐字段白名单 —— 往 DTO 里多塞一个字段(比如内部的 ``claimed_by``
    实例 id、``enqueued_input`` 原始输入)会立刻失败。"""
    await ctx.seed_agent()
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    for run in resp.json()["data"]["runs"]:
        assert set(run.keys()) == {
            "run_id",
            "session_id",
            "status",
            "created_at",
            "finished_at",
            "error",
        }
```

`_Ctx` 需要一个 `seed_agent_named(name)`(照 `seed_agent` 写,把 `_SPEC` 的 `metadata.name` 换掉)。

已核对的签名(直接用):`app.state.tenant_user_repo`(`app.py:2260`),`TenantUserStore.list_by_tenant(tenant_id, *, subject_type=None, limit=100, offset=0)` —— 注意 `tenant_id` 是**位置参数**,其余是关键字参数(`tenant_user/base.py:84`)。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest services/control-plane/tests/test_external_runs_list.py -v
```

预期:全部 405 或 404(路由不存在)。

- [ ] **Step 3: 写端点**

在 `external_runs.py` 的 `build_external_runs_router()` 里,`cancel` 那条路由**之前**加。文件顶部补 import:

```python
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
    load_owned_session,
    lookup_external_user_id,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from expert_work.runtime.runs import RunStatus, RunStore
```

路由本体:

```python
    @router.get(
        "/{agent_code}/runs",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_runs(
        agent_code: str,
        request: Request,
        runs: Annotated[RunStore, Depends(_get_run_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        session_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[RunStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """列出这个终端用户在这个 agent 上跑过的 run。

        ``user_id`` 必填、无默认 —— 漏传是 422,不是「列出整个租户」。

        ``session_id`` 选填:给了就先验它属于 ``(user, agent)``(不属于就
        404,不是空列表 —— 响应不能携带存在性信息),再按那个 thread 过滤。

        ``mint=False``:一个这个租户从没见过的 ``user_id`` 返回空列表,
        不建 ``tenant_user`` 行(P1 复审 T3)。「没跑过 run」和「没这个人」
        对第三方是同一个事实,空列表泄露的信息不比 404 多。

        ``error`` 与 SSE ``error`` 帧携带的是同一个字符串(两者都是
        ``str(exc)``,``orchestrator/sse.py:745``/``:779``),所以这里给出
        它没有新开任何泄露面。
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        thread_ids: list[UUID] | None = None
        if session_id is not None:
            # 给了 ``session_id`` 时,归属校验是权威的,**即使 ``end_user_id``
            # 是 None**(这个 user_id 从没在本租户出现过)——
            # ``load_owned_session(mint=False)`` 自己会再解析一次并且两种情况
            # 都 404。把「陌生 user 返回空列表」的早返回放在这个分支**之前**
            # 是错的:那样一个陌生 ``user_id`` 拿别人的真实 ``session_id`` 来探,
            # 会拿到 200 空列表而不是 404 —— 200-empty 与 404 可区分,等于把
            # 「这个 user_id 在本租户存在过没有」变成可枚举的。
            #
            # 还有第二层理由:``list_for_tenant(user_id=None)`` 的语义是
            # **不按 user 过滤**。所以 ``end_user_id`` 为 None 时绝不能落到
            # 下面那个查询上 —— 下面的 ``elif`` 保证了这一点(走到查询时
            # ``end_user_id`` 必非 None:session 校验通过 ⟹ 该 user 存在)。
            try:
                await load_owned_session(
                    tenant_id=tenant_id,
                    agent_code=agent_code,
                    user_id=user_id,
                    session_id=session_id,
                    threads=threads,
                    users=users,
                    mint=False,
                )
            except ExternalScopeError as exc:
                return external_error(exc)
            thread_ids = [session_id]
        elif end_user_id is None:
            return JSONResponse(
                {
                    "success": True,
                    "data": {"runs": [], "limit": limit, "offset": offset},
                    "error": None,
                }
            )

        rows = await runs.list_for_tenant(
            tenant_id=tenant_id,
            user_id=end_user_id,
            agent_name=agent_code,
            thread_ids=thread_ids,
            status=status,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "runs": [
                        {
                            "run_id": str(r.run_id),
                            "session_id": str(r.thread_id),
                            "status": r.status.value,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                            "error": r.error,
                        }
                        for r in rows
                    ],
                    "limit": limit,
                    "offset": offset,
                },
                "error": None,
            }
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest services/control-plane/tests/test_external_runs_list.py -v
```

- [ ] **Step 5: 变异自证(三条)**

1. 删掉 `agent_name=agent_code` → `test_runs_of_another_agent_are_not_listed` 必须变红
2. 把 `lookup_external_user_id` 换成 `resolve_external_user_id` → `test_unknown_user_returns_empty_and_mints_no_row` 必须变红
3. 把 `session_id` 分支的 `except` 改成 `pass`(吞掉 404)→ `test_session_id_filter_404s_on_someone_elses_session` 必须变红

每条 `git diff` 确认落地,跑完用副本还原。

- [ ] **Step 6: 跑三个自审 + 平面分区**

```bash
uv run pytest services/control-plane/tests/test_external_only_gate.py \
              services/control-plane/tests/test_external_path_param_nul_guard.py \
              services/control-plane/tests/test_external_route_reachability.py \
              services/control-plane/tests/test_route_plane_partition.py -v
```

新路由挂在既有的 external router 上,四个自审应当自动覆盖它并全绿。

- [ ] **Step 7: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_runs.py \
        services/control-plane/tests/test_external_runs_list.py
git commit -m "feat(control-plane): GET /v1/agents/{code}/runs——对外 run 列表(按 user+agent,可选 session/status)"
```

---

## Task 7:配置页加「显示名」输入框

**Files:**
- Modify: `apps/admin-ui/src/components/manifest-editor/form_model.ts:427`(reader)与 `:515`(writer)
- Modify: `apps/admin-ui/src/components/manifest-editor/FormView.tsx:163-210`(basic section)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts`(类型接口约 938 段 + 值约 3922 段)
- Modify: `apps/admin-ui/src/i18n/locales/zh-CN.ts`(约 950 段)
- Test: `apps/admin-ui/src/components/manifest-editor/__tests__/`(照该目录既有 FormView 测试的文件与写法)

**Interfaces:**
- Consumes: `AgentSpecBody.display_name`(Task 1)
- Produces: 无(纯前端)

- [ ] **Step 1: 写失败的测试**

加进既有的 `apps/admin-ui/src/components/manifest-editor/__tests__/FormView.test.tsx`(那个文件已经有 basic section 的用例和全套 mock —— 新建文件要把 `tenant/TenantScopeContext`、`api/mcp-servers`、`api/mcp-catalog` 那几组 `vi.mock` 全抄一遍,没必要)。

**用 `data-testid` 定位,不要用 `getByLabelText` 加中文文案** —— 该文件既有的断言全是 `getByTestId("af-...")`,因为标签文案取决于测试环境的 i18n locale,写死中文会在 locale 变化时假红:

```tsx
describe("FormView basic section — display_name", () => {
  it("renders the current spec.display_name", () => {
    render(
      <FormView
        formData={{ metadata: { name: "a" }, spec: { display_name: "报表助手" } }}
        onChange={() => {}}
        section="basic"
      />,
    );
    const field = screen.getByTestId("af-display-name");
    expect(within(field).getByRole("textbox")).toHaveValue("报表助手");
  });

  it("falls back to an empty box when spec.display_name is absent", () => {
    render(
      <FormView
        formData={{ metadata: { name: "a" }, spec: {} }}
        onChange={() => {}}
        section="basic"
      />,
    );
    const field = screen.getByTestId("af-display-name");
    expect(within(field).getByRole("textbox")).toHaveValue("");
  });

  it("writes through to spec.display_name without clobbering siblings", async () => {
    const onChange = vi.fn();
    render(
      <FormView
        formData={{ metadata: { name: "a" }, spec: { description: "keep me" } }}
        onChange={onChange}
        section="basic"
      />,
    );

    const field = screen.getByTestId("af-display-name");
    await userEvent.type(within(field).getByRole("textbox"), "X");

    const written = onChange.mock.calls.at(-1)?.[0];
    expect(written.spec.display_name).toBe("X");
    // 兄弟字段不能被写坏 —— patchSpec 的既有语义。
    expect(written.spec.description).toBe("keep me");
  });
});
```

`within` / `screen` / `userEvent` / `vi` 在该文件顶部已经 import 好了(`FormView.test.tsx:1-3`),不用重复 import。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd apps/admin-ui && pnpm vitest run src/components/manifest-editor/__tests__/FormView.test.tsx -t display_name
```

- [ ] **Step 3: 加 reader / writer**

`form_model.ts:427` 的 `readDescription` 旁边加:

```ts
export const readDisplayName = (m: unknown): string =>
  specOf(m).display_name ?? "";
```

`:515` 的 `setDescription` 旁边加:

```ts
export const setDisplayName = (
  m: unknown,
  display_name: string,
): AgentManifest => patchSpec(m, { display_name });
```

如果 `AgentManifest` 的 spec 类型是显式声明的(不是 `Record<string, unknown>`),把 `display_name?: string` 加进去。

- [ ] **Step 4: 加输入框**

`FormView.tsx` 的 basic section:在 `af-name` 那个 div **之后**、`{!bare && (` 描述块**之前**插入。import 里补 `readDisplayName, setDisplayName`。

```tsx
        {/* 显示名 —— 给终端用户看的名字(阶段 3, 3.1)。``name`` 是机器
            标识(对外的 agent_code),直接显示在第三方界面上很难看。
            与描述同款:折进别的 tab(``bare``)时不渲染。 */}
        {!bare && (
          <div style={FIELD} data-testid="af-display-name">
            <label style={LABEL}>
              {t("agent_form.field_display_name")}
              <FieldHelp
                text={t("agent_form.field_display_name_help")}
                testId="af-display-name"
              />
            </label>
            <Input
              value={readDisplayName(formData)}
              aria-label={t("agent_form.field_display_name")}
              onChange={(e) =>
                onChange(setDisplayName(formData, e.target.value))
              }
            />
          </div>
        )}
```

- [ ] **Step 5: 加 i18n 三处**

**`en.ts` 类型接口**(约 938 行的 `agent_form:` 段,`field_description` 旁边):

```ts
    field_display_name: string;
    field_display_name_help: string;
```

**`en.ts` 值**(约 3922 行的 `agent_form:` 段):

```ts
    field_display_name: "Display name",
    field_display_name_help:
      "The name end users see in third-party apps. Leave blank to fall back to the agent name.",
```

**`zh-CN.ts`**(约 950 行的 `agent_form:` 段):

```ts
    field_display_name: "显示名",
    field_display_name_help:
      "第三方 App 里终端用户看到的名字。留空则回落到 agent 名称。",
```

**加之前先 grep 确认这两个 key 没和既有的撞** —— 同一个 object 里重复的 key 会被 esbuild 静默覆盖(本仓踩过):

```bash
rg -n "field_display_name" apps/admin-ui/src/i18n/locales/
```

- [ ] **Step 6: 跑测试 + 类型检查 + 全量前端测试**

```bash
cd apps/admin-ui
pnpm vitest run src/components/manifest-editor/
pnpm typecheck
```

**必须用 `pnpm typecheck`**(`tsc -b`)。裸 `tsc --noEmit` 在本仓恒绿、一个文件都不检查(tsconfig 是 solution 文件,`files: []`)。

编辑器里的诊断可能是 stale 的 —— 以 `pnpm typecheck` 和 `pnpm vitest` 的真实输出为准。

- [ ] **Step 7: 跑改动组件的全套测试**

```bash
pnpm vitest run
```

改了共享组件(`FormView` 被 `BasicSection` / `AgentTemplateConfigForm` / stories 多处消费),必须跑全套 —— 只跑本目录会漏掉下游。

- [ ] **Step 8: 变异自证**

把 `setDisplayName` 的 `patchSpec(m, { display_name })` 改成 `patchSpec(m, {})`,重跑,确认写入测试变红。改回来。

- [ ] **Step 9: 提交**

```bash
git add apps/admin-ui/src/components/manifest-editor/ apps/admin-ui/src/i18n/locales/
git commit -m "feat(admin-ui): Agent 配置页基础组加「显示名」输入框"
```

---

## Task 8:公开文档站

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/run-agent.md`
- Modify: `apps/admin-ui/docs-site/.vitepress/config.mts`

**Interfaces:**
- Consumes: Task 3、Task 6 的两个端点契约
- Produces: 无

**红线**:公开文档不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径。写文档时**不要**引用 `orchestrator/sse.py` 这类内部路径 —— 那是 spec 和代码注释里的东西。

- [ ] **Step 1: 加 agent 目录一节**

**已核对的现状**:`run-agent.md` 的 H1 是 `# 4 接口详情`,文件内**所有 H2 都不带编号**(`## 端点` / `## 请求体参数` / `## 工作区文件` …)。所以新节也用不带编号的 H2,标题就写 `## Agent 目录`(anchor 是 `#agent-目录`)。

在 `## 端点`(第 5 行)**之前**插入 —— 客户端对接的第一步就是「我能调哪个 agent」,排在发 run 前面。

内容要点(照该文件既有小节的行文风格写,每个端点都有:一句话说明 + 请求示例 + 响应示例 + 字段表 + 注意事项):

- 路径 `GET /v1/agent-catalog`,只需要 `Authorization`,不需要 `user_id`(目录是租户级的,与具体终端用户无关)
- 四个字段的表:`agent_code`(发 run 时填在路径里的那个)、`display_name`(**永远非空**,没配就是 `agent_code`)、`description`、`available`
- 明写:**`available: false` 的 agent 仍然会列出来**,界面上置灰即可;直接对它发 run 会被拒
- 明写:**只剩已弃用版本的 agent 不会出现在目录里**(没有可调版本 = 不属于「能调什么」这个问题的答案)
- 分页 `limit`(1–200,默认 50)/ `offset` / **`total`**(去重后的 agent 总数,不是版本行数、也不是当前页长度)
  - **`total` 是实施期新增的字段**(Task 3 修复轮 M-3),原计划的响应示例里没有 —— 写文档时以实际响应为准,别照抄本计划早先那份示例
  - 顺带写一句翻页建议:**用 `total` 判断是否翻完,不要用「返回数 < limit 即最后一页」** —— 后者是这个端点曾经出过的 bug 形态
- 需要的 scope:`read` 及以上

- [ ] **Step 2: 加 run 列表一节**

标题写 `## run 列表`(不带编号,与该文件其余 H2 一致;anchor 即 Step 3 侧栏用的 `#run-列表`)。在「会话列表与历史消息」一节(约 264 行)**之后**插入,与它并列:

- 路径 `GET /v1/agents/{agent_code}/runs`
- 参数表:`user_id`(**必填**)、`session_id`(选填)、`status`(选填)、`limit` / `offset`
- 响应字段表:`run_id` / `session_id` / `status` / `created_at` / `finished_at` / `error`
- 明写 `error` 的语义:**与 SSE `error` 帧里的 `message` 是同一个字符串**,不是另一套错误码。要判断失败原因用它,不要解析文案
- 明写 `session_id` 不属于这个用户时是 **404**,不是空列表
- `status` 的可取值列一遍

- [ ] **Step 3: 补侧栏**

**已核对的现状**:`config.mts` 第 40 行的 `{ text: "4 接口详情", link: "/guide/run-agent" }` **没有 `items`**(第 5 章那条有,可照它的形状抄)。要加子条目就得把这一条扩成带 `items` 的形式:

```ts
          {
            text: "4 接口详情",
            link: "/guide/run-agent",
            items: [
              { text: "Agent 目录", link: "/guide/run-agent#agent-目录" },
              { text: "run 列表", link: "/guide/run-agent#run-列表" },
            ],
          },
```

**anchor 必须真验**。VitePress 对纯中文标题生成的 anchor 是标题本身(空格换横杠);标题里带 ASCII 数字开头时会加 `_` 前缀(第 5 章那些 `#_5-1-帧格式` 就是这么来的)。上面两个 anchor 是按「`Agent 目录`」「`run 列表`」这两个标题推的 —— **构建后打开页面点一遍确认跳对了**,或者直接看构建产物里的 `id=`。写错的 anchor 是死链,而死链**在构建时不报错**。

- [ ] **Step 4: 构建文档站**

```bash
cd apps/admin-ui/docs-site && pnpm build
```

预期:构建成功,零 dead link 警告。

- [ ] **Step 5: 人工核对渲染结果**

```bash
cd apps/admin-ui/docs-site && pnpm preview
```

打开新增的两节,确认:表格渲染正常、代码块语言标注正确、侧栏两个新条目点得动且跳到正确位置。

**不要只看构建成功就收工** —— 上一轮文档发布的教训是「HTTP 200 不等于内容对」,要逐页看。

- [ ] **Step 6: 红线自检**

```bash
cd apps/admin-ui/docs-site
rg -n "sse\.py|control_plane|orchestrator/|packages/|services/|\.svc\.cluster|10\.|aforge_pat_" guide/run-agent.md
```

预期:零命中。有命中就是写进了内部路径 / 内网地址 / 密钥前缀,必须删。

- [ ] **Step 7: 提交**

```bash
git add apps/admin-ui/docs-site/guide/run-agent.md \
        apps/admin-ui/docs-site/.vitepress/config.mts
git commit -m "docs(api): 文档站补 agent 目录与 run 列表两节"
```

---

## 收尾:全量回归

- [ ] **Step 1: 后端全量**

```bash
cd /Users/mac/src/github/jone_qian/expert-work
uv run ruff check .
uv run ruff format --check .
uv run mypy services/control-plane/src packages/
uv run pytest packages/ services/control-plane/tests/ -q
```

`ruff` 跑全库**含 tests**(CI 就是这么跑的);`mypy` 的范围要和 CI 一致 —— 本地只跑单文件会出假阳性也会漏真阳性。

- [ ] **Step 2: 集成测试(真容器)**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests/ packages/expert-work-runtime/tests/ -q -m integration
```

- [ ] **Step 3: 前端全量**

```bash
cd apps/admin-ui && pnpm typecheck && pnpm vitest run && pnpm build
```

- [ ] **Step 4: 文档站构建**

```bash
cd apps/admin-ui/docs-site && pnpm build
```

- [ ] **Step 5: 开 PR**

```bash
git push -u origin <branch>
gh pr create --title "feat(external-api): 阶段 3 PR-A——对外 agent 目录 + run 列表" --body "..."
```

PR 描述里必须写清:

1. 两个新端点的契约
2. **`/v1/agent-catalog` 是第一条不在 `/v1/agents/` 下的对外路由**,Task 4 把三个自审的发现器改成了纯 tag 驱动 —— 说明为什么这不是「顺手改测试」而是必需的
3. **`error` 字段与 SSE `error` 帧同源**的论证(附那条测试的名字)
4. **发布前待办**:抽查测试 / 生产环境存量 agent 的 `spec.description` 实际内容 —— `/v1/agent-catalog` 让它首次对第三方可见,而它是员工在控制台自己写的,可能是内部备注。**这是抽查数据,不是抽查代码。**
5. **真栈验收清单**(下面这三条单测覆盖不到,发布后必须真跑):

| 验什么 | 怎么验 | 判据 |
|---|---|---|
| 目录的 `available` 与真实 run 行为一致 | 真栈拉一遍 `/v1/agent-catalog`,对**每个** agent 各发一次 run | `available: true` 的全部被接受;`false` 的全部被拒 |
| **`error` 与 SSE `error` 帧同源** | 真栈跑一个必然失败的 run,同时抓 SSE `error` 帧的 `message` 和 `GET .../runs` 里那条的 `error` | 两个字符串**逐字节相等**。单测环境不跑真 `sse.py`,这一半只能在这里验 |
| `description` 的真实内容 | 拉测试环境全部 agent 的目录响应,人眼过一遍 `description` | 没有内部备注 / 人名 / 内部系统名 |

---

## 附:本计划刻意不做的

| 不做 | 为什么 |
|---|---|
| agent 目录带版本号 / 工具清单 / 模型配置 | 第三方不选版本;后两者属于 manifest 面,`85abdb39` 刻意对第三方关死 |
| run 列表带 token 用量 / 耗时明细 | 那是控制台的可观测面。第三方要细节可以拉该 run 的事件流 |
| 第 8 章四语言示例新增条目 | 8.1–8.7 已覆盖全部调用模式(带信封的 GET、分页、错误处理),这两个端点是同款形状。若终审认为某条形状确实新,再单独补 |
| 给 `spec.description` 加脱敏 / 长度截断 | 那是员工自己填的租户内数据,不是不可信输入。真正的处置是发布前抽查内容(见 PR 描述第 4 条),不是在读路径上加工 |
| `str(exc)` 本身的脱敏 | 既有面(SSE `error` 帧)的问题,不是本轮引入。已记 backlog,不在本轮扩范围 |
