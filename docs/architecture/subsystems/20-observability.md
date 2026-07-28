# 20 Observability — span/metric/log 命名规范、关键 dashboard、SLO

> 让"agent 跑通"升级为"产品级可运维"：所有跨服务调用走 W3C Trace Context、metric 命名统一前缀 `expert_work_*`、结构化日志强制字段、SLO/错误预算驱动发布冻结。

---

## 1. 职责 & 边界

### ✅ 做
- **三大支柱**：metric (Prometheus) / trace (OTel + Tempo) / log (Loki)
- **命名规范**：span / metric / log 字段的统一标准
- **强制 schema**：日志必填字段（含 redaction 中间件保证不带 PII）
- **W3C Trace Context 传播**：跨 Control Plane → Orchestrator → Sandbox → MCP → LLM Gateway 全链路
- **SLO/SLI 定义**：可用性、首个图节点、冷启动、API 延迟
- **错误预算策略**：耗尽 → 自动冻结新 manifest 发布 24h
- **Dashboard 模板**：每个核心子系统一份 Grafana JSON
- **告警分级**（P0/P1/P2）+ 路由到 PagerDuty / 飞书

### ❌ 不做
- 不做业务可视化（agent 调用链、token 大盘） → M2 引入 Langfuse 或自建
- 不做用户反馈、A/B 报表 → [26 Eval Framework](./26-eval-framework.md)
- 不做应用层 unhandled exception 监控 → Sentry / GlitchTip 集成（M1 落地）
- 不做计费 chargeback → 业务运营层
- 不做日志全文搜索（明文 prompt/response 存 event_log，按需读）

---

## 2. 上下游依赖

```
所有服务 ──▶ OTel SDK ──▶ OTel Collector ──▶ ┬─▶ Tempo（trace）
                                                ├─▶ Prometheus（metric remote-write）
                                                └─▶ Loki（log）
                                                          │
                                                          ▼
                                                   Grafana（统一展示）
                                                          │
                                                          ▼
                                                   Alertmanager ──▶ PagerDuty / 飞书
```

| 依赖方向 | 子系统 | 关系 |
|---------|--------|------|
| 上游 | 所有服务 | 强制注入 OTel SDK + structured logger |
| 下游 | OTel Collector / Prometheus / Loki / Tempo / Grafana | 自托管或托管服务 |
| 横切 | [17 Audit Log](./17-audit-log.md) | audit 事件部分进 Loki（合规视角不同：audit 偏 who/what，log 偏 how） |
| 横切 | PII Redactor 中间件 | 写 log/trace 前过 redactor，按 `tenant_config.pii_fields` 脱敏 |

---

## 3. 数据模型 / 状态机

不是有状态服务，本子系统主要是**规范**。下面是**强制约束**的物化形式。

### 3.1 命名前缀

| 类型 | 前缀 | 例 |
|------|------|-----|
| span name | `expert_work.{component}.{action}` | `expert_work.orchestrator.session_run` |
| metric name | `expert_work_{component}_{noun}_{unit}` | `expert_work_llm_latency_seconds` |
| log logger | `expert_work.{component}` | `expert_work.sandbox.pool` |
| label key | snake_case | `tenant`, `agent`, `trace_id` |

**关键决策**：**所有 metric 严格走 `expert_work_*` 前缀**，与 vendor 中间件库的命名隔离；防止 LangGraph / langchain 自带 metric 与我们混淆。

### 3.2 SLO 表（M1 目标）

```sql
CREATE TABLE slo_definition (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,             -- 'session_first_node_p95'
    component     TEXT NOT NULL,                    -- orchestrator / sandbox / control_plane / global
    metric        TEXT NOT NULL,                    -- 'expert_work_session_first_node_seconds'
    target_pct    NUMERIC(5,2) NOT NULL,            -- 99.0 表示 99% 时间满足
    threshold     NUMERIC(10,3) NOT NULL,           -- 1.5（秒）
    window_days   INT NOT NULL DEFAULT 30,
    alert_burn_rate NUMERIC(4,2) NOT NULL DEFAULT 14.4,  -- 错误预算燃烧率告警阈值
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 4. 关键接口

### 4.1 OTel 初始化（每个服务统一）

```python
# packages/expert-work-common/src/Expert Work/common/otel.py
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource

def init_otel(service_name: str) -> None:
    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "Expert Work",
        "service.version": __version__,
        "deployment.environment": os.environ["ENV"],
    })
    # exporter 走 OTel Collector（同 pod sidecar）
    ...

def with_agent_span(name: str, **attrs):
    """统一的业务 span 装饰器，强制注入 tenant/agent/trace_id。"""
```

### 4.2 结构化日志

```python
# packages/expert-work-common/src/Expert Work/common/log.py
logger = get_logger("expert_work.orchestrator")

