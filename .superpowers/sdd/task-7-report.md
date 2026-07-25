# Task 7 Report: delete_eval_dataset 先回退再删 + 审计(依赖 T6)

## STATUS
DONE

## Commit
- `751de14a` `feat(control-plane): 删 eval dataset 先回退 promoted candidate(悬空修复)`(worktree 分支,基于 `006d1004` ff 同步 fix-deletion-hygiene-pr3 后提交)

## 改动文件
- `services/control-plane/src/control_plane/api/curation.py` — `delete_eval_dataset` 端点:
  - 注入 `candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)]`(getter 复用同文件既有 `_get_curation_store`)。
  - **删除前**调 `await candidates.revert_promoted_for_dataset(dataset_id=dataset_id, tenant_id=tenant_id)`,回退异常**不捕获**(上抛阻断删除;代码内注释写明理由:先删后崩重现悬空,先回退后崩安全——dataset 行还在、candidate 可再 promote)。
  - 既有 `emit` 加 `details={"candidates_reverted": reverted}`(该 emit 原本无 details)。
  - 回退发生在 404 存在性判定**之前**(照 brief/spec「先回退再删」字面顺序;对已悬空的重删场景是自愈行为,重删幂等——回退无匹配行计 0)。
- `services/control-plane/tests/test_curation_api.py` — 新增一节 5 个测试 + fixture 小改:
  - fixture:`InMemoryAuditLogStore` 实例保留进 `_Ctx.audit_store`(原先匿名传入无法断言 details);新增 `AuditQuery` / `UUID` 导入。
  - ① `test_delete_eval_dataset_reverts_promoted_candidate`:promote 铸 dataset → 删 → candidate 回 `pending`、`eval_dataset_id is None`、可再 promote 且铸出**新** dataset id。未断言 `reviewed_at`(T6 既定保留)。
  - ② `test_delete_eval_dataset_leaves_dismissed_candidate_alone`:dismissed candidate 在同租户另一 candidate 的 dataset 被删后状态不动。
  - ③ `test_delete_eval_dataset_audit_counts_reverted`:审计 `eval_dataset:delete` 行 `details["candidates_reverted"] == 1`。
  - ④ `test_delete_eval_dataset_without_candidate_counts_zero`:golden dataset(无关联 candidate)删除照常 200、计数 0。
  - ⑤(变异哨兵,brief 之外补)`test_delete_eval_dataset_blocked_when_revert_fails`:monkeypatch revert 抛 RuntimeError → 异常穿透(ASGITransport `raise_app_exceptions` 默认 True,以 `pytest.raises` 断言)+ **dataset 行仍在**。此测试专门钉死「先回退再删 + 不捕获」全局约束——前 4 个测试杀不掉「先删后回退」变异体(revert 按 candidate 行匹配,与 dataset 行存亡无关,换序照样全绿)。

## TDD 过程
1. Step 1-2(红):先写 ①②③④,真跑红——① `assert 'promoted' == 'pending'`、③④ `KeyError: 'candidates_reverted'`;② 天生绿(dismissed 无指针本就不会被扫),留作回归卫兵。
2. Step 3-4(绿):实现后全文件 17 passed。
3. **变异自验(已做)**:把实现换序成「先删 dataset 后回退」→ 哨兵⑤红(dataset 行在异常前已被删)→ 恢复正确顺序 → 18 passed。确认 ⑤ 是唯一能杀该变异体的测试。

## 测试摘要
`uv run --group dev pytest services/control-plane/tests/test_curation_api.py` → **18 passed**(含既有 13 个回归);`services/control-plane/tests/test_tenant_scope_endpoints.py`(另一处打 `/v1/eval-datasets` 的测试文件)→ 45 passed;`ruff check .` 全库 All checks passed;`ruff format --check` 两改动文件干净。CI-scope mypy 不含 control-plane(`.github/workflows/ci.yml:75`),本任务改动全在 control-plane,未跑。

## 事实核查(实现依据)
- `app.state.curation_candidate_store` 在 `create_app` 里**无条件**赋值(`app.py:1948`,injected repo 或 lifespan 构建二选一),eval-dataset router 挂载处依赖必可解析,无第三处 wiring 断裂。
- 审计 details 断言姿势照 `test_agents_api.py` / `test_admin_api.py` 先例(`audit_store.query(AuditQuery(tenant_id=...))` + `entry.details[...]`);`AuditAction.EVAL_DATASET_DELETE.value == "eval_dataset:delete"`。
- 默认 redactor 不动 int 计数(先例:`grace_period_s`/`revision` 等 int details 均可直读断言)。

## Concerns
- 无阻塞。两点提示终审:
  1. 回退在 404 判定之前 → 对不存在 dataset 的 DELETE(404 响应)也会执行回退。正常路径回退计 0 无副作用;唯一有副作用的场景是存量悬空 PROMOTED(指向已删 dataset)被重删触发自愈回 PENDING——与 Task 8 迁移 0135 的存量清理方向一致,视为良性。若终审认为 404 必须零副作用,可把回退挪到 `datasets.get` 存在性预检之后(需多一次读)。
  2. 测试⑤是 brief 四测之外的补充(变异自验发现前 4 测杀不掉换序变异体),行为约束直接来自计划 Global Constraints,非 scope 外新需求。
