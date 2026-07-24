# Task 4 Report: trigger / webhook 删除端点接线(纯接线)

**STATUS**: DONE

**Commit**: `05c605ab feat(control-plane): trigger/webhook 删除级联投递行(接线既有 delete_for_* 方法)`(worktree 分支 `worktree-agent-a523a219169424791`,基于 `333a541c` = fix-deletion-hygiene-pr3 tip,`git merge --ff-only` 同步成功)

## 改动

### 实现(2 文件)

- `services/control-plane/src/control_plane/api/triggers.py`
  - `delete_trigger` 签名新增 `trigger_runs: Annotated[TriggerRunStore, Depends(_get_trigger_run_store)]`(getter 复用既有 :235)。
  - 权限检查后、`triggers.delete` **前**调 `trigger_runs.delete_for_triggers(trigger_ids=[trigger_id], tenant_id=tenant_id)`(先子后父,崩溃安全,带注释说明)。
  - `TRIGGER_DELETE` emit 补 `details={"runs_removed": runs_removed}`。
- `services/control-plane/src/control_plane/api/webhook_endpoints.py`
  - 新增 `_get_delivery_store` getter(读 `app.state.webhook_delivery_store`,照 :128 `_get_store` 模式);import 补 `WebhookDeliveryStore`。
  - `delete_endpoint` 签名新增 `deliveries` 依赖;`store.delete` **前**调 `deliveries.delete_for_endpoints(endpoint_ids=[endpoint_id], tenant_id=tenant_id)`。
  - `WEBHOOK_ENDPOINT_DELETE` emit 补 `details={"deliveries_removed": deliveries_removed}`。

### 测试(2 文件,TDD:先红后绿)

- `services/control-plane/tests/test_triggers_api.py`(+2 测试 + 2 小 helper)
  - `test_delete_trigger_cascades_trigger_runs`:删 trigger 后其 2 条 trigger_run 全消失、他 trigger 的 1 条不动、审计 `details == {"runs_removed": 2}`。
  - `test_delete_trigger_without_runs_audits_zero`:0 子行时 `details == {"runs_removed": 0}`。
- `services/control-plane/tests/test_webhook_endpoints_api.py`(+2 测试 + 2 小 helper)
  - `test_delete_endpoint_cascades_deliveries`:同型,`deliveries_removed == 2`,他 endpoint 的不动。
  - `test_delete_endpoint_without_deliveries_audits_zero`:0 子行时计 0。
  - 造子行直接走 `app.state.*_store.create(...)`(照文件内 `app.state.trigger_run_store` 既有用法);审计断言用类型化 `AuditQuery` 查询(CI mypy 扫 tests 教训,避免 object 上加 ignore 链)。

## 验证

- **红**:4 个新测试实现前全 FAIL,失败原因正确(孤儿行残留于 `list_by_trigger` / `list_by_endpoint`)。
- **绿**:`uv run --group dev pytest services/control-plane/tests/test_triggers_api.py services/control-plane/tests/test_webhook_endpoints_api.py` → **45 passed**(含全部既有测试,无回归)。
- `uv run ruff check` → All checks passed;`uv run ruff format --check` → 1464 files already formatted。
- CI mypy 范围不含 control-plane src/tests(ci.yml:75),不适用本改动。
- 全库 grep 确认无其他测试断言这两个 delete 审计或调用这两个 DELETE 端点(无隐性回归面)。

## Concerns

1. **webhook 404 路径下级联先跑**:`delete_endpoint` 没有先 `get` 的存在性检查(既有结构如此),对不存在的 endpoint_id,`delete_for_endpoints` 会先跑(no-op:delivery 行 tenant_id + endpoint_id 双条件过滤,无越租户风险),然后才 404。行为无害且符合 brief「放 store.delete 前」,未加额外前置 get(surgical)。
2. 日志约束不适用:本 task 未新增任何 log 语句;审计 `details` 只放整型计数,非请求派生字符串。
3. 本报告文件原有 PR2 时代 task-4 旧内容(迁移 0132 报告,另一 worktree 遗留),已按本次任务指令覆写。