logger.info("session.start", extra={
    "tenant": ctx.tenant,
    "session_id": ctx.session_id,
    "agent": ctx.agent_name,
    "agent_version": ctx.agent_version,
    "trace_id": ctx.trace_id,
    "span_id": ctx.span_id,
})
```

底层 formatter 强制：
- JSON 格式
- ISO 8601 时间戳
- 必填字段缺失 → 写 `WARNING expert_work.log_schema_violation` 但不阻断业务
- 经过 redactor 中间件（按 `tenant_config.pii_fields`）

---

## 5. 算法 / 关键决策

### 5.1 Span 命名规范

`expert_work.{component}.{action}`，component 必须来自固定枚举：

| component | 说明 |
|-----------|------|
| `control_plane` | manifest CRUD、user actions |
| `orchestrator` | graph 调度、LLM/tool 调用 |
| `sandbox` | 容器生命周期、命令执行 |
| `credential_proxy` | secret 注入、出站 HTTP |
| `mcp_gateway` | MCP server 路由 |
| `llm_gateway` | LLM provider 调用 |
| `memory` | 短期/长期记忆读写 |
| `subagent` | 子 agent spawn / lifecycle |
| `hitl` | 暂停 / 审批流程 |
| `eval` | 评测运行 |

例：

```
expert_work.orchestrator.session_run
  ├─ expert_work.orchestrator.llm_call
  │    └─ expert_work.llm_gateway.provider_request
  ├─ expert_work.orchestrator.tool_call
  │    ├─ expert_work.credential_proxy.inject
  │    └─ expert_work.sandbox.exec
  └─ expert_work.orchestrator.checkpoint
```

每个 span 必有 attrs：`tenant`, `agent`, `agent_version`, `session_id`。

### 5.2 关键 metric

本节分两块：**§5.2.1 as-built（M0 已 emit）**= 全仓实测 107 个 `expert_work_*` 指标（A.9，据 `expert_work_counter`/`expert_work_histogram`/`expert_work_gauge` 定义点逐个核），**§5.2.2 M1-target**= 设计期规划但 M0 未实现的指标。**强制真值源** = `expert-work-common/observability/metrics.py` 的 validator（`expert_work_*` 前缀 + label cardinality 拦截）；新增/改名指标先过它。

> 全部指标为模块级单例，经三个 builder 定义。`expert_work_uplift_*` 几乎全在 `expert-work-common/uplift_metrics.py`；`expert_work_control_plane_*` 散在各 worker / middleware；orchestrator 指标在 `sse.py` / `graph_builder/builder.py`。

#### 5.2.1 as-built（M0 已 emit，107 项）

**`expert_work_control_plane_*`（26）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_control_plane_http_requests_total` | counter | `method,route,status_code` | 控制面 HTTP 请求计数 |
| `expert_work_control_plane_http_request_duration_seconds` | histogram | `method,route,status_code` | 控制面 HTTP handler 时延 |
| `expert_work_control_plane_rate_limit_decisions_total` | counter | `dimension,decision` | gateway 限流决策 |
| `expert_work_control_plane_backpressure_shed_total` | counter | — | 过载守卫甩负（in-flight 深度超阈值 503，16.3） |
| `expert_work_control_plane_tenant_rate_limit_decisions_total` | counter | `decision` | 租户 tier 限流决策 |
| `expert_work_control_plane_audit_query_total` | counter | `tenant_scope,result` | GET /v1/audit 调用 |
| `expert_work_control_plane_audit_query_seconds` | histogram | `tenant_scope` | GET /v1/audit 时延（含自审计 emit） |
| `expert_work_control_plane_run_list_total` | counter | `tenant_scope` | GET /v1/runs 调用 |
| `expert_work_control_plane_run_list_seconds` | histogram | `tenant_scope` | GET /v1/runs 时延 |
| `expert_work_control_plane_approvals_pending` | gauge | `()` | 平台范围待审批行数（HX-4） |
| `expert_work_control_plane_approval_gauge_cycle_errors_total` | counter | `()` | ApprovalGaugeWorker 周期异常 |
| `expert_work_control_plane_feedback_consumed_total` | counter | `action` | 👎 反馈被学习闭环消费（HX-2） |
| `expert_work_control_plane_feedback_consumer_cycle_errors_total` | counter | `()` | FeedbackConsumerWorker 周期异常 |
| `expert_work_control_plane_triggers_fired_total` | counter | `()` | 触发器启动 run |
| `expert_work_control_plane_trigger_scheduler_cycle_errors_total` | counter | `()` | trigger 调度器周期异常 |
| `expert_work_control_plane_trigger_dead_letters_total` | counter | `()` | trigger 耗尽重试预算入 DLQ |
| `expert_work_control_plane_curation_candidates_detected_total` | counter | `()` | 新标记的策划候选轨迹 |
| `expert_work_control_plane_curation_worker_cycle_errors_total` | counter | `()` | 策划 worker 周期异常 |
| `expert_work_control_plane_skill_evolution_grounded_total` | counter | `()` | 产出 grounded DRAFT skill 的候选 |
| `expert_work_control_plane_skill_evolution_cycle_errors_total` | counter | `()` | skill 进化 worker 周期异常 |
| `expert_work_control_plane_skill_rollback_total` | counter | `()` | rollback monitor 自动归档的 ACTIVE skill |
| `expert_work_control_plane_skill_rollback_cycle_errors_total` | counter | `()` | rollback monitor 周期异常 |
| `expert_work_control_plane_memory_dlq_dead_letters_total` | counter | `()` | memory DLQ 超重试上限弃置 |
| `expert_work_control_plane_memory_dlq_retries_succeeded_total` | counter | `()` | memory DLQ 重试成功出队 |
| `expert_work_control_plane_memory_dlq_cycle_errors_total` | counter | `()` | memory DLQ worker 周期异常 |
| `expert_work_control_plane_quota_reaper_cycle_errors_total` | counter | `()` | 配额预留 reaper 周期异常 |

