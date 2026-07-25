# Task 5 报告 — 三条护栏测试补强(纯测试,依赖 T2 落地)

分支:`fix-deletion-hygiene-followups`(经 `git merge --ff-only` 同步到本 worktree,起点
`8d3a71f1`,merge 后 HEAD `747b9189`)。本任务只改三个测试文件,**未改任何生产代码**——
每条变异都是临时注入,验证完立即 `git checkout --` 复原,并用 `git diff --quiet` 确认。

## 前置核对

任务书写作时 T2/T4 尚未落地,dispatch 时提醒的两点已核实生效:

- `mcp_servers.py:1041-1052` 已是 `_SPEC_PAGE_SIZE = 200` 的分页循环(T2),第 3 条测试写在
  这之上,不用自己引入分页。
- `purge/user_purge.py` 的 approval 清理块在 `_purge_threads`(:223-229),与 T4 改的
  `members.py` 是两处不同代码——本任务第 2 条测试打的是前者。

## 三条测试的最终形态

### 1. `services/control-plane/tests/test_triggers_api.py::test_403_delete_leaves_trigger_run_rows_intact`

非 admin 删他人无主(service-principal 建、`user_id IS NULL`)trigger → 403,且该 trigger
的两条 `trigger_run` 子行仍在。复用已有的 `_client_as` / `_create_cron` / `_seed_trigger_run`
helper,构造攻击者是真实存在的另一 subject(非 admin),不是同一 caller 打自己——与文件里
既有的 `test_null_owner_trigger_delete_requires_admin` 同一构造套路。

断言:
```python
assert resp.status_code == 403
assert resp.json()["detail"]["code"] == "USER_SCOPE_FORBIDDEN"
run_store = app.state.trigger_run_store
surviving = await run_store.list_by_trigger(trigger_id=trigger_id, tenant_id=_DEFAULT_TENANT)
assert len(surviving) == 2
```

锁定的是 `delete_trigger`(`services/control-plane/src/control_plane/api/triggers.py:507-550`)
的操作顺序不变式:所有权闸必须先于 PR3 引入的孤儿行级联跑。

### 2. `services/control-plane/tests/test_user_purge.py::test_purge_user_approval_cleanup_failure_recorded_and_does_not_abort`

新增 `_FailingApprovalStore(InMemoryApprovalStore)`,只重写 `delete_for_threads` 使其抛
`RuntimeError`,其余方法(`create` 等)照常。种一个用户 + 一条线程 + 该线程上的一条 NULL-user
approval,跑 `purge_user`。

断言(三条,分别锁住"失败被记录在正确的 key 下"和"函数内后续步骤照跑"两件事):
```python
assert "agent_approval" in summary.failures
assert summary.failures["agent_approval"]
assert summary.threads_purged == 1        # _purge_threads 内、approval 清理块之后的
                                           # per-thread 删除循环仍执行完
assert "threads" not in summary.failures  # 外层 _step 没有看到异常穿出 _purge_threads
```

锁定的是 `_purge_threads`(`services/control-plane/src/control_plane/purge/user_purge.py:223-229`)
approval 清理块自己的内层 try/except——与 `purge_user` 里包住整个 `_purge_threads` 协程的外层
`_step`(:328-333)是两层不同的防护,断言刻意区分两者,免得变异把异常挪到外层兜住也能骗过测试。

### 3. `services/control-plane/tests/test_mcp_servers_api.py::test_delete_counts_deprecated_wildcard_follower_in_implicit_all`

种一个 `status=DEPRECATED`、`servers=[]`(通配符)的 agent spec,删 MCP server。

断言:
```python
assert delete_resp.status_code == 200, delete_resp.text
assert delete_resp.json()["data"]["implicit_all_agents"] == 1
```

