# Task 6 Report: CurationCandidateStore.revert_promoted_for_dataset(双实现)

## STATUS
DONE

## Commit
- `d102f5d9` `feat(persistence): curation candidate 按 dataset 回退 PENDING(双实现)`(worktree 分支,基于 `333a541c` ff 同步 fix-deletion-hygiene-pr3 后提交)

## 改动文件
- `packages/expert-work-persistence/src/expert_work/persistence/curation/base.py` — `CurationCandidateStore` 追加抽象方法 `revert_promoted_for_dataset(*, dataset_id: UUID, tenant_id: UUID) -> int`(置于 `update` 与 `anonymize_all_for_user` 之间),docstring 写明谓词、一次性双字段写入(PROMOTED 无指针是非法中间态)、其余列不动、回退后可再 promote 铸新行。
- `packages/expert-work-persistence/src/expert_work/persistence/curation/sql.py` — `SqlCurationCandidateStore` 实现:UPDATE 谓词 `tenant_id == tenant_id AND eval_dataset_id == dataset_id AND status == CandidateStatus.PROMOTED.value`,values `status=PENDING.value, eval_dataset_id=None`;事务模式照同文件 `anonymize_all_for_user`(stmt 构建 → session.execute → commit → rowcount)。
- `packages/expert-work-persistence/src/expert_work/persistence/curation/memory.py` — `InMemoryCurationCandidateStore` 实现:同序三条件谓词(`tenant_id` → `eval_dataset_id` → `status is CandidateStatus.PROMOTED`),命中行 `model_copy(update={"status": PENDING, "eval_dataset_id": None})` 一次性改两字段(不分两步,frozen 模型校验 PROMOTED 必带指针)。
- 测试两文件同型新增 `test_revert_promoted_for_dataset_reverts_only_matching_rows`(`packages/expert-work-persistence/tests/test_in_memory_curation_store.py` / `test_sql_curation_store.py`):2 行 PROMOTED→D1 回退成 PENDING+无指针且 `reviewed_at` 不动;PROMOTED→D2 不动;**DISMISSED→D1 不动(变异哨兵)**;他租户 PROMOTED→D1 不动;返回计数 2。

## TDD 过程
1. Step 1-2(红):先写两侧测试,真跑确认红——in-memory `AttributeError: 'InMemoryCurationCandidateStore' object has no attribute 'revert_promoted_for_dataset'`;SQL 同型 AttributeError(真容器起来后才炸,非 fixture 层)。
2. Step 3-4(绿):实现后两文件全绿。
3. **Step 5 变异自验(已做,两侧都做)**:
   - in-memory 去掉 `status is PROMOTED` 谓词 → 哨兵红:`assert 3 == 2`(DISMISSED 行被扫进计数)→ 恢复后绿。
   - SQL 去掉 `status == PROMOTED.value` 谓词 → 哨兵红:`assert 3 == 2`(真 Postgres 容器)→ 恢复后绿。

## 测试摘要
`DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest packages/expert-work-persistence/tests/test_in_memory_curation_store.py packages/expert-work-persistence/tests/test_sql_curation_store.py` → **33 passed**(含 SQL 集成真容器);`uv run ruff check .` 全库 All checks passed;`ruff format --check` 干净(formatter 折了测试里一根 101 字符长行);CI-scope `mypy` strict → Success, 777 files。

## 事实核查(实现依据)
- `eval_dataset_id` 列无 FK(migration `0034_eval_dataset.py:75` 裸 UUID 列)→ 悬空指针前提成立,SQL 测试直接用裸 uuid4 当 dataset id(与既有 promotion 测试同法)。
- pydantic 校验(`protocol/eval_dataset.py:110-119`)只要求 非PENDING带reviewed_at + PROMOTED带eval_dataset_id;DISMISSED 带指针合法 → 哨兵行可构造;回退后 PENDING+reviewed_at 合法,round-trip 无碍。
- 全库仅 Sql/InMemory 两个 `CurationCandidateStore` 子类,新抽象方法无第三处断裂。

## Concerns
- 无阻塞。仅提示 Task 7 接线:回退行保留 `reviewed_at`(历史),status 回 PENDING——review UI 若按「PENDING 必无 reviewed_at」假设渲染需留意(现有代码未见此假设,列出仅供终审)。
