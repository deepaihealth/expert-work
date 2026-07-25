# Task 8 报告(删除接口卫生 PR3):迁移 0135 curation 悬空回退 + FK RESTRICT + store 409 兜底

> 注:本路径原为 PR2 Task 8 报告(文件名撞车,PR2/PR3 各自独立编号),照 PR2 报告 concern #3 的既定先例直接覆盖;PR2 内容在 git 历史可查。

## 状态:完成

TDD 全程:4 个新测试先行为性红(①迁移 `ResolutionError: No such revision '0135_curation_candidate_fk'` ②`DID NOT RAISE IntegrityError` ③`DID NOT RAISE EvalDatasetInUseError` ④端点非 409),实现后全绿。

## worktree / 分支

- worktree:`/Users/mac/src/github/jone_qian/expert-work/.claude/worktrees/agent-a400321382fea6d26`
- 起手 `git merge --ff-only fix-deletion-hygiene-pr3` 成功(tip `006d1004`),拿到 0134 迁移(T5)、`revert_promoted_for_dataset` 双实现(T6)。

## 改动文件

### 迁移 `0135_curation_candidate_fk.py`(新建)

- `packages/expert-work-persistence/migrations/versions/0135_curation_candidate_fk.py`
- `down_revision = "0134_orphan_run_delivery_cleanup"`(打开 0134 核对的真实 revision id);revision id 26 字符,低于 `alembic_version` varchar(32) 长度闸(PR2 T8 教训)。`uv run alembic heads` 单头 = 0135。
- 两条 UPDATE 用 brief SQL 逐字,提为模块级常量 `_REVERT_DANGLING_PROMOTED_SQL` / `_CLEAR_DANGLING_POINTER_SQL`(ScriptDirectory SSOT 套路,照 0134 —— 审查者判定优于复制式):①悬空 PROMOTED 一条 UPDATE 同时回 `pending` + 清指针;②兜其余悬空指针。之后 `op.create_foreign_key("fk_curation_candidate_eval_dataset", "curation_candidate", "eval_dataset", ["eval_dataset_id"], ["id"], ondelete="RESTRICT")`。
- downgrade 只 drop FK;数据修正不可逆 by design(0132/0134 同姿态)。fresh 容器(pgvector/pgvector:pg16)head → downgrade -1 → head 往返验证通过。

### store 层 409 语义化

- `curation/base.py`:新增 `EvalDatasetInUseError`(照 `McpConnectorCatalogInUseError` 先例:`dataset_id` keyword-only + 语义 message + 属性保留);`EvalDatasetStore.delete` docstring 补契约(SQL 抛、in-memory 不模拟 FK,集成测试盯 SQL 侧)。
- `curation/sql.py`:`SqlEvalDatasetStore.delete` 的 try **覆盖 execute + commit**(非延迟 RESTRICT 在 execute 炸不在 commit —— PR2 T8 教训,全局约束;代码注释已写明),`IntegrityError → rollback → EvalDatasetInUseError`。
- 导出:`curation/__init__.py` + `persistence/__init__.py`(import + `__all__`,字母序插位)。
- `curation/memory.py` 未动(与 mcp catalog in-memory 现状一致)。

### 端点 catch → 409

- `services/control-plane/src/control_plane/api/curation.py` `delete_eval_dataset`:仅把 `datasets.delete(...)` 一行包进 try/except,`EvalDatasetInUseError → 409 {"code": "EVAL_DATASET_IN_USE", "message": ...}`(照 mcp_catalog.py `CATALOG_IN_USE` 先例)。落点与 Task 7 的"先回退"插入点(delete 之前)分离。正常路径不触发(注释已说明:绕过 app 层回退的写路径命中 FK 时的语义化出口,不再裸 500)。

## 测试

### 新建 `packages/expert-work-persistence/tests/test_curation_candidate_fk.py`(3 个集成测试)

1. `test_dangling_candidates_reverted_and_pointers_cleared` —— 数据修正:在**一个最终 ROLLBACK 的事务**里 DROP FK → 插悬空 PROMOTED / 悬空 DISMISSED(带指针)/ 非悬空 PROMOTED → 经 alembic `ScriptDirectory` 加载 0135 模块执行其 UPDATE 常量(单一事实源)→ 断言 ①回 `pending`+清指针 ②只清指针、状态不动 ③原样不动。**与 0134 测试的差异**:0135 是 DDL+数据修正混合迁移,head 状态下 FK 已生效、悬空行插不进去,故不能照抄 0134 的 AUTOCOMMIT 留数据写法——改用事务回滚,共享 session 容器的约束与数据原样恢复(Postgres DDL 可事务回滚)。
2. `test_fk_blocks_direct_sql_delete_of_referenced_dataset` —— FK 生效:绕 app 层直接 SQL DELETE 被引用 dataset → `pytest.raises(IntegrityError)`(RESTRICT 非延迟,DELETE 语句即炸)。
3. `test_store_delete_maps_restrict_to_in_use_error` —— 真 FK 下 `SqlEvalDatasetStore.delete` → `EvalDatasetInUseError` 且行保留;`revert_promoted_for_dataset` 清引用后 delete 返回 True(姊妹 `test_mcp_catalog_oauth_fk_restrict.py` 同型)。

