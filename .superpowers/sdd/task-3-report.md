# Task 3 报告(PR3)—— purge_session 接线 approval 级联 + 失败审计可见 + 端到端测试

## STATUS: DONE

- worktree:`/Users/mac/src/github/jone_qian/expert-work/.claude/worktrees/agent-aea6649d653f1a473`,分支 `worktree-agent-aea6649d653f1a473`
- 起手 `git merge --ff-only fix-deletion-hygiene-pr3`(fast-forward `5928ad83..006d1004`,带入 T1/T2 成果)。
- Commit:`4670ce1c feat(control-plane): purge_session 级联 approval + 删除失败审计可见 + 补端到端测试`
- 注:本文件覆盖的旧 `task-3-report.md` 是 PR2 遗留(McpOAuthConnectionStore 报告),沿 PR1→PR2 既定覆盖惯例;旧版完整保留在 git 历史。

## 变更

### 1. `services/control-plane/src/control_plane/api/sessions.py`

- 新增 import `from expert_work.persistence.approval import ApprovalStore`(随本文件既有 submodule import 风格)。
- 新增依赖 getter `_get_approval_store`(照 runs.py:357 模式,读同一 `app.state.approval_store` 挂名),置于既有 provider 块。
- `purge_session`:
  - 签名新增 `approvals: Annotated[ApprovalStore, Depends(_get_approval_store)]`。
  - `deleted` 初始 `{"checkpoint": False, "runs": 0, "approvals": 0}`。
  - run 删除 except 分支加 `deleted["runs_delete_failed"] = True`。
  - 新增 approval 级联(run 删除后、thread_meta 删除前):`await approvals.delete_for_threads(thread_ids=[thread_id], tenant_id=tenant_id)`(keyword-only 关键字实参,按 T2 既定签名);except 分支 `deleted["approvals_delete_failed"] = True`。
  - 失败日志为静态串(`session_purge.approvals_failed`,无请求派生值——CodeQL py/log-injection 安全);新键经既有 `**deleted` 自动进审计 details 与响应 data。
  - docstring 更新:级联范围含 run_event 子行 + agent_approval 行;失败以 `*_delete_failed` 审计可见。

### 2. `services/control-plane/src/control_plane/purge/user_purge.py`

- `_purge_threads` run 删除 except 的旧行尾注释「run_event RESTRICT may block; anonymize catches survivors」已失效(T1 后 `delete_by_thread` 单事务自排空 run_event),改为块注释:RESTRICT 不再挡,anonymize 仅兜底 store 异常。纯注释改动,无行为变化。

### 3. `services/control-plane/tests/test_sessions_api.py`(端点原零覆盖)

新增「purge — hard-delete cascade」段,4 个端到端测试(ASGI HTTP 面 + `session_client._transport.app` 取 app.state 播种,照 test_runs_api 既有套路):

| 测试 | 断言 |
|---|---|
| `test_purge_cascades_runs_events_and_approvals` | 播种有事件 run(`run_store.create` + `run_event_store.append`)+ pending approval → purge 200;data `runs==1`/`approvals==1`;store 层 run/run_event/approval 全消失;HTTP 层 GET session 404、`GET /v1/runs?q=<tid>` items 空;审计 details `meta_removed=True`/`runs=1`/`approvals=1` |
| `test_purge_run_delete_failure_is_audit_visible` | monkeypatch `runtime.run_manager.delete_by_thread` 抛 → 仍 200 success;响应与审计 details 均 `runs_delete_failed: true`;thread_meta 仍被删(best-effort) |
| `test_purge_approval_delete_failure_is_audit_visible` | monkeypatch `approval_store.delete_for_threads` 抛 → 仍 200;响应与审计 details 均 `approvals_delete_failed: true` |
| `test_repeat_purge_returns_404` | 首次 purge 200;重复 purge 404(现语义,见 Concerns 1) |

## TDD 过程

1. 先写测试,真跑确认红:3 FAILED,失败点正是缺失的新键(`KeyError: 'approvals'`、`KeyError: 'runs_delete_failed'`);`test_repeat_purge_returns_404` 天然绿(锁定现状语义)。
2. 实现后全绿。

## 验证

- `uv run pytest services/control-plane/tests/test_sessions_api.py -q` → **41 passed**(37 存量 + 4 新增)。
- 关联面(全部含 `:purge` 的测试文件):`test_sessions_api.py + test_user_purge.py + test_runs_api.py` → **105 passed**。
- 全 control-plane 套件:**2070 passed**;6 failed(`test_eval_engine_live.py`)+ 18 errors(`*_integration.py` / `test_encrypted_secret_store.py`)——已 `git stash` 对照确认**全部改动前就存在**(live/Docker testcontainers 依赖,与本任务无关)。
- `uv run ruff check` + `uv run ruff format --check` 全库通过。
- CI 同款 mypy strict(packages + orchestrator 等 CI 范围)→ Success(control-plane 不在 CI mypy 范围,符合既有认知)。

## Concerns

1. **重复 purge 断言与计划括注偏差(按 brief 允许项处理)**:计划括注设想「threads.delete 返回 False 时端点不 404,断 `meta_removed: false`」。实测:首次 purge 删掉 thread_meta 后,第二次请求在 `_load_owned_session` 门(sessions.py:744-746)即 404——`meta_removed: false` 的 200 响应经 HTTP 面**不可达**(仅 gate 命中后 `threads.delete` 并发竞态返回 False 才可能)。brief 明确接受「404 或计数 0」,故断 404,测试 docstring 注明依据。
2. 计划文本的 `/v1/runs?thread_id=` 参数不存在——`GET /v1/runs` 只有 `q`(run_id/thread_id 子串匹配)。测试以 `?q=<thread_id>` 达成同一断言意图。
3. approval 失败注入是 brief 三测之外补充的第 4 测:`approvals_delete_failed` 分支为本任务新增代码,按全局约束「best-effort 清理失败必须审计可见」直接覆盖,成本一个 monkeypatch。
