# 11 Credential Proxy

> **凭证零落地**：sandbox 永远拿不到真实 token；proxy 在出站链路实时注入。LLM Gateway / MCP Gateway / HTTP tool 调用所需的 secret 全部经此子系统。

---

## 1. 职责 & 边界

### ✅ 做
- **secret 注入**：sandbox / orchestrator 发出的 outbound HTTP 请求带 `X-Expert-Work-Secret-Ref: <ref>`，proxy 解析后替换为真实 `Authorization: Bearer <token>`（或其他 header / query 参数）
- **secret allow-list**：每个 manifest 在加载时声明能引用的 secret refs，proxy 强制核对；越权引用 → 403
- **进程内 LRU 缓存**：减少 Vault 压力，TTL 取自 secret 元数据（静态 secret 长 TTL，dynamic 短 TTL）
- **Vault 集成**：M0 静态 KV，M1 dynamic（DB credential / 短 TTL token）
- **审计日志**：每次注入写一条 audit（仅记 ref + tenant + agent + timestamp，**绝不记明文 secret**）
- **多种注入位置**：HTTP header / query string / body field（声明式配置）
- **secret 生命周期回调**：被引用的 secret 被吊销时，相关 sandbox 和 in-flight 请求收到 401 后立即触发刷新

### ❌ 不做
- **不做** secret 的存储后端（Vault 才是 source of truth；proxy 只是注入器）
- **不做** outbound 内容审查（属于 [21 Network Policy](./21-network-policy.md)）
- **不做** TLS 终结（依赖 Envoy / 服务网格）
- **不做** 客户端侧的 secret 抽象（这是 SDK 层，业务代码用 `!secret xxx` 引用）

---

## 2. 上下游依赖

```
       sandbox / orchestrator
                │
                │ outbound HTTP (header: X-Expert-Work-Secret-Ref: <ref>)
                ▼
        ┌───────────────────────┐
        │  Credential Proxy     │
        │  (此子系统)            │
        │   M0: aiohttp 反向代理 │
        │   M1: Envoy + Lua     │
        └───┬─────────────┬─────┘
            │             │
            ▼             ▼
       ┌─────────┐   ┌──────────┐
       │  Vault  │   │ 审计日志  │ → [17 Audit Log]
       └─────────┘   └──────────┘
            │
            ▼
       上游真实 API（api.anthropic.com / mcp.internal / ...）
```

调用方：
- **[10 LLM Gateway](./10-llm-gateway.md)** — 取 provider API key
- **[12 MCP Gateway](./12-mcp-gateway.md)** — 取 MCP server 凭证（OAuth token / API key）
- **[14 Sandbox Pool](./14-sandbox-pool.md)** — sandbox 内的所有 outbound HTTP 必须经此 proxy（Network Policy 强制）
- **业务侧 `http` tool** — manifest 声明的 HTTP API 调用

---

## 3. 数据模型 / 状态机

### 3.1 Postgres DDL — secret allow-list 与审计

```sql
-- 每个 (tenant, agent, version) 允许引用的 secret refs
CREATE TABLE secret_allowlist (
    tenant_id      TEXT NOT NULL,
    agent_name     TEXT NOT NULL,
    agent_version  TEXT NOT NULL,
    secret_ref     TEXT NOT NULL,             -- 如 "anthropic/api-key" / "tenant-x/gitlab-token"
    purpose        TEXT,                       -- 自由文本，给审计用
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, agent_name, agent_version, secret_ref)
);
CREATE INDEX secret_allowlist_lookup ON secret_allowlist (tenant_id, agent_name, agent_version);

-- 注入审计（每次成功/失败注入都写一条）— 与 [17 Audit Log] 同表族但独立 source
CREATE TABLE credential_proxy_audit (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    agent_name     TEXT NOT NULL,
    agent_version  TEXT,
    session_id     UUID,
    sandbox_id     TEXT,
    secret_ref     TEXT NOT NULL,
    target_host    TEXT NOT NULL,             -- 真实上游 host（注入后）
    inject_kind    TEXT NOT NULL,             -- header / query / body
    status         TEXT NOT NULL,             -- ok / denied / vault_miss / cached
    error_msg      TEXT,
    duration_ms    INT,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX credential_proxy_audit_tenant_time ON credential_proxy_audit (tenant_id, occurred_at DESC);
CREATE INDEX credential_proxy_audit_session ON credential_proxy_audit (session_id);
```

