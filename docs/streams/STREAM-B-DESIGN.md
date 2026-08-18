# Stream B — Control Plane（设计先行）

> 落实 [docs/ITERATION-PLAN.md](../ITERATION-PLAN.md) § Stream B；执行的是
> [subsystems/19 Durable Execution § 4.2](../architecture/subsystems/19-durable-execution.md#41-python-orchestrator-内部)
> 的 HTTP 表层、[subsystems/16 Quota / Rate Limit § 3.2](../architecture/subsystems/16-quota-rate-limit.md) 的
> **网关层（第 1 层）** + [subsystems/28 Reliability Primitives § 4](../architecture/subsystems/28-reliability-primitives.md)
> 的中间件接线。

设计先行规则（[memory:feedback_design_first_iteration.md](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/feedback_design_first_iteration.md)）：
所有架构 / 接口 / mini-ADR 必须在编码前就锁定，B.1-B.7 PR 仅执行本文档。

---

## 1. 范围 & 边界

### 1.1 In-scope（B.1 – B.7）

| 子项 | 实现内容 | 关联子系统 |
|------|---------|-----------|
| **B.1 FastAPI 骨架** | `services/control-plane`：FastAPI app factory、Pydantic v2 BaseSettings、SQLAlchemy 2.0 async session、A.7 / A.8 / A.9 / A.11 / A.12 / A.13 中间件接线 | 03 monorepo, 28 § 4 |
| **B.2 网关层限流 middleware** | per-IP + per-API-key token bucket；M0 in-process 实现，`RateLimiter` Protocol 已分层为 Redis 实现预留 | 16 § 3.2 + § 5.1（仅第 1 层） |
| **B.3 取消信号链 — API 层** | `request.is_disconnected()` 轮询 → CancelToken 触发；`X-Expert-Work-Deadline-Ms` header → 请求级 DeadlineContext | 28 § 4 (DeadlineContext) |
| **B.4 Manifest 加载与 Pydantic 校验** | `expert-work-protocol/agent_spec.py` 扩展为完整 `AgentSpec` Pydantic v2 schema；`manifest/loader.py` 提供 YAML → AgentSpec、Jinja2 渲染、最小 lint（M0 子集） | 02 AGENT-MANIFEST § AgentSpec |
| **B.5 Agent CRUD API** | `POST/GET/LIST/PUT/DELETE /v1/agents`；persistence 落 `agent_spec` 表；每次 mutation 写 audit_log (A.4) | 02, 17 |
| **B.6 Session CRUD API** | `POST/GET/LIST /v1/sessions`、`:pause`、`:resume`、`:cancel`；走 A.7 `thread_meta` Repository | 19 § 4.2 |
| **B.7 Run trigger API + 假 SSE** | `POST /v1/sessions/{id}/runs` 返回 `text/event-stream`；M0 emit fake event token，Stream E 接 LangGraph 真流后无缝替换 | 19, 20 § 5 (expert_work.session.run.*) |

### 1.2 Out-of-scope（明确推迟）

| 推迟项 | 落地 Stream | 备注 |
|-------|------------|------|
| OIDC/JWT 验证、租户解析、RLS | C.1, C.4 | B.5/B.6 actor/tenant 从 header `X-Expert-Work-Actor` / `X-Expert-Work-Tenant` **dev-only** 读，prod env 强制拒绝 |
| mTLS 服务间认证 | C.2 | A.10 已铺 TLS 静态证书；B 启 HTTPS 由 reverse proxy（nginx）接 |
| 业务层（per-tenant）+ provider 层限流 | C.6 + E.6 | B.2 仅做网关层 |
| 真 LLM 调用 / LangGraph runtime | E.1+ | B.7 fake SSE |
| API Key CRUD | C.3 | B.2 仅做"如果 X-API-Key 存在则桶 key 取它否则取 IP" |
| PII redactor、Audit WORM | D.1, D.2 | A.4 audit_log 仅落库；不可篡改性后置 |

### 1.3 验收门（来自 ITERATION-PLAN）

1. **可通过 HTTPS 创建 agent + session + 触发 run 拿到 SSE stream**
2. **限流压测得到 429** — 用 `httpx` 并发 > burst 验证
3. **客户端断开后服务端 context 在 200 ms 内收到 cancellation** — 集成测试 + tracing 验证
4. **所有 admin 动作落 A.4 audit_log** — `audit.write` 调用点 100% 覆盖 mutation

---

## 2. 架构

### 2.1 服务布局（新增）

```
services/
└── control-plane/
    ├── pyproject.toml                      # 依赖 fastapi、uvicorn、expert-work-* workspace
    ├── src/control_plane/
    │   ├── __init__.py                     # __version__
    │   ├── app.py                          # create_app() — 单一入口
    │   ├── settings.py                     # Pydantic BaseSettings：EXPERT_WORK_*
    │   ├── deps.py                         # DI: db_session, lifecycle, rate_limiter, audit_logger
    │   ├── middleware/
    │   │   ├── observability.py            # log/trace/metrics 接线（A.4/A.5/A.8/A.9）
    │   │   ├── deadline.py                 # B.3：X-Expert-Work-Deadline-Ms → DeadlineContext
    │   │   ├── cancellation.py             # B.3：is_disconnected 轮询 → CancelToken
    │   │   ├── rate_limit.py               # B.2：网关层
    │   │   └── audit_context.py            # 提取 actor / tenant 注入 ctxvar
    │   ├── ratelimit/
    │   │   ├── base.py                     # RateLimiter Protocol
    │   │   └── in_process.py               # asyncio.Lock + 令牌桶（M0）
    │   ├── api/
    │   │   ├── health.py                   # /healthz/{live,ready,startup}
    │   │   ├── agents.py                   # B.5
    │   │   ├── sessions.py                 # B.6
    │   │   └── runs.py                     # B.7 SSE
    │   ├── manifest/
    │   │   ├── loader.py                   # B.4：YAML + Jinja2 → AgentSpec
    │   │   └── errors.py
    │   ├── persistence/
    │   │   ├── agent_spec_repo.py          # AgentSpec CRUD
    │   │   └── models.py                   # SQLAlchemy DeclarativeBase
    │   └── main.py                         # uvicorn 入口
    └── tests/
        ├── conftest.py                     # ASGI httpx.AsyncClient fixture
        ├── test_health.py
        ├── test_rate_limit.py
        ├── test_cancellation.py
        ├── test_manifest_loader.py
        ├── test_agents_api.py
        ├── test_sessions_api.py
        └── test_runs_sse.py
```

### 2.2 请求生命周期（中间件栈 — 由内向外执行）

```
HTTP request
  ↓
[outer]   ObservabilityMiddleware       — log + trace + metrics 头（A.4/A.5/A.8/A.9）
[outer]   AuditContextMiddleware        — 注入 actor / tenant ctxvar
[outer]   DeadlineMiddleware            — X-Expert-Work-Deadline-Ms → request.state.deadline_ctx
[outer]   CancellationMiddleware        — 起 is_disconnected poll task；提供 request.state.cancel
[outer]   RateLimitMiddleware           — 网关层桶；超额返 429 + Retry-After + audit(QUOTA_RATE_LIMIT_DENIED)
  ↓
[handler] route 实现
  ↓
[inner]   await DeadlineContext.check() — 触发取消
```

**关键决策**：取消 middleware 在限流后面，因为限流必须先拒掉非法洪流，再花成本起 poll task。

### 2.3 Lifecycle / Health 接线

- B.1 启动时：`Lifecycle()` 实例化 → 注册 startup hooks → `lc.mark_ready()` 在 DB ping + migration 就绪后调
- `make_health_handlers(DefaultHealthProvider(...))` 拼三个 endpoint
- DB 连接、storage backend 作为 `DependencyCheck` 注入；live 不查依赖
- `graceful_shutdown` 接 uvicorn signal handler

### 2.4 DB 表（B.5 新增）

```sql
-- B.5 — AgentSpec 注册表
CREATE TABLE agent_spec (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    name          TEXT NOT NULL,           -- metadata.name
    version       TEXT NOT NULL,           -- metadata.version
    spec_json     JSONB NOT NULL,          -- 完整 AgentSpec（去敏感字段已是 D.2 工作）
    spec_sha256   CHAR(64) NOT NULL,       -- 规范化 JSON 的 sha256，用于幂等 PUT
    status        TEXT NOT NULL,           -- ACTIVE / DEPRECATED / DELETED
    created_by    TEXT NOT NULL,           -- actor_id
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name, version)
);
CREATE INDEX ON agent_spec (tenant_id, status, name);
```

Session 表已在 A.7 `thread_meta` 落地，B 仅消费。

---

## 3. Mini-ADRs

### ADR B-1 — `RateLimiter` Protocol；M0 用 in-process 实现

**问题**：subsystems/16 § 3.2 要求 Redis Lua atomic token bucket。M0 是否立刻引入 Redis 依赖？

**选项**：
- (A) 现在引入 Redis → 与 16 一步到位，但 Stream B 多一个外部依赖、docker-compose、CI 容器
- (B) M0 in-process（`asyncio.Lock` + dict 状态），定义 `RateLimiter` Protocol；Redis 实现作为 C.6 PR 上 — **推荐**
- (C) 跳过 B.2，直接放 C.6 — 违 bottom-up（B.1 一上线就裸暴露）

**决策**：B。`RateLimiter` Protocol 提前定义；C.6 引入 Redis 时实现替换是无侵入的。

**Why**：M0 单实例 control-plane 不需要分布式状态；Protocol 隔离让后续替换零返工。

### ADR B-2 — 取消信号链：`is_disconnected` 轮询 + CancelToken

**问题**：FastAPI 没有内建的请求断开 → 任务取消机制。

**选项**：
- (A) Starlette `request.is_disconnected()` 后台轮询（默认 50ms 一次） — **推荐**
- (B) HTTP/2 RST_STREAM → 由 ASGI server 抛 `asyncio.CancelledError` — Uvicorn 当前仅 HTTP/1.1 部分支持，靠不住
- (C) WebSocket-only — 不适用 REST

**决策**：A。每个 request 启一个 50ms 间隔轮询的 background task；侦测到断开后调用 `CancelToken.cancel(reason="client_disconnected")`，进入 handler 的 DeadlineContext 通过 `check()` / `wait_cancellable` 拿到信号。

**预算**：50ms 轮询 + 100ms 调度抖动 + 50ms 业务侧 check 间隔 = **≤ 200ms** 端到端，对齐验收门 #3。

### ADR B-3 — Manifest 校验：B 只做 Pydantic schema + Jinja2 渲染

> **Superseded(2026-08-18)**:保存期 Jinja 渲染整层已下线(调试台重设计 PR0),`{{ }}` 只作 run 期动态 prompt 语义,见 `docs/design/jinja-dynamic-prompt.md` §2 勘误。

**问题**：02-AGENT-MANIFEST 列出 8 条 lint 规则；全部塞进 B.4 会膨胀。

**决策**：B.4 仅落实
1. Pydantic v2 类型 / 必填字段校验
2. Jinja2 渲染（`{{ tenant_id }}` 等模板变量）
3. 第 7 条「网络 allowlist 不能为 `["*"]`」（一行）
4. 第 8 条「fallback chain 不成环」（拓扑序检测）

第 1-6 条（secret 引用、tool/subagent 解析、quota、MCP 白名单、Python 包、镜像校验）依赖 Vault / MCP Gateway / Sandbox / quota engine，全部留给后续 Stream（C.5 quota、D 安全、E tool dispatcher、F sandbox）。

**Why**：避免编一个用不上的占位 lint；后续 Stream 触达对应依赖时按需补。

### ADR B-4 — SSE 输出协议

**问题**：SSE 流用什么 event 词汇？

**M0 占位决策（B.7，已废）**：fake stream 用 `token` / `heartbeat` / `done` 三种 event：

```
event: token
data: {"seq": 1, "text": "Hello"}
```

**修正决策（control-plane 切真时定稿）**：B.7 的 `token/done` 是 fake stream 占位词汇，写在真实 orchestrator 之前。真实流由 E.14 `run_agent` worker 经 LangGraph `astream(stream_mode="updates")` 产出 —— 是**节点输出**（`updates`），不是 LLM token 增量（token 增量要 `messages` stream mode）。规范词汇改为 E.14 `sse_consumer` 实际产出的一族：

```
event: metadata      data: {"run_id": "...", "thread_id": "..."}   # 流首
event: updates       data: {"<node>": {<state writes>}}            # 每个节点完成
: heartbeat                                                        # 空闲（SSE 注释帧）
event: end           data: null                                   # 流尾
event: error         data: {"message": "...", "name": "..."}       # 失败
```

`event:` 字段即 `StreamBridge.StreamEvent.event`（E.14 § 4.5）。M0 无真实外部客户端，词汇切换无破坏面；将来需要 token 级增量时，worker 的 `stream_mode` 加 `messages` 即可，新增 `messages` event 不破坏现有词汇。

### ADR B-5 — Actor / Tenant 提取（auth 推迟）

**问题**：B 没有真 auth，但 audit_log / rate-limit 都要 tenant_id。

**决策**：`AuditContextMiddleware` 两段式逻辑：
- 环境 `EXPERT_WORK_AUTH_MODE=dev`（默认）：从 `X-Expert-Work-Actor` / `X-Expert-Work-Tenant` header 读，明文 trust；header 缺失 → fallback `actor=anonymous`、`tenant=public`
- 环境 `EXPERT_WORK_AUTH_MODE=prod`：拒绝；C.1 OIDC middleware 上线后切换

prod 模式启动校验：`auth_mode == "prod"` 时 fail-fast 拒绝启动（C.1 未到位）。Stream C.1 落地时移除这个守卫。

### ADR B-6 — control-plane 状态持久化：store backend 由 settings flag 选

**问题**：M0 收尾前 `create_app` 的全部 store（`agent_spec` / `thread_meta` / `audit` / `api_key` / `role_binding` / `service_account` / `tenant_quota` / `token_reservation` / `tenant_config` / `feedback`）默认 `InMemory*` —— control-plane 整个跑在进程内存，重启即丢状态。`Sql*Store` / `DbFeedbackStore` 早已落地并各自集成测试（`packages/expert-work-persistence/tests/test_sql_*.py`）。缺口是接线：engine 接在哪、谁负责 `dispose()`？

**选项**：
- (A) 在 `main.py` 构造 engine + SQL store 传给 `create_app` —— 但 `AsyncEngine` 必须在 lifespan `finally` 里 `await engine.dispose()`，`main.py` 拿不到 lifespan，要么泄漏 engine，要么给 `create_app` 再加一个纯为 dispose 服务的 `db_engine` 参数。
- (B) 加 `store_backend: Literal["memory", "sql"]` setting，`create_app` 在 `sql` 时内部建 engine + `build_rls_sessionmaker` 包装的 sessionmaker + 全部 `Sql*Store`，并在自己的 lifespan `finally` dispose engine。`main.py` 保持一行。 — **推荐**
- (C) 永远连 SQL、删 `InMemory*` —— 每个单测都要起 Postgres 容器，CI 慢且重，违 [测试金字塔]。

**决策**：B。与既有三个 backend-选择 setting 同构 —— `checkpointer_backend`（memory/postgres）、`quota_redis_url`（in-memory/Redis）、`single_instance`（in-process/Redis 限流）；`create_app` 本就是「按 settings 选实现」的工厂。`create_async_engine` 是惰性的（首次用才连库），放进同步的 `create_app` 不破坏其「同步 + 无副作用」契约 —— 无 I/O 发生在工厂调用期。注入参数（`agent_spec_repo=` 等）仍优先于 backend，单测照旧注 `InMemory*`。

**Why**：engine 生命周期与 app 生命周期绑定，只有 `create_app` 同时持有 engine 和 lifespan；放 `main.py` 会逼 `create_app` 多开一个只为 dispose 存在的参数。ITERATION-PLAN「main.py 接线」的措辞早于此 conditional 工厂模式确立。

**RLS**：全部 SQL store 共用一个 `build_rls_sessionmaker` 包装的 sessionmaker —— 每事务自动 `SET LOCAL app.tenant_id`（C.4 / ADR-0002）。租户隔离对 store 层透明。

**生产开启**：`infra/docker-compose.yml` 的 `control-plane` 服务设 `EXPERT_WORK_STORE_BACKEND=sql`；`migrate` 一次性服务先跑 alembic，`depends_on: service_completed_successfully` 保证 schema 就位。

**已知项（不在本次范围）**：`/v1/quota/*` 经 mTLS 调用时 principal 是 system tenant（`ffff…ffff`），而 quota 表是 `FORCE` RLS。若该端点对*目标*租户做写入，GUC 与行 `tenant_id` 不一致会被 `WITH CHECK` 拒。M0 是进程内单体，orchestrator 直接调 quota service、不 HTTP 打 `/v1/quota`，该路径暂处休眠。在它被端到端走通前（Stream C 深化 / M1 拆服务），需让该 handler 用 `bypass_rls_var` 或显式切目标租户 GUC。本 PR 仅做接线，不改 quota handler。

---

## 4. 接口（HTTP）

| Path | Method | 入参 | 出参 | Audit Action |
|------|--------|------|------|--------------|
| `/healthz/live` | GET | — | `HealthReport` | — |
| `/healthz/ready` | GET | — | `HealthReport` | — |
| `/healthz/startup` | GET | — | `HealthReport` | — |
| `/v1/agents` | POST | YAML body | `AgentSpec` | `manifest:write` |
| `/v1/agents` | GET | `?status=&name=` | `{items: [...]}` | `manifest:read` |
| `/v1/agents/{name}/{version}` | GET | — | `AgentSpec` | `manifest:read` |
| `/v1/agents/{name}/{version}` | PUT | YAML body | `AgentSpec` | `manifest:write` |
| `/v1/agents/{name}/{version}` | DELETE | — | `204` | `manifest:delete` |
| `/v1/sessions` | POST | `{agent_name, agent_version, input}` | `{thread_id, state}` | `session:write` |
| `/v1/sessions/{id}` | GET | — | `DurableThreadMeta` | `session:read` |
| `/v1/sessions/{id}:pause` | POST | `{reason}` | `{state}` | `session:write` |
| `/v1/sessions/{id}:resume` | POST | `{}` | `{state}` | `session:write` |
| `/v1/sessions/{id}:cancel` | POST | `{reason}` | `204` | `session:cancel` |
| `/v1/sessions/{id}/runs` | POST | `{input}` | `text/event-stream` | `session:write` |

错误响应统一 envelope（来自 common patterns）：
```json
{"success": false, "data": null, "error": {"code": "RATE_LIMIT_EXCEEDED", "message": "...", "retry_after_s": 3}}
```

---

## 5. 测试矩阵

| 项 | 单测 | 集成 | E2E |
|----|------|------|-----|
| FastAPI app 启动 | ✓ | — | — |
| Lifecycle / health endpoints | ✓ | ✓ (testcontainers postgres) | — |
| RateLimit middleware（in-process） | ✓（直 awaitable） | ✓（httpx 并发 N>burst） | — |
| Cancellation middleware（is_disconnected） | ✓（mock starlette） | ✓（httpx 主动 close） | ✓（curl --max-time + 服务端 trace） |
| Manifest loader（valid / 4 类 invalid） | ✓ | — | — |
| AgentSpec Repository | ✓ in-memory | ✓ SQL | — |
| Agent CRUD API | ✓（mock repo） | ✓（real DB + audit_log assert） | — |
| Session CRUD API | ✓ | ✓ | — |
| Run SSE | ✓（assert event 顺序） | ✓ | — |
| SQL store 接线（ADR B-6） | ✓（`store_backend` 选型 + 类型断言，engine 惰性免容器） | ✓（testcontainers Postgres + alembic，HTTP round-trip 证落库） | — |

**覆盖率目标**：80% 行覆盖（common/testing.md）。

---

## 6. 风险 & 缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `is_disconnected` 在 ASGI server 行为有边界条件（uvicorn vs hypercorn） | 取消 ≥ 200ms | CI 集成测试钉 uvicorn 版本；E2E 用 `curl --max-time` 真实验证 |
| In-process token bucket 单实例假设破裂（部署成 2 个 control-plane） | 限流失效 | settings 加 `single_instance` 守卫，多副本部署时 startup 拒绝（提示升级到 C.6 Redis impl）|
| YAML loader DoS（巨大 anchor 展开） | OOM | `yaml.safe_load` + 文件大小上限 64 KiB（B.4 settings） |
| SSE keep-alive 与代理超时不匹配 | 客户端在中间被 nginx 切断 | 固定 15s heartbeat；TLS-RUNBOOK 注明 nginx `proxy_read_timeout 60s` |
| `X-Expert-Work-Tenant` header 伪造（dev mode） | dev 环境跨租户 | dev mode 仅本地 dev / staging；prod 启动守卫拒绝 |

---

## 7. 里程碑 / PR 切分

每个 B.x 一 PR；每个 PR 自给自足、可独立合入 main 且 CI 绿。

```
B.1   FastAPI 骨架 + Lifecycle/health 接线 + Settings + Dockerfile + alembic 0003 (agent_spec)
B.2   网关层 RateLimit middleware（in-process）
B.3   Deadline + Cancellation middleware（X-Expert-Work-Deadline-Ms + is_disconnected）
B.4   expert-work-protocol AgentSpec 全字段 + manifest/loader（Jinja2 + 4 条 lint）
B.5   Agent CRUD API + audit 接线
B.6   Session CRUD API + pause/resume/cancel（thread_meta 上）
B.7   Run trigger API + fake SSE + 整 Stream B 验收门
```

每 PR 收尾必须满足[零技术债规则](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/feedback_zero_tech_debt.md)。

---

## 8. 横切依赖回看（自下而上验证）

| Stream B 使用的下层能力 | 来源 | 状态 |
|------|------|------|
| 结构化日志 expert_work.audit/* | A.4 | ✅ |
| W3C trace context 注入 | A.5 / A.8 | ✅ |
| Prometheus expert_work_http_* metrics | A.9 | ✅ |
| Lifecycle / Health | A.11 / A.12 | ✅ |
| DeadlineContext / CancelToken | A.13 | ✅ |
| audit_log table + Repository | A.4 + A.7 | ✅ |
| thread_meta Repository | A.7 | ✅ |
| TLS 静态证书 / 1.2 floor | A.10 | ✅ |

所有依赖均已 main 落地；无反向边。
