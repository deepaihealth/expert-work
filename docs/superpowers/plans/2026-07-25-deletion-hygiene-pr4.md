# 删除接口卫生 PR4:删除前置检查 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除动作补前置依赖检查:平台模板 extends 炸雷 409 拦、agent 软删级联(禁 trigger+取消在飞 run+410 语义)、MCP 引用检查修缮(假 409 bug + 留空影响面提示)。

**Architecture:** spec 见 `docs/superpowers/specs/2026-07-25-deletion-hygiene-pr4-design.md`(D1/D2/D3 用户拍板)。无迁移、无新表;1 个新 store 方法 + 3 个端点改造。

**Tech Stack:** FastAPI + SQLAlchemy async + pytest。

## Global Constraints

- SQL 与 in-memory 双实现谓词**逐字节一致** + 平价测试;集成测须 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。
- best-effort 清理失败必须审计可见(details 布尔);**§A 反查失败阻断删除**(fail-closed,非 best-effort)。
- 日志不放请求派生值(CodeQL py/log-injection 对 `extra=` 同样追踪);**副作用不进 assert**(CodeQL py/side-effect-in-assert,PR3 被逮)。
- 变异自验 load-bearing(brief 指定的变异必须做并记录红/绿)。
- 终门 CI 同款:ruff 全库、`ruff format --check`、CI-scope mypy(`packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src`)、全量 pytest(已知本机非回归红:rls_detect 顺序依赖 / pgbouncer / eval_engine_live / pg_restore_drill)。
- 分支 `fix-deletion-hygiene-pr4`,基 main(含 6cf1f6cd)。

## 并行波次(SDD 控制器用)

- **波 1(3 并行 worktree,文件互不相交)**:T1(persistence trigger)/ T2(agent_templates.py)/ T4(mcp_servers.py)
- **波 2**:T3(agents.py + runs.py,依赖 T1)
- **T5 终门** + opus 全分支终审。
- worktree 从 main 切出:每个 dispatch 第一步 `git merge --ff-only fix-deletion-hygiene-pr4`。

---

### Task 1: TriggerStore.disable_for_agent(双实现)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/trigger/base.py`(:95 update 附近追加抽象)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/trigger/sql.py`、`memory.py`
- Test: `packages/expert-work-persistence/tests/test_sql_trigger_store.py`、`test_in_memory_trigger_store.py`

**Interfaces:**
- Produces: `disable_for_agent(*, agent_name: str, agent_version: str, tenant_id: UUID) -> int`(T3 消费)。谓词:`enabled == true AND agent_name == AND agent_version == AND tenant_id ==`;写 `enabled=false`;返回改动行数。

- [ ] **Step 1: 失败测试**(两文件同型):目标 name+version 的 enabled trigger 被禁、**同 name 他 version 不动(变异哨兵)**、已 disabled 的不计数、他租户不动、返回计数。

```python
async def test_disable_for_agent_scopes_by_name_version_tenant():
    # 同租户:target@v1 enabled ×2、target@v2 enabled ×1、target@v1 已 disabled ×1
    # 他租户:target@v1 enabled ×1
    n = await store.disable_for_agent(agent_name="target", agent_version="v1", tenant_id=TEN_A)
    assert n == 2
    # target@v2 仍 enabled;他租户仍 enabled;此前 disabled 的仍 disabled(未被重复计数)
```

- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——SQL:

```python
async def disable_for_agent(
    self, *, agent_name: str, agent_version: str, tenant_id: UUID
) -> int:
    stmt = (
        update(AgentTriggerRow)
        .where(
            AgentTriggerRow.tenant_id == tenant_id,
            AgentTriggerRow.agent_name == agent_name,
            AgentTriggerRow.agent_version == agent_version,
            AgentTriggerRow.enabled.is_(True),
        )
        .values(enabled=False)
    )
```

事务模式照同文件既有写方法(execute+commit+rowcount);memory 版按同容器过滤,record 经 `model_copy(update={"enabled": False})` 替换(TriggerRecord frozen)。docstring 写明"deletion hygiene PR4 §B — agent soft-delete cascade"与四谓词。
- [ ] **Step 4: 确认绿**(SQL 侧带 DOCKER_HOST)
- [ ] **Step 5: 变异自验**——去掉 `agent_version` 谓词 → "他 version 不动"哨兵红;恢复绿。记录。
- [ ] **Step 6: Commit** `feat(persistence): TriggerStore.disable_for_agent 双实现(agent 软删级联)`

### Task 2: 平台模板删除继承者反查 + 409(D1)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agent_templates.py:206-235`(delete_template)
- Test: `services/control-plane/tests/test_agent_templates_api.py`

**Interfaces:**
- Consumes: `agent_spec.list_all_tenants(*, status=None, name=None, limit, offset)`(base.py:89,须 `bypass_rls_session()` 内);`parse_extends_ref`(protocol `agent_template_resolve.py:111`,latest 字面量可能出现);模板 store `list_versions(name)` / `get_latest(name, status=None)`(base.py:77-85,latest=created_at 最新,可选 status 过滤)。

