# W1-PR2 限流/配额多副本正确性 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 多副本正确性修复波第 2 批:apikey 限流 HMAC 盐共享化 / Redis 故障策略落地 / 月预算 TOCTOU / count-then-insert 三胞胎 / 配置一致性真守卫 + _respawn 守卫。

**Architecture:** 照仓内先例:`secret_encryption_key` SecretStr 模式、`claim_queued` 条件 UPDATE 哲学、`quality_drift` advisory xact lock、PR-1 的 `fail_if_active`/`request_cancel` 形状。审计出处 `docs/research/2026-07-28-multi-replica-readiness-audit.md` 第 0 波 1 + 第 1 波 4/5/7 项 + PR-1 follow-up 池。

## Global Constraints

- 分支 `deploy-w1-pr2-quota-guards`(基 main cbe0f199)。
- CI 门:`uv run ruff check .` + `uv run ruff format --check .` + CI-scope mypy + control-plane pytest(`-m "not integration"`)+ testcontainers 文件 `DOCKER_HOST= uv run pytest <file> -q`(本机 DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock)。
- 双 store 谓词 byte-identical;新 store 行为必须 SQL/Redis 真容器集成测。
- 实施者所有命令前台同步跑完;创建后台任务=任务失败。
- conventional commits,无 attribution。

---

### Task 1: apikey 限流桶 HMAC 盐共享化

**Files:**
- Modify: `services/control-plane/src/control_plane/settings.py`(照 `secret_encryption_key`(:126-130)形状加 `apikey_rate_limit_hmac_salt: SecretStr | None = None`,docstring 写明:多副本必配同值,未配 fallback 进程随机盐仅单副本安全)
- Modify: `services/control-plane/src/control_plane/middleware/rate_limit.py:46-51`(`_BUCKET_HMAC_KEY` 改为可注入:中间件构造时从 settings 读盐,None 时保留 `secrets.token_bytes(32)` 旧行为;`_derive_bucket_id` 改实例方法或传参)
- Modify: app.py 中间件装配点补传 settings 值
- Test: `services/control-plane/tests/test_rate_limit_middleware.py`

- [ ] Step 1(RED):测试:①配盐时两个独立中间件实例对同一 api key 派生同一 bucket id(跨副本一致性的进程内模拟);②不配盐时行为不变(随机)。跑 FAIL。
- [ ] Step 2(GREEN)实现;Step 3 回归+ruff;Step 4 Commit `fix(ratelimit): apikey 限流桶 HMAC 盐接 settings(多副本同 key 同桶)`

### Task 2: Redis 故障策略落地(fail-open/fail-closed 分层)

**Files:**
- Modify: `services/control-plane/src/control_plane/middleware/rate_limit.py:100-101` 与 `tenant_rate_limit.py:115-116`:`limiter.acquire` 包 try/except RedisError → **fail-open**(放行 + `logger.warning` 限频 + 新 counter `expert_work_rate_limit_backend_errors_total`(`expert_work_counter` 包装器,label backend=gateway/tenant))
- Modify: `services/control-plane/src/control_plane/api/_quota_admission.py:71`:`quota.check` 包 try/except RedisError → **fail-closed** HTTP 503 `quota_engine_unavailable`(照 `redis_quota.py:13-19` docstring 承诺原文)
- Test: 三处各加 RedisError 注入测试(mock limiter/quota 抛 `redis.exceptions.RedisError`)

- [ ] Step 1(RED):①网关限流 Redis 挂 → 请求 200 放行 + counter inc;②租户限流同;③配额准入 Redis 挂 → 503 且 detail=quota_engine_unavailable。跑 FAIL。
- [ ] Step 2(GREEN);Step 3 回归+ruff;Step 4 Commit `fix(ratelimit): Redis 故障策略落地——限流 fail-open+配额准入 fail-closed(兑现 §5.2 退化表承诺)`

