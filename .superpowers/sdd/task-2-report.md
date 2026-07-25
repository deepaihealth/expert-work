# Task 2 Report — POST /v1/members/{member_id}:purge(PR5 组合端点,D1+D2)

## STATUS

DONE — TDD 先红后绿,7 测全绿,变异自验通过,已提交。

## Commits

- `9eb96081` feat(control-plane): 成员一键停用并清除端点(生命周期+KC 删账号+收权+数据清除)

## 变更文件

- `packages/expert-work-protocol/src/expert_work/protocol/audit.py` — MEMBER_SUSPEND 后加 `MEMBER_PURGE = "member:purge"`(带 PR5 注释;全库 grep 确认 member 系 action 字符串无前端/其他镜像,单源)。
- `services/control-plane/src/control_plane/api/members.py` — revoke 端点后新增 `POST /{member_id}:purge`:
  1. lifecycle:invited→revoked / active→suspended(转移失败 → **409 MEMBER_STATE_CONFLICT,零副作用**);suspended/revoked 补清路径不转移;
  2. role_binding 清理(照 revoke 块,`keycloak_user_id` 键,best-effort + 失败布尔);
  3. KC `delete_user`(D2,best-effort,`KeycloakUnavailableError` → `kc_delete_failed`;client 404=成功幂等);`keycloak_user_id is None` 跳过;
  4. 数据级联:`member.subject_id` 非 NULL → `users.get` 取 user 行拿字符串 subject → `purge_user(...)`;user 行缺失(异常态)→ `data_purged: false` 不炸;
  5. 审计 `MEMBER_PURGE`(details:email/from_status/kc_deleted/kc_delete_failed/role_bindings_removed/role_bindings_cleanup_failed/data_purged);
  6. 200 信封 `data = {member_id, status, kc_deleted, kc_delete_failed, role_bindings_removed, role_bindings_cleanup_failed, data_purged, purge}`(`purge` = PurgeSummary.as_dict() 或 null)——契约照 brief,T3 消费。
  - 复用 `agent_users._build_purge_deps`(同应用内私有共享,import 处加注释说明设计决定);日志全静态串 + `exc_info=True`。
- `services/control-plane/tests/test_members_api.py` — 状态机矩阵 7 测(brief ①–⑦逐字)+ 两个小 helper(`_invite_one` / `_activate_with_data`)。

## 测试矩阵(brief Step 1 ①–⑦)

| # | 用例 | 断言要点 |
|---|------|---------|
| ① | invited → revoked | KC 账号删(`len(kc.users)==0`)、`data_purged=false`、`purge=null`、binding 清 1、审计 details 全布尔/计数 |
| ② | active(subject_id+thread 数据)→ suspended | KC 删、`purge.threads_purged==1`、`purge.deactivated=true`、`purge.ok=true`、thread 行真消失、审计 `data_purged=true` |
| ③ | suspended 补清 | 状态不变、KC 删(suspend 时只 disabled 过)、数据照清 |
| ④ | 重跑幂等 | 200、binding=0、`purge.threads_purged==0`、`purge.ok=true`、无失败布尔 |
| ⑤ | KC 失败注入(monkeypatch 抛 KeycloakUnavailableError) | 200、`kc_deleted=false`+`kc_delete_failed=true`、其余步照走(状态转移+binding+数据全完成) |
| ⑥ | viewer | 403,KC 账号原封不动 |
| ⑦ | transition 注入返 False | **409 MEMBER_STATE_CONFLICT**;KC 账号在、binding 在、thread 在、无 MEMBER_PURGE 审计行(零副作用) |

- **红**:7 测先写,实现前全 FAILED(405 Method Not Allowed —— 路由不存在,失败原因正确)。
- **绿**:实现后 `test_members_api.py` 27 passed(20 存量 + 7 新)。

## 变异自验(brief Step 5,必做)

- 注掉 `await keycloak.delete_user(...)` 步 → **4 测红**:①②③(`len(kc.users)==0` 断言炸)+ ⑤(失败注入永不触发,`kc_delete_failed` 恒 False)。
- 恢复 → 全绿。KC 删除步被测试真实压住,①②的 KC 断言 load-bearing 确认。

## 验证摘要

- `test_members_api.py` 27 passed;`test_user_purge.py` 8 passed;三文件(含 `test_agent_users_api.py`)合计 48 passed。
- `packages/expert-work-protocol` 404 passed(枚举加项无回归)。
- `uv run ruff check .` 全库 All checks passed;`ruff format --check` 触及 3 文件 formatted。
- CI 同款 mypy strict(packages + 5 services)Success: no issues found in 783 source files。

## 实现备注 / concerns

1. **测试 app 无 supervisor**:`create_app` 只从 `settings.sandbox_supervisor_url`(测试为 None)建 supervisor client → purge 级联的 workspace 步会记 `failures["workspace"]="no supervisor client wired"`(与存量 `test_user_purge.py` 端点测同状况,该文件因此从不断言 ok)。测 ②④ 需断言 `purge.ok=true`,故在这两测内局部 `app.state.supervisor_client = RecordingSupervisorClient()`(不动共享 fixture)。**给 T3 的提示**:无 supervisor 的部署里 `purge.ok` 会因 workspace 步为 false——partial 判定读 `purge.ok === false` 时这是预期形状(与 /users 页 PurgeUserModal 行为一致,非本端点新问题)。
2. 幂等重跑时 `kc_deleted` 会再报 true(delete_user 404=成功语义),`data_purged` 也照 true(级联安全 no-op 重试)——布尔语义是"本次请求该步执行成功",非"本次真删了东西"。
3. 环境插曲:worktree 首次 `uv run` 时 hatchling 并行 build 被 SIGKILL(本机内存压力),`UV_CONCURRENT_BUILDS=1 uv sync` 后正常,与代码无关。
4. worktree 基于 `fix-deletion-hygiene-pr5`(75892f60,已 ff 合入);未触碰 T1 的 `purge/user_purge.py`,并行安全。本文件覆盖了 worktree 里残留的 PR4 旧 task-2-report(历史文档,PR4 已合并 #1051)。
