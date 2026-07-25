# Follow-ups Task 4 报告:成员清除可追责性 + 数据步 best-effort

STATUS: DONE

> 文件名说明:brief 说写 `.superpowers/sdd/task-4-report.md`,但该路径**已被 PR4 的
> Task 4 报告(MCP 引用检查修缮)占用且已入库**(8c264b26)。改用与本批兄弟任务一致的
> `followups-task-N-report.md` 命名,避免覆盖历史报告 / 与兄弟分支撞名。

## 交付

### ① 审计 `purge_ok` —— 三态可追责
`services/control-plane/src/control_plane/api/members.py` `purge_member` 的
`MEMBER_PURGE` emit details 增两键:

| 键 | 语义 |
|---|---|
| `purge_ok` | `None` = 没跑数据步(`subject_id` 为 NULL / registry 无行 / 数据步整体炸);`True` = 跑了且每个 store 都成功;`False` = 跑了但有 store 失败 |
| `data_purge_failed` | 数据步整体炸(解析/依赖装配抛异常)时 `True` |

原先只有粗布尔 `data_purged`,审计行分不清"跑了且全成功"与"跑了但 partial 失败"
——summary 本身不落库,事后无从复原。

### ② 数据步 best-effort 化
`users.get(...)` + `_build_purge_deps(request)` + `purge_user(...)` 整块包 try/except:
失败置 `data_purge_failed = True`、`data_purged` 保持 `False`、`purge` 保持 `null`,
`logger.warning("member_purge.data_purge_failed", exc_info=True)`(静态串 + exc_info,
与同端点其余 best-effort 步同形状),然后**照常落审计 + 返回 200**。

修复前:这两步在 `purge_user` 的 per-step 网**之外**,transient 失败会在 **Keycloak 账号
已删之后** 500 中断,留下"破坏性前缀 + 零审计行"。端点幂等,操作者重跑即补上数据步。

**未动既有 409 半态防护**:生命周期转移仍是唯一 BLOCKING 步(测试⑦
`test_purge_transition_conflict_409_blocks_all_side_effects` 原样绿)。

### ③ `PurgeSummary.ok` 单源
`services/control-plane/src/control_plane/purge/user_purge.py`:把 `as_dict()` 里的
`"ok": not self.failures` 抽成 `@property ok`,`as_dict()` 与 members.py 的审计 detail
共用一处。避免"同一语义两处各写一遍"的漂移(响应 `purge.ok` 与审计 `purge_ok` 永不打架)。

### ④ 前端(最小)
`apps/admin-ui/src/api/members.ts`:`MemberPurgeResult` 加可选 `data_purge_failed?: boolean`
(带注释)。**未动 `isMemberPurgePartial` 判定逻辑**(按 brief 要求),未动任何 UI。

## 响应契约(只增不改)

`data` 由 8 键 → 9 键,新增 `data_purge_failed`;既有 8 键语义/类型全未变。

## TDD 记录

**RED**(Step 1-2):`uv run pytest services/control-plane/tests/test_members_api.py -k purge`
→ **4 failed, 5 passed**,红因逐条核过:

| 测试 | 红因 |
|---|---|
| ①`test_purge_invited_member_revokes_and_deletes_kc_account` | `KeyError: 'data_purge_failed'` |
| ②`test_purge_active_member_suspends_deletes_kc_and_purges_data` | `KeyError: 'data_purge_failed'` |
| ⑧`test_purge_partial_cascade_records_purge_ok_false`(新) | `KeyError: 'data_purge_failed'` |
| ⑨`test_purge_data_step_failure_is_best_effort_and_audited`(新) | `RuntimeError: forced registry read failure (test)` —— 即 500 中断,正是要修的行为 |

**GREEN**(Step 3-4):`test_members_api.py` 全文件 **29 passed**。

## 变异自验(Step 5)

| 变异 | 期望 | 实测 |
|---|---|---|
| `"purge_ok": True if summary is not None else None`(恒 True) | 测试⑧红 | **1 failed, 8 passed**;唯一红 = `test_purge_partial_cascade_records_purge_ok_false`,`assert True is False` ✅ |

变异已恢复,终态全绿(29/29)。

## 新增/修改测试(`services/control-plane/tests/test_members_api.py`)

新增 2 个:
- **⑧ `test_purge_partial_cascade_records_purge_ok_false`** —— 接上
  `RecordingSupervisorClient`(让 workspace 步真跑,保证注入的失败是**唯一**失败),
  monkeypatch `app.state.memory_repo.delete_all_for_user` 抛 → 200 /
  `data_purged: true` / `data_purge_failed: false` / `purge.ok: false` /
  `failures` 含 `memory_item` / 审计 `purge_ok: false`。
- **⑨ `test_purge_data_step_failure_is_best_effort_and_audited`** —— monkeypatch
  `app.state.tenant_user_repo.get` 抛 → **200**(非 500)/ `data_purged: false` /
  `data_purge_failed: true` / `purge: null`;前置步如实记(`status: suspended` /
  `kc_deleted: true` / `role_bindings_removed: 1` / KC 账号确已删);数据未动
  (thread 行仍在,等重跑);**审计行照落**且 `purge_ok: None`。

既有 2 个补断言:①(无数据步)`data_purge_failed: false` + 审计 `purge_ok: None`;
②(全成功)`data_purge_failed: false` + 审计 `purge_ok: True`。

副作用不进 assert(审计断言走 `InMemoryAuditLogStore.query`,照既有套路)。

## 验证

- `uv run pytest services/control-plane/tests/test_members_api.py` → **29 passed**
- `+ test_user_purge.py + test_agent_users_api.py` → **51 passed**
- control-plane 全量 `uv run pytest services/control-plane/tests` → **2112 passed**,
  6 failed + 18 errors 全为**既有环境性失败**,与本改动无关:
  errors = 需 Docker 的 integration(未 export `DOCKER_HOST`);
  failures = `test_eval_engine_live.py` 的 `ModuleNotFoundError: No module named 'tools'`。
- `uv run ruff check services/control-plane packages apps` → All checks passed;
  `ruff format --check` → 396 files already formatted
- CI 同款 `uv run mypy packages …/src` → Success (783 files);
  另单跑 `mypy` 两个改动的 control-plane 文件 → Success
- `pnpm -C apps/admin-ui typecheck` → 通过(worktree 内先 `pnpm install --frozen-lockfile`)
- `pnpm -C apps/admin-ui exec vitest run src/pages/__tests__/SettingsMembers.test.tsx` → 9 passed

## Concerns / follow-up

1. **UI 不会把 `data_purge_failed` 显示成 partial**。`isMemberPurgePartial` 现只看
   `kc_delete_failed || role_bindings_cleanup_failed || purge.ok === false`;数据步整体炸时
   `purge` 是 `null`、前两者是 `false` → 前端会当**全成功**弹绿提示,而实际数据一行没清。
   按 brief 明令"不要改 partial 判定逻辑"我没动;**建议后续加一行**
   `|| result.data_purge_failed === true`(响应字段已就位,一行即可)。
2. `except Exception` 的宽口径与同端点其余 best-effort 步一致(role-binding 步同款);
   `HTTPException` 不会经过这里(数据步不抛它),故不存在吞掉 4xx 的路径。
