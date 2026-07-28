# 16 Quota / Rate Limit — 多维度令牌桶 + Token 预算 Reservation

> 三层限流（IP / tenant / provider key）+ 四元组维度（tenant × agent × user × model）令牌桶 + 大模型 token 预算 reservation。一个租户的过载不能拖垮其他租户，一个 agent 的 token 爆炸要在调用前可预算。

---

## 1. 职责 & 边界

### ✅ 做
- **多维度令牌桶**：`(tenant, agent, user, model)` 四元组的任意组合
- 三层限流：网关层（per IP / API key）→ 业务层（per tenant / agent）→ provider 层（per LLM key）
- **Token 预算 reservation**：[10 LLM Gateway](./10-llm-gateway.md) 调用前 reserve(estimated)，调用后 commit(actual) 或 release
- 静态 quota（M0：QPS、daily token cap）
- 动态 quota（M1：按月预算分摊到日，按用量 reshape）
- 429 响应 + Retry-After header
- 多维度叠加（AND，任一拒绝即拒绝）
- Quota 来源合并：`tenant_quota` 表 + manifest.policies.rate_limit override

### ❌ 不做
- 身份验证 → [15 AuthN/AuthZ](./15-authn-authz.md)
- Sandbox 实例数 quota → [14 Sandbox Pool](./14-sandbox-pool.md)（acquire 时调本子系统校验"sandbox" 维度）
- 计费 / chargeback 报表 → 业务运营层（消费 token_usage_middleware 数据）
- 自适应限流（基于错误率自动降速）→ M2 考虑

---

## 2. 上下游依赖

| 依赖方向 | 子系统 | 关系 |
|---------|--------|------|
| 上游调用方 | API Gateway middleware | 每个 HTTP 请求过 `check(...)` |
| 上游调用方 | Orchestrator | LLM 调用前 `reserve_tokens` |
| 上游调用方 | [10 LLM Gateway](./10-llm-gateway.md) | provider key 维度限流 |
| 上游调用方 | [14 Sandbox Pool](./14-sandbox-pool.md) | sandbox 实例数维度 |
| 下游 | Redis（cluster）| 令牌桶状态（Lua atomic） |
| 下游 | Postgres | quota 配置、月度预算 |
| 横切 | [15 AuthN/AuthZ](./15-authn-authz.md) | tenant 必须先认证；防伪造 header |
| 横切 | [17 Audit Log](./17-audit-log.md) | quota 写操作 audit；429 拒绝 audit（采样）|
| 横切 | [20 Observability](./20-observability.md) | 拒绝率、桶水位 metric |

---

## 3. 数据模型 / 状态机

### 3.1 Postgres DDL

```sql
-- tenant 维度配额（管理员配置）
CREATE TABLE tenant_quota (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  dimension       TEXT NOT NULL,        -- qps / tokens_per_day / sandboxes / monthly_token_budget
  scope           JSONB NOT NULL,       -- {"agent": "*"} 或 {"agent": "code-reviewer", "model": "claude-sonnet-4-5"}
  limit_value     BIGINT NOT NULL,
  burst           BIGINT,               -- 仅 qps 维度有意义
  effective_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
  effective_until TIMESTAMPTZ,
  updated_by      TEXT,                 -- actor_id（user.id / 'system' / agent_name@version）
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, dimension, scope)
);
CREATE INDEX ON tenant_quota (tenant_id, dimension);

-- 月度预算消耗（动态分摊用）
CREATE TABLE token_budget_ledger (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  month           DATE NOT NULL,        -- 月份首日
  budget_total    BIGINT NOT NULL,      -- 月初拷贝自 tenant_quota
  used_total      BIGINT NOT NULL DEFAULT 0,
  reserved_total  BIGINT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, month)
);

-- Reservation 记录（用于异常 release / reconcile）
CREATE TABLE token_reservation (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       TEXT NOT NULL,
  agent_name      TEXT NOT NULL,
  thread_id       UUID NOT NULL,        -- 全局 thread_id 类型，对齐 19/24
  parent_thread_id UUID,                -- subagent 模式：指向 lead 的 thread_id（见 § 5.4）
  model           TEXT,                 -- M0 记录不参与限流；M1 起加入维度
  estimated       BIGINT NOT NULL,
  actual          BIGINT,
  state           TEXT NOT NULL,        -- RESERVED / COMMITTED / RELEASED / EXPIRED
  reserved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at       TIMESTAMPTZ
);
CREATE INDEX ON token_reservation (state, reserved_at);  -- 过期 reaper
```

