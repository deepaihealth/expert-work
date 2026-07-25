# 删除接口卫生 PR3:孤儿行级联 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复删除父行不清子行的四处孤儿(幽灵 run / approval / trigger_run+webhook_delivery / curation 悬空)与 knowledge 删除竞态。

**Architecture:** store 层补批删原语(双实现)→ 端点接线(先子后父)→ 三个串行迁移(0134 孤儿清 / 0135 curation 回退+FK RESTRICT / 0136 knowledge FK CASCADE)→ ingest 写回守卫。spec 见 `docs/superpowers/specs/2026-07-24-deletion-hygiene-pr3-design.md`。

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + pytest(testcontainers 集成)。

## Global Constraints

- SQL 与 in-memory 双实现谓词**逐字节一致**,每个新 store 方法配平价测试;改 SQL 约束/迁移必须本地跑 integration(`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`),in-memory 不校验 FK。
- 迁移链严格串行:`0133 → 0134(T5) → 0135(T8) → 0136(T9)`,down_revision 逐个接。
- 删除顺序恒为**先子行后父行**(崩溃安全:中途失败不产生新孤儿,重跑幂等)。
- best-effort 清理失败必须审计可见(details 布尔标记);日志**不放请求派生值**(CodeQL py/log-injection 对 `extra=` 同样追踪,PR2 教训)。
- `IntegrityError` catch 必须覆盖 `execute()`——非延迟 RESTRICT 在 execute 炸,不在 commit(PR2 T8 教训)。
- 变异自验要 load-bearing:翻转比较/谓词方向,勿用会被 SQL NULL 三值逻辑吞掉的变异(PR1 教训)。
- 终门跑 CI 同款:ruff 全库、`ruff format --check`、CI-scope mypy(`packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src`)、全量 pytest。已知本机非回归红:test_rls_detect(顺序依赖)、test_pgbouncer_integration。
- 分支 `fix-deletion-hygiene-pr3`,基 main(含 8f689c58)。

## 并行波次(SDD 控制器用)

- **波 1(文件互不相交,5 并行 worktree)**:T1(runtime runs store)/ T2(approval store)/ T4(trigger+webhook 端点)/ T5(迁移 0134)/ T6(curation store)
- **波 2(3 并行)**:T3(purge_session 端点,依赖 T1+T2)/ T7(curation 端点,依赖 T6)/ T8(迁移 0135,依赖 T5 revision)
- **波 3**:T9(迁移 0136 + knowledge 守卫,依赖 T8 revision)
- **T10 终门** + opus 全分支终审。
- worktree 从 main 切出:每个 dispatch 第一步 `git merge --ff-only fix-deletion-hygiene-pr3`。

---

### Task 1: run 删除原子排空 run_event(store 层)

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/event_store.py`(RunEventStore 抽象 + InMemory + Sql)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`(SqlRunStore.delete_by_thread ~:830;InMemoryRunStore.__init__ ~:364 与 delete_by_thread ~:434)
- Modify: `services/control-plane/src/control_plane/app.py:630`(InMemoryRunStore 装配加 event_store)
- Modify: `packages/expert-work-persistence/tests/test_purge_store_methods.py`(更新 "run_event RESTRICT — never deleted" 相关断言,~:136-165)
- Test: `packages/expert-work-runtime/tests/test_run_event_store.py`、`packages/expert-work-runtime/tests/test_run_store.py`、`packages/expert-work-runtime/tests/test_sql_run_store.py`

**Interfaces:**
- Produces: `RunEventStore.delete_for_runs(*, run_ids: Sequence[UUID]) -> int`;`RunStore.delete_by_thread` 新契约 = 同时清空子 run_event(SQL 单事务原子;InMemory 经注入的 event_store);`InMemoryRunStore(..., event_store: RunEventStore | None = None)`。
- T3 依赖:`run_manager.delete_by_thread` 行为修复后不再被 RESTRICT 挡。

- [ ] **Step 1: 失败测试**——event store 双实现 `delete_for_runs`(删指定 run 的全部事件、不碰其他 run、空输入返回 0);SQL run store 集成:有事件的 run `delete_by_thread` 后 agent_run 与 run_event 全消失(现状被 RESTRICT 挡,测试红);InMemory run store:注入 event_store 后 delete_by_thread 同步清事件。