### 3.2 Pydantic Schema — secret 元数据与注入规则

```python
# packages/expert-work-protocol/src/Expert Work/protocol/credential.py
from pydantic import BaseModel, Field
from typing import Literal

class SecretMetadata(BaseModel):
    """从 Vault 读取的 secret 元数据 (不含明文 value)."""
    ref: str                                   # "anthropic/api-key"
    kind: Literal["static", "dynamic"]
    rotation_ttl_s: int                        # static 默认 3600，dynamic 默认 300
    issued_at: int                             # epoch sec
    expires_at: int | None                     # dynamic 必填

class InjectRule(BaseModel):
    """声明 secret 注入到 outbound 请求的什么位置."""
    secret_ref: str
    inject_to: Literal["header", "query", "body_json"]
    field_name: str                            # "Authorization" / "api_key" / "auth.token"
    value_template: str = "Bearer {value}"     # 默认 Bearer 前缀

class ProxyRouteConfig(BaseModel):
    """每条转发规则：哪些 host -> 用哪些 secret 注入."""
    upstream_host_pattern: str                 # "api.anthropic.com" / "*.internal"
    inject_rules: list[InjectRule]
    allow_methods: list[str] = ["GET", "POST", "PUT", "DELETE"]
    timeout_s: int = 60
```

### 3.3 注入流程状态机

```
[req in]
   │ 1. 解析 X-Expert-Work-Secret-Ref
   ▼
[parse_ref] ─ missing ─► 透传（无敏感注入），仅记 audit
   │ ok
   ▼
[allowlist_check]  query secret_allowlist
   │ deny ─► 403 + audit{status=denied}
   │ ok
   ▼
[fetch_secret]
   │ ┌── lru_cache hit ──► [inject]
   │ ├── miss ──► Vault GET ──► cache put ──► [inject]
   │ └── vault err ──► 502 + audit{status=vault_miss}
   ▼
[inject]
   │ 替换 header / query / body field
   ▼
[forward upstream]
   │ 记录 latency / status_code
   ▼
[audit] write credential_proxy_audit
   ▼
[resp out]   header X-Expert-Work-Secret-Ref 必须从响应中**剥离**
```

---

## 4. 关键接口

### 4.1 调用方契约（sandbox / orchestrator / 控制面服务视角）

**M0 工作模式 = 显式代理**：所有调用方（sandbox、orchestrator、MCP Gateway 等控制面服务）必须**显式**地把 outbound 请求发到 `credential-proxy.internal:443/forward`，并通过 `X-Expert-Work-Upstream` header 声明真实上游目标，通过 `X-Expert-Work-Secret-Ref` header 声明要注入的 secret 引用。proxy 解析后注入真实 `Authorization` 头并转发到上游。

```http
POST https://credential-proxy.internal:443/forward
Host: credential-proxy.internal
X-Expert-Work-Tenant: tenant-x
X-Expert-Work-Agent: code-reviewer-agent
X-Expert-Work-Agent-Version: 1.4.2
X-Expert-Work-Session: <uuid>
X-Expert-Work-Sandbox: <sandbox_id>           # 由 sandbox supervisor 注入
X-Expert-Work-Secret-Ref: anthropic/api-key
X-Expert-Work-Upstream: https://api.anthropic.com/v1/messages
Content-Type: application/json

{...原始请求体...}
```

> **M0 决策：显式代理**（与 [21 § 5.4 网络策略](./21-network-policy.md) iptables allowlist 一致：sandbox 出站只允许 `credential-proxy.internal:443`；任何直连真实上游 IP 的尝试被 DROP）。
> **M1+ 升级 Envoy 后切换到透明代理（REDIRECT）**：按目标 host 自动路由，调用方不再需要显式 header。

#### 控制面服务调用模式（[12 MCP Gateway](./12-mcp-gateway.md) 等）

控制面服务（MCP Gateway、HTTP tool 调用器等）也走相同的 `/forward` 接口；secret 在 proxy 端注入，**不暴露任何"读 secret 明文"的 admin API**（避免凭证泄漏面扩大）。即使是控制面也必须以"提交带 ref 的请求 → proxy 注入 → 转发上游"的模式工作。

