# W1-PR1 后台 worker 多副本单飞与领取 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 多副本正确性修复波第 1 批:webhook 投递 CAS 领取 / MemoryDLQ claim / Consolidator+SkillCurator advisory lock 与 env 开关 / OrphanSweep 终态 CAS 守卫——消除 N 副本下重复投递、重复 embed、重复 audit。

**Architecture:** 全部照仓内既有 CAS/锁先例落地:`claim_queued`(UPDATE…RETURNING)、`claim_documents_for_ingest`(SKIP LOCKED)、`quality_drift_worker` advisory lock、`mark_decided`(前置状态谓词)。SQL 与 in-memory 双实现谓词 byte-identical(仓规)。

**来源:** `docs/research/2026-07-28-multi-replica-readiness-audit.md` 第 1 波 1/2/3/8 项;侦察素材在 2026-07-28 会话(签名/行号已核)。

## Global Constraints

- 分支 `deploy-w1-replica-fixes`(已建,基 main ed02f527 + 文档 commit)。
- CI 门:`uv run ruff check .` 全库 + `uv run ruff format --check .` + CI-scope mypy + control-plane pytest(`uv run pytest services/control-plane/tests -m "not integration" -q`)+ 涉及 testcontainers 的用 `DOCKER_HOST= uv run pytest <file> -q`。
- 双 store 谓词 byte-identical;新 store 方法必须 SQL 真容器集成测(仓规,I-1 教训)。
- 不改熔断器进程内状态(webhook per-replica 熔断可容忍,记 follow-up 池);不动 per-run 预算家族(审计"明确不改")。
- conventional commits,无 attribution。

---

### Task 1: webhook 投递 CAS 领取(claim_ready)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/webhook/base.py`(ABC 加 `claim_ready`)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/webhook/sql.py`(`list_ready` 附近加 `claim_ready`;现 `list_ready` 保留给只读用途)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/webhook/memory.py`(同名同谓词)
- Modify: `services/control-plane/src/control_plane/webhook_delivery_worker.py:496-534`(`run_once` 改用 `claim_ready`)
- Test: `packages/expert-work-persistence/tests/test_sql_webhook_store.py`、`test_in_memory_webhook_store.py`、`services/control-plane/tests/test_webhook_delivery_worker.py`

**Interfaces:**
- Produces: `async def claim_ready(self, *, before: datetime, limit: int = 1000) -> list[WebhookDeliveryRecord]` —— 原子领取:把 `status=PENDING or (RETRYING and next_retry_at<=before)` 的行置为 `DELIVERING` 并返回。新增 `WebhookDeliveryStatus.DELIVERING = "delivering"` 枚举值。
- 领走后由既有 `_finish` 落终态(DELIVERED/RETRYING/DEAD_LETTER);`DELIVERING` 滞留行(副本崩溃)由既有重试语义兜底:`claim_ready` 谓词额外含 `(DELIVERING and updated_at <= before - stale_window)`,`stale_window` 用常量 `_DELIVERING_STALE_S = 300`。

- [ ] **Step 1(RED)**:三个测试文件加用例:
  - SQL 集成测(`DOCKER_HOST=`):两个并发 `claim_ready` 同一批 5 行 → 两边返回集合不相交且并集=5(照 `test_claim_queued_cas_one_winner` 写法 `asyncio.gather`)。
  - in-memory 同断言。
  - worker 测:`run_once` 后 store 里行状态经历 DELIVERING(mock store 断言调用 `claim_ready` 而非 `list_ready`)。
  - 跑:`DOCKER_HOST= uv run pytest packages/expert-work-persistence/tests/test_sql_webhook_store.py -q` → FAIL(方法不存在)。
- [ ] **Step 2(GREEN)**:SQL 版 `UPDATE webhook_delivery SET status='delivering', updated_at=:now WHERE id IN (SELECT id FROM webhook_delivery WHERE <ready谓词> ORDER BY created_at ASC LIMIT :limit FOR UPDATE SKIP LOCKED) RETURNING *`(SQLAlchemy `update().where(id.in_(subq)).returning()`);in-memory 版同谓词顺序扫描置状态;ABC 加抽象方法;worker `run_once` 换 `claim_ready`;`_finish` 的 `update` 不变(终态覆盖 DELIVERING)。
- [ ] **Step 3**:全量回归 `uv run pytest services/control-plane/tests/test_webhook_delivery_worker.py packages/expert-work-persistence/tests/test_sql_webhook_store.py packages/expert-work-persistence/tests/test_in_memory_webhook_store.py -q`(SQL 文件带 `DOCKER_HOST=`)→ 全绿;若既有用例断言 `list_ready` 行为需同步(领取语义变更)。
- [ ] **Step 4**:Commit `fix(webhook): 投递领取改 CAS(claim_ready+DELIVERING 态),多副本不再重复 POST`

### Task 2: MemoryDLQ 领取合并进事务(take_ready→claim 语义)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/memory/dlq.py`(SQL `take_ready` 254-263 与 in-memory 171-174)
- Modify: `services/control-plane/src/control_plane/memory/dlq_worker.py`(`_attempt_one` 的 attempts 语义对齐)
- Test: `services/control-plane/tests/test_memory_dlq_worker.py` + dlq store 对应测试文件

