# 17 Audit Log — 合规级操作审计（与 event_log 分离）

> **event_log = agent 行为追溯**（"agent 调了什么 tool"），**audit_log = 合规级操作审计**（"who did what when from where, allowed or denied"）。两套表、两套保留策略、两套查询路径。M0 基础 → M1 WORM → M2 hash chain 防篡改。

---

## 1. 职责 & 边界

### ✅ 做
- 记录"管理性 / 合规性"操作：登录、manifest 修改、secret 访问、配额修改、role 授予、审计本身的查询
- **与 event_log 物理分离**：独立表、独立保留策略、独立查询接口
- WORM（write-once-read-many）保证：M1 数据库角色禁 UPDATE/DELETE + S3 Object Lock 备份
- 合规保留期（按 tenant_config.audit_retention_days；HIPAA 默认 2555 天）
- 查询 API：tenant scoping，admin 可看全 tenant
- PII / secret 自动脱敏（reuse 通用 redactor 中间件）
- M2：hash chain 防篡改（每条引用前一条 hash）
- 自审计：admin 查 audit log 也写一条 audit
- 合规导出（M2）：按 tenant 一键导出指定时间窗的全部 audit

### ❌ 不做
- Agent 行为追溯（tool 调用、LLM 输出）→ event_log（vendor 自 deer-flow）
- 应用层 INFO/DEBUG 日志 → Loki
- Metric / span → [20 Observability](./20-observability.md)
- 实时安全告警（异常行为检测）→ M2 的 SIEM 集成层（消费 audit_log）

---

## 2. 上下游依赖

| 依赖方向 | 子系统 | 关系 |
|---------|--------|------|
| 上游写入方 | [15 AuthN/AuthZ](./15-authn-authz.md) | 登录 / 授权决策每条都写 |
| 上游写入方 | [16 Quota](./16-quota-rate-limit.md) | quota 写操作 + 429 采样 |
| 上游写入方 | Control Plane（manifest CRUD）| manifest 写、签名、发布 |
| 上游写入方 | [11 Credential Proxy](./11-credential-proxy.md) | secret 读 / 写 / 轮换 |
| 上游写入方 | [14 Sandbox Pool](./14-sandbox-pool.md) | force_destroy、配额拒绝（高风险事件）|
| 下游 | Postgres + S3 (Object Lock) | 主存 + WORM 备份 |
| 横切 | [15 AuthN/AuthZ](./15-authn-authz.md) | 查询 audit 自身需 audit:read 权限 |
| 横切 | 通用 Redactor middleware | details_jsonb 写入前过 PII / secret 脱敏 |

---

## 3. 数据模型 / 状态机

### 3.1 Postgres DDL

```sql
CREATE TABLE audit_log (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  actor_type    TEXT NOT NULL,             -- user / service_account / system / agent
  actor_id      TEXT NOT NULL,             -- user.id / sa.id / 'system' / agent_name@version（统一 TEXT，兼容非 UUID 形态）
  on_behalf_of  TEXT,                      -- service_account 触发时记录原始 user
  action        TEXT NOT NULL,             -- 见 5.1 action 词表
  resource_type TEXT NOT NULL,             -- manifest / session / sandbox / secret / audit / quota / user / role_binding / api_key / dr / eval / subagent
  resource_id   TEXT,                      -- 具体 id（manifest = name@version）
  result        TEXT NOT NULL,             -- success / denied / error
  reason        TEXT,                      -- denied / error 时填
  ip            INET,
  user_agent    TEXT,
  request_id    UUID,                      -- 与 trace_id 关联
  trace_id      TEXT,                      -- W3C
  details       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 已脱敏
  prev_hash     BYTEA,                     -- M2: 前一条 hash
  row_hash      BYTEA,                     -- M2: 本条 hash = sha256(canonical_json + prev_hash)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 主索引：按 tenant + 时间倒序查（最常见查询）；本表索引以 17 为权威定义，23 不再重复
CREATE INDEX audit_log_tenant_created_idx ON audit_log (tenant_id, created_at DESC);
CREATE INDEX audit_log_actor_idx ON audit_log (tenant_id, actor_type, actor_id, created_at DESC);
CREATE INDEX audit_log_resource_idx ON audit_log (tenant_id, resource_type, resource_id, created_at DESC);
CREATE INDEX audit_log_action_idx ON audit_log (tenant_id, action, created_at DESC);
CREATE INDEX audit_log_request_idx ON audit_log (request_id);

-- M2: Postgres 角色 audit_writer 仅 INSERT 权限；audit_reader 仅 SELECT；管理员账号也不能 UPDATE/DELETE
-- REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;
-- GRANT INSERT ON audit_log TO audit_writer;
-- GRANT SELECT ON audit_log TO audit_reader;

-- 分区（M1，按月分区，方便归档与清理）
-- 详见 [23 Postgres Scalability](./23-postgres-scalability.md)
```