### Task 3: reserve_tokens 月预算 TOCTOU(条件 UPDATE)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/quota/sql.py`(`reserve` 路径:月预算检查收进条件 UPDATE——`UPDATE quota_ledger SET reserved_total=reserved_total+:d WHERE tenant+month AND (budget_total<=0 OR reserved_total+used_total+:d<=budget_total) RETURNING`,零行=拒绝;或同事务 `with_for_update()` 锁 ledger 行后 check+bump,选与既有 `_lock_reservation`(:225-242)风格一致者,报告写明选择)
- Modify: `services/control-plane/src/control_plane/quota/redis_quota.py:135-150` 与 `quota/in_memory.py:138-156`(调用路径对齐;in-memory 版把检查+bump 收进 `self._lock` 临界区,谓词与 SQL byte-identical)
- Test: 新 `packages/expert-work-persistence/tests/test_sql_quota_reserve_race.py`(真容器:并发 N 个 reserve 总额恰不超 budget)+ in-memory 并发测 + 既有 `test_quota_in_memory.py` 回归

- [ ] Step 1(RED):真容器并发 10×reserve(each 100,budget 500)→ granted 恰 5;in-memory 同。跑 FAIL。
- [ ] Step 2(GREEN);Step 3 回归+ruff;Step 4 Commit `fix(quota): 月预算预留改条件更新,并发不超发(TOCTOU)`

### Task 4: count-then-insert 三胞胎(per-tenant advisory lock)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/curation.py:115-127`(`_enforce_quota`+insert 收进 advisory xact lock 临界区)
- Modify: `services/control-plane/src/control_plane/api/triggers.py:311` 附近同款
- Modify: `services/control-plane/src/control_plane/api/webhook_endpoints.py:166-167` 同款
- 共享 helper:新 `services/control-plane/src/control_plane/_tenant_resource_lock.py`(`@asynccontextmanager tenant_resource_lock(session_factory, tenant_id, resource_kind)`——`pg_try_advisory_xact_lock(classid, hashtext(tenant_id..resource_kind))` 抢不到抛 429/重试一次;classid 新常量 8618,grep 确认不撞;session_factory None(in-memory 栈)时 no-op 降级,与 quality_drift 同款)
- Test: 三个 API 各一并发测(mock store count/create 编排竞态窗口)+ helper 集成测(真容器两 session 互斥)

- [ ] Step 1(RED);Step 2(GREEN);Step 3 回归(`test_curation_api.py`/`test_eval_dataset_api.py`/`test_triggers*.py`/`test_webhook_endpoints*.py`)+ruff;Step 4 Commit `fix(api): 配额上限检查加 per-tenant 序列化锁——curation/triggers/webhook_endpoints 三胞胎 TOCTOU`

### Task 5: 配置一致性真守卫 + _respawn 守卫

**Files:**
- Modify: `services/control-plane/src/control_plane/app.py` lifespan 入口(约 :1092-1103):启动守卫——`single_instance=False and not quota_redis_url` → `raise RuntimeError("multi-replica requires EXPERT_WORK_QUOTA_REDIS_URL ...")`(兑现 `ratelimit/in_process.py:1-8` docstring 承诺;同步把该 docstring 措辞改准确)
- Modify: `services/control-plane/src/control_plane/orphan_sweep.py:236-249`(`_respawn` kill-switch 分支:无条件 `set_status(INTERRUPTED)` 改 `request_cancel`(已有 CAS,谓词 running/pending),False 时跳过 counter/audit——终审裁决的修法)
- Test: ①create_app(single_instance=False,无 redis url)→ RuntimeError;配了 → 正常;②_respawn kill-switch 败者不重复 audit(照 Task 4-PR1 的 sweep 测试形状)

- [ ] Step 1(RED);Step 2(GREEN);Step 3 回归(`test_orphan_sweep.py`+app factory 测试)+ruff;Step 4 Commit `fix(app): 多副本配置一致性启动守卫 + _respawn kill-switch 改 CAS(follow-up)`

---

## 验证(整 PR)

- 五 task 测试全绿 + CI 门全过;真容器测:quota race + tenant lock。
- follow-up 池消化核对:_respawn(本 PR 修)、三胞胎(本 PR 修);剩余(熔断器/索引已修/常量/打点/长 sweep 锁)保持池内。