### 新建 `services/control-plane/tests/test_curation_api_dataset_in_use.py`

独立文件(避开 Task 7 正在改的 `test_curation_api.py`,零冲突):delete 必抛 `EvalDatasetInUseError` 的 in-memory 子类驱动端点 catch 分支 → 409 + `detail.code == "EVAL_DATASET_IN_USE"` + 行未动。店内预 seed 真实行,对 Task 7 改后的端点形态(先回退再删)鲁棒。

### 修 2 处既有测试(FK 落地的直接后果,非范围蔓延)

`packages/expert-work-persistence/tests/test_sql_curation_store.py` 的 `test_candidate_update_records_promotion` 与 `test_revert_promoted_for_dataset_reverts_only_matching_rows` 原先写**悬空** `eval_dataset_id=uuid4()`(无 FK 时代的写法),0135 落地后必炸 —— 改为先 `datasets.create(...)` 真实父行再引用,语义断言全部原样保留(含 DISMISSED 哨兵/跨租户不动断言)。

### 变异自验(load-bearing,手工执行未固化进 CI)

0135 第一条 UPDATE 的 `NOT EXISTS` 翻成 `EXISTS` → 测试 1 变红(非悬空 PROMOTED 被误回退,`live_promoted` 保留断言失败);改回后复绿。SSOT 加载保证迁移文本变异测试必然看得见。

## 测试结果

```
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock

uv run pytest packages/expert-work-persistence/tests/test_curation_candidate_fk.py \
  packages/expert-work-persistence/tests/test_sql_curation_store.py -q
→ 16 passed

(services/control-plane) uv run pytest tests/test_curation_api_dataset_in_use.py tests/test_curation_api.py -q
→ 14 passed

uv run pytest packages/expert-work-persistence/tests/ -q
→ 914 passed, 2 failed, 3 errors —— test_rls_detect(顺序依赖)与 test_pgbouncer_integration
  为计划已知非回归红;test_sql_webhook_store 一例为 T5 测试污染(见 Concern 1),单跑绿。

uv run ruff check . → All checks passed!
uv run ruff format --check . → 1469 files already formatted(新测试文件 ruff format 就地修正一次)
uv run mypy packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src
→ Success: no issues found in 781 source files

alembic heads → 0135_curation_candidate_fk (head) 单头
fresh 容器 upgrade head → downgrade -1 → upgrade head → OK
```

## Concerns

1. **T5 测试污染共享容器(非本任务引入,T10 全量跑会撞)**:`test_orphan_run_delivery_cleanup.py`(0134,T5)用 AUTOCOMMIT 留下存活 `webhook_endpoint` 行不清理,同 session 后跑的 `test_sql_webhook_store.py::test_endpoint_list_enabled_all_tenants`(all-tenants 无过滤列举)失败。最小复现:`pytest test_orphan_run_delivery_cleanup.py test_sql_webhook_store.py` → 1 failed;两文件各自单跑均绿。修法建议:T5 测试收尾删掉自己插入的 live 行(或该断言改集合包含式)。超出 Task 8 范围,未动。
2. **ORM 模型未加 FK 属性**:`CurationCandidateRow.eval_dataset_id` 保持裸 UUID 列 —— 照 0133 先例(`mcp_oauth_connection.catalog_id` 的 ORM 至今仍写 `ondelete="CASCADE"`,DB 真身已是 RESTRICT):迁移是 schema 权威,ORM `ondelete` 只影响 DDL 生成。若终审想拉齐 ORM↔DB,两处应一起改(“同一语义分散多处”教训)。
3. **端点 catch 与 Task 7 同函数**:我只包装了 `datasets.delete(...)` 调用行;merge 时若 T7 重排函数体,保住"delete 调用被 try/except EvalDatasetInUseError 包住"即可。新端点测试对两种形态均鲁棒。
4. `test_rls_detect` / `test_pgbouncer_integration` 为计划已知非回归红,本机复现与计划一致。

## Commit

- `feat(persistence): 迁移 0135 curation 悬空回退 + eval_dataset FK RESTRICT 兜底`(含本报告,git add -f)
