# Runbook — Control Plane

> Stream G.3 故障预案。适用告警：`ExpertWorkControlPlaneDown`（P0）、
> `ExpertWorkControlPlaneHigh5xxRate`（P1）、`ExpertWorkControlPlaneHighLatency`（P2）、
> `ExpertWorkQuotaReaperErrors`（P2）、`ExpertWorkSandboxEgressAuthBlocked`（P2）
> —— 见 [`tools/observability/rules/alerts.yml`](../../tools/observability/rules/alerts.yml)。

## 适用范围

control-plane 是 M0 的 API 入口 + in-process orchestrator 宿主（STREAM-E § 2.6）。
它挂了 = agent 全停。

## 故障现象

| 告警 | 现象 |
|------|------|
| `ExpertWorkControlPlaneDown` | Prometheus 2m 抓不到 `expert-work-control-plane` target；API 不可达 |
| `ExpertWorkControlPlaneHigh5xxRate` | 5m 成功率 < 95%；客户端大量 5xx |
| `ExpertWorkControlPlaneHighLatency` | P99 > 0.5s（SLO < 0.2s）|
| `ExpertWorkQuotaReaperErrors` | quota reaper 后台循环报错（reservation 可能泄漏）|
| `ExpertWorkSandboxEgressAuthBlocked` | 沙箱出网被 credential-proxy 以 407 拒绝（`blocked_auth`）——egress token 过期或缺失 |

## 诊断

1. **容器状态**：`docker compose ps control-plane` —— Up / Restarting / Exited？
2. **日志**：`docker compose logs --tail 100 control-plane` —— 查 `ERROR` / 启动栈。
3. **健康探针**：`GET /healthz/ready`（容器内 `localhost:8000`）——
   `ready` 聚合依赖检查，看哪个 dep 失败。
4. **依赖**：
   - Postgres / PgBouncer：见 [postgres.md](./postgres.md)。
   - Redis（quota 后端）：`docker compose ps redis`。
5. **延迟/5xx**：Grafana `Expert Work — Overview` 大盘 + Tempo 查慢 trace
   （`expert_work.control_plane.http_request` span）。
6. **reaper 报错**：日志查 `quota.reaper`；通常是 DB 连接抖动。
7. **沙箱出网 407（`blocked_auth`）**：credential-proxy 侧语义见
   [credential-proxy.md](./credential-proxy.md)——egress token 缺失/校验失败时
   proxy 在识别身份前就拒绝，`sandbox_egress_audit` 的 `blocked_auth` 行因此
   **没有** `sandbox_id`/`tenant_id`（design §3.1 audit-eval Phase 4，platform
   级匿名异常），不能直接按行 join 回具体沙箱。改按时间关联：
   - 查 `sandbox_instance` 表里 `acquired_at` 早于「现在 −
     `egress_token_ttl_s`」（默认 24h）的行——这些热会话仍在用已过期的
     egress token；正常情况下年龄封顶（`ttl // 2`，默认 12h）会在下次
     `acquire()` 时强制重建，见不到这类行。
   - 若查到了，说明年龄封顶没生效（`acquire()` 里 `_max_warm_age_s()`
     的年龄检查没跑到——比如复用路径被绕过、或该沙箱走的是不含此检查
     的后端/路径）；orchestrator 日志查
     `"warm sandbox %s past age cap"`（`agent_sandbox.py`）确认重建是否
     真的触发过。

## 处置

- **容器 crash / 不健康**：`docker compose restart control-plane`；起不来看日志定位
  （多为 DB DSN / 迁移未应用 / 配置错误）。
- **5xx 飙升**：定位是依赖故障（DB/Redis）还是代码缺陷。依赖故障 → 修依赖；
  代码缺陷 → 回滚（见下）。
- **延迟升高**：查 Postgres 慢查询（`pg_stat_statements`）、PgBouncer 连接池水位。
- **reaper 报错**：DB 恢复后 reaper 下个周期自愈；持续报错则查 `tenant_quota` /
  `token_reservation` 表与 DB 角色权限。
- **沙箱出网 407**：诊断锁定的单个 warm session → `destroy` 掉对应
  `sandbox_instance` 行（下次该 agent acquire 会拿到新沙箱 + 新 token，
  407 自愈）；若是批量出现（年龄封顶逻辑本身失效，不是个别沙箱倒霉）→
  `POST /v1/sandboxes/reap?force=true`（system_admin，见 `api/sandboxes.py`）
  拆掉表里记着的每一个活跃热会话（不看空闲时间，语义与
  `sandbox.md` 里 docker-supervisor 后端自己的 TTL reaper 不同——这条打的
  是 AgentSandboxClient 云后端），逼所有 agent 下次 acquire 都拿新 token；
  收尾要确认 `agent_sandbox.py` 的年龄封顶检查确实在对应后端路径上生效，
  不然过一个 age-cap 周期又会复发。

## 回滚

control-plane 镜像无状态，回滚 = 部署上一版镜像：

```bash
docker compose pull control-plane          # 或指定上一个 tag
docker compose up -d control-plane
```

DB schema 向后兼容（expand-contract，M1-B 规范化前 M0 靠人工确认）——
回滚镜像前确认目标版本与当前 schema 兼容；不兼容则需同时回滚迁移。

## 升级

P0（Down）5min 未恢复 → 升级到 oncall negative；
依赖根因（Postgres 主挂）→ 转 [postgres.md](./postgres.md) 并按其 P0 流程。