**`expert_work_llm_*`（5）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_llm_token_usage_total` | counter | `tenant_id,agent_name,model,type` | 每次 LLM 调用 token 消耗，按 type 分（G.9） |
| `expert_work_llm_billed_cost_micros` | gauge | `tenant,model` | rollup 计算的计费成本（µUSD）/租户·模型·月 |
| `expert_work_llm_stream_stale_total` | counter | `provider_key` | provider 超 `stream_deadline_s`（L.L3） |
| `expert_work_llm_auth_refresh_total` | counter | `provider_key,result` | OAuth provider 401 触发的凭证刷新（L.L8） |
| `expert_work_llm_tool_disclosure_fallback_total` | counter | `provider` | 厂商原生工具披露档被拒、回落 HX-12 档 |

**`expert_work_sandbox_*` / `expert_work_session_*` / `expert_work_tool*`（11）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_sandbox_cold_start_seconds` | histogram | `()` | 启动决策到 `wait_ready` 成功的秒数 |
| `expert_work_sandbox_pool_total` | counter | `event` | 暖池流转事件（HX-6） |
| `expert_work_sandbox_pool_ready` | gauge | `variant` | 当前 READY 暖池容器数/镜像变体 |
| `expert_work_session_first_node_seconds` | histogram | `()` | RUNNING 到首个图节点 chunk 的秒数（任意节点,非特指 agent；前名 `..._ttft_seconds`） |
| `expert_work_first_output_seconds` | histogram | `source` | 用户首次看到内容的秒数，按来源分（`token`=流式首帧 / `node`=无流式 run 的 agent updates 帧兜底） |
| `expert_work_session_duration_seconds` | histogram | `outcome` | RUNNING 到终态的秒数，按 run 结局 |
| `expert_work_tool_call_total` | counter | `tool,outcome` | 工具分派计数（ok/error/blocked） |
| `expert_work_tool_latency_seconds` | histogram | `tool` | 每次工具分派 wall-clock |
| `expert_work_tool_promotion_total` | counter | `event` | deferred-tool promotion 生命周期（HX-12） |
| `expert_work_tools_stages_total` | counter | `()` | 执行的工具调用 stage 数（L.L6） |
| `expert_work_tools_dispatched_total` | counter | `()` | L6 stage 内分派的工具调用数（÷stages=均并发） |

