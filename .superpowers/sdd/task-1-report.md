# Task 1 Report — purge_user approval 空转收口(§B)

删除接口卫生 PR5 Task 1。`agent_approval` 的唯一创建路径(orchestrator sse.py:1013
暂停流)硬编码 `user_id=None`,`purge_user` 的 per-user 清理步(`delete_all_for_user`
按 `(tenant_id, user_id)` 过滤)对现实中的每一行都不匹配——从未删过任何行,全空转。
收口方式:在 `_purge_threads` 接 PR3 已有的 `ApprovalStore.delete_for_threads`
thread 级批删。

## STATUS

DONE — TDD 先红后绿,变异自验通过。

## Commits

- `c67389a3` fix(control-plane): purge_user 接 thread 级 approval 清理(per-user 步对 NULL-user_id 行全空转)

## 改动

### `services/control-plane/src/control_plane/purge/user_purge.py`

1. `_purge_threads`:feedback `delete_for_threads` 块(原 :204-210)正下方,同
   try/except 形状追加 approval thread 级批删(brief 追加块逐字使用):
   - `summary.deleted["agent_approval"] = await deps.approvals.delete_for_threads(thread_ids=..., tenant_id=...)`
   - 关键字实参:approval 版 `(*, thread_ids, tenant_id)` 与 feedback 版
     `(*, tenant_id, thread_ids)` 顺序相反(PR3 既定),均 keyword-only,无位置风险。
   - 失败走 `failures["agent_approval"]`,best-effort,不 abort 级联。
2. per-user 步(原 :348-353)**保留**作 backstop,两处更新:
   - 记账键改 `deleted["agent_approval_user_scope"]`(`_step` name 同步改)——
     该步在 thread-scope 之后运行,若仍记 `agent_approval` 会把 thread-scope 的
     计数覆盖成 0(brief 预告的键冲突;既有 `deleted` 记账习惯是单键单写、无累加
     先例,故按"改记独立键"处理)。
   - 注释更新为:backstop for future user-stamped rows;thread-scope pass 覆盖
     今天的 NULL-user_id 行。

### `services/control-plane/tests/test_user_purge.py`

- 新增 `_approval(*, tenant, thread)` helper:`user_id=None` 的 `ApprovalRecord`
  (照 orchestrator sse.py 现实;字段构造照 `protocol/approval.py`,样式套
  `test_sessions_api.py` 既有构造)。
- 新增回归哨兵 `test_purge_user_deletes_null_user_approvals_on_user_threads`
  (fixture 块照抄本文件既有套路):
  - 用户 A 线程挂 NULL-user approval → purge A → 行消失
    (`get_by_run` 返 None),`summary.deleted["agent_approval"] == 1`
    ——该断言同时守住"计数不被后跑的 per-user 步覆盖"。
  - 用户 B 线程挂 NULL-user approval → **不动**(thread 谓词哨兵:删的是
    thread 范围,不是 tenant 全表)。
  - `"agent_approval" not in summary.failures`。

## TDD 记录

| 步骤 | 结果 |
|---|---|
| Step 1-2 RED | `assert summary.deleted["agent_approval"] == 1` → `assert 0 == 1`(per-user 步删 0 行,空转坐实) |
| Step 3-4 GREEN | `test_user_purge.py` 9 passed |
| Step 5 变异自验 | 注释掉新增 try/except 块 → 哨兵红(`KeyError: 'agent_approval'`,该键已归 thread-scope 块独有,变异杀干净);恢复 → 9 passed |

## 验证

- `uv run pytest services/control-plane/tests/test_user_purge.py` → 9 passed
- `uv run pytest .../test_user_purge.py .../test_sessions_api.py` → 50 passed
  (sessions 侧 `delete_for_threads` 既有用例不受影响)
- `uv run ruff check .`(全库)→ All checks passed;`ruff format --check`
  两改动文件 → already formatted
- CI mypy 范围不含 control-plane(ci.yml:75),不适用
- 记账键消费面 grep:`deleted["agent_approval"]` 在 control-plane src / apps
  前端无硬编码消费点,per-user 步改键 `agent_approval_user_scope` 无下游破坏

## 约束遵守

- 日志无请求派生值(`purge_user.approvals_failed` 固定串 + `exc_info=True`)
- 测试 assert 全为读操作(`get_by_run`),无副作用进 assert(CodeQL)
- brief 追加块逐字使用;commit message 用 brief 给定文案

## Concerns

- `summary.deleted` 新增键 `agent_approval_user_scope` 会出现在 purge 端点响应与
  USER_PURGE 审计 details(泛型 dict 渲染,无硬编码消费点)。今天恒为 0,语义即
  "backstop 没接到活";若 orchestrator 将来真落 user_id,该键自然开始计数。
- SQL 侧 `delete_for_threads` 是 PR3 已合并、带真容器集成测的存量方法,本任务只
  改编排层接线(无谓词改动),故仅在 in-memory 套件验证,未重跑 SQL 集成测。
- 环境插曲:首跑 pytest 放后台被中断导致 worktree `.venv` python 二进制损坏
  (SIGKILL),`uv venv --allow-existing` + `uv sync` 重建后正常;与代码无关。