- [ ] **Step 1: 失败测试**:①建模板+租户 spec `extends="tpl@1"` → DELETE tpl@1 → **409 TEMPLATE_IN_USE**,body 带 `dependents_total` 与 `dependents[]`(tenant_id+agent);继承者构建仍 200;②无继承者 → 204;③**软删的继承者不拦**(哨兵:spec status=DELETED → 204);④`extends="tpl@latest"` 且 tpl 有两个版本 → 删其中一个 → 204(latest 重解析仍成功,不误拦);删最后一个版本 → 409;⑤继承者在分页第二页(>100 spec)仍被找到;⑥>20 继承者时 `dependents` cap 20 且 `dependents_total` 为真实数。⑤⑥可合并造数。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——delete_template 在 `store.delete` **前**(同一 `bypass_rls_session` 内或并列开一个):

```python
dependents: list[dict[str, str]] = []
target_is_last_resolvable = <删除后该名字无其他可解析版本>  # 经 store.list_versions(name)
# 与构建侧 latest 解析语义对齐:先读 runtime.py:548-573 的 _resolve_template_extends
# 用 get_latest 时传的 status 参数,复用同一谓词判"可解析"。
offset = 0
while True:
    page = await agent_spec.list_all_tenants(limit=200, offset=offset)
    for s in page:
        if s.status is AgentSpecStatus.DELETED:
            continue
        ref = s.spec.extends
        if ref is None:
            continue
        try:
            ref_name, ref_version = parse_extends_ref(ref)
        except ValueError:
            continue
        if ref_name != name:
            continue
        if ref_version == version or (ref_version == "latest" and target_is_last_resolvable):
            dependents.append({"tenant_id": str(s.tenant_id), "agent": f"{s.name}@{s.version}"})
    if len(page) < 200:
        break
    offset += 200
if dependents:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "TEMPLATE_IN_USE",
            "message": f"extended by {len(dependents)} tenant agent(s)",
            "dependents_total": len(dependents),
            "dependents": dependents[:20],
        },
    )
```

`spec.extends` 的实际属性路径以 `AgentSpec` 模型为准(brief 实现者核 `agent_spec.py:1153` 所在层级——`AgentSpecBody` 上,record.spec 下的访问链自行确认)。反查中 store 异常**不捕获**(fail-closed 阻断删除)。无继承者路径:既有审计 details 增 `dependents_checked: True`。409 路径不发删除审计。
- [ ] **Step 4: 确认绿**(`uv run pytest services/control-plane/tests/test_agent_templates_api.py -q`)
- [ ] **Step 5: 变异自验**——把 DELETED 跳过行注释掉 → 哨兵③红;恢复绿。记录。
- [ ] **Step 6: Commit** `feat(control-plane): 删平台模板前反查 extends 继承者(409 拦,无 force)`

### Task 3: delete_agent 级联 + 410 语义(D2,依赖 T1)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py:1168-1203`(delete_agent)
- Modify: `services/control-plane/src/control_plane/api/runs.py:1002-1010` 与 `:643-647`(410 改造)
- Test: `services/control-plane/tests/test_agents_api.py`、`services/control-plane/tests/test_runs_api.py`

**Interfaces:**
- Consumes: T1 `disable_for_agent(*, agent_name, agent_version, tenant_id) -> int`;取消在飞 run 套路照 `disable_agent`(agents.py:1254-1274:`run_store.list_running_for_agent` → `runtime.run_manager.cancel(run.run_id) or run_store.request_cancel(...)`,每 run 一条 SESSION_CANCEL 审计,reason 用 `"agent_deleted"`);`agent_repo.get(..., include_deleted=True)`(base.py:66-72)。

- [ ] **Step 1: 失败测试**:①删 agent 后其 enabled trigger 变 disabled、他 agent/他 version 的不动、审计 details 带 `triggers_disabled`/`runs_cancelled`;②有在飞 run 时删 → run 被取消 + 计数;③禁用失败注入(monkeypatch store 抛)→ 删除仍 204 + details `triggers_disable_failed: true`;④对 DELETED agent 的会话发消息/起 run → **410** body code `AGENT_DELETED`;对从未存在的 agent → 404 照旧;审批续跑同型两断言。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——delete_agent 软删成功后、审计前:

```python
cancelled = 0
try:
    running = await run_store.list_running_for_agent(tenant_id=tenant_id, agent_name=name)
    now = datetime.now(UTC)
    for run in running:
        stopped = await runtime.run_manager.cancel(run.run_id) or await run_store.request_cancel(
            run_id=run.run_id, tenant_id=tenant_id, updated_at=now
        )
        if stopped:
            cancelled += 1
            await emit(..., action=AuditAction.SESSION_CANCEL, resource_type="run",
                       resource_id=str(run.run_id), reason="agent_deleted")
except Exception:
    logger.warning("agent_delete.runs_cancel_failed", exc_info=True)
    details["runs_cancel_failed"] = True
try:
    disabled = await triggers.disable_for_agent(
        agent_name=name, agent_version=version, tenant_id=tenant_id
    )
except Exception:
    logger.warning("agent_delete.triggers_disable_failed", exc_info=True)
    details["triggers_disable_failed"] = True
```