**`expert_work_cm_*` 上下文管理（15）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_cm_ingest_total` | counter | `outcome` | run 起始工作区状态 ingest（CM-0） |
| `expert_work_cm_projection_total` | counter | `outcome` | turn 边界工作区状态投影（CM-0） |
| `expert_work_cm_recitation_chars` | gauge | `()` | 注入 prompt 尾的 plan 复述字符数（CM-0 N1） |
| `expert_work_cm_tool_error_total` | counter | `error_class,tool` | 分类后入恢复 advisory 的工具失败（CM-1） |
| `expert_work_cm_recovery_advisory_chars` | gauge | `()` | 注入 prompt 尾的恢复 advisory 字符数（CM-1） |
| `expert_work_cm_working_window_trim_total` | counter | `outcome` | agent_node 入口工作记忆滑窗（CM-2） |
| `expert_work_cm_working_window_dropped_turns` | gauge | `()` | 最近滑窗 trim 丢弃的 user turn 数（CM-2） |
| `expert_work_cm_precompaction_flush_total` | counter | `outcome` | 压缩前记忆 flush（CM-3） |
| `expert_work_cm_precompaction_flush_memories` | gauge | `()` | 最近 pre-compaction flush 写入的记忆数（CM-3） |
| `expert_work_cm_tool_overflow_total` | counter | `outcome,tool` | 工具结果溢出外置（CM-5） |
| `expert_work_cm_tool_overflow_chars` | gauge | `()` | 最近外置工具结果溢出字符数（CM-5） |
| `expert_work_cm_effort_escalation_total` | counter | `signal` | 升档高 effort caller 服务的 turn（CM-9 撞限 `loop`/`budget`；CM-11 事件 `error`/`goal`） |
| `expert_work_cm_memory_rerank_total` | counter | `outcome` | 长期记忆召回 rerank（CM-4） |
| `expert_work_cm_memory_mmr_total` | counter | `outcome` | 长期记忆召回 MMR 选择（CM-6） |
| `expert_work_cm_memory_reconcile_total` | counter | `op` | run-end 记忆写回对账（CM-7） |

**`expert_work_uplift_*` skills/memory/mcp/curator/threat/credentials（30）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_uplift_threat_scan_total` | counter | `scope,result,variant` | 威胁扫描调用 |
| `expert_work_uplift_threat_pattern_hit_total` | counter | `pattern_id,scope,variant` | 每 pattern 命中数（调参） |
| `expert_work_uplift_triggers_blocked_total` | counter | `phase` | 被注入扫描器拒的触发器 |
| `expert_work_uplift_memory_writes_blocked_total` | counter | `source` | 被严格注入扫描器拒的记忆写 |
| `expert_work_uplift_memory_recalls_redacted_total` | counter | `()` | 召回时被 `[BLOCKED:<cat>]` 替换的记忆 |
| `expert_work_uplift_memory_drift_total` | counter | `()` | content_hash 与存储不符的记忆行 |
| `expert_work_uplift_memory_retrieval_total` | counter | `mode,result` | 记忆召回调用（hybrid/vector） |
| `expert_work_uplift_memory_recall_inject_mode_total` | counter | `mode` | agent_node 召回注入模式（per_session/per_turn） |
| `expert_work_uplift_anthropic_cache_anchors_total` | counter | `()` | 上游注入的 cache_control anchor |
| `expert_work_uplift_mcp_call_total` | counter | `transport,server,result` | MCP 工具调用尝试 |
| `expert_work_uplift_mcp_circuit_state_total` | counter | `server,state` | MCP per-server 断路器转换（U-13） |
| `expert_work_uplift_skill_view_total` | counter | `result` | skill_view 调用（ok/not_found/truncated） |
| `expert_work_uplift_skill_zip_reject_total` | counter | `reason` | skill ZIP 导入边界拒绝 |
| `expert_work_uplift_skill_blocked_total` | counter | `phase` | 被威胁扫描器拒的 skill 内容（U-21） |
| `expert_work_uplift_skill_drift_total` | counter | `()` | content_hash 漂移的 skill 行（U-21） |
| `expert_work_uplift_skill_redacted_total` | counter | `()` | 读时被 `[BLOCKED]` 替换的 skill_view（U-21） |
| `expert_work_uplift_skill_high_risk_event_total` | counter | `event` | 高风险 skill 发布门事件（U-24） |
| `expert_work_uplift_skill_view_archived_blocked_total` | counter | `()` | skill_view 命中已归档 skill 冷路径（U-29） |
| `expert_work_uplift_curator_transition_total` | counter | `from_state,to_state` | skill 生命周期转换（U-26/U-29） |
| `expert_work_uplift_curator_pinned_skills` | gauge | `()` | 全租户 pinned skill 总数（U-25） |
| `expert_work_uplift_memory_cluster_candidates_total` | counter | `()` | consolidator embedding 预筛簇候选（U-35） |
| `expert_work_uplift_memory_cluster_rejected_total` | counter | `reason` | consolidator LLM 拒簇，按防误学原因 |
| `expert_work_uplift_memory_consolidated_total` | counter | `()` | consolidator 写入的合并长期记忆 |
| `expert_work_uplift_memory_purged_total` | counter | `category` | noise-purge 软删的孤项瞬态记忆（U-37） |
| `expert_work_uplift_memory_reviewed_durable_total` | counter | `()` | 复核后保留的孤项瞬态记忆（U-37） |
| `expert_work_uplift_consolidator_llm_tokens_total` | counter | `model,kind` | MemoryConsolidator aux 模型 token |
| `expert_work_uplift_consolidator_runs_total` | counter | `outcome` | consolidator worker 扫描完成 |
| `expert_work_uplift_credentials_resolve_total` | counter | `mode,role,key,result` | CredentialsResolver 查找（O-8） |
| `expert_work_uplift_manifest_provider_rejected_total` | counter | `provider` | manifest 发布因不支持 provider 被拒 |
| `expert_work_uplift_legacy_credentials_fallback_total` | counter | `role` | Stream O 回落：读弃用 `*_api_key_ref` 字段 |

**`expert_work_webhook_*`（6，HX-9）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_webhook_deliveries_enqueued_total` | counter | `()` | 从 3 源表入队的投递（run/approval/artifact） |
| `expert_work_webhook_deliveries_succeeded_total` | counter | `()` | 收到 2xx 的投递 |
| `expert_work_webhook_deliveries_retried_total` | counter | `()` | 排定退避重试的投递 |
| `expert_work_webhook_deliveries_dead_lettered_total` | counter | `()` | 弃置投递（4xx 配置错或重试耗尽） |
| `expert_work_webhook_breaker_skips_total` | counter | `()` | 因 endpoint 断路器打开而跳过的投递 |
| `expert_work_webhook_delivery_cycle_errors_total` | counter | `()` | 投递 worker 周期异常 |

**`expert_work_billing_*` / `expert_work_platform_*`（3）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_billing_rollup_unpriced_rows_total` | counter | `()` | rollup 无法定价的 token_usage 行 |
| `expert_work_billing_rollup_unpriced_buckets_total` | counter | `()` | 写入 priced=false 的 ledger bucket |
| `expert_work_platform_credentials_tenant_overrides` | gauge | `()` | 配置的 per-tenant 凭证 override 行数（HX-8） |

**其他 / Misc（12）**