```python
# test_run_event_store.py 追加(InMemory 版;SQL 版进 test_sql_run_store.py 同型)
async def test_delete_for_runs_removes_only_targeted_runs():
    store = InMemoryRunEventStore()
    await store.append(_mk_event(run_id=RUN_A, seq=0))
    await store.append(_mk_event(run_id=RUN_A, seq=1))
    await store.append(_mk_event(run_id=RUN_B, seq=0))
    assert await store.delete_for_runs(run_ids=[RUN_A]) == 2
    assert list(await store.list(run_id=RUN_A)) == []
    assert len(await store.list(run_id=RUN_B)) == 1
    assert await store.delete_for_runs(run_ids=[]) == 0
```

- [ ] **Step 2: 跑测试确认红**(`DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest packages/expert-work-runtime/tests/ -q -k delete_for_runs or delete_by_thread`)
- [ ] **Step 3: 实现**

```python
# event_store.py — 抽象:
@abc.abstractmethod
async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
    """Remove ALL events for the given runs. Empty input removes nothing.

    Returns rows removed. Deletion-hygiene PR3 §A — called by the
    in-memory RunStore mirror; the SQL RunStore empties run_event
    inside its own delete transaction instead (atomicity).
    """

# InMemoryRunEventStore:
async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
    removed = 0
    for rid in run_ids:
        removed += len(self._events.pop(rid, []))
    return removed

# SqlRunEventStore:
async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
    if not run_ids:
        return 0
    async with self._sf() as session:
        result = await session.execute(
            delete(RunEventRow).where(RunEventRow.run_id.in_(list(run_ids)))
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
```

```python
# store.py SqlRunStore.delete_by_thread — 单事务先子后父(import RunEventRow 与 AgentRunRow 同源):
async def delete_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> int:
    async with self._sf() as session:
        run_ids = (
            select(AgentRunRow.id)
            .where(
                AgentRunRow.thread_id == thread_id,
                AgentRunRow.tenant_id == tenant_id,
            )
            .scalar_subquery()
        )
        await session.execute(delete(RunEventRow).where(RunEventRow.run_id.in_(run_ids)))
        result = await session.execute(
            delete(AgentRunRow).where(
                AgentRunRow.thread_id == thread_id,
                AgentRunRow.tenant_id == tenant_id,
            )
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
```

```python
# InMemoryRunStore.__init__ 加 kwarg(照 thread_meta_store 注入先例):
event_store: RunEventStore | None = None
# delete_by_thread 删 victims 前先:
if self._event_store is not None:
    await self._event_store.delete_for_runs(run_ids=victims)
```

app.py:630 装配:`InMemoryRunStore(thread_meta_store=resolved_threads, event_store=<in-memory RunEventStore 装配变量>)`——同文件 grep `RunEventStore` 找 resolved 变量名,注意定义顺序(必要时把 event store 装配上移)。
`test_purge_store_methods.py` 中断言 run 行"永不删除、只匿名"的用例:更新为 delete_by_thread 现在连子行真删,anonymize 仍只匿名(该方法本身行为不变)。

- [ ] **Step 4: 跑测试确认绿**(同 Step 2 命令 + `test_purge_store_methods.py`)
- [ ] **Step 5: 变异自验**——把 SqlRunStore 里 run_event 排空语句注释掉 → 集成测试必红(RESTRICT 挡删);恢复后绿。报告记录。
- [ ] **Step 6: Commit** `feat(runtime): run 删除原子排空 run_event 子行(幽灵 run 根因修复)`

### Task 2: ApprovalStore.delete_for_threads(双实现)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/approval/base.py`(抽象,照 :132 delete_all_for_user 相邻)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/approval/sql.py`(照 :216 delete_all_for_user 模式)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/approval/memory.py`(照 :102)
- Test: `packages/expert-work-persistence/tests/test_sql_approval_store.py`、`test_in_memory_approval_store.py`

**Interfaces:**
- Produces: `ApprovalStore.delete_for_threads(*, thread_ids: Sequence[UUID], tenant_id: UUID) -> int`(T3 消费)。