是既有 `test_delete_reports_implicit_all_agents_in_response_and_audit`(全 ACTIVE 通配符)与
`test_delete_conflicts_when_deprecated_agent_references`(DEPRECATED + 显式引用 → 409)的补空
格:DEPRECATED + 隐式通配符这个组合此前没人测过。锁定的是
`mcp_servers.py:1053`(`active_specs = [s for s in specs if s.status is not AgentSpecStatus.DELETED]`)
这一行——`implicit_all` 的统计口径与"是否算引用"共用同一个 `active_specs` 集合。

## Step 2:三条对当前代码跑绿

```
uv run pytest services/control-plane/tests/test_triggers_api.py::test_403_delete_leaves_trigger_run_rows_intact \
  services/control-plane/tests/test_user_purge.py::test_purge_user_approval_cleanup_failure_recorded_and_does_not_abort \
  services/control-plane/tests/test_mcp_servers_api.py::test_delete_counts_deprecated_wildcard_follower_in_implicit_all -v
```
结果:3 passed。

全文件回归(确认没有撞坏既有测试):
```
uv run pytest services/control-plane/tests/test_triggers_api.py services/control-plane/tests/test_user_purge.py services/control-plane/tests/test_mcp_servers_api.py
```
结果:**82 passed**, 5 warnings(均为 jieba/swig 的既有第三方 DeprecationWarning,与本次改动无关)。

## Step 3:逐条变异自验(本任务的唯一价值证明)

每条变异都用 `Edit` 直接改生产代码,跑对应测试确认红,再 `git checkout --` 复原并用
`git diff --quiet` 确认干净。三次操作全部按任务书括注的变异逐字做,没有一条需要换靶子——
三条都在第一次尝试就咬合。

### 变异 1 —— trigger 403:级联移到权限门之前

**注入**(`services/control-plane/src/control_plane/api/triggers.py`,`delete_trigger`):把
`trigger_runs.delete_for_triggers(...)` 挪到 `if record.user_id is None: ...` 所有权检查之前
(顺序颠倒,404 检查仍在最前)。

**实际报错**:
```
run_store = app.state.trigger_run_store
surviving = await run_store.list_by_trigger(trigger_id=trigger_id, tenant_id=_DEFAULT_TENANT)
>       assert len(surviving) == 2
E       assert 0 == 2
E        +  where 0 = len([])
```
测试从绿变红,且是"子行数量不对"而非其他原因跑挂——精确命中。

**复原确认**:`git checkout -- services/control-plane/src/control_plane/api/triggers.py` 后
`git diff --quiet` 返回 0(无残留)。跑单测确认恢复绿(见 Step 2 全文件回归,复原后再次整体跑过
82 全绿,详见下方"复原后总验")。

### 变异 2 —— approval 清理失败分支:去掉 try/except

**注入**(`services/control-plane/src/control_plane/purge/user_purge.py`,`_purge_threads`):
把
```python
try:
    summary.deleted["agent_approval"] = await deps.approvals.delete_for_threads(
        thread_ids=thread_ids, tenant_id=tenant_id
    )
except Exception as exc:
    logger.warning("purge_user.approvals_failed", exc_info=True)
    summary.failures["agent_approval"] = f"{type(exc).__name__}: {exc}"
```
去掉 try/except,只剩裸调用。

**实际报错**:
```
>       assert "agent_approval" in summary.failures
E       AssertionError: assert 'agent_approval' in {'threads': 'RuntimeError: approval store unavailable'}
```
且 traceback 显示异常穿透 `_purge_threads` 被外层 `_step`(`step=threads`)兜住——印证了断言设计
时的预判:异常改记在 `failures["threads"]` 而非 `failures["agent_approval"]`,且
`threads_purged` 停在 0(per-thread 删除循环在异常处提前中止,没跑到)。三条断言里第一条先炸,
但从 traceback 能确认另外两条(`threads_purged == 1` / `"threads" not in failures`)在这个变异下
也会一并失败——`_purge_threads` 被整体判为失败步骤,函数内该 approval 清理之后的所有代码(包括
per-thread 删除循环)都没有机会执行。

