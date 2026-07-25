# Task 1 Report — TriggerStore.disable_for_agent(双实现)

删除接口卫生 PR4 Task 1。agent 软删级联的 store 层原语:按 (tenant_id, agent_name, agent_version) 批量禁用 enabled 触发器,返回改动行数。Task 3 将在 delete_agent 端点消费。

## STATUS

DONE — TDD 先红后绿,双实现平价测试全绿,变异自验双侧均杀。

## Commits

- `dc0bebae` feat(persistence): TriggerStore.disable_for_agent 双实现(agent 软删级联)

## 改动文件

- `packages/expert-work-persistence/src/expert_work/persistence/trigger/base.py` — `update` 之后追加抽象 `disable_for_agent(*, agent_name: str, agent_version: str, tenant_id: UUID) -> int`,docstring 注明 deletion hygiene PR4 §B(agent soft-delete cascade)与四谓词(enabled == true AND agent_name == AND agent_version == AND tenant_id ==)。
- `packages/expert-work-persistence/src/expert_work/persistence/trigger/sql.py` — SQL 实现,brief Step 3 片段逐字(where 四谓词 + `.values(enabled=False)`),事务照同文件既有写方法(execute + commit + rowcount)。
- `packages/expert-work-persistence/src/expert_work/persistence/trigger/memory.py` — in-memory 实现,同容器四谓词过滤(与 SQL 侧语义逐条对应),frozen TriggerRecord 经 `model_copy(update={"enabled": False})` 替换,返回 len(victims)。
- `packages/expert-work-persistence/tests/test_in_memory_trigger_store.py`、`tests/test_sql_trigger_store.py` — 两侧同型平价测试 `test_disable_for_agent_scopes_by_name_version_tenant`:同租户 target@v1 enabled x2 / target@v2 enabled x1 / target@v1 已 disabled x1,他租户 target@v1 enabled x1;断言 n == 2、他 version 不动(变异哨兵)、他租户不动、已 disabled 不计数且保持、幂等重跑返回 0。

## TDD 记录

1. **RED**:两侧测试先写,实现前运行 —— in-memory `AttributeError: 'InMemoryTriggerStore' object has no attribute 'disable_for_agent'`;SQL `AttributeError: 'SqlTriggerStore' object has no attribute 'disable_for_agent'`(带 DOCKER_HOST 真容器)。
2. **GREEN**:三层实现后,两测试文件 60 passed(含全部既有回归)。

## 变异自验(brief Step 5)

- **SQL 侧**:删去 `AgentTriggerRow.agent_version == agent_version` 谓词 → 哨兵红(`assert 3 == 2`,v2 触发器被误禁);恢复后绿。
- **in-memory 侧**:删去 `r.agent_version == agent_version` 谓词 → 哨兵红(`assert 3 == 2`);恢复后绿。
- 两侧变异均被同一断言杀死,后续 enabled 校验(`_enabled(r_v2) is True`)为第二重哨兵。

## 测试/门禁摘要

- `pytest packages/expert-work-persistence/tests/test_in_memory_trigger_store.py test_sql_trigger_store.py`(DOCKER_HOST 真容器):**60 passed**。
- 全 persistence 包:923 passed;仅两处**预先存在**的环境/flake 问题(见 concerns)。
- `ruff check .` 全库:All checks passed(顺手修掉自己测试注释里 8 处 RUF003 `×`→`x`)。
- `ruff format --check`:already formatted。
- CI-scope `mypy packages services/...`(strict):Success, 783 files。

## Concerns

1. **预先存在、与本任务无关**(clean tree 复现验证过):
   - `test_rls_detect.py::test_detects_would_fail_closed_when_no_tenant_and_not_bypass` 在全包顺序跑时挂、单跑绿 —— 顺序依赖 flake,stash 后的干净树同样挂。
   - `test_pgbouncer_integration.py` 3 个 setup ERROR —— 本机 6379 端口被占(老 docker 栈占端口的已知坑),非代码问题。
2. SQL 实现未 stamp `updated_at`(brief 逐字只 `.values(enabled=False)`);若 Task 3/审计侧需要 updated_at 语义,消费时留意。
3. 副作用不进 assert:测试内 `_enabled` 辅助只做只读 get,CodeQL 安全。