- [ ] **Step 1: 失败测试**(两文件同型):目标 thread 的 approval 全删、他 thread/他租户不动、空输入 0。

```python
async def test_delete_for_threads_scopes_by_tenant_and_thread():
    await store.create(_mk_approval(thread_id=T1, tenant_id=TEN_A))
    await store.create(_mk_approval(thread_id=T2, tenant_id=TEN_A))
    await store.create(_mk_approval(thread_id=T1, tenant_id=TEN_B))
    assert await store.delete_for_threads(thread_ids=[T1], tenant_id=TEN_A) == 1
    assert await store.delete_for_threads(thread_ids=[], tenant_id=TEN_A) == 0
    # TEN_B 的 T1 行仍在;TEN_A 的 T2 行仍在
```

- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——SQL:`delete(AgentApprovalRow).where(tenant_id ==, thread_id.in_(list(thread_ids)))`,空输入短路返回 0,事务模式照 delete_all_for_user;memory:照 feedback_store.py:144-152 过滤模式。docstring 照 feedback `delete_for_threads`(:97-110)写明 tenant+thread 双谓词与 RLS 语境。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: Commit** `feat(persistence): ApprovalStore.delete_for_threads 双实现`

### Task 3: purge_session 接线 approval + 失败审计可见 + 端到端测试(依赖 T1/T2)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/sessions.py:815-865`(purge_session)
- Modify: `services/control-plane/src/control_plane/purge/user_purge.py:220-225`(注释更新:RESTRICT 不再挡,anonymize 仅兜底 store 异常)
- Test: `services/control-plane/tests/test_sessions_api.py`(新增 purge 段,现状零覆盖)

**Interfaces:**
- Consumes: T1 修复后的 `run_manager.delete_by_thread`;T2 `approvals.delete_for_threads`。

- [ ] **Step 1: 失败测试**:①造会话 + 有事件 run + pending approval → purge → thread_meta/agent_run/run_event/agent_approval 全消失,`/v1/runs?thread_id=` 空,响应 data 含 `runs`/`approvals` 计数;②run 删除失败注入(桩 run_manager 抛)→ 响应仍 success、审计 details 含 `runs_delete_failed: true`;③重复 purge 幂等(404 或计数 0,按 threads.delete 现语义——`removed=False` 走 404?现代码不 404,保持现状断言 `meta_removed: false`)。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——sessions.py 增 `approvals: Annotated[ApprovalStore, Depends(_get_approval_store)]`(本地 getter 照 runs.py:357 模式,读同一 app.state 挂名);deleted 初始 `{"checkpoint": False, "runs": 0, "approvals": 0}`;两个 try/except 失败分支分别加 `deleted["runs_delete_failed"] = True` / `deleted["approvals_delete_failed"] = True`(审计 details 经 `**deleted` 自动携带);approval 删除放 run 删除后、thread_meta 删除前。
- [ ] **Step 4: 确认绿**(`uv run pytest services/control-plane/tests/test_sessions_api.py -q`)
- [ ] **Step 5: Commit** `feat(control-plane): purge_session 级联 approval + 删除失败审计可见 + 补端到端测试`

### Task 4: trigger / webhook 删除端点接线(纯接线)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/triggers.py:507-543`(delete_trigger;`_get_trigger_run_store` 已在 :235)
- Modify: `services/control-plane/src/control_plane/api/webhook_endpoints.py:306-328`(delete_endpoint;新增 `_get_delivery_store` getter 读 `app.state.webhook_delivery_store`,照 :128 `_get_store` 模式)
- Test: `services/control-plane/tests/test_triggers_api.py`、`test_webhook_endpoints_api.py`

**Interfaces:**
- Consumes: 既有 `TriggerRunStore.delete_for_triggers(*, trigger_ids, tenant_id) -> int`、`WebhookDeliveryStore.delete_for_endpoints(*, endpoint_ids, tenant_id) -> int`。

- [ ] **Step 1: 失败测试**:删 trigger 后其 trigger_run 全消失、他 trigger 的不动、审计 details `runs_removed` 计数正确(0 子行时为 0);webhook 同型 `deliveries_removed`。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——delete_trigger 在 `triggers.delete` **前**:

```python
runs_removed = await trigger_runs.delete_for_triggers(
    trigger_ids=[trigger_id], tenant_id=tenant_id
)
```

emit details 加 `details={"runs_removed": runs_removed}`(该 emit 现无 details 参数,补上)。webhook 同型:`deliveries_removed = await deliveries.delete_for_endpoints(endpoint_ids=[endpoint_id], tenant_id=tenant_id)` 放 `store.delete` 前,emit 补 `details={"deliveries_removed": deliveries_removed}`。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: Commit** `feat(control-plane): trigger/webhook 删除级联投递行(接线既有 delete_for_* 方法)`

### Task 5: 迁移 0134 存量孤儿 trigger_run / webhook_delivery 清理

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0134_orphan_run_delivery_cleanup.py`(down_revision = 0133 的 revision id,文件内查)
- Test: `packages/expert-work-persistence/tests/test_orphan_run_delivery_cleanup.py`(照 `test_role_binding_orphan_cleanup.py` 集成套路)

- [ ] **Step 1: 失败测试**(集成,真容器):造 孤儿 trigger_run(指向不存在 trigger)+ 有主 trigger_run + 孤儿 webhook_delivery + 有主 delivery → upgrade → 孤儿消失、有主保留。
- [ ] **Step 2: 确认红**(迁移不存在)
- [ ] **Step 3: 实现**:

```python
def upgrade() -> None:
    op.execute(
        """
        DELETE FROM trigger_run tr
        WHERE NOT EXISTS (SELECT 1 FROM agent_trigger t WHERE t.id = tr.trigger_id)
        """
    )
    op.execute(
        """
        DELETE FROM webhook_delivery wd
        WHERE NOT EXISTS (SELECT 1 FROM webhook_endpoint we WHERE we.id = wd.endpoint_id)
        """
    )

def downgrade() -> None:
    pass  # 删掉的孤儿本就不该存在,不可逆是意图(0132 同姿态)
```

- [ ] **Step 4: 确认绿**(DOCKER_HOST 前缀)
- [ ] **Step 5: 变异自验**——把 NOT EXISTS 改 EXISTS → "有主行保留"断言红;恢复。
- [ ] **Step 6: Commit** `feat(persistence): 迁移 0134 清理存量孤儿 trigger_run/webhook_delivery`

### Task 6: CurationCandidateStore.revert_promoted_for_dataset(双实现)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/curation/base.py`(:84-165 内追加抽象)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/curation/sql.py`、`memory.py`
- Test: `packages/expert-work-persistence/tests/test_sql_curation_store.py`、`test_in_memory_curation_store.py`

**Interfaces:**
- Produces: `revert_promoted_for_dataset(*, dataset_id: UUID, tenant_id: UUID) -> int`(T7 消费)。谓词:`status == PROMOTED AND eval_dataset_id == dataset_id`;写 `status=PENDING, eval_dataset_id=None`,其余列(evolved_at/reviewed_at 等)不动。

- [ ] **Step 1: 失败测试**(两文件同型):PROMOTED 指向 D1 的回退成 PENDING 无指针;指向 D2 的 PROMOTED 不动;**DISMISSED 行不动(变异哨兵)**;他租户不动;返回计数。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——SQL:

```python
async def revert_promoted_for_dataset(self, *, dataset_id: UUID, tenant_id: UUID) -> int:
    stmt = (
        update(CurationCandidateRow)
        .where(
            CurationCandidateRow.tenant_id == tenant_id,
            CurationCandidateRow.eval_dataset_id == dataset_id,
            CurationCandidateRow.status == CandidateStatus.PROMOTED.value,
        )
        .values(status=CandidateStatus.PENDING.value, eval_dataset_id=None)
    )
```

事务模式照同文件既有写方法;memory 版照同文件容器结构,record `model_copy(update={"status": PENDING, "eval_dataset_id": None})`。谓词两实现逐字节对齐。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——去掉 status 谓词 → DISMISSED 哨兵测试红;恢复。
- [ ] **Step 6: Commit** `feat(persistence): curation candidate 按 dataset 回退 PENDING(双实现)`

### Task 7: delete_eval_dataset 先回退再删 + 审计(依赖 T6)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/curation.py:437-458`(delete_eval_dataset)
- Test: `services/control-plane/tests/test_curation_api.py`

