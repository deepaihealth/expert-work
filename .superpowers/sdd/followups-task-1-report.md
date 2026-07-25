# Task 1 报告 —— 在飞 run 取消收窄到版本级(删除卫生 follow-up)

> 注:本文件覆盖了 PR5 时期同名的 `task-1-report.md`(purge_user approval 空转收口)。
> 旧内容仍在 git 历史里(commit `8d3a71f1`)。follow-up 程序复用了 task-N 槽位。

**STATUS: DONE**

## 问题

PR4 的 `delete_agent` 级联用 `list_running_for_agent(agent_name=name)` 取消在飞 run。取消范围是 **agent name 级**:删 `foo@v1` 会连带把仍活跃的 `foo@v2` 会话 INTERRUPT 掉(终审 Minor,当时判「宁可多取消」)。

SQL 侧本来就 join 了 `thread_meta`,而 `thread_meta.agent_version` 列存在(`models/thread_meta.py:34`,nullable Text),所以加一个可选谓词即可精确 —— **未给 `RunInfo` 加列、未加迁移**。

## 改动

### 1. `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`

签名(三处一致:抽象 / InMemory / Sql):

```python
async def list_running_for_agent(
    self, *, tenant_id: UUID, agent_name: str, agent_version: str | None = None
) -> list[RunInfo]:
```

- 抽象 docstring 补:`agent_version` 给值时收窄到该版本(删除级联用);`None` 保持 name 级(kill switch 用);并写明 `thread_meta.agent_version` 可空 —— NULL 行不匹配版本级查询是**期望行为**(未绑版本的老线程不被误取消)。
- `SqlRunStore`:既有 join / where / order_by 一字不动,按需追加谓词

```python
if agent_version is not None:
    stmt = stmt.where(ThreadMetaRow.agent_version == agent_version)
```

- `InMemoryRunStore`:镜像同一谓词(把原来的 `if meta is not None and meta.agent_name == agent_name` 拆成两个 `continue` 守卫再加版本守卫,逻辑等价)。`_thread_meta_store is None` 仍返 `[]`(未动)。

**双实现谓词等价性**:SQL 的 `ThreadMetaRow.agent_version == "x"` 对 NULL 行求值为 NULL → 不入结果;in-memory 的 `meta.agent_version != agent_version → continue` 对 `None` 也排除。两侧 NULL 语义逐字对齐,且各自有同型测试。

### 2. `services/control-plane/src/control_plane/api/agents.py`

- `:1213`(`delete_agent` 级联)改为传 `agent_version=version`;顺带把上方那段声称「cancel 是 name 级,比 version 级 delete 更宽」的注释改写成现状(该注释是本改动直接作废的,属于自己的 orphan)。
- `:1309`(`disable_agent` kill switch)**一字未动** —— kill switch 覆盖全版本,是设计。

## 测试

三面同型覆盖(两 store 平价 + 端点):

| 文件 | 用例 |
|---|---|
| `packages/expert-work-runtime/tests/test_run_store_list_running_for_agent.py` | `test_list_running_for_agent_filters_by_version_when_given`(in-memory) |
| `packages/expert-work-runtime/tests/test_sql_run_store.py` | `test_list_running_for_agent_filters_by_version_when_given`(SQL,testcontainers) |
| `services/control-plane/tests/test_agents_api.py` | `test_delete_cancels_only_this_versions_in_flight_runs`(端点) |

store 两侧各播三个 thread:`foo@1.0.0` / `foo@2.0.0` / `foo@NULL`,各挂一个 RUNNING run。断言:

- 不传版本 → 三个 run 全返(旧语义未回归);
- 传 `agent_version="1.0.0"` → 只返 v1 的 run(版本哨兵 + NULL 行被排除)。

端点用例:注册 `code-reviewer@2.0.0`,两版本各开一个 session + RUNNING run,删 v1 后断言 v1 run = INTERRUPTED、**v2 run 仍 RUNNING**、`SESSION_CANCEL` 审计只有 v1 那条、`runs_cancelled == 1`。

`_seed_thread` 加了 `agent_version: str | None = "1.0.0"` 默认参数,既有三个用例调用点不变。

### 红 → 绿

**Step 2(红)**

- in-memory:`TypeError: InMemoryRunStore.list_running_for_agent() got an unexpected keyword argument 'agent_version'`
- SQL:同上(`1 failed, 25 deselected`)
- 端点:`assert ... <RunStatus.INTERRUPTED> is <RunStatus.RUNNING>` —— 正是 brief 预测的「v2 run 被误取消」

**Step 4(绿)**:三文件合跑 `56 passed`。

### Step 5 变异自验

brief 写的是「把 SQL 侧新谓词改成 `if False:` → 端点测试红」。实测**端点测试打不到 SQL 谓词**(`cascade_ctx` 用 `InMemoryRunStore`),所以做了两次变异,各由对应层的测试杀死:

| 变异 | 结果 |
|---|---|
| A:`SqlRunStore` 的 `if agent_version is not None:` → `if False:` | SQL 平价测试 **红**(`test_list_running_for_agent_filters_by_version_when_given` FAILED);端点测试仍 **绿**(1 passed)——证实端点只覆盖 in-memory 路径 |
| A 复原 | 绿 |
| B:`InMemoryRunStore` 的版本守卫 → `if False and ...` | in-memory 单测 **红** + 端点测试 **红**(`2 failed, 28 passed`) |
| B 复原 | 绿 |

结论:SQL 谓词由 SQL 平价测试守,in-memory 谓词由单测 + 端点测试双守,无空转。

### 回归

- `packages/expert-work-runtime/tests`:`1 failed, 463 passed, 9 errors`。同一命令在 stash 掉本改动的基线上跑:`1 failed, 461 passed, 9 errors` —— **同款失败/报错,全部先存**(`test_tokens.py::test_tiktoken_estimator_load_failure_falls_back` 是跨用例污染,单跑通过;9 个 error 是 minio / pg_dump 容器环境)。差的 2 passed 就是本任务新增的 2 个用例。
- `services/control-plane/tests`:`2135 passed`(全绿,7m16s)。
- `uv run ruff check .` → All checks passed;`ruff format` 已跑(格式化了 `test_agents_api.py`)。
- CI-scope mypy(`packages` + 5 个 service src)→ `Success: no issues found in 783 source files`。
- SQL 集成走 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。

## Concerns

1. **brief Step 5 的变异预期与实际分层不符**(端点测试跑 in-memory store,杀不了 SQL 谓词)。已按实际做双变异,两层各自有杀手,见上表。
2. `list_running_for_agent` 只有这两个调用点(已全库 grep 确认),`RunStore` 也只有 In-memory / Sql 两个实现,新参数带默认值故对外无破坏性。
3. **老线程 NULL 版本不被取消**是有意为之(brief 指定):如果租户有 `thread_meta.agent_version` 为 NULL 的活跃线程,删 agent 版本时它们的在飞 run 不会被取消,需靠 `disable_agent`(name 级)兜。已在抽象 docstring 写明。
4. 本报告覆盖了 PR5 的同名 tracked 文件,见页首注。
