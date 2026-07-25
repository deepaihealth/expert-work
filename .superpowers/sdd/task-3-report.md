# Task 3 报告(PR4)—— delete_agent 级联 + runs 两处 410 语义

## STATUS: DONE(TDD 先红后绿 + 变异自验通过)

- 注:本文件覆盖的旧 `task-3-report.md` 是 PR3 遗留(purge_session 报告),沿既定覆盖惯例;旧版完整保留在 git 历史。
- 起手 `git merge --ff-only fix-deletion-hygiene-pr4`(fast-forward `d2ae1fcb..9bcd2848`,带入 T1 `disable_for_agent`)。

## 变更

### 实现

- `services/control-plane/src/control_plane/api/agents.py`
  - `delete_agent` 新增 Depends:`run_store` / `runtime` / `triggers`(新增本地 getter `_get_trigger_store` 读 `app.state.trigger_store`,照 triggers.py:232 模式);import 增 `TriggerStore`。
  - 软删成功、build cache 失效后、MANIFEST_DELETE 审计前插入级联(brief Step 3 代码逐字落地):
    - 取消在飞 run:`run_store.list_running_for_agent` → 每条 `runtime.run_manager.cancel(...) or run_store.request_cancel(...)`(照 `disable_agent` RT-ADR-17 套路),每成功一条 SESSION_CANCEL 审计(reason=`"agent_deleted"`)。整段 try/except:失败 → `logger.warning("agent_delete.runs_cancel_failed", exc_info=True)` + `details["runs_cancel_failed"] = True`,删除不受阻。
    - 禁用 trigger:`triggers.disable_for_agent(agent_name=name, agent_version=version, tenant_id=tenant_id)`(T1 接口,版本级)。失败同型 → `triggers_disable_failed: true`。
  - MANIFEST_DELETE 审计 details 合入 `runs_cancelled` / `triggers_disabled` 计数 + 失败布尔(仅失败时出现)。`trace_id` 函数头取一次,SESSION_CANCEL 与 MANIFEST_DELETE 共用(照 disable_agent)。
- `services/control-plane/src/control_plane/api/runs.py`
  - protocol import 增 `AgentSpecStatus`。
  - 两处 `agent_repo.get` 改 `include_deleted=True` + `status is DELETED` → 410 `{"code": "AGENT_DELETED", "message": ...}`、None → 404 照旧(brief 410 结构逐字):
    - `trigger_run`(起 run / 会话发消息,原 :1002-1010)。
    - `resolve_approval_decision`(审批续跑,原 :643-647;404 detail 顺带带上 name@version,同型)。

### 测试(先写、确认红、实现转绿)

- `services/control-plane/tests/test_agents_api.py` 新增级联段:
  - `cascade_ctx` fixture(照 test_agent_disable_api.py `_Ctx` 套路:threads/run_store 共享 + `stub_agent_runtime` + 可 introspect 的 audit store)。
  - ① `test_delete_disables_only_this_versions_triggers`:目标 trigger 变 disabled,他 agent / 他 version 不动;details `triggers_disabled==1`、`runs_cancelled==0`、无失败布尔。
  - ② `test_delete_cancels_in_flight_runs`:RUNNING run → INTERRUPTED;恰一条 SESSION_CANCEL(reason=`agent_deleted`、resource_id=run_id);details `runs_cancelled==1`。
  - ③ `test_delete_survives_trigger_disable_failure`:monkeypatch `disable_for_agent` 抛 → 仍 204 + `triggers_disable_failed is True` + `triggers_disabled==0`。
  - ③b `test_delete_survives_run_cancel_failure`:monkeypatch `list_running_for_agent` 抛 → 仍 204 + `runs_cancel_failed is True` + `runs_cancelled==0`(Global Constraint:两个 best-effort 失败布尔都有测试咬住)。
- `services/control-plane/tests/test_runs_api.py` 新增 410 段(测试④):
  - `test_run_on_deleted_agent_returns_410` / `test_resume_on_deleted_agent_returns_410`:删 agent 后起 run / 审批续跑 → 410,body `detail.code == "AGENT_DELETED"`(control-plane 无自定义 exception handler,FastAPI 默认 `{"detail": {...}}` 包裹)。
  - `test_run_on_never_registered_agent_still_404` / `test_resume_on_never_registered_agent_still_404`:seed 绑到从未注册 agent 的 thread(`_seed_ghost_thread` 直插 thread_meta)→ 404 照旧。
- 波及修复:`tests/test_resume_idempotency_flow.py` 与 `tests/test_approval_timeout_sweep.py` 的 `_FakeAgentRepo.get` 桩不接受新 `include_deleted` kwarg、返回值无 `status` 属性(9 测 TypeError 红)→ 桩补 `include_deleted: bool = False` + `status=AgentSpecStatus.ACTIVE`,与 `AgentSpecStore.get` 真契约对齐(修桩非削测试)。

## TDD 记录

1. RED:6 个新行为测试失败且失败原因正确(trigger 仍 enabled / 410 处仍返 404 `agent code-reviewer@1.0.0 not found`);2 个 404-保持测试在旧代码上即绿(语义未变,预期内)。
2. GREEN:实现后 `test_agents_api.py + test_runs_api.py` 85 passed。

## 变异自验(brief Step 5,必做项)

- 变异:runs.py 两处 410 status 判定改永假(`if False and record.status is ...` / `if False and spec_record.status is ...`)。
- 结果:`test_run_on_deleted_agent_returns_410` 与 `test_resume_on_deleted_agent_returns_410` 双双红(`assert 404 == 410`),两个 404 测试仍绿 → 测试④确实咬住 410 判定,非空转。
- 恢复后同组 4/4 绿。

## 验证

- `uv run pytest services/control-plane/tests/test_agents_api.py services/control-plane/tests/test_runs_api.py -q` → **85 passed**
- 影响面 10 文件(agents/runs/sessions/agent_disable/agents_run_for_user/resume_idempotency_flow/approval_timeout_sweep/approvals/orphan_sweep/run_queue_worker)→ **177 passed**(修桩后;全量套件由 T5 终门统一跑)
- `uv run ruff check .`(全库)All checks passed;`uv run ruff format --check .`(全库)通过
- CI mypy 范围不含 control-plane(ci.yml:75),无新增扫描面

## 设计说明 / 注意点

- **RunInfo 无 agent_version 字段**(`packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py:66`;run→agent 绑定经 thread_meta join,只有 agent_name)→ 取消范围保持 **name 级**(brief 预案:宁可多取消不留残留)。同名他版本的在飞 run 也会被取消;trigger 禁用则是精确的 name+version 级。测试①同时锁住 trigger 的版本精确性。
- 审批续跑路径的 410 在 `mark_decided` CAS **之后**抛出(approval 决定已消费)——与改动前该处 404 的时序一致,只是状态码更精确,未引入新语义。
- 日志两条失败告警不含任何请求派生值,仅事件名 + `exc_info`;副作用调用均先赋值再断言,未进 assert。

## Commit

`feat(control-plane): agent 软删级联(禁 trigger+取消在飞 run)+ 已删 agent 起 run 410`