### 4.2 SDK helper（业务代码不直接拼 header）

```python
# packages/expert-work-sdk/src/Expert Work/sdk/secret.py
from expert_work.sdk import AgentContext

async def fetch_with_secret(
    ctx: AgentContext,
    upstream_url: str,
    secret_ref: str,
    method: str = "GET",
    json: dict | None = None,
) -> httpx.Response:
    """业务代码使用：实际发往 credential proxy；secret 由 proxy 注入。"""
```

### 4.3 内部管理 API（admin only）

| Method | Path | 说明 |
|---|---|---|
| POST | `/admin/allowlist` | 注册 (tenant, agent, version) 的 secret refs（manifest 加载时调用） |
| DELETE | `/admin/allowlist/{tenant}/{agent}/{version}` | 撤销 |
| POST | `/admin/cache/invalidate` | 强制清进程内缓存（用于 secret 紧急轮换） |
| GET | `/admin/health` | liveness + Vault 连接状态 |

前三条要 `Authorization: Bearer $EXPERT_WORK_CRED_PROXY_ADMIN_TOKEN`（B-31 ②）；
**未配置该 token 时它们答 503，不是放行**。`/admin/health` 不设闸 —— kubelet 的
startup / liveness / readiness 探针都打它。

写入面必须设闸的理由不止「这是管理 API」:`/admin/allowlist` 写的正是 `/forward`
唯一校验的那张表，而 `/forward` 会把解析出的密钥当 `Authorization: Bearer` 发往
**调用方自己指定的 upstream**（该 URL 不做校验）。能写白名单 = 能让代理把任意租户的
密钥送到任意地址。

---

## 5. 算法 / 关键决策

### 5.1 进程内 LRU 缓存策略（**关键决策**）

```python
# 缓存键: (tenant, secret_ref)；不跨租户共享
class SecretCache:
    def __init__(self, max_size=10_000):
        self._lru = LRUDict(max_size)   # OrderedDict-based

    async def get(self, tenant: str, ref: str) -> tuple[str, SecretMetadata]:
        key = (tenant, ref)
        entry = self._lru.get(key)
        if entry and entry.expires_at > now():
            return entry.value, entry.meta
        # miss → fetch from Vault
        value, meta = await vault.read(tenant, ref)
        ttl = min(meta.rotation_ttl_s, 60 if meta.kind == "static" else meta.rotation_ttl_s // 2)
        # 关键：dynamic secret 缓存 TTL = 实际 TTL 的一半，留出刷新窗口
        self._lru.put(key, value, expires_at=now() + ttl, meta=meta)
        return value, meta
```

**TTL 决策依据**：
- static secret（如长期 API key）：60s 内 LRU，避免 Vault 高 QPS
- dynamic secret（如 PostgreSQL 短期凭证）：取 secret TTL 的一半（典型 15min secret → 7.5min 缓存），留出刷新窗口

### 5.2 sandbox 流量强制经过 proxy（**关键决策：显式代理**）

**M0 显式代理模式**：sandbox 内业务代码必须显式 POST 到 `https://credential-proxy.internal:443/forward`，并带 `X-Expert-Work-Upstream` + `X-Expert-Work-Secret-Ref` header；不存在透明拦截，任何直连真实 host 的尝试都会失败。

由 [21 Network Policy](./21-network-policy.md) 配合：sandbox 出站 iptables 仅放行目标 = `credential-proxy.internal:443`；任何尝试直连外部 IP 的流量会被 DROP（不是 REDIRECT，是 DROP，因此调用方必须显式地把请求送到 proxy）。

**测试用例**（M0 必跑）：
1. sandbox 内 `curl https://api.anthropic.com/v1/messages` → **必须 connection refused**（绕过 proxy 直连被 DROP）
2. sandbox 内 `curl -H "X-Expert-Work-Secret-Ref: anthropic/api-key" -H "X-Expert-Work-Upstream: https://api.anthropic.com/..." https://credential-proxy.internal:443/forward` → 200
3. sandbox 内 `cat /run/secrets/* 2>&1` → 全为空
4. sandbox 内 `env | grep -i token` → 无敏感 env

> M1 升级 Envoy 后改为透明 REDIRECT 模式：iptables 把出站重定向到本地 Envoy listener，对调用方无感知。