### 3.2 Redis key schema

```
qb:{dim}:{value}                 # 令牌桶 hash {tokens, last_refill_ms}
  例：qb:tenant:medical-saas
  例：qb:agent:medical-saas:triage-agent
  例：qb:user:medical-saas:user-123
  例：qb:model:tenant=medical-saas:model=claude-sonnet-4-5
  例：qb:provider_key:anthropic:key-id-A1

budget:tenant:{tenant}:{YYYY-MM}            # 月度 hash {limit, used, reserved}
budget:tenant:{tenant}:{YYYY-MM}:daily:{D}  # 日预算 hash
```

TTL：桶 key 30 天滚动；budget key 60 天（跨月对账留出窗口）。

### 3.3 Pydantic schema

```python
class CheckRequest(BaseModel):
    tenant: str
    agent: str | None = None
    user: str | None = None
    model: str | None = None
    cost: int = 1                          # 消耗多少令牌（QPS 维度通常 1）
    purpose: Literal["production", "summarization", "eval", "judge"] = "production"
    # M0 仅记录到 metric label，不参与限流；M2 起 eval/judge 走独立 bucket

class CheckResult(BaseModel):
    allowed: bool
    blocked_dimension: str | None = None   # 第一个拒绝的维度
    retry_after_s: int | None = None
    remaining: dict[str, int]              # 各维度剩余令牌

class ReserveRequest(BaseModel):
    tenant: str
    agent: str
    thread_id: str
    estimated_tokens: int
    model: str | None = None                 # M0 仅记录（写入 token_reservation 便于审计/对账），不参与限流；M1 起加入维度（与 5.6 三层限流的 model 维度联动）
    purpose: Literal["production", "summarization", "eval", "judge"] = "production"
    # M0 仅记录到 metric label，不参与限流；M2 起 eval/judge 走独立 bucket
    # metric label 规范同步：expert_work_quota_* 系列加可选 purpose label（默认 production）

class ReserveResult(BaseModel):
    reservation_id: UUID
    granted: bool
    reason: str | None = None              # over_budget / dimension_blocked

class CommitRequest(BaseModel):
    reservation_id: UUID
    actual_tokens: int                     # 实际消耗
```

---

## 4. 关键接口

### 4.1 Python（包内 API）

```python
class QuotaService:
    async def check(self, req: CheckRequest) -> CheckResult: ...
    async def reserve_tokens(self, req: ReserveRequest) -> ReserveResult: ...
    async def commit_tokens(self, req: CommitRequest) -> None: ...
    async def release_tokens(self, reservation_id: UUID) -> None: ...
    async def reset_quota(self, tenant: str, dimension: str) -> None: ...   # admin
```

### 4.2 HTTP API（仅内部服务调用）

```
POST /v1/quota/check                Body: CheckRequest         → CheckResult
POST /v1/quota/reserve              Body: ReserveRequest       → ReserveResult
POST /v1/quota/commit               Body: CommitRequest        → 204
POST /v1/quota/release/{id}                                    → 204

# 配置 API（admin）
GET  /v1/tenants/{tenant}/quotas
POST /v1/tenants/{tenant}/quotas    Body: tenant_quota row     → 201
DELETE /v1/quotas/{id}
```

### 4.3 429 响应格式

```json
{
  "error": "rate_limit_exceeded",
  "dimension": "tokens_per_day",
  "tenant": "medical-saas",
  "agent": "triage-agent",
  "retry_after_s": 3672,
  "doc": "https://docs.expert_work.io/errors/rate_limit"
}
```