### 3.2 Pydantic schema

```python
class AuditEntry(BaseModel):
    tenant: str
    actor_type: Literal["user", "service_account", "system", "agent"]
    actor_id: str
    on_behalf_of: str | None = None
    action: str                            # 见 5.1
    resource_type: Literal["manifest","session","sandbox","secret","audit",
                           "quota","user","role_binding","api_key",
                           "dr","eval","subagent"]
    resource_id: str | None = None
    result: Literal["success", "denied", "error"]
    reason: str | None = None
    ip: IPvAnyAddress | None = None
    user_agent: str | None = None
    request_id: UUID | None = None
    trace_id: str | None = None
    details: dict = Field(default_factory=dict)  # 写入前过 redactor

class AuditQuery(BaseModel):
    tenant: str | Literal["*"] = ...        # admin 才能填 *
    actor_id: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    result: Literal["success","denied","error"] | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None
    limit: int = 100                        # 上限 1000
    cursor: str | None = None               # base64(id)，用于分页
```

---

## 4. 关键接口

### 4.1 Python（包内 API）

```python
class AuditLogger:
    async def write(self, entry: AuditEntry) -> None:
        """同步写主库 + 异步推 S3（M1）；写入前过 redactor。"""

    async def query(self, q: AuditQuery, principal: Principal) -> AuditPage:
        """查询本身写 audit (action='audit:read')。"""

    async def export(self, tenant: str, from_ts, to_ts) -> ExportJob:
        """合规导出（M2）：异步任务，产物落 S3 + 签名链接。"""
```

### 4.2 HTTP API

```
GET  /v1/audit?actor_id=&action=&resource_type=&from=&to=&limit=&cursor=
                                              → AuditPage（X-Expert-Work-Tenant 必填）
GET  /v1/audit/{id}                           → AuditEntry（detail）
POST /v1/audit/exports                        → 202 + export_job_id（admin only, M2）
GET  /v1/audit/exports/{id}                   → ExportJob（含下载链接）
```

**关键约束**：
- 无 `POST /v1/audit`（外部不允许写 audit；只能由内部服务通过 Python API 写）
- 无 `DELETE`（任何 actor 都不能删；M1 起 DB 层 REVOKE）
- 查询响应包含 `prev_hash / row_hash`（M2），调用方可自行验证链完整性

### 4.3 写入接入约定

所有上游通过 FastAPI dependency `audit: AuditLogger = Depends(get_audit_logger)`，统一格式：

```python
await audit.write(AuditEntry(
    tenant=tenant,
    actor_type="user",
    actor_id=principal.id,
    action="manifest:write",
    resource_type="manifest",
    resource_id=f"{name}@{version}",
    result="success",
    ip=request.client.host,
    request_id=request.state.request_id,
    trace_id=current_trace_id(),
    details={"diff_lines_added": 42, "diff_lines_removed": 7},
))
```

**关键决策**：写 audit 不 raise（除了 redactor 校验失败）；DB 失败转入本地 fallback queue（磁盘 jsonl）+ 告警，避免审计失败拖垮业务。

---

## 5. 算法 / 关键决策

### 5.1 Action 词表（强 schema，不允许自由文本）

格式：`<resource>:<verb>`，verb 与 [15 AuthN](./15-authn-authz.md) action enum 对齐：

```
auth:login, auth:logout, auth:login_failed, auth:token_refresh
manifest:read, manifest:write, manifest:delete, manifest:sign, manifest:publish
manifest:restore, manifest:purge                                     -- B-278 恢复 / 彻底删除智能体
session:read, session:write, session:cancel, session:debug
session:resume, session:pause, session:force_resume                   -- 与 19 durable execution / 25 HITL 联动
run:completed, run:failed                                              -- 编排器 run_agent worker 在 run 结束时写（F-3）
sandbox:acquired, sandbox:force_destroy, sandbox:quota_denied            -- acquired 由 F.1 supervisor 写
secret:read, secret:write, secret:rotate, secret:delete
quota:read, quota:write, quota:rate_limit_denied                      -- denied 走采样
user:create, user:update, user:disable
role_binding:create, role_binding:delete
api_key:create, api_key:revoke
audit:read, audit:export
dr:restore, dr:failover, dr:drill                                     -- 与 22 disaster recovery 联动
eval:force_promote                                                    -- 与 26 eval gate 联动（手工跳过 gate 强制晋级）
subagent:spawn_denied                                                 -- 与 24 subagent execution 联动（quota / depth 限制拒绝）
subagent:depth_limit_violation                                        -- 与 24 联动：超出 max_depth 触发；写 audit + event_log
subagent:cross_tenant_attempt                                         -- 与 24 联动：尝试 spawn 跨租户 child（必拒绝），P1 安全告警
```