- [ ] **Step 1: 失败测试**:①promote 一个 candidate(铸出 dataset 行)→ 删该 dataset → candidate 回 PENDING 无指针、可再 promote;②dismissed candidate 不受影响;③审计 details `candidates_reverted: 1`;④无关联 candidate 时删 dataset 照常、计数 0。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——端点注入 `candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)]`(getter 已在文件内);删除前:

```python
reverted = await candidates.revert_promoted_for_dataset(
    dataset_id=dataset_id, tenant_id=tenant_id
)
```

回退异常**不捕获**(上抛 500 阻断删除——先删后崩会重现悬空,spec 错误处理节);既有 emit details 加 `candidates_reverted": reverted`。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: Commit** `feat(control-plane): 删 eval dataset 先回退 promoted candidate(悬空修复)`

### Task 8: 迁移 0135 curation 悬空回退 + FK RESTRICT + store 409 兜底(依赖 T5 revision)

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0135_curation_candidate_fk.py`(down_revision = 0134)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/curation/sql.py`(EvalDataset 删除的 IntegrityError 包装,:151-160 附近)+ 对应异常类型定义(照 mcp_connector_catalog `McpConnectorCatalogInUseError` 先例,放 curation store 模块)+ in-memory 对应行为
- Modify: `services/control-plane/src/control_plane/api/curation.py`(delete 端点 catch 新异常 → 409,理论不可达但兜绕过路径)
- Test: `packages/expert-work-persistence/tests/test_curation_candidate_fk.py`(照 `test_mcp_catalog_oauth_fk_restrict.py` 套路)

- [ ] **Step 1: 失败测试**(集成):①造悬空 PROMOTED(指向已删 dataset)→ upgrade → 变 PENDING 无指针;②悬空 DISMISSED 带指针 → 指针清 NULL、状态不动;③非悬空 PROMOTED 不动;④FK 生效:直接 SQL 删被引用 dataset → ForeignKeyViolation。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**:

```python
def upgrade() -> None:
    op.execute(
        """
        UPDATE curation_candidate c SET status = 'pending', eval_dataset_id = NULL
        WHERE c.status = 'promoted' AND c.eval_dataset_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM eval_dataset d WHERE d.id = c.eval_dataset_id)
        """
    )
    op.execute(
        """
        UPDATE curation_candidate c SET eval_dataset_id = NULL
        WHERE c.eval_dataset_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM eval_dataset d WHERE d.id = c.eval_dataset_id)
        """
    )
    op.create_foreign_key(
        "fk_curation_candidate_eval_dataset",
        "curation_candidate",
        "eval_dataset",
        ["eval_dataset_id"],
        ["id"],
        ondelete="RESTRICT",
    )

def downgrade() -> None:
    op.drop_constraint(
        "fk_curation_candidate_eval_dataset", "curation_candidate", type_="foreignkey"
    )
    # 数据修正不可逆(0132 同姿态)
```

store 兜底:eval_dataset SQL delete 的 try **覆盖 execute**,`IntegrityError → EvalDatasetInUseError`;api delete 端点 catch → 409 `{"code": "EVAL_DATASET_IN_USE"}`(T7 先回退,正常路径不触发;这是绕过 app 层的 DB 兜底语义化)。in-memory dataset store delete 不模拟 FK(与 mcp catalog in-memory 现状一致,集成测试盯 SQL 侧)。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: Commit** `feat(persistence): 迁移 0135 curation 悬空回退 + eval_dataset FK RESTRICT 兜底`