HTTP header：`Retry-After: 3672`（秒数）。

---

## 5. 算法 / 关键决策

### 5.1 令牌桶（Redis Lua atomic）

**关键决策**：所有桶操作走单脚本 atomic，避免 race condition。

```lua
-- KEYS[1] = bucket key
-- ARGV[1] = capacity, ARGV[2] = refill_rate (tokens/sec * 1000),
-- ARGV[3] = now_ms, ARGV[4] = cost
local b = redis.call('HMGET', KEYS[1], 'tokens', 'last_ms')
local tokens = tonumber(b[1]) or tonumber(ARGV[1])
local last_ms = tonumber(b[2]) or tonumber(ARGV[3])
local elapsed = math.max(0, tonumber(ARGV[3]) - last_ms)
tokens = math.min(tonumber(ARGV[1]),
                  tokens + elapsed * tonumber(ARGV[2]) / 1000)
local cost = tonumber(ARGV[4])
if tokens < cost then
  -- 估算 retry_after
  local need = cost - tokens
  local retry_ms = math.ceil(need * 1000 / tonumber(ARGV[2]))
  return {0, retry_ms, math.floor(tokens)}
end
tokens = tokens - cost
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last_ms', ARGV[3])
redis.call('PEXPIRE', KEYS[1], 30 * 86400 * 1000)
return {1, 0, math.floor(tokens)}
```

返回 `{allowed, retry_after_ms, remaining}`。

### 5.2 多维度叠加（AND）

**关键决策**：所有命中的维度都过 → AND；任一拒绝即拒绝；返回**第一个**拒绝的维度。

```python
async def check(req):
    dims = build_dimensions(req)              # 按 quota 表展开成 [(key, capacity, refill, cost)]
    # 按 capacity 升序 check（最严格的先过，减少不必要 Redis 调用）
    dims.sort(key=lambda d: d.capacity)
    for d in dims:
        ok, retry_ms, remaining = await redis_eval(d)
        if not ok:
            return CheckResult(allowed=False, blocked_dimension=d.name,
                               retry_after_s=ceil(retry_ms / 1000))
    return CheckResult(allowed=True, remaining={...})
```

注意：拒绝时不能 rollback 已扣减的桶——会造成"AB 都过、AB 都被扣"。**正确做法**：先做"试扣"（query mode）再统一 commit。或：所有维度桶 fail 后接受少量"过扣"（实际生产中可接受，因为下次请求会被拒绝）。

**采用方案**：M0/M1 接受少量过扣（误差 < 1 个 cost），实现简单；M2 升级"两阶段"。

### 5.3 Quota 配置合并

来源优先级（高 → 低）：

1. **manifest.policies.rate_limit**（agent 自身覆盖；前提：值不能宽于 tenant 上限）
2. **tenant_quota 表**（agent / user 维度）
3. **tenant 默认**（plan-level：free / pro / enterprise）

合并发生在每次 `check()` 时，结果缓存 60s（按 (tenant, agent) key）。

### 5.4 Token 预算 reservation

**调用流程**（[10 LLM Gateway](./10-llm-gateway.md) 集成）：

```
LLM 调用前：
  estimated = len(prompt_tokens) + max_tokens
  reserve_id = QuotaService.reserve_tokens(tenant, agent, thread, estimated)
  if not granted: 抛 BudgetExceeded
LLM 调用后（成功）：
  QuotaService.commit_tokens(reserve_id, actual = input_tokens + output_tokens)
LLM 调用后（失败 / 取消）：
  QuotaService.release_tokens(reserve_id)
```

**`actual_tokens` 口径**（与 10 § 5.3 对齐）：`actual_tokens = input_tokens + output_tokens`。`cache_read_input_tokens` 和 `cache_creation_input_tokens` **不计入** budget commit（已在 provider 端享受折扣，重复扣会双重计费），但单独通过 metric `expert_work_llm_cache_read_tokens_total` / `expert_work_llm_cache_creation_tokens_total` 上报供成本分析（与 20 命名规范一致）。

**算法**：