CI lint 校验代码里所有 `audit.write(action=...)` 都在词表内。

### 5.2 PII / Secret 脱敏

**关键决策**：写入前强制经过通用 redactor 中间件，按 `tenant_config.pii_fields` + 全局 secret pattern 过滤 `details` 字段。

- 全局 pattern：`/sk-[A-Za-z0-9]{20,}/`、`/aforge_pat_[A-Za-z0-9_]+/`、JWT 三段、bcrypt hash、私钥头
- per-tenant：tenant_config.pii_fields 列出的字段名递归 mask
- 不允许 details 包含密码、JWT、API key 明文 —— 检测到直接拒写并告警

### 5.3 保留期与归档

```python
retention_days = tenant_config.audit_retention_days  # 默认 90, HIPAA 2555
```

- M0：单 Postgres 表 + cron 删过期行（`DELETE WHERE created_at < now() - interval '90 days'`）
- M1：Postgres 月分区 + S3 Object Lock 备份（compliance mode，retention=audit_retention_days）+ 过期分区 detach 归档
- M2：归档分区可通过 Athena / DuckDB 查 S3 parquet（冷数据查询）

**关键决策**：retention 多 tenant 不同 → 分区按 (tenant_short, month) 双键 + 每 tenant 独立 cleanup job；不能用全局 retention。

### 5.4 Hash chain（M2）

每条 audit_log row：

```
canonical = json_canonicalize({tenant, actor_type, actor_id, action, resource_type,
                               resource_id, result, ip, request_id, details, created_at})
row_hash = sha256(canonical || prev_hash)
```

- `prev_hash` = 同 tenant 上一条的 `row_hash`（每 tenant 一条链）
- 链头：`prev_hash = sha256(tenant + 'genesis')`
- 周期性（每天）：把当天最后一条的 row_hash 公证（写入只读对象存储 + 第三方时间戳服务）
- 验证：扫描时间窗，重算每行 hash，对比 row_hash；发现不一致 → P0 告警

代价：写入串行化（同 tenant），需 `SELECT ... FOR UPDATE` 锁该 tenant 上一条；高 QPS tenant 评估异步链构建（chain builder 后台 job）。

### 5.5 自审计（meta-audit）

admin 查 audit 自身也写一条 `action='audit:read'`，避免"管理员悄悄查 audit 不留痕"。details 里记录查询条件（tenant、from、to、actor_id 等），但**不记录返回结果内容**（避免 audit 互相膨胀）。

### 5.6 写入路径

```
应用层 audit.write(entry)
  ↓ Pydantic 校验 + 词表 check
  ↓ Redactor middleware（PII / secret）
  ↓ INSERT INTO audit_log RETURNING id, row_hash
  ↓ 异步 push S3（M1+，object key = {tenant}/{YYYY/MM/DD}/{id}.json）
  ↓ 失败 → 本地磁盘 jsonl 队列 + 5min 重试 + 告警
```

---

## 6. 失败模式 & 缓解

| 失败模式 | 影响 | 缓解 |
|---------|------|------|
| Postgres 不可写 | audit 丢失风险 | 本地 fallback jsonl + reaper 重放；告警 |
| Redactor 漏过 secret | 合规事故 | 多层 pattern + CI 测试用例（每个新 secret 类型加 case）+ DB INSERT 触发器 final check |
| 审计写失败但业务成功 | 操作无痕 | 关键操作（secret:write / role_binding:create / manifest:sign）走"先写 audit 再执行业务"模式（同事务）|
| 大 details 撑爆表 | 写延迟 | details 大小限制 16KB；超出走截断 + 引用 S3 完整 payload |
| 时间倒流（NTP 漂移）| 时间排序错乱 | 只信服务器时间；M1 起 NTP 强制；hash chain 不依赖时间排序 |
| Hash chain 锁竞争（高 QPS）| 写延迟激增 | M2 异步 chain builder：先 INSERT 不算 hash，后台 job 顺序补 hash + sign |
| 跨 tenant 误查（admin 误填 *）| 信息泄漏 | tenant='*' 必须 admin role + step-up auth + 自身 audit 强制记录 |
| 归档 S3 Object Lock 配置错（governance 而非 compliance）| 可被删除 | IaC 强制 compliance mode + lock period 与 retention_days 对齐；定期合规扫描 |
| Cleanup 误删未归档数据 | 永久丢失 | cleanup 前校验该分区已 backup_acked=TRUE；缺失则跳过 + 告警 |
| 查询慢（无索引命中）| 查询超时 | 强制至少一个 indexed 字段过滤（tenant + (actor_id / resource / action)）；无则拒绝（400）|

