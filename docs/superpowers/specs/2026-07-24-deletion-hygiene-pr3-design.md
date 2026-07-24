# PR3 孤儿行级联:幽灵 run + 投递残留 + curation 悬空 + knowledge 竞态 — 设计文档

> 删除接口卫生修复计划第 3 批(共 5 批)。PR1(#1048 软删生命周期)、PR2(#1049 安全洞)已合并。
> 本批主题:删除父行不清子行导致的**孤儿行**,以及删除与后台任务的**竞态**。

## 背景(审计 + 侦察结论,均有代码证据,2026-07-24 按 main 复核)

1. **幽灵 run**:`sessions.py:845-850` purge_session 删 run 走
   `run_manager.delete_by_thread`,但 `run_event.run_id → agent_run.id` 是
   **ondelete=RESTRICT**(migration 0038):有事件的 run 整条 DELETE 被 DB 挡下,
   异常被裸 `except Exception` 吞成 warning → run 行残留,继续出现在 `/v1/runs`。
   `RunEventStore`(runtime `event_store.py`)**没有任何 delete 方法**,全仓无
   "按 run 清 run_event"入口。`purge_user._purge_threads` 同样被 RESTRICT 挡,
   靠后续 `anonymize_all_for_user` 把幸存 run 匿名兜底——不是真删。
   purge_session **现无任何测试覆盖**。
2. **审批行孤儿**:purge_session 不删 `agent_approval`(run_id/thread_id 均裸
   UUID 列无 FK)。`ApprovalStore` 只有 user 级 `delete_all_for_user`,无 thread
   级批删。
3. **trigger_run / webhook_delivery 孤儿**:`triggers.py:531` 删 trigger、
   `webhook_endpoints.py:315` 删 endpoint 都只删父行;`trigger_run.trigger_id`
   与 `webhook_delivery.endpoint_id` 均裸列无 FK。`delete_for_triggers` /
   `delete_for_endpoints` 双实现**已存在**(PR Phase 3a 为 purge_user 建),
   端点没接线。
4. **curation candidate 悬空**:`curation.py:437-458` 删 eval dataset 只删
   dataset 行;`curation_candidate.eval_dataset_id` 裸列无 FK。PROMOTED 是终态
   (promote/dismiss 均要求 PENDING,409),且模型校验 **PROMOTED 必须带
   eval_dataset_id**(protocol `eval_dataset.py:110-119`)——dataset 删后
   candidate 永久卡死:指针悬空、无回退路径、无法只清指针。store 无按 dataset
   批量回退方法。**勘误(写 plan 时复核)**:promote 端点是"从 candidate 铸
   一行新 eval_dataset"(每行一个样本,candidate 与铸出的行 1:1),不存在
   "promote 进已有 dataset",故无"promote 进已删 dataset"的新悬空入口——
   初版 §C 的 promote 校验项基于侦察误判,已删除。
5. **knowledge 删除 vs 在途向量化竞态**:`knowledge/sql.py:585-610` 删文档 =
   DELETE chunk + DELETE document,无守卫;`knowledge_chunk.document_id` 裸列
   无 FK(migration 0021)。后台 `KnowledgeIngestionRunner._run` 在
   `claim_document`(CAS 领活)之后、`replace_chunks` + `set_document_status`
   之前与删除交错:删除提交后在途任务把 chunk **重插到已删 document_id**(孤儿
   向量,DB 不拦),再对已删行发 0 命中状态 UPDATE。现有租约 CAS 只协调并发
   worker,防不了删后写回;无乐观锁列、无软删标记。

## 用户拍板(2026-07-24)

| # | 决策 | 结论 |
|---|------|------|
| D1 | dataset 删后 PROMOTED candidate | **回退 PENDING**:清指针 + 状态回 PENDING,样本回待审池可再 promote;存量悬空迁移一次性回退(PR2 D1 同先例) |
| D2 | knowledge 删除竞态 | **FK CASCADE + 写回守卫**:`knowledge_chunk.document_id` 补 FK(ondelete=CASCADE)+ 迁移顺清存量孤儿 chunk;ingest 写回抓 FK 异常优雅结束 |

## 设计

### A. purge_session 补全:run_event 排空 + approval 级联

1. `RunEventStore` Protocol 新增(SQL + in-memory 双实现,谓词逐字节一致):

```python
async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
    """Remove all events for the given runs. Returns rows removed."""
```

2. run 删除编排(plan 定稿):**`SqlRunStore.delete_by_thread` 单事务内
   先子查询排空 run_event 再删 agent_run**(原子:要么全删要么全留;
   `run_event` 无 tenant_id 列,RLS 走父 FK,子查询按父行过滤)。
   `InMemoryRunStore` 构造新增可选 `event_store` 注入(照既有
   `thread_meta_store` 注入先例),删除时调 `delete_for_runs` 保双实现
   语义平价。`purge_session` 与 `purge_user._purge_threads` 均经
   `run_manager.delete_by_thread`,零改动自动受益(匿名兜底保留;
   `test_purge_store_methods.py` 中 "run_event RESTRICT — never deleted"
   的断言随之更新)。
3. `ApprovalStore` 新增(照 feedback `delete_for_threads` 套路):

```python
async def delete_for_threads(self, *, thread_ids: Sequence[UUID], tenant_id: UUID) -> int
```

   purge_session 接线;purge_user 已有 user 级删,不动。
4. purge_session 的裸 except 收窄:run 删除失败不再静默——保留 best-effort
   (会话主体照删),但失败记入审计 details(`runs_delete_failed: true`),
   成功路径审计带 `runs_removed / run_events_removed / approvals_removed` 计数
   (PR2 教训:best-effort 失败必须审计可见)。
5. 补 purge_session 测试(现状零覆盖):有事件 run 的会话 purge 后
   agent_run/run_event/approval 全消失、`/v1/runs` 不再出现、幂等重跑。

### B. trigger / webhook 删除端点接线(纯接线)

1. `triggers.py` delete_trigger:删 trigger 行**前**调
   `trigger_runs.delete_for_triggers(trigger_ids=[id], tenant_id=...)`
   (先子后父,崩溃安全同 §A);审计 details 加 `runs_removed` 计数。
2. `webhook_endpoints.py` delete_endpoint:同型,
   `webhook_deliveries.delete_for_endpoints(...)`;审计 details 加
   `deliveries_removed`。
3. 存量孤儿(trigger/webhook 子行指向已删父行):**迁移 0134 一次性清**
   (D1 同先例;两表一个迁移,DELETE WHERE 父行不存在,downgrade no-op)。

### C. curation:回退 PENDING + FK RESTRICT 兜底(D1)

1. `CurationCandidateStore` 新增(SQL + in-memory 双实现):

```python
async def revert_promoted_for_dataset(self, *, dataset_id: UUID, tenant_id: UUID) -> int:
    """PROMOTED candidates pointing at the dataset → PENDING, pointer cleared."""
```

   谓词:`status == 'promoted' AND eval_dataset_id == dataset_id`;写
   `status='pending', eval_dataset_id=NULL`(`evolved_at` 等其余列不动)。
2. `curation.py` delete_eval_dataset:**先回退再删 dataset**(先删 dataset 后
   崩会重现悬空;先回退后崩 = dataset 还在、candidate 回池,可重 promote,
   重删幂等)。审计 details 加 `candidates_reverted` 计数。
3. **迁移 0135**:
   a. 存量悬空 PROMOTED(指向不存在 dataset)回退 PENDING + 清指针;
   b. 清其余悬空指针(非 PROMOTED 行的 `eval_dataset_id` 指向不存在 dataset
      → NULL,状态不动);
   c. `curation_candidate.eval_dataset_id` 加 FK → `eval_dataset.id`
      **ondelete=RESTRICT**(DB 兜底防未来绕过 app 层直删 dataset;不能用
      SET NULL——会制造"PROMOTED 带 NULL 指针"的非法态,读出即 pydantic 炸)。
   downgrade:drop FK;数据修正不可逆(与 0132 同姿态)。
4. app 删除顺序(先回退清指针再删 dataset)保证 RESTRICT 正常路径不触发;
   dataset store `delete` 对 IntegrityError 的包装照 PR2 T8 教训——try 必须
   覆盖 `execute`(RESTRICT 在 execute 炸,不在 commit)。

### D. knowledge:FK CASCADE + 写回守卫(D2)

1. **迁移 0136**:
   a. 清存量孤儿 chunk(`document_id` 无对应 knowledge_document 行);
   b. `knowledge_chunk.document_id` 加 FK → `knowledge_document.id`
      **ondelete=CASCADE**(chunk 是纯派生数据,随文档消失是意图)。
2. `delete_document` 保持显式 DELETE chunk + DELETE document(in-memory 无 FK,
   双实现行为一致靠显式;FK 只是兜底)。
3. ingest 写回守卫:
   - `replace_chunks`:文档已删时插入触发 ForeignKeyViolation → 抓住视为
     "文档已被删除,正常终止",不计失败、不重试、不留 FAILED 状态;
   - `set_document_status`:UPDATE 0 命中(行已删)→ 返回 False,调用方
     静默结束(现状已是事实 no-op,补上检测与日志级别 debug)。
   - in-memory knowledge store 镜像同语义:replace_chunks 对不存在的
     document 抛同类型异常/同结果(双 store 谓词一致命门)。
4. recovery worker 不动:行删了扫不到,天然安全。

### E. 审计与可观测

- 本批所有删除动作审计 details 带级联计数:§A `runs_removed /
  approvals_removed`(run_event 随 run 在同一事务原子消失,不单列计数)、
  §B `runs_removed` / `deliveries_removed`、§C `candidates_reverted`。
- 失败分支审计可见(PR2 教训):purge_session run 删除失败带
  `runs_delete_failed`。
- 日志不放请求派生值(PR2 CodeQL 教训:extra 也被 log-injection 追踪);
  定位靠审计 + 行 UUID。

## 错误处理

- §A/§B 级联删除失败:best-effort,主删除照走,失败进审计 details。
- §C 回退失败:**阻断** dataset 删除(否则重现悬空)——回退是删除的前置,
  非 best-effort。
- §D FK 异常在 ingest 写回是正常路径(文档已删),静默结束;其余异常照旧
  进重试/FAILED 通道。

## 测试

- store 双实现平价:`delete_for_runs` / `delete_for_threads`(approval)/
  `revert_promoted_for_dataset` + 真容器集成(改 SQL 约束必跑 integration,
  in-memory 不校验 FK)。
- purge_session 端到端(新增,现状零覆盖):见 §A5;含 run 删除失败注入 →
  审计 `runs_delete_failed`。
- trigger/webhook 删除端点:子行消失 + 审计计数;空子行时计数 0。
- 迁移 0134/0135/0136 集成测试:造孤儿 → upgrade → 孤儿没了 / 悬空 PROMOTED
  回退且非悬空行不动 / FK 生效(RESTRICT 拦直删 dataset、CASCADE 随删 chunk)。
- knowledge 竞态模拟:删除文档后直接调 `replace_chunks` → SQL 侧 FK 异常走
  优雅终止路径、无孤儿 chunk;in-memory 同语义。`set_document_status` 0 命中
  返回 False。
- 变异自验:破坏 §C 回退谓词(status 条件改永真)→ "非 PROMOTED 行不动"
  测试红;破坏 §A 排空顺序(先删父)→ RESTRICT 集成测试红。

## 范围外

- PR4(删除前置检查:平台模板 extends / agent 软删僵尸 trigger / mcp server
  servers-留空缝隙)/ PR5(成员页员工清除入口)。
- agent_run 账单语义调整:token_usage 才是账单真源,本批维持"purge 即删 run"
  的既有意图,不新增匿名保留开关。
- curation candidate 的用户侧"promote 被回退"通知(审计留痕,平台无通知设施)。