**Interfaces:**
- `take_ready` 改为原子领取:同一事务内 `SELECT … WHERE next_retry_at<=:now ORDER BY next_retry_at ASC LIMIT :n FOR UPDATE SKIP LOCKED` → `UPDATE … SET attempts=attempts+1, next_retry_at=:now+:lease RETURNING *`(照 `knowledge/sql.py:450-490 claim_documents_for_ingest` 抄);返回行的 `attempts` 已含本次。`:lease` 用常量 `_CLAIM_LEASE_S = 600`(领取即推迟下次可见,崩溃后 10 分钟重新可取)。
- worker `_attempt_one` 改:`next_attempt_number = row.attempts`(不再 +1,claim 时已计);成功清行、失败 `record_failure` 不再 attempts+1 只更新 next_retry_at/last_error(**record_failure 签名与实现同步改,双实现**)。

- [ ] **Step 1(RED)**:并发测:两 worker 同时 `take_ready` → 行集不相交;attempts 单调 +1 不翻倍;`test_max_attempts_marks_dead_letter` 按新语义校准。跑 → FAIL。
- [ ] **Step 2(GREEN)**:双实现落地;worker 对齐。
- [ ] **Step 3**:`DOCKER_HOST= uv run pytest <dlq 相关测试文件> -q` + `uv run pytest services/control-plane/tests/test_memory_dlq_worker.py -q` 全绿。
- [ ] **Step 4**:Commit `fix(memory-dlq): 领取合并进单事务(SKIP LOCKED+attempts 原子计数),多副本不重复 embed/不假死信`

### Task 3: Consolidator/SkillCurator advisory lock + 三开关接 env

**Files:**
- Modify: `services/control-plane/src/control_plane/settings.py`(加 `enable_scheduler: bool = True` / `enable_curation_worker: bool = True` / `enable_reaper: bool = True` 三字段,env 前缀自动生效)
- Modify: `services/control-plane/src/control_plane/main.py:7`(`create_app(enable_scheduler=s.enable_scheduler, …)` 从 settings 读)
- Modify: `services/control-plane/src/control_plane/memory_consolidator.py`(`run_once` 入口加 `pg_try_advisory_xact_lock`)
- Modify: `services/control-plane/src/control_plane/skill_curator.py`(同)
- Test: `services/control-plane/tests/test_memory_consolidator.py`、`test_skill_curator.py` + 新集成测(照 `test_workspace_lock_integration.py` 形状)

**Interfaces:**
- 两 worker `__init__` 增加 `session_factory`(挂锁用;调用点 app.py 装配处补传;in-memory 栈无 session_factory 时锁步骤跳过——与 quality_drift 同款降级)。
- 锁键:`pg_try_advisory_xact_lock(:classid, hashtext(:key))`,classid 各自新常量(照 `_DRIFT_LOCK_CLASSID` 命名),key = `"memory_consolidator"` / `"skill_curator"`。抢不到 → rollback + return 0(照 quality_drift_worker.py:188-193 逐字形状)。
- `create_app` 三参数默认值不变(测试兼容);main.py 显式传 settings 值。

- [ ] **Step 1(RED)**:①settings 测:env `EXPERT_WORK_ENABLE_SCHEDULER=false` → create_app 后 app.state 无 consolidator 任务(或等价可观察断言);②集成测:两 session 并发 run_once,断言只有一个真执行(另一个返回 0)。跑 → FAIL。
- [ ] **Step 2(GREEN)**:实现。
- [ ] **Step 3**:`uv run pytest services/control-plane/tests/test_memory_consolidator.py services/control-plane/tests/test_skill_curator.py -m "not integration" -q` + 集成测 `DOCKER_HOST=` 全绿。
- [ ] **Step 4**:Commit `fix(workers): consolidator/curator 加 advisory 单飞锁 + 后台任务三开关接 env(生产可关)`

### Task 4: OrphanSweep `_fail_orphan` CAS 守卫

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`(SQL+in-memory 加 `fail_if_active`)
- Modify: `services/control-plane/src/control_plane/orphan_sweep.py:200-212`(`_fail_orphan` 改用;False 时跳过 audit+counter)
- Test: `packages/expert-work-runtime/tests/test_run_store.py`、`services/control-plane/tests/test_orphan_sweep.py`

**Interfaces:**
- `async def fail_if_active(self, *, run_id, tenant_id, error: str, now: datetime) -> bool` —— `UPDATE agent_run SET status='error', error=:e, finished_at=:now, updated_at=:now WHERE id=:id AND tenant_id=:t AND status IN ('running','pending') RETURNING`,rowcount>0(照 `approval/sql.py:177-215 mark_decided` 形状;谓词与 `request_cancel` 的 active 集合一致)。in-memory 同谓词。

- [ ] **Step 1(RED)**:store 测:RUNNING 行两次并发 `fail_if_active` → 恰一个 True;ERROR 行再调 → False。sweep 测:第二次 sweep 同一孤儿不再发 audit(mock audit 计数=1)。跑 → FAIL。
- [ ] **Step 2(GREEN)**:实现;`_fail_orphan` 拿 False 时 debug 日志返回,不 inc 不 audit。
- [ ] **Step 3**:`uv run pytest packages/expert-work-runtime/tests/test_run_store.py services/control-plane/tests/test_orphan_sweep.py -m "not integration" -q` 全绿。
- [ ] **Step 4**:Commit `fix(orphan-sweep): 终态迁移加 CAS 守卫(fail_if_active),多副本不重复 audit/计数`

---

## 验证(整 PR)

- 四 task 测试全绿 + CI 门四道全过。
- follow-up 池记录:webhook 熔断器 per-replica(可容忍,暂不改)、#9 三胞胎 TOCTOU 归 PR-2。
