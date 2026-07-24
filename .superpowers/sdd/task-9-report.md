# Task 9 报告:迁移 0136 knowledge FK CASCADE + ingest 写回守卫

**STATUS: DONE**
**Commit:** `eb4b5665 feat(knowledge): chunk FK CASCADE + ingest 写回守卫(删除竞态修复)`(10 files,+509/−21)

## 交付内容

### 1. 迁移 0136(`packages/expert-work-persistence/migrations/versions/0136_knowledge_chunk_fk.py`)
- `down_revision = "0135_curation_candidate_fk"`(打开 0135 核对的真实 revision id)。
- 存量孤儿 chunk 一次性 DELETE(brief SQL 逐字,提为模块级常量 `_DELETE_ORPHAN_CHUNK_SQL` —— 0134/0135 的 ScriptDirectory SSOT 套路)。
- `fk_knowledge_chunk_document`:`knowledge_chunk.document_id → knowledge_document.id ON DELETE CASCADE`;downgrade 只撤 FK,数据修复不可逆(同 0134/0135 姿态)。

### 2. store 三处(`knowledge/base.py` / `sql.py` / `memory.py`)
- `set_document_status -> bool`:SQL 版 `rowcount > 0`;in-memory 版文档缺失 `False`、更新后 `True`;抽象签名 + docstring 同步。现有调用点(ingestion/recovery)不消费返回值,零传染。
- in-memory `replace_chunks`:文档不存在 `raise KeyError(document_id)` 镜像 SQL 侧 FK `IntegrityError`;`base.py` 抽象 docstring 写明双实现的拒绝语义。
- SQL `delete_document` 显式 DELETE chunk 保留未动(FK 只是兜底),`sql.py` 的 `replace_chunks` 未动 —— 均照 brief。

### 3. ingest 写回守卫(`services/control-plane/src/control_plane/knowledge/ingestion.py` `_run` except 分支)
- brief 代码逐字:失败后查 `get_document`,`gone → logger.debug + return`(不 mark failed);文档还在 → 照旧 `mark_document_failed_terminal`。守卫在 mark 之前,`finally` 的 tenant context reset 不受 return 影响。
- 唯一偏差:except 行追加 `# noqa: S110`(ruff S110 try-except-pass,照 `agent_factory.py:1274` 的仓库先例),pragma 注释保留。

### 4. 测试
- 新增 `packages/expert-work-persistence/tests/test_knowledge_chunk_fk.py`(集成,3 测):
  - 孤儿 chunk 清理(DROP FK → 插孤儿+有主 → 执行迁移模块常量 → 孤儿删/有主留,事务回滚还原共享容器);
  - FK CASCADE(直接 SQL 删 document 行 → chunk 随删);
  - 删除竞态(store `delete_document` 后 `replace_chunks` 抛 `IntegrityError` 且无孤儿行,收尾 `delete_base` 清理)。
- `test_sql_knowledge_store.py` / `test_in_memory_knowledge_store.py`:`set_document_status` 存在 True/已删 False 两侧同型;in-memory "删文档后 replace_chunks 抛 KeyError 且无孤儿"与 SQL 侧同型(docstring 互相引用)。
- `test_knowledge_ingestion.py`:`_ConcurrentDeleteStore`(replace_chunks 内删文档再抛,精确模拟竞态)→ 不 mark failed、静默结束;`_FailingReplaceStore`(文档还在)→ 照旧 mark failed(error 断言)。
- 连带修正:in-memory 既有 10 个测试用裸 `uuid4()` 当 document_id 直插 chunk —— FK 语义下先 `upsert_document` 再插(最小 diff:声明行改一行,用法不动)。

## TDD 记录

- **RED**(实现前真跑):in-memory 2 新测 FAIL(返回 None / 不抛);ingestion gone 分支 FAIL(mark failed 被调);集成 4 FAIL(0136 revision 不存在的 RED gate / chunk 不随删 / 无 IntegrityError / set_document_status 返 None)。"还在→mark failed"测试实现前即绿(回归护栏)。
- **GREEN**:上述全绿(见下测试摘要)。
- **变异自验 ①(brief Step 5,必做)**:`_run` gone 判定改永 `False` → `test_ingest_document_deleted_mid_flight_is_not_marked_failed` 红(mark failed 被调);恢复后复绿。
- **变异自验 ②(迁移 SSOT,手工)**:0136 `NOT EXISTS` 改 `EXISTS` → 孤儿清理测试红(孤儿存活断言失败);恢复后 3/3 复绿,`grep` 核对文本还原。

## 测试摘要

persistence 单元 604 passed;persistence 集成 318 passed(仅 test_pgbouncer_integration 3 errors,环境性:其 fixture `docker compose up --wait` 起整套 infra 栈失败,发生在任何 SQL 之前,与本任务无关);control-plane 单元 2077 passed + 6 failed(test_eval_engine_live.py `No module named 'tools'`,**stash 后同样 6 failed,确认存量环境问题**);knowledge 全 scope(ingestion/api/e2e/recovery)44 passed;orchestrator test_knowledge_tool passed;`ruff check` / `ruff format --check` / CI-scope mypy(783 files)全过。

## Concerns

1. **recovery worker 同一竞态未加守卫(有意留白,brief 范围外)**:`recovery.py::_drive` 失败分支在 attempts 耗尽时也会对已删文档调 `mark_document_failed_terminal` —— 但那是 UPDATE、匹配 0 行、无复活无孤儿,只多一轮空转日志;FK 本身已挡住孤儿写回。如要日志干净可在 PR3 后续补同款守卫。
2. **in-memory `replace_chunks` 空 chunks 也校验文档存在**:SQL 侧空 insert 不触 FK(静默成功),in-memory 按 brief 无条件抛 KeyError —— 对已删文档写空集时两侧行为有毫米级差异(生产路径 `ingest_document_bytes` 空文档 + 并发删,两侧最终均为"无行无 mark failed",结果一致)。按 brief 字面实现,记录备查。
3. `models/knowledge.py` 模块 docstring 原话"tables 间无 FK"被 0136 证伪,已顺手改为"0021 原无 FK,0136 补 chunk→document CASCADE 兜底"(老注释说谎教训);ORM 列本体照 0135 先例不加 `ForeignKey`(FK 只存在于迁移层)。