**复原确认**:`git checkout -- services/control-plane/src/control_plane/purge/user_purge.py` 后
`git diff --quiet` 返回 0。

### 变异 3 —— DEPRECATED wildcard:active 过滤收窄成只留 ACTIVE

**注入**(`services/control-plane/src/control_plane/api/mcp_servers.py:1053`):
```python
active_specs = [s for s in specs if s.status is not AgentSpecStatus.DELETED]
```
改成
```python
active_specs = [s for s in specs if s.status is AgentSpecStatus.ACTIVE]
```

**实际报错**:
```
>           assert delete_resp.json()["data"]["implicit_all_agents"] == 1
E           assert 0 == 1
```
顺带核实了这个变异也会让既有的 `test_delete_conflicts_when_deprecated_agent_references`(DEPRECATED
+ 显式引用应 409)一并变红(实测 FAILED)——佐证 `active_specs` 这个集合是"是否算引用"和
"implicit_all 计数"两处共用的同一份数据,变异收窄它是牵一发动全身的破坏,不是只影响本测试的窄口径
巧合。

**复原确认**:`git checkout -- services/control-plane/src/control_plane/api/mcp_servers.py` 后
`git diff --quiet` 返回 0。

### 复原后总验

三次变异逐条复原后,分别重跑对应单测确认转绿;三个文件全体最终状态:
```
uv run pytest services/control-plane/tests/test_triggers_api.py services/control-plane/tests/test_user_purge.py services/control-plane/tests/test_mcp_servers_api.py
```
→ **82 passed**, 5 warnings(同 Step 2,无新增)。

```
git status --short
```
→ 只有三个测试文件 `M`(生产代码零改动,`git diff --stat` 只列这三个测试文件)。

## 跑测命令与工具链核对

- 测试:`uv run pytest <files>`(workspace 根目录跑,不要在 `services/control-plane/` 下单独
  `uv run` ——那会新建一个不含 pytest 的局部 venv,报 `Failed to spawn: pytest`)。
- `uv run ruff check` 三文件:All checks passed。
- `uv run ruff format --check` 三文件:already formatted。
- `uv run mypy` 三文件:control-plane 测试目录本就不在本项目 CI-scope mypy 范围内(CI-scope 只
  含 `packages` + `services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,
  orchestrator,retention-cleanup-job}/src`,见 SDD 计划 Global Constraints)。逐文件对比编辑前后
  错误数:
  - `test_triggers_api.py`:12 → 13(新增 1 条 `Unused "type: ignore[union-attr]" comment`——
    我的新测试里 `app = triggers_client._transport.app  # type: ignore[attr-defined,union-attr]`
    是照抄文件里 `test_delete_trigger_cascades_trigger_runs` 等既有测试的**逐字同款**写法,那些
    既有用法本身也在同一 mypy 配置下报同样的 "unused ignore"(该文件里已有 5 处此类既存未修的
    错误)。不是我引入的新问题类别,是复用既有(未修)惯例的必然结果。
  - `test_user_purge.py`:7 → 7(零新增)。
  - `test_mcp_servers_api.py`:50 → 51(新增 1 条 `ASGITransport` 的 `app: object` 类型不匹配,
    同样是 `_make_app_with_admin()` 这个既有 helper 返回 `object` 元组导致的、文件里已有 ~20 处
    的既存模式,不是新问题类别)。
  这些 mypy 噪音全部是"复用既有代码惯例"的副产品而非本任务引入的新缺陷类型,且不在 CI 门禁范围
  内,故不视为阻塞项;未去"顺手"改动 `_make_app_with_admin()` 等共享 helper 的类型签名——那会
  超出本任务"只改三个测试文件"的边界。

## 结论

三条护栏测试对当前代码全部先绿,三次变异全部按任务书指定的注入方式一次命中(无需更换替代变异),
复原后 `git diff` 均干净,最终三文件合计 82 用例全绿。生产代码零改动。