| metric | type | labels | 用途 |
|---|---|---|---|
| `expert_work_orchestrator_run_retry_total` | counter | `outcome` | run 级瞬态重试，按最终结局（HX-3） |
| `expert_work_durable_resume_seconds` | histogram | `()` | resumed run 从 RUNNING 到首 chunk 秒数 |
| `expert_work_run_event_persist_total` | counter | `event_name` | run_agent 后台批写队列（有界，drop-oldest）经 `append_batch` 落库成功的帧（二期 PR3，终态前 `_drain_persist_queue` 等 flush） |
| `expert_work_run_event_persist_errors_total` | counter | `event_name` | 同上批写路径，`append_batch` 整批失败 |
| `expert_work_run_event_queue_dropped_total` | counter | `event_name` | 后台批写队列已满时 drop-oldest 丢弃的帧（二期 PR3） |
| `expert_work_delegations_gated_total` | counter | `tool` | 委托被全局并发闸拒绝（acquire 超时），按工具（二期 PR3；`tool` 标签为 follow-up 补） |
| `expert_work_delegation_gate_fail_open_total` | counter | `()` | `DelegationGate` 容量 provider 读失败，闸 fail-open（不限流）（follow-up） |
| `expert_work_built_agent_cache_entries` | gauge | `scope` | in-process built-agent 缓存现存条目数，按 scope（二期 PR2 T4；此前漏登记） |
| `expert_work_trajectory_recorded_total` | counter | `outcome` | 成功写 ObjectStore 的轨迹（L.L7） |
| `expert_work_trajectory_record_errors_total` | counter | `outcome,reason` | 轨迹写失败（吞掉以保终态路径干净） |
| `expert_work_ew_token_estimated_total` | counter | `tenant_id,agent_name,model` | 每次 LLM 调用估算 prompt token（HX-1 drift 分子） |
| `expert_work_checkpoint_op_seconds` | histogram | `op` | checkpointer 每次 IO 调用 wall-clock |

> 计：counter ~84 / histogram 9 / gauge ~14；共 107。

#### 5.2.2 M1-target（设计规划，M0 未实现）

下列指标在早期设计列出但 M0 **未** emit；按子系统上线时补，命名先过 validator：

| metric | type | 归属 | 备注 |
|---|---|---|---|
| `expert_work_llm_latency_seconds` | histogram | 10 | LLM 调用时延（现仅 token/cost，无独立 latency） |
| `expert_work_subagent_total` / `expert_work_subagent_depth` | counter/histogram | 24 | 子 agent spawn/深度（Stream 24 上线时补） |
| `expert_work_quota_exceeded_total` | counter | 16 | 配额拒绝（现以 `rate_limit_decisions_total` 近似，语义不同） |
| `expert_work_auth_decisions_total` | counter | C | 认证授权决策 |
| `expert_work_hitl_decision_duration_seconds` | histogram | 25 | 审批决策时长（现仅 `approvals_pending` gauge） |
| `expert_work_eval_gate_decision_total` / `expert_work_eval_regression_total` | counter | 26 | eval gate / 回归（Stream 26） |
| `expert_work_sandbox_acquire_latency_seconds` | histogram | 14 | 沙箱获取时延（现仅 cold_start + pool gauge） |
| `expert_work_event_log_append_duration_seconds` | histogram | A.4 | event_log 追加时延 |
| `expert_work_pg_connection_pool_in_use` | gauge | 23 | PG 连接池占用 |
| `expert_work_redis_command_duration_seconds` | histogram | 13/16 | Redis 命令时延 |
| `expert_work_network_egress_meta_attempt_total` | counter | 21 | 元数据端点 egress 尝试（安全 P0，>0 即告警） |
| `expert_work_dr_backup_age_seconds` | gauge | 22 | 备份年龄（RPO SLI） |
| `expert_work_tls_cert_expiry_seconds` | gauge | A.10 | TLS/mTLS 证书剩余有效期，30 天阈值告警（现靠 [runbooks/tls-certs.md](../../runbooks/tls-certs.md) 人工核） |

**关键决策**：**label cardinality 严格管控**——`agent` 是 enum（manifest name），`session_id` / `trace_id` 不进 label（高基数会爆 Prometheus），validator 在定义时拦截高基数 label。session_id 留在 trace 与 log。

### 5.3 日志必填字段

所有 INFO 及以上日志强制：

| 字段 | 类型 | 说明 |
|------|------|------|
| `timestamp` | ISO8601 | 由 logger 自动注入 |
| `level` | enum | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `logger` | str | `expert_work.{component}` |
| `message` | str | 事件名（snake_case，非自由文本） |
| `tenant` | str | 必填，contextvar 注入 |
| `trace_id` | str\|null | W3C，OTel 活跃 span 优先、contextvar 兜底；无 trace 上下文（如启动期日志）为 `null` |
| `span_id` | str\|null | W3C，同 `trace_id` 的 null 语义 |
| `run_id` | str\|null | run-worker 作用域（HX-4 Mini-ADR HX-D4）；run 外（HTTP handler / 后台 sweep）为 `null` |
| `service` | str | service name |
| `env` | str | dev/staging/prod |

> 实现：`_MANDATORY_FIELDS`（`expert-work-common/observability/log.py`）固定这 10 个键；缺失（应有却无 `tenant`/`trace_id`）不抛异常，写 `null` + 单独 WARNING `expert_work.log_schema_violation`。

可选但常见：`session_id`, `agent`, `agent_version`, `actor_id`——经 `extra={...}` 传入。

**redaction**：mandatory 字段永不被 `extra` 覆盖；`extras` 部分过 `ExtrasRedactor`（与 audit `AuditRedactor` 同形，按 `tenant_config.pii_fields`）后才落盘。