既有 MANIFEST_DELETE 审计 details 合入 `runs_cancelled`/`triggers_disabled` 计数与失败布尔。所需 Depends(run_store/runtime/triggers)照 disable_agent 端点与 triggers.py 的 getter 模式补注入。注意 `list_running_for_agent` 只按 name(disable 是全 name 级)——delete 按 name+version,取消范围保持 name 级即可?**不**——按 spec 对齐 disable 力度,但 delete 是版本级动作:取消列表过滤 `run.agent_version == version`?`RunInfo` 是否携带 agent_version(brief 实现者核 list_running_for_agent 返回结构;若无版本字段则保持 name 级取消并在报告注明,宁可多取消不留残留)。
runs.py 两处 410:

```python
record = await agent_repo.get(
    tenant_id=tenant_id, name=meta.agent_name, version=meta.agent_version,
    include_deleted=True,
)
if record is None:
    raise HTTPException(status_code=404, detail=f"agent {meta.agent_name}@{meta.agent_version} not found")
if record.status is AgentSpecStatus.DELETED:
    raise HTTPException(
        status_code=410,
        detail={"code": "AGENT_DELETED", "message": f"agent {meta.agent_name}@{meta.agent_version} has been deleted"},
    )
```

(:643-647 同型;后续 `runtime.get_agent` 用回原 record 语义不变。)
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——把 410 分支的 status 判定改永假 → 测试④红;恢复绿。记录。
- [ ] **Step 6: Commit** `feat(control-plane): agent 软删级联(禁 trigger+取消在飞 run)+ 已删 agent 起 run 410`

### Task 4: MCP 引用检查修缮(D3 + 假 409 bug 修)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/mcp_servers.py`(:50-69 helper 区 + :1006-1058 delete 端点)
- Test: `services/control-plane/tests/test_mcp_server_reference_check.py`、`services/control-plane/tests/test_mcp_servers_api.py`

- [ ] **Step 1: 失败测试**:①**bug 回归哨兵**:软删 agent 的 spec 显式引用 server → 删除 **204**(现状假 409,测试先红);②active agent 显式引用 → 409 照旧;③`servers=[]` 不算硬引用(helper 单测补空列表用例)→ 删除 204;④删除成功响应 data 与审计 details 带 `implicit_all_agents: N`(N=该租户 active spec 中含 `type=="mcp"` 且 servers 为空的 agent 数;无 mcp 工具的 agent 计 0);⑤`manifest_uses_implicit_all` 单测(有 mcp 工具 servers 空 → True;servers 显式 → False;无 mcp 工具 → False)。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——新 helper(:69 后,照 `manifest_references_server` 风格,读 raw manifest dict):

```python
def manifest_uses_implicit_all(manifest: dict[str, object], /) -> bool:
    """True when the manifest has an ``mcp`` tool whose ``servers`` list is
    empty/absent — the documented "every available server" wildcard. Such an
    agent follows the live server set dynamically, so it is NOT a hard
    reference (delete proceeds), but the impact count is surfaced."""
```

delete 端点引用块改造:

```python
specs = await agent_spec_store.list_by_tenant(tenant_id=tenant_id, limit=1000)
active_specs = [s for s in specs if s.status is not AgentSpecStatus.DELETED]
referencing = [
    s.name for s in active_specs
    if manifest_references_server(s.spec.model_dump(mode="json"), name)
]
if referencing:
    raise HTTPException(409, ...)  # 既有 detail 不变
implicit_all = sum(
    1 for s in active_specs
    if manifest_uses_implicit_all(s.spec.model_dump(mode="json"))
)
```

`implicit_all` 进删除成功响应 data 与 MCP_SERVER_DELETE 审计 details(`implicit_all_agents`)。`model_dump` 每 spec 只调一次(局部变量复用,别对同一 spec dump 两遍)。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——去掉 status 过滤 → 哨兵①红;`manifest_uses_implicit_all` 判定改永假 → 测试④红。恢复绿,记录。
- [ ] **Step 6: Commit** `fix(control-plane): MCP server 引用检查滤软删 spec(假 409)+ 留空影响面提示`

### Task 5: 终门(全库门 + 修串扰)

- [ ] `uv run ruff check .` / `uv run ruff format --check .`
- [ ] CI-scope mypy(Global Constraints 命令)
- [ ] 全量 pytest(DOCKER_HOST 前缀);新红查因修复,已知本机噪音对照清单
- [ ] 全绿后 opus 全分支终审(`review-package $(git merge-base main HEAD) HEAD`)

## Self-Review 记录

- Spec 覆盖:§A=T2,§B=T1+T3,§C=T4,§D 审计分散各任务。
- 类型一致:`disable_for_agent(agent_name, agent_version, tenant_id)` T1/T3 一致;`manifest_uses_implicit_all(dict) -> bool` T4 内一致。
- 无迁移无新表;波 1 三任务文件互不相交。