---

## 7. 可观测性

> 日志完整字段遵循 [20 § 5.3](./20-observability.md)。

### 7.1 Prometheus metric

```
expert_work_audit_write_total{tenant, action, result}                     counter
expert_work_event_log_append_duration_seconds                             histogram   # 与 20 § 5.2 对齐（统一 *_duration_seconds 命名）
expert_work_audit_redact_hit_total{pattern}                               counter
expert_work_audit_fallback_queue_depth                                    gauge
expert_work_audit_query_total{tenant_scope="self|cross", result}          counter
expert_work_audit_query_duration_seconds                                  histogram   # 命名对齐 *_duration_seconds
expert_work_audit_hash_chain_break_total{tenant}                          counter     # M2，应永远 0
expert_work_audit_archive_lag_seconds{tenant}                             gauge
```

### 7.2 OTel span

- `audit.write`（attrs：tenant, agent_name, agent_version, actor_type, action, resource_type, result, redact_hits）
- `audit.query`（attrs：tenant_scope, agent_name, agent_version, filters, returned_rows, latency_ms）
- `audit.hash_verify`（attrs：tenant, range_count, mismatch_count）

**告警**：`audit_fallback_queue_depth > 0` 持续 1min → P1；`audit_hash_chain_break_total > 0` → P0。

---

## 8. 安全考虑

- **不可变性**：M1 起 DB role REVOKE UPDATE/DELETE；admin 也不能改；任何"修正"通过新增 audit 行（action='audit:correction'）说明
- **机密泄漏**：Redactor + 全局 pattern + size limit + DB trigger final check（多重防护）
- **审计本身的权限**：`audit:read` action 默认仅 admin / 合规员 role；普通 operator 看不到
- **跨 tenant 查询**：tenant='*' 限制 admin + step-up + meta-audit 强制
- **导出文件保护**：M2 export 产物加密（KMS）+ 签名 URL 5min 过期 + 下载行为再写一条 audit
- **Replay 攻击**：客户端不能伪造 ip / user_agent → 由 server 从 request 提取（不接受 client 提交的这些字段）
- **审计风暴**：429 拒绝、failed_login、quota_denied 这些高频事件做采样（1%）+ 每分钟 summary 行，避免 attacker 用爆破撑爆 audit 表
- **审计 reader 越权**：审计员可能恶意查别人的活动 → 全部查询本身走 meta-audit + 周期性合规审查

**关键决策**：audit_log 表对应用账号是 INSERT-only，对 reader 账号是 SELECT-only；连接池为 audit 提供独立账号，应用账号无法读 audit log（隔离写读权限）。

---

## 9. M0 / M1 / M2 演进

### M0 —— 基础审计
- audit_log 单表 + 三个索引
- AuditLogger Python API + redactor 集成
- 词表 lint
- 查询 API（限 tenant scope，admin 全 tenant）
- cron 按 retention_days 删过期
- 本地 jsonl fallback

### M1 —— WORM + 分区
- DB role 隔离（writer / reader）+ REVOKE UPDATE/DELETE
- Postgres 月分区（[23](./23-postgres-scalability.md)）
- S3 Object Lock（compliance mode）异步备份
- per-tenant retention 分区清理
- 关键操作"先写 audit 再业务"事务模式

### M2 —— 防篡改 + 合规导出
- Hash chain（异步 builder）+ 每天公证
- 合规导出 API（GDPR 数据请求等）
- Athena / DuckDB 冷查询 S3 归档
- SIEM 集成（audit → Splunk / 对接客户合规系统）

---

## 10. 开放问题

1. **粒度边界**：`session:read` 是否每次都写？session 详情查看可能很高频。倾向：列表查询不写，单条详情查询写；debug 模式必写。
2. **on_behalf_of 的链路**：service account 触发自动化任务，背后可能是 cron 而非 user，那 on_behalf_of 应填什么？倾向：cron job 注册时分配虚拟 actor，actor_type=system + actor_id=cron_xxx。
3. **Hash chain 是否对所有 tenant 开**：HIPAA 客户需要，普通客户成本高。倾向：tenant_config.audit_hash_chain=bool，默认按 compliance_pack 自动启用。
4. **跨 region 副本**：M3 多 region 时 audit 是否每 region 一份还是中心化？合规上倾向就近写 + 每天同步到主 region 归档。
5. **Agent 自身写 audit**：Agent 内部行为应走 event_log 而非 audit_log，但"agent 主动调 secret API"这种边界事件归哪？倾向：走 audit（敏感）+ event_log（链路完整）双写，actor_type=agent。
