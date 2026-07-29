# W1-PR3 PENDING 回收 + store setup 重试 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 多副本正确性修复波第 3 批(收官):PENDING 卡死 run 回收 / LangGraph store setup 并发 DDL 竞态重试。审计出处 `docs/research/2026-07-28-multi-replica-readiness-audit.md` 第 1 波 6/11 项。

**范围裁决(2026-07-29 用户拍板):** 审计项 9(文档上传改对象存储)**并入 W3**——工作区定案 NAS 后"多节点读不到"被基础设施消掉,且 W3 前沙箱上不了云,现在改存在返工风险。文档路径卫生缺口(零配额/无登记行/无删除端点/purge 无行级清理)记 follow-up 池,与 W3 文档写路径一体设计。

**Architecture:** 照仓内先例:`list_orphans` 形状(cross-tenant + caller bypass_rls)、`fail_if_active` CAS(谓词已含 PENDING,只有 list 侧缺候选)、checkpointer `_setup_with_retry`(整函数提共享)。

## Global Constraints

- 分支 `deploy-w1-pr3-runs-and-objectstore`(已建,基 main f9e6f10e)。
- CI 门:`uv run ruff check .` + `uv run ruff format --check .` + CI-scope mypy(`uv run mypy packages`)+ control-plane pytest(`-m "not integration"`)+ testcontainers 文件 `DOCKER_HOST= uv run pytest <file> -q`。
- 双 store 谓词 byte-identical;新 store 方法必须 SQL 真容器集成测。
- 实施者所有命令前台同步跑完;创建后台任务=任务失败。
- conventional commits,无 attribution。

---

### Task 1: PENDING 卡死 run 回收

