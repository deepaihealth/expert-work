# SLO / SLI 定义 — Stream G.1

> 落实 P0 #13；物化 [subsystems/20-observability § 5.4](../architecture/subsystems/20-observability.md) 的 SLO 表。
> 设计见 [STREAM-G-DESIGN § 1.1 G.1](../streams/STREAM-G-DESIGN.md)。

本文档是 M0 的 **SLO 定义**（"我们承诺什么、怎么测"）。**错误预算燃烧率告警 + 耗尽自动冻结发布**是 M1（Mini-ADR G-1）——
本阶段只把 SLO 定下来、把可测的 SLI 用 Prometheus recording rule 预聚合。

## SLO 一览

| # | SLO | 目标 | 窗口 | SLI 指标 | M0 状态 |
|---|-----|------|------|----------|---------|
| 1 | 控制平面 API 可用性 | ≥ 99.9% | 30d | `expert_work_control_plane_http_requests_total` | ✅ 指标已 emit |
| 2 | 控制平面 API P99 延迟 | < 200ms | 30d | `expert_work_control_plane_http_request_duration_seconds` | ✅ 指标已 emit |
| 3 | Session 首个图节点 P95 | < 1.5s | 30d | `expert_work_session_first_node_seconds`（一期 Task 3 前名 `..._ttft_seconds`,改名说实话——测的是首个图节点完成,不是首字）| ✅ 指标已 emit（K10） |
| 4 | Sandbox 冷启动 P95 | < 3s（M0）/ < 500ms（M1 warm pool，临时沙盒池命中路径） | 30d | `expert_work_sandbox_cold_start_seconds`；M1 验收并读池命中率 `Expert Work:sli:sandbox_pool_hit:ratio1h` + `expert_work_sandbox_pool_ready` | ✅ 指标已 emit（K10）；池指标已 emit（HX-6） |
| 5 | Durable resume P95 | < 1.5s | 30d | `expert_work_durable_resume_seconds` | ✅ 指标已 emit（K10）|
| 6 | Memory recall@5（zh+en） | ≥ 0.7（M1 against real embedder） | 单测 | `tools/eval/memory_recall.py` | ✅ 框架 + seed set 已 ship（K12）|
| 7 | Session end-to-end P95（outcome=success）| < 30s（M0 Gate）| 30d | `expert_work_session_duration_seconds{outcome="success"}` | ✅ 指标已 emit（Stream M Gate follow-up）|
| 8 | Run 成功率（排除用户中止） | TBD（基线数据未到，不预设） | 30d | `expert_work_session_duration_seconds_count{outcome}` 推导（Mini-ADR HX-D1） | ✅ rule 已落（HX-4） |
| 9 | Run 瞬态重试恢复率 | TBD | 30d | `expert_work_orchestrator_run_retry_total{outcome}` | ✅ 指标已 emit（HX-3），rule 已落（HX-4） |
| 10 | Token 估算漂移比 | ~1.0 ± 0.15（观察性，非告警线） | 7d | `expert_work_ew_token_estimated_total` / `expert_work_llm_token_usage_total{type=~"input\|cache_.*"}` | ✅ 指标已 emit（HX-1），rule 已落（HX-4） |
| 11 | Checkpoint IO P95（per op） | TBD | 30d | `expert_work_checkpoint_op_seconds{op}` | ✅ 指标已 emit（HX-4） |
| 12 | Pending approvals 积压 | 告警线 TBD（持续增长告警，M1 Alertmanager） | 即时 | `expert_work_control_plane_approvals_pending` gauge | ✅ 指标已 emit（HX-4） |
| 13 | Sandbox 池命中率（临时沙盒） | TBD（基线数据未到；池仅覆盖无 user_id acquire——持久用户首触不可池化，Mini-ADR HX-F2） | 30d | `expert_work_sandbox_pool_total{event=hit/miss}` → `Expert Work:sli:sandbox_pool_hit:ratio1h` | ✅ 指标已 emit（HX-6），rule 已落 |

> ⏳ 的三条 SLO 在 M0 已**定义**，但对应指标尚未在 M0 代码路径 emit（orchestrator session 指标、sandbox 冷启动指标、durable resume 指标分属后续 Stream）。
> 其 recording rule 随指标落地补入 `tools/observability/rules/sli.yml` —— 本阶段不写引用空指标的惰性规则。

## SLI 测量

可测的两条（recording rule 见 [`tools/observability/rules/sli.yml`](../../tools/observability/rules/sli.yml)）：

### SLO 1 — 控制平面 API 可用性

```promql
# 5 分钟成功率（非 5xx 占比）
sum(rate(expert_work_control_plane_http_requests_total{status_code!~"5.."}[5m]))
/
sum(rate(expert_work_control_plane_http_requests_total[5m]))
```

recording rule：`Expert Work:sli:control_plane_availability:ratio5m`。
无流量时分母为 0 → 记为 `NaN`（Grafana 显示 no-data），M0 接受。

### SLO 2 — 控制平面 API P99 延迟

```promql
histogram_quantile(
  0.99,
  sum by (le) (rate(expert_work_control_plane_http_request_duration_seconds_bucket[5m]))
)
```

recording rule：`Expert Work:sli:control_plane_http_latency:p99_5m`，单位秒（目标 < 0.2）。

## 错误预算（M1）

subsystems/20 § 5.4 的燃烧率策略：

```
error_budget = 1 - SLO 目标         # 99.9% → 0.1% → 30d 内 ~43min
burn_rate(1h)  > 14.4  → P0
burn_rate(6h)  > 6     → P1
burn_rate(24h) > 3     → P2
错误预算耗尽 → CI 自动冻结新 manifest 发布 24h
```

燃烧率 recording + 多窗告警已落地（Stream 10.5，`tools/observability/rules/burn_rate.yml`）：
record `expert_work_slo_burn_rate{slo,window}`（窗口 5m/30m/1h/2h/6h/24h），多窗告警 Google SRE 模式
（长窗确认 + 短窗加速：1h&5m>14.4 P0 / 6h&30m>6 P1 / 24h&2h>3 P2），promtool 规则单测覆盖
（`burn_rate_test.yml`）。**仍 M1**：错误预算耗尽 → CI 自动冻结新 manifest 发布（Alertmanager
路由 + 发布冻结钩子，Mini-ADR G-1）。
