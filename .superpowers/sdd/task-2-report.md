# Task 2 报告(PR4)—— 平台模板删除继承者反查 + 409(D1,无 force)

## 状态
DONE

注:本文件路径沿历史惯例整篇覆盖(此前内容是 PR2 Task 2 —— SecretStore.delete
原语 —— 的报告,与本 PR4 Task 2 无关)。

## worktree / 分支
- worktree:`/Users/mac/src/github/jone_qian/expert-work/.claude/worktrees/agent-a5ba7c37679b1f074`
- 起手 `git merge --ff-only fix-deletion-hygiene-pr4`(d2ae1fcb → 8e5b549d,快进,仅带入 PR4 计划/设计文档,无冲突)。

## 交付

`DELETE /v1/platform/agent-templates/{name}/{version}` 在 `store.delete` 前、同一
`bypass_rls_session()` 内做跨租户 extends 反查;有存活继承者 → 409
`TEMPLATE_IN_USE`(detail 结构逐字采用 brief 伪码:`code` / `message` /
`dependents_total` 精确值 / `dependents` cap 20)。409 路径不发删除审计、不
invalidate;无继承者路径既有删除审计 details 增 `dependents_checked: True`。

## 关键判定(brief 留给实现者核实的点)

- **extends 访问链**:`AgentSpecRecord.spec` 是 `AgentSpec`,`extends` 声明在
  `AgentSpecBody`(protocol `agent_spec.py:1153` 一带)→ 实际链为
  `record.spec.spec.extends`(与同文件 `_reject_extends` 的 `spec.spec.extends`
  一致)。brief 伪码的 `s.spec.extends` 相应校正。
- **latest 可解析谓词**:构建侧 `app.py _platform_template_resolver`(供
  `runtime.py:548 _resolve_template_extends` 消费)对 `"latest"` 用
  `get_latest(name, status=PUBLISHED)` —— 不看 `enabled`(那是 fork 入口的额外
  闸,非构建闸)。故 `target_is_last_resolvable` = `list_versions(name)` 中不存在
  **另一个**(version ≠ 被删版本)status=PUBLISHED 的版本。误拦双向断言见测试④:
  删非最后 PUBLISHED 版本 → 204;删最后一个 → 409。
- **fail-closed**:反查中的 store 调用异常一律不捕获(仅 `parse_extends_ref` 的
  `ValueError` 按伪码 continue 吞),反查失败即删除失败。
- 分页 `limit=200` 循环扫全量 `list_all_tenants`;软删(status=DELETED)继承者
  跳过。

## TDD 记录

- **Step 1/2(RED)**:新增 5 个测试(6 断言场景);实现前
  `4 failed, 9 passed`,失败形态为 `assert 204 == 409`(现状无条件删)。
  软删哨兵测试③当时天然绿(现状本来就 204),其价值在 Step 5 变异下兑现。
- **Step 3/4(GREEN)**:实现后 `13 passed`。
- **Step 5(变异自验,必做项)**:注释掉 `_find_extends_dependents` 中
  `if s.status is AgentSpecStatus.DELETED: continue` 两行 → 精确只红哨兵
  `test_delete_soft_deleted_dependent_does_not_block`(`1 failed, 12 passed`,
  软删继承者被误当依赖 → 409)。恢复后 `13 passed`。

## 测试清单(新增)

1. `test_delete_with_pinned_dependent_409` —— 钉版 extends → 409 + detail 全字段
   + 模板仍在(GET 200)+ 409 无删除审计。
2. `test_delete_without_dependents_204_and_audit_flag` —— 无关 spec(无 extends /
   extends 他模板)不拦 → 204 + 审计 `dependents_checked: True`。
3. `test_delete_soft_deleted_dependent_does_not_block` —— 软删继承者不拦(变异哨兵)。
4. `test_delete_latest_track_both_directions` —— `@latest` 双向:非最后版 204 /
   最后版 409。
5. `test_delete_dependents_pagination_and_cap` —— 21 继承者 + 200 filler(>1 页):
   `dependents_total==21`、`len(dependents)==20`。

## 验证

- `uv run pytest services/control-plane/tests/test_agent_templates_api.py -q` → 13 passed
- 回归:`test_agent_templates_api.py + test_agents_fork.py + test_runtime_template_extends.py` → 27 passed
- `uv run ruff check`(全库)All checks passed;`ruff format --check`(全库)通过
- CI mypy 范围不含 control-plane(ci.yml:75),无新增扫描面

## 文件

- `services/control-plane/src/control_plane/api/agent_templates.py` —— 反查助手
  `_find_extends_dependents` + delete 端点接线 + `_get_agent_spec_store` 依赖
  (`app.state.agent_spec_repo`)+ 常量 `_DEPENDENT_PAGE_SIZE=200` /
  `_DEPENDENT_LIST_CAP=20`。
- `services/control-plane/tests/test_agent_templates_api.py` —— `_Ctx` 增
  `app` + `seed_tenant_spec`(照 `test_agents_bind_session.py` 的
  `agent_spec_repo.create` 播种套路)+ 上述 5 测试。

## Concerns

- **幽灵模板 + 悬空钉版 extends 的边界**:反查按 brief 伪码置于存在性检查(store
  NotFound → 404)之前;若模板 (name,version) 本不存在但有租户 spec 钉着它,DELETE
  现在回 409 而非 404。语义偏 fail-closed(该继承者构建本来已破),与伪码逐字
  一致;若终审认为 404 应优先,可在反查前对 `versions` 做一次存在性短路。
- 反查谓词只认 `status=PUBLISHED`、不看 `enabled`,与构建侧解析器严格对齐;若日后
  构建侧加 enabled 闸,此处谓词须同步(同一语义分散多处的老教训)。