### 5.3 secret 引用的静态校验（manifest 加载时）

```yaml
tools:
  - http:
      name: notify_slack
      auth: !secret slack-bot-token       # ← lint 时校验
```

校验流程（manifest_loader 的一部分）：
1. 收集 manifest 内所有 `!secret <ref>` 引用
2. 调用 Vault `read_metadata`，确认 ref 存在且当前 tenant 有读权限
3. 写入 `secret_allowlist` 表
4. 任何 ref 不存在 → manifest 加载失败

### 5.4 secret 吊销响应

发生方式：
- secret 被 admin 主动吊销 → Vault 通知 proxy → 调 `/admin/cache/invalidate`
- 上游 API 返回 401 → proxy 自动失效该 ref 的缓存条目 + 记 metric `credential_proxy_upstream_401_total`

> **不做主动 secret push 给已运行 sandbox**：sandbox 下次请求时自然命中新 secret。

---

## 6. 失败模式 & 缓解

| 失败模式 | 触发场景 | 影响 | 缓解 |
|---|---|---|---|
| Vault 不可达 | 网络故障 / Vault 维护 | 所有新 secret 拉取失败 | 进程内 LRU 内仍有效 secret 继续工作；告警 + degraded 模式 |
| Vault 返回 secret 不存在 | manifest 校验漏 / secret 被删 | 该 agent 调用失败 | 立即 502，明确 error_msg；触发 manifest 重新校验 |
| 上游 401（secret 失效） | 厂商吊销 / 过期 | 调用失败 | 失效缓存 + 告警；下次请求拉新 secret 后恢复 |
| 上游 5xx 风暴 | 上游 outage | 请求积压 | proxy 自身有 per-host concurrency limit（默认 200），超过 503 fast-fail |
| sandbox 伪造 ref | 恶意 manifest 引用别人的 ref | 未遂 | allowlist 强校验；ref 命名空间含 tenant 前缀 |
| header 注入攻击 | 用户输入含 `\r\nAuthorization:` | 头部污染 | proxy 解析 header 名严格 ASCII；upstream 转发前 sanitize |
| secret 写入响应日志 | 上游响应内容回显 secret | 泄漏 | proxy 不记响应 body 到日志，仅 status + size |
| LRU 命中率过低 | 高频不同 ref | Vault 压力大 | 容量调优 + secret 命名规范（同租户共享 base secret） |
| sandbox 进程读取 proxy 日志 | 信息泄漏 | 失效 | proxy 与 sandbox 进程隔离（独立容器 / netns），sandbox 无文件系统访问 proxy 日志的途径 |
| 中间人攻击（proxy ↔ Vault） | 内网嗅探 | secret 泄漏 | mTLS（M0 必备）+ Vault response wrapping |

---

## 7. 可观测性

> 日志完整字段遵循 [20 § 5.3](./20-observability.md)，此处仅展示子系统专属字段。
> Metric / span 命名遵循 [20 § 5.x 命名规范](./20-observability.md)：metric 统一 `expert_work_*` 前缀（snake_case），span 统一 `expert_work.{component}.{action}`。

### Prometheus metrics

```
expert_work_credential_proxy_requests_total{tenant,agent,upstream_host,status}
expert_work_credential_proxy_inject_total{tenant,secret_ref,inject_kind,status}
expert_work_credential_proxy_cache_hits_total{tenant,secret_kind}
expert_work_credential_proxy_cache_misses_total{tenant,secret_kind}
expert_work_credential_proxy_vault_duration_seconds{operation}                 histogram
expert_work_credential_proxy_upstream_duration_seconds{upstream_host}          histogram
expert_work_credential_proxy_upstream_401_total{tenant,secret_ref}             counter（异常告警）
expert_work_credential_proxy_denied_total{tenant,reason}                       counter
```

### OTel spans

```
expert_work.credential_proxy.forward
├── attrs: tenant, agent, secret_ref, upstream_host, inject_kind, status
├── span: expert_work.credential_proxy.allowlist_check
├── span: expert_work.credential_proxy.cache_lookup
├── span: expert_work.credential_proxy.vault_fetch (仅 cache miss)
├── span: expert_work.credential_proxy.inject
└── span: expert_work.credential_proxy.upstream  → upstream HTTP span (如目标支持 W3C Trace Context)
```