背景事实(侦察已核):PENDING 是同步 SSE 路径 create→RUNNING 的瞬态(`schemas.py:24-29`),正常毫秒级;副本在窗口内崩则永久卡住(lease 三字段全 NULL,`list_orphans` 两版谓词只看 running+过期 lease 永不提名)。`fail_if_active`/`request_cancel` 的 CAS 谓词**已含 PENDING**(store.py:463/481/869/892)——只缺 list 侧。卡死 PENDING 的实际伤害:`plan.py:45` 把 PENDING 算 `_WRITE_BLOCKED_STATUSES`,永久阻塞该 thread 的外部 plan 写。回收动作=打 ERROR(run 从未启动,SSE 客户端早断,respawn 无意义);多副本安全由 `fail_if_active` CAS 天然兜底(败者跳过 audit/counter 已是现行为)。

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`(ABC+SQL+in-memory 加 `list_stale_pending`;ABC 挨着 `list_orphans`(:336),docstring 照它写明 caller MUST wrap `bypass_rls_session()`)
- Create: `packages/expert-work-persistence/migrations/versions/<next>_agent_run_pending_sweep.py`(偏索引 `ix_agent_run_pending_sweep = (created_at) WHERE status='pending'`,照 `ix_agent_run_queue_scan`(agent_run.py:66-101)形状;模型文件同步登记;编号先看 migrations 目录当前最大值)
- Modify: `services/control-plane/src/control_plane/orphan_sweep.py`(`run_once` 追加 pending 扫描段)
- Test: `packages/expert-work-runtime/tests/test_run_store.py`(或既有 run store 测试文件,先 ls 确认)+ persistence SQL 真容器测 + `services/control-plane/tests/test_orphan_sweep.py`

**Interfaces:**
- Produces: `async def list_stale_pending(self, *, cutoff: datetime, limit: int) -> list[RunInfo]` —— 谓词 `status='pending' AND created_at < cutoff`,按 `created_at ASC` 排序,`limit(max(1, limit))`,cross-tenant 无 tenant 过滤(照 `list_orphans` SQL 版 :1195-1209 注释形状);in-memory 同谓词同排序。
- sweep 段:`run_once` 在既有 orphan 循环后追加——`cutoff = now - timedelta(seconds=_PENDING_STALE_S)`(新常量 `_PENDING_STALE_S = 600`,注释写明:正常 create→RUNNING 毫秒级,600s=远超任何正常窗口)→ `_bypass_rls()` 内 `list_stale_pending(cutoff=cutoff, limit=self._batch_size)` → 逐个 `_fail_orphan(orphan, reason="stale_pending")`(复用既有,含 `_failed_total.labels(reason=...)` + audit + CAS 败者静默);handled 计数并入返回值。

- [ ] **Step 1(RED)**:三处测试:
  - store 双实现:①created_at 早于 cutoff 的 PENDING 行返回;②新 PENDING(created_at ≥ cutoff)不返回;③RUNNING/终态行不返回;④排序+limit。SQL 版真容器 `DOCKER_HOST= uv run pytest <persistence 测试文件> -q`。
  - sweep 测:stale PENDING → `fail_if_active` 生效行变 ERROR + audit 恰一次;第二次 `run_once` 同一行不再 audit(CAS False 路径);新 PENDING 不动。
  - 跑 → FAIL(方法不存在)。
- [ ] **Step 2(GREEN)**:双 store 实现+迁移+sweep 段。
- [ ] **Step 3**:回归 `uv run pytest services/control-plane/tests/test_orphan_sweep.py -q` + run store 测试(含 `DOCKER_HOST=` 真容器)+ ruff。
- [ ] **Step 4**:Commit `fix(orphan-sweep): PENDING 卡死 run 回收(created_at 宽限扫描,复用 fail_if_active CAS)`

### Task 2: LangGraph store setup 并发 DDL 竞态重试

背景事实(侦察已核):`store/factory.py:77` `await store.setup()` 裸调;checkpointer 同款问题已修在 `checkpointer/factory.py:43-82`(`_SETUP_MAX_ATTEMPTS=8` / `_SETUP_RETRY_BASE_DELAY_S=0.1` / `_setup_with_retry`,捕 psycopg UniqueViolation/DuplicateObject/DuplicateTable/DeadlockDetected,线性退避,末次 raise)。`make_store` 当前零生产调用方(仅测试引用)——潜伏修复,防未来接线时踩同一坑。

**Files:**
- Create: `packages/expert-work-runtime/src/expert_work/runtime/_setup_retry.py`(把 checkpointer 的 `_setup_with_retry` 函数+两常量+成因注释整体搬来,改公开名 `setup_with_retry(target: Any) -> None`,docstring 保留原成因注释)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/checkpointer/factory.py`(删本地实现,改 import 共享;行为零变)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/store/factory.py:77`(`await store.setup()` → `await setup_with_retry(store)`)
- Test: `packages/expert-work-runtime/tests/test_store_factory.py`(加重试用例)+ 既有 `test_checkpointer_factory.py` 回归

**Interfaces:**
- Produces: `async def setup_with_retry(target: Any) -> None`(`target` 有 `async setup()`;psycopg.errors 函数内延迟 import 保持原形状)。

- [ ] **Step 1(RED)**:test_store_factory 加用例:fake store `setup()` 前两次抛 `psycopg.errors.UniqueViolation`,第三次成功 → `make_store("postgres", dsn=...)` 需 mock `AsyncPostgresStore.from_conn_string` 返回 fake(照既有测试文件的 mock 手法);断言 setup 被调 3 次且最终 yield。非竞态异常(如 ValueError)直接穿透的用例一条。跑 → FAIL。
- [ ] **Step 2(GREEN)**:提共享模块+两处接线。
- [ ] **Step 3**:`uv run pytest packages/expert-work-runtime/tests/test_store_factory.py packages/expert-work-runtime/tests/test_checkpointer_factory.py -q` + `uv run mypy packages` + ruff。
- [ ] **Step 4**:Commit `fix(store): LangGraph store setup 补并发 DDL 竞态重试(与 checkpointer 共享 setup_with_retry)`

---

## 验证(整 PR)

- 两 task 测试全绿 + CI 门全过;真容器:run store pending 扫描。
- follow-up 池增补:文档路径卫生四缺口(配额/登记行/删除端点/purge 行级)标注"与 W3 文档写路径一体设计"。