**关键决策**：**`message` 必须是 snake_case 的事件 ID**（如 `session.start`、`tool.call.start`），不允许 `f"started session {x}"`。事件 ID 是查询 key，自由文本进 `extra` 字段。

### 5.4 SLO 与错误预算

**M1 目标**：

| SLO | 目标 | 测量 |
|-----|------|------|
| 控制平面 API 可用性 | 99.9% / 30d | `1 - rate(http_5xx) / rate(http_total)` |
| 控制平面 API P99 延迟 | < 200ms | `expert_work_control_plane_http_request_duration_seconds_bucket` |
| Session 首个图节点 P95（前名 TTFT） | < 1.5s | `expert_work_session_first_node_seconds` |
| Sandbox 冷启动 P95 | < 3s (M0) / < 500ms (M1 warm pool) | `expert_work_sandbox_cold_start_seconds` |
| Durable resume 成功率 | > 99% | `expert_work_resume_total{outcome="ok"} / total` |

**错误预算策略**：

```
error_budget = 1 - SLO target
# 99.9% → 0.1% → 30d 内 ~43min 预算

burn_rate(1h)  > 14.4  → P0 告警（5min 烧完 1h 预算）
burn_rate(6h)  > 6     → P1 告警
burn_rate(24h) > 3     → P2 告警

错误预算耗尽 → CI 自动冻结新 manifest 发布 24h（写 audit）
```

**as-built（Stream 10.5）**：可用性 SLO 的多窗烧录率 recording + 告警已落
`tools/observability/rules/burn_rate.yml`——record `expert_work_slo_burn_rate{slo,window}`
（窗口 5m/30m/1h/2h/6h/24h），Google SRE 多窗模式（长窗确认 + 短窗加速）落
`ExpertWorkSLOAvailability{Fast,Medium,Slow}Burn`（P0/P1/P2，阈值同上），promtool 规则单测
（`burn_rate_test.yml`）证算术 + 告警。**仍 M1**：错误预算耗尽 → 发布冻结钩子。

### 5.5 Agent 生命周期完整追踪（E3）

> 解决"业务方想从 session_id 拉完整时间线 / 长会话 + subagent + HITL 全可见"的端到端追踪需求。
> 关键决策：**长会话不强行同 trace**——subagent / HITL pause / Durable resume 用**新 trace + Span Link**关联，
> 通过 `session_id` 在查询层聚合（理由：长 pause 期 OTel TTL 失效，必须新 trace；用 Link 维持拓扑可见）。

**session root span**

- 每个 session 对应一个根 span：`expert_work.session.run`
- TTL：7 天（足以覆盖跨节点漂移、HITL 多日 pause）
- 必填 attrs：`tenant`, `session_id`, `agent`, `agent_version`, `root_span_id`
- 起止：session 创建时 start，正常 complete / cancelled / failed / timeout 时 end + 设置 status

**子 trace 拆分规则**

| 触发 | 处理 | 关联方式 |
|------|------|---------|
| `subagent.spawn`（[24](./24-subagent-execution.md)） | child 用**新 trace** | parent span 持 child trace 的 `Span Link`；child root span attrs 含 `parent.session_id` / `parent.span_id` |
| HITL pause（[25](./25-hitl.md)） | resume 时用**新 trace** | resume span 持 pause span 的 `Span Link`；attrs `expert_work.hitl.pause_span_id` |
| Durable resume（[19](./19-durable-execution.md) crash 接管 / 长 pause 后） | 接管 worker 用**新 trace** | resume span 持 last checkpoint span 的 `Span Link`；attrs `expert_work.durable.resume_from_checkpoint_id` |

**跨 trace 拼接**：所有同 `session_id` 的 trace 通过 Tempo 索引聚合（按时间戳排序拼出完整时间线）。

**业务查询 API**

```
GET /v1/sessions/{session_id}/trace-tree
     → {
         "session_id": "...",
         "traces": [
           {"trace_id": "...", "kind": "root|subagent|hitl_resume|durable_resume",
            "parent_link": null | "<trace_id>", "started_at": "...", "ended_at": "...",
            "critical_events": [ { "span_id": "...", "name": "expert_work.hitl.pause", "attrs": {...} } ]
           }, ...
         ]
       }
```

权限：等同 session 读权限（按租户隔离 + RBAC `session:read`）。

**Tempo 必索引字段**

`session_id` / `agent_name` / `error.class` 强制索引（支持业务侧反查）。

**关键事件标注**

下列 span 必须设置 `expert_work.critical=true`，dashboard 高亮 + UI 时间线突出：

| 事件 | 来源 | span 名 |
|------|------|---------|
| HITL 触发 | 25 | `expert_work.hitl.interrupt` |
| 模型降级触发 | 10 | `expert_work.llm_gateway.fallback` |
| Quota 拒绝 | 16 | `expert_work.quota.deny` |
| Subagent spawn | 24 | `expert_work.subagent.spawn` |
| Sandbox 驱逐 | 14 | `expert_work.sandbox.evict` |
| Durable resume | 19 | `expert_work.durable.resume` |

**session 异常终止**

`expert_work.session.run` 根 span status **必须**置 ERROR + reason（`crash` / `oom` / `cancelled` / `timeout` / `unrecoverable_error`），
并写 attr `expert_work.session.exit_reason`。