```
reserve_total += estimated
if used_total + reserve_total > budget_total: deny
commit:  used_total += actual; reserve_total -= estimated
release: reserve_total -= estimated
```

reservation 30min 未 commit/release 自动 release（reaper job）。

**关键决策**：reserve 时拒绝预算超限，避免"已经花了 token 但 budget 拒绝"的尴尬；actual 通常 < estimated（max_tokens 是上限），commit 时多还少补。

#### Subagent 模式：commit 触发 lead bucket 二次累加

与 [24 Subagent Execution](./24-subagent-execution.md) 联动。child agent 走自己的 `child_thread_id` 做 reservation，**commit 时 server 端**根据 `parent_thread_id`（在 `token_reservation` 行或 metadata 中携带）识别父 thread 对应的 lead agent，**自动**把 `actual_tokens` 累加到 lead 的 `token_budget_ledger`：

```
child commit(reserve_id, actual):
  1. token_reservation[reserve_id].state = COMMITTED, actual=actual
  2. budget_ledger[(tenant, lead_agent, month)].used_total += actual   # 二次累加
  3. metric expert_work_subagent_token_total{tenant, lead, child, model} += actual
```

防爆边界：lead 维度独立设 `monthly_token_budget`；子树整体超限时拒绝在 child reserve 层（`if lead.used_total + child.estimated > lead.budget: deny`）。agent 维度 metric 仍按 child 独立计（observability 可见每个 child 的成本贡献）。

### 5.5 月度预算动态分摊（M1）

```
daily_quota = remaining_budget / remaining_days * smooth_factor
smooth_factor = 1.5  # 允许某天突破到 1.5× 平均
但月底前 5 天强制收紧到 1.0×
```

每日 00:00 跑 reset job，刷新 `budget:tenant:{t}:{YYYY-MM}:daily:{D}`。

### 5.6 三层限流分工

| 层 | 维度 | 实现位置 |
|----|------|---------|
| 网关 | per IP / per API key | API Gateway middleware（最早拦截）|
| 业务 | per tenant / agent / user | 本子系统 |
| Provider | per (provider, key) | [10 LLM Gateway](./10-llm-gateway.md) 自管 |

---

## 6. 失败模式 & 缓解

| 失败模式 | 影响 | 缓解 |
|---------|------|------|
| Redis 不可达 | 所有限流失效 | 限流层（网关 per-IP/per-apikey + 业务 per-tenant/agent/user）统一 **fail-open 放行** + `expert_work_rate_limit_backend_errors_total` counter 告警；配额准入（花费控制，`_quota_admission` 及 `/v1/quota/*` 端点）**fail-closed**，返回 503 `quota_engine_unavailable`——花费风险由准入层兜底，限流层 fail-open 不产生未计量成本（2026-07-28 多副本就绪改造定，替代原 per-维度分裂策略） |
| Lua 脚本未加载 | EVALSHA NOSCRIPT 错误 | 客户端自动 EVAL fallback + 重新缓存 sha |
| Tenant header 伪造 | 跨租户绕过 | [15 AuthN](./15-authn-authz.md) 强校验 |
| 配置漂移（manifest 比 tenant_quota 宽）| 实际无限制 | check 时取 min；CI lint 校验 manifest 不能宽 |
| Reservation 泄漏（commit/release 都没调）| budget 永远不归还 | 30min reaper 强制 release + 告警 |
| 月度预算重置错过 | tenant 跨月仍按上月限制 | reset job + 兜底：check 时 lazy 创建当月 ledger |
| 配额被绕过（未过本子系统） | 业务无控制 | 强制 API gateway 全量经过；CI 检查所有 API 路由都带 quota middleware |
| 桶 capacity 配置错误（太小） | 误拒大量请求 | admin 改配置后 `expert_work_quota_exceeded_total` rate 监控 + 自动告警 |
| Redis cluster 跨 slot 失败 | 多维度合并失败 | 桶 key 用 `{tenant}` hashtag 强制同 slot；M2 跨集群另议 |

---

## 7. 可观测性