### 关键告警

| 告警 | 条件 | 严重度 |
|---|---|---|
| Vault 不可达 | `vault_latency` 5min P99 > 5s 或失败率 > 10% | P0 |
| 异常 401 突增 | `upstream_401_total` 5min 增量 > 10 | P1（提示 secret 被吊销） |
| 缓存命中率过低 | `cache_hits / (hits+misses)` < 50% 持续 10min | P2 |
| denied 突增 | `denied_total{reason=allowlist}` 5min > 5 | P1（疑似 manifest 越权） |

---

## 8. 安全考虑

| 攻击面 | 防御 |
|---|---|
| sandbox 抓 header | sandbox **永远拿不到 token**：proxy 注入在 outbound 链路；sandbox 只能见到自己写的 `X-Expert-Work-Secret-Ref` |
| sandbox 提权读 proxy 内存 | gVisor 用户态隔离 + proxy 与 sandbox 在不同 PID/network namespace |
| 跨租户 secret 引用 | allowlist 强校验 (`tenant, agent, version, ref`) 四元组 |
| log 泄漏 | 全链路日志 redactor 强制屏蔽 `Authorization`、`X-Api-Key`、`token=...` 等模式（参见 [17 Audit Log] PII 字段） |
| Replay attack | proxy ↔ Vault 走 mTLS + Vault token 短 TTL；proxy ↔ upstream 走 TLS |
| 攻击 proxy 自身 | proxy 进程 dropping privileges + readonly rootfs + 仅监听内网 |
| 信息侧信道（timing） | 缓存命中/未命中延迟差异不会泄漏 secret 内容 |
| 越权调用管理 API | `/admin/*` 仅 SPIFFE ID = `control-plane` 可访问（mTLS SAN 校验）|
| sandbox 写 audit 表 | sandbox 无 DB 凭证；audit 由 proxy 自身写入（隔离写者） |

---

## 9. M0 / M1 / M2 演进

### M0 — MVP（aiohttp 自研版）
- aiohttp 反向代理实现，~1500 行
- Vault KV v2（静态 secret）
- 进程内 LRU（容量 1万，TTL 60s）
- allowlist 表 + manifest lint 校验
- 显式 `X-Expert-Work-Upstream` header 路由
- 审计写 Postgres
- mTLS（自签 CA，control plane 签发）

### M1 — 多租户生产化（Envoy + Vault dynamic）
- 替换 aiohttp 为 **Envoy + Lua filter**（性能 ~5x，连接管理交给 Envoy）
- Vault **dynamic secrets**：DB credential / 短 TTL OAuth token
- 自动 secret 轮换（监听 Vault lease 事件）
- per-tenant Envoy listener（强隔离）
- ⚠️ 注意：Envoy Lua 不支持复杂逻辑，allowlist 校验仍由 sidecar Python 服务做（Envoy ext_authz）

### M2 — 短 TTL + 合规
- 全部 secret 强制 dynamic（即使是长期 API key 也包一层短 TTL token）
- secret 轮换 SLA：< 5min 全集群刷新
- per-secret 加密审计（HSM 后端）
- 跨 region 副本（M3 准备）

### M3 — 零信任
- SPIFFE/SPIRE workload identity，secret 引用绑定 workload
- 完全去除"长期 API key"概念；上游全部走 OIDC token exchange

---

## 10. 开放问题

1. **M1 Envoy + Lua vs ext_authz：** allowlist 校验是写到 Lua（性能）还是放 ext_authz Python sidecar（可维护）？倾向 ext_authz，性能损失约 1ms 可接受。
2. **secret 注入到 body 的 schema 校验：** 注入到 body json 时是否需要 schema 验证（防止结构错乱）？M1 议题。
3. **proxy 的高可用拓扑：** sandbox 直连 proxy 还是经 service mesh sidecar？倾向 sidecar 模式（M1）。
4. **upstream 响应缓存：** 是否在 proxy 层加上小 LRU（如对幂等 GET）？倾向**不做**——proxy 应保持纯转发语义，缓存归 orchestrator 中间件。
5. **secret 紧急吊销链路：** 当前依赖 admin 手动 invalidate；是否需要 Vault webhook → proxy 自动失效？M2 议题。