**replay 时 trace**

副作用工具被 idempotency_key 命中、跳过实际 execute 时，仍 emit 新 span（不 silent），但 attribute 加 `expert_work.replayed=true`，
便于查询时区分"实际发生" vs "replay 占位"。

**UI（admin web）**

提供「session lifecycle 时间线视图」：
- 瀑布图：root trace + 子 trace 按时间排序，子 trace 用不同颜色标识 kind
- 关键事件标注：critical=true 的 span 在时间线上以图标标注
- 子 trace 跳转链接：点击切换到 Tempo 详情视图
- 异常终止徽章：root status=ERROR 时顶部显示 reason

### 5.6 Dashboard 矩阵

每个核心组件一份 Grafana JSON（存 `tools/observability/dashboards/`）：

| Dashboard | 关键 panel |
|-----------|----------|
| `01-overview` | 全局可用性、QPS、错误率、租户 top10 |
| `02-orchestrator` | session 生命周期分布、LLM tokens、tool 调用图 |
| `03-sandbox` | pool 水位、冷启动延迟、驱逐原因分布 |
| `04-llm-gateway` | provider 失败率、断路器状态、token / $ |
| `05-control-plane` | API 延迟分布、用户操作热区 |
| `06-tenant-{tenant_id}` | 单租户视图：QPS、cost、error |
| `07-slo` | 各 SLO 燃烧率、剩余预算 |

### 5.7 告警分级

| 级别 | 路由 | 响应 SLA | 例 |
|------|------|---------|-----|
| P0 | PagerDuty + 飞书 oncall | 5 min | 控制平面不可用、Postgres 主挂 |
| P1 | PagerDuty | 30 min | LLM provider 全部失败、SLO 14.4 burn rate |
| P2 | 飞书告警群 | 24h | 错误预算 24h burn、单 tenant 异常飙升 |
| P3 | 邮件周报 | 周 | 容量趋势、cost 异常 |

每个告警必须有 runbook 链接（`docs/runbooks/{alert_name}.md`）。

### 5.8 跨服务 trace 传播实现纪律（A.8 收尾）

> §6 的"trace 丢失 parent span"缓解项落地。原语（`observability/propagation.py` 的 `extract_context` / `inject_context`，OTel textmap facade）+ 入站提取（control-plane `middleware/observability.py`）已就位；本节锁**注入边界**、**提取边界**、**测试契约**三条纪律，闭合调用链断裂风险。

**进程拓扑前提**：`orchestrator` 是 `control-plane` 进程内库（无独立服务入口），二者同进程靠 OTel contextvars 自动传播，**不需要 header 注入**。真正的跨进程 HTTP hop 只有信任面内的：

```
control-plane / orchestrator ──HTTP──> sandbox-supervisor   ← 唯一需手动 inject 的内部 hop
            （HTTPSupervisorClient, tools/sandbox.py）
```

**纪律 1 — 注入边界 = 仅信任内部 hop，外部 egress 绝不注**

| 出站点 | 是否注 traceparent | 理由 |
|--------|:--:|------|
| `HTTPSupervisorClient`（→ sandbox-supervisor） | ✅ | 信任面内部 hop，续接 trace |
| `http` 工具 / `mcp` / `web_search` | ❌ | 租户指定的**第三方** URL；注入会把内部 trace_id 泄给外部，且对方不消费 |
| LLM provider（anthropic/openai/…） | ❌ | 外部 SaaS，同上 |
| sandbox → `credential-proxy` egress | ❌（出 scope） | proxy 只接**非信任** sandbox 的 egress；我方 traceparent 不进沙箱，故 proxy 入站无我方 trace 可续。proxy 自身仍 emit `expert_work.credential_proxy.*` component span（§5.1），但不参与跨进程 parent 续接 |

> 安全公理：traceparent 仅在**信任边界内**传播。跨信任边界（→ 第三方 / → 沙箱）一律不带，避免内部拓扑信息外泄。

**纪律 2 — 提取边界 = supervisor 入站补 middleware**

`sandbox-supervisor/app.py` 当前**无**入站 trace 提取——注了也续不上。补一个镜像 control-plane `middleware/observability.py` 的 ASGI middleware：以 incoming `traceparent` 为 parent 续 server span（无 header 则起新 root span，向后兼容直接调用 supervisor 的运维/测试场景）。

**纪律 3 — 测试契约 = 跨服务 round-trip（ASGI in-process）**

supervisor 是独立服务，但验证传播**无需真起两进程**——用 `httpx.ASGITransport` 直打 supervisor app：

1. orchestrator 侧：`HTTPSupervisorClient` 发请求时，断言 outbound headers 含 `traceparent` 且其 trace_id == 当前活跃 span 的 trace_id。
2. supervisor 侧：入站 middleware 提取后，断言 server-side 活跃 span 的 trace_id == client trace_id（parent 续接成立）。
3. 无活跃 span（OTel 未 init）时：inject 不写 header / 写无效 traceparent，supervisor 起新 root span——两端都不报错（向后兼容）。

**显式不做（本期）**：baggage 传播（租户路由依赖时再做，M2，见 propagation.py 注释）；tracestate 透传（仅 traceparent，W3C 必需项足够）。