> 日志完整字段遵循 [20 § 5.3](./20-observability.md)。

### 7.1 Prometheus metric

```
expert_work_quota_check_total{tenant, dimension, result="allow|deny"}     counter
expert_work_quota_check_latency_seconds                                   histogram
expert_work_quota_bucket_remaining_ratio{tenant, dimension}               gauge
expert_work_quota_exceeded_total{tenant, dimension, reason}               counter   # 与 20 § 5.2 命名对齐；保留 reason label
expert_work_token_reservation_active{tenant}                              gauge
expert_work_token_reservation_expired_total                               counter
expert_work_token_budget_used_ratio{tenant}                               gauge   # used/budget
expert_work_token_budget_overshoot_total{tenant}                          counter
```

### 7.2 OTel span

- `quota.check`（attrs：tenant, agent, dimensions_checked, blocked_dimension, latency_ms）
- `quota.reserve`（attrs：tenant, agent, estimated, granted）
- `quota.commit`（attrs：reservation_id, actual, delta）

**告警示例**：`expert_work_token_budget_used_ratio > 0.8` 持续 10min → tenant admin 通知；`> 1.0` 紧急告警。

---

## 8. 安全考虑

- **配额绕过攻击**：必须先过 [15 AuthN](./15-authn-authz.md)，JWT 与 X-Expert-Work-Tenant 一致才进入 quota check
- **Redis 命令注入**：所有维度名 / 值走 strict whitelist + KEY 拼接前 sanitize（避免 `qb:tenant:foo\nflushall`）
- **配额信息泄漏**：429 响应不能暴露其他 tenant 的 quota 数值；retry_after_s 是粗粒度时间，不暴露当前 token 数
- **管理员误改配额（DoS 自己）**：所有 quota 写操作走 [17 Audit Log](./17-audit-log.md) + 24h 内可一键回滚（保留 history 表）
- **Resource exhaustion**：reservation 表大量泄漏会撑爆 → 30min TTL 兜底
- **审计**：quota 配置 read/write、429 拒绝（采样 1%）、reservation expired 全写 audit

**关键决策**：429 拒绝不全量 audit，按 tenant + dimension 维度做**采样**（1%）+ **聚合统计**（每分钟一条 summary），避免 audit log 被写爆。

---

## 9. M0 / M1 / M2 演进

### M0 —— 静态限流
- 维度：tenant + agent + user 三类（model 维度先不做）
- 配额来源：tenant_quota 表（手工配）；manifest override 暂不做
- Redis 单实例 + Lua 桶
- Token reservation 已上线（避免后期重大改造）
- 429 + Retry-After 标准化

### M1 —— 动态预算 + 全维度
- 加 model 维度（按 model + provider key）
- 月度预算 + 日分摊
- manifest.policies.rate_limit override（CI lint 防越界）
- Redis cluster
- Plan-level 默认（free / pro / enterprise）

### M2 —— 跨集群 + 自适应
- 跨集群全局 counter（基于 Redis Cluster + 每集群 local 桶 + 周期同步）
- 自适应限流（基于错误率自动降速）
- 弹性 burst（按时段动态调 capacity）
- Quota Plans UI（自助升级 / 试用申请）

---

## 10. 开放问题

1. **多维度过扣容忍**：M0/M1 接受少量过扣是否会被合规客户挑战？需求方如严格，M2 必须实现两阶段 commit。
2. **Token 估算误差**：tokenizer 不同 provider 不同，estimated 偏差大；是否引入预估安全系数（× 1.2）？倾向是。
3. **Burst 策略**：tenant 短时突发是否允许超 QPS（burst capacity）？M0 = burst 等于 capacity；M1 评估业务方需求。
4. **Subagent 调用是否计 quota**：父 agent 调子 agent，是否独立计 user 维度？倾向：subagent 走父 thread 的 user，但 agent 维度独立计。
5. **跨集群强一致 vs 最终一致**：M2 跨集群限流，强一致代价高；倾向最终一致 + 节点本地 + 偶尔 1-2% 突破。