### Task 9: 迁移 0136 knowledge FK CASCADE + ingest 写回守卫(依赖 T8 revision)

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0136_knowledge_chunk_fk.py`(down_revision = 0135)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/knowledge/base.py`(set_document_status 返回类型 `-> bool`)+ `sql.py`(:553 set_document_status 返 rowcount>0;delete_document/replace_chunks 不改)+ `memory.py`(:322 set_document_status 返 bool;:355 replace_chunks 文档不存在 `raise KeyError(document_id)`)
- Modify: `services/control-plane/src/control_plane/knowledge/ingestion.py:216-227`(except 分支加已删判定)
- Test: `packages/expert-work-persistence/tests/test_sql_knowledge_store.py`、`test_in_memory_knowledge_store.py`、`services/control-plane/tests/test_knowledge_ingestion.py`、新增 `packages/expert-work-persistence/tests/test_knowledge_chunk_fk.py`

- [ ] **Step 1: 失败测试**:①集成:造孤儿 chunk → upgrade → 消失;FK CASCADE:直接 SQL 删 document 行 → chunk 随删;删除后 `replace_chunks` 抛 IntegrityError 且无孤儿行;②in-memory:删文档后 `replace_chunks` 抛 KeyError;③`set_document_status` 对已删文档返回 False、存在返回 True(两实现);④ingestion:桩 store 使 `ingest_document_bytes` 抛且 `get_document` 返 None → **不**调 `mark_document_failed_terminal`、静默结束;`get_document` 返回文档 → 照旧 mark failed。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——迁移:

```python
def upgrade() -> None:
    op.execute(
        """
        DELETE FROM knowledge_chunk kc
        WHERE NOT EXISTS (SELECT 1 FROM knowledge_document kd WHERE kd.id = kc.document_id)
        """
    )
    op.create_foreign_key(
        "fk_knowledge_chunk_document",
        "knowledge_chunk",
        "knowledge_document",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )

def downgrade() -> None:
    op.drop_constraint("fk_knowledge_chunk_document", "knowledge_chunk", type_="foreignkey")
```

ingestion._run except 分支(:216)改:

```python
except Exception as exc:
    gone = False
    try:
        gone = (
            await self._store.get_document(tenant_id=tenant_id, document_id=document_id)
        ) is None
    except Exception:  # pragma: no cover - 判定失败按未删处理
        pass
    if gone:
        # 文档已被并发删除 — FK/守卫拒绝写回是正常终止,不算失败。
        logger.debug("knowledge.ingest_document_gone document=%s", document_id)
        return
    logger.warning("knowledge.ingest_failed document=%s", document_id, exc_info=True)
    await self._store.mark_document_failed_terminal(...)  # 原样
```

set_document_status:base 签名 `-> bool`;sql 版 `result = await session.execute(...)` 后 `return int(getattr(result, "rowcount", 0) or 0) > 0`;memory 版文档缺失 `return False`、更新后 `return True`;现有调用点不消费返回值,无需改。
- [ ] **Step 4: 确认绿**(集成 + control-plane ingest 测试)
- [ ] **Step 5: 变异自验**——把 _run 的 gone 判定改永 False → "已删不 mark failed"测试红;恢复。
- [ ] **Step 6: Commit** `feat(knowledge): chunk FK CASCADE + ingest 写回守卫(删除竞态修复)`

### Task 10: 终门(全库门 + 修串扰)

- [ ] ruff 全库:`uv run ruff check .`
- [ ] format:`uv run ruff format --check .`
- [ ] CI-scope mypy:`uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src`
- [ ] 全量 pytest(DOCKER_HOST 前缀);红项对照已知非回归清单(rls_detect 顺序依赖 / pgbouncer 本机),新红必须查因修复
- [ ] 修任何跨任务串扰后重跑;全绿后 opus 全分支终审(`review-package $(git merge-base main HEAD) HEAD`)

## Self-Review 记录

- Spec 覆盖:§A=T1/T2/T3,§B=T4/T5,§C=T6/T7/T8,§D=T9,§E 分散在 T3/T4/T7 审计步骤。promote 校验项已随 spec 勘误删除。
- 类型一致:`delete_for_runs(run_ids)`(无 tenant——run_event 无 tenant 列,RLS 走父 FK)/`delete_for_threads(thread_ids, tenant_id)`/`revert_promoted_for_dataset(dataset_id, tenant_id)` 全文一致。
- 迁移链 0134→0135→0136 与波次(T5 波1、T8 波2、T9 波3)对齐,无并行冲突。