---

## 6. 失败模式 & 缓解

| 失败模式 | 影响 | 缓解 |
|---------|------|------|
| OTel Collector 不可达 | trace/metric/log 全断 | SDK 配 retry buffer（10MB）+ batch；buffer 满后丢弃最旧 + WARNING |
| Prometheus remote-write 拥堵 | metric 滞后 | 提高 batch、降低 push 频率（30s）；本地 ring buffer |
| Loki 写入失败 | 日志丢失 | sidecar 保留磁盘缓冲 24h；超出按 FIFO 丢 |
| 高基数 label 爆 Prometheus | 查询慢 / OOM | label cardinality 检查 CI gate（regex 检查 metric definition） |
| 日志泄漏 PII | 合规违规 | redactor 中间件强制；CI 跑 PII detector 扫描 fixture log；定期审计 |
| trace 丢失 parent span（context 不传播） | 调用链断 | 内部 hop 强制 `traceparent` header（注入/提取/测试纪律见 §5.8）；CI 集成测试覆盖 |
| 告警风暴 | oncall 疲劳 | Alertmanager group_by + inhibit + silence；每周回顾删掉低价值告警 |
| dashboard 慢查询 | 查询超时 | 大 panel 用 recording rule 预聚合；不直接查原始 metric |
| dev 环境告警被生产看到 | 噪音 | 强制 `env` label；告警路由按 env 分组 |

---

## 7. 可观测性

（本子系统是 observability 自身，meta 层）

| 自监控 metric | 说明 |
|---------------|------|
| `expert_work_otel_export_failed_total{type}` | export 失败计数（type=trace/metric/log） |
| `expert_work_log_schema_violation_total` | 日志缺必填字段次数 |
| `expert_work_redaction_applied_total{tenant,field}` | redactor 命中次数（健康度） |
| `expert_work_slo_burn_rate{slo}` | recording rule 计算的燃烧率 |
| `expert_work_alert_active_total{severity}` | 当前活跃告警数 |

---

## 8. 安全考虑

| 攻击面 | 防御 |
|--------|------|
| 日志含 secret/PII | redactor 中间件默认拦截；secret 字段 `*` 完全替换；PII 按 `tenant_config.pii_fields` 哈希或截断 |
| 跨租户日志可读 | Loki 按 `tenant` label 强制 RBAC（Grafana datasource per tenant）；查询语句注入 `{tenant="..."}` |
| trace 中 prompt 内容泄漏 | span attrs 不存 prompt body；只存 hash 与长度；body 进 event_log（受租户隔离） |
| metric label 含 user 输入 | 严禁；CI 静态检查 `Counter().labels(...)` 中的 label 必须是常量 |
| Grafana 公网暴露 | 内网 only + SSO；公开 dashboard 走只读快照 |
| 告警内容含敏感数据 | 告警模板限制为 metric 值 + tenant + service，不带 prompt/PII |

**关键决策**：**redaction 在 SDK 层而非 Collector 层**——SDK 拦截源头，避免 Collector 故障时绕过；Collector 再做一遍兜底。

---

## 9. M0 / M1 / M2 演进

### M0（5-7 周）—— 基础 trace + 简单 dashboard
- OTel SDK 接入所有服务（trace + metric）
- Loki 接入 stdout JSON 日志
- 命名规范文档化 + CI lint（regex 检查 metric/span 命名）
- Grafana 基础 dashboard：overview + orchestrator + sandbox（3 个）
- 简单告警：5xx 率、Postgres 连接耗尽

### M1（6-8 周）—— 完整 SLO + 错误预算 + 告警体系
- 完整 6 个 dashboard 上线
- SLO 定义入库 + recording rule 自动计算 burn rate
- Alertmanager 分级路由（PagerDuty + 飞书）
- runbook 库初版（每个告警一篇）
- redactor 中间件正式上线 + CI PII 扫描
- 错误预算冻结发布机制

### M2（4-6 周）—— Agent 视角观测 + 业务大盘
- 集成 Langfuse（开源）做 agent 链路可视化（token 分布、tool 调用图）
- session 时间线 UI（基于 event_log + trace）
- 业务大盘（每租户 cost、quality、QPS）
- session 回放（time travel）：基于 event_log 重放
- 慢查询 / 火焰图（py-spy）

### M3 —— 跨集群 + 异常根因
- 多 region trace 联邦（Tempo federation）
- 自动异常根因（基于 trace + metric correlation）
- 容量预测模型

---

## 10. 开放问题

1. **Langfuse 自建 vs 托管**：自建 Postgres 多一份压力；托管 SaaS 涉及租户数据出境。倾向 M2 自建。
2. **trace 采样率**：100% 太贵，按 tenant 配置 vs 全局固定？倾向 99% 错误必采 + 业务 1% 采样 + 可配 override。
3. **业务 PII 审计**：redaction 命中率监控告警阈值多少？倾向 < 0.01%（命中说明业务侧没按 schema 写）。
4. **跨集群 trace 联邦**：M3 用 Tempo 还是切 Honeycomb？取决于规模。
5. **session 回放权限**：admin 才能看明文 vs 业务 owner 也可看？倾向 RBAC 细分 `session:replay:read`。
