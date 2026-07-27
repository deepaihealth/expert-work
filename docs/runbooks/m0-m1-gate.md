# M0 → M1 Gate runbook (Stream M)

> Operator-facing protocol for the 30-day Gate window that decides
> whether Expert Work promotes from M0 to M1 production. Replaces the original
> "相对 Dify" framework — see
> [`docs/streams/STREAM-M-DESIGN.md`](../streams/STREAM-M-DESIGN.md)
> for the design and [`docs/ITERATION-PLAN.md`](../ITERATION-PLAN.md)
> § "M0→M1 Gate" for the canonical Exit Criteria.
>
> Use this runbook to drive the 30-day window, day to day, and to run
> the Go/No-Go decision at the end.

## 0. Pre-Gate readiness checklist

Confirm every item is green before opening the Gate window. The window
starts the day **all** these are checked — not before.

- [ ] Stream J 已交付能力 PR 全部合 main (J.1 / J.2 / J.3 / J.6 / J.11 /
      J.13a / J.14 / J.15 + 任何额外完工的 J 子项)
- [ ] `tools/eval/baselines/m0_gate_baseline.yaml` 锁定 + 当前 main 跑
      `python tools/eval/run_baseline.py` 重产物 = checked-in 内容
      (capability 分数 100% 稳定, 只 metadata 变)
- [ ] 24 项 P0 检查全部勾选 (参见
      [`docs/architecture/07-INFRASTRUCTURE-GAPS.md`](../architecture/07-INFRASTRUCTURE-GAPS.md))
- [ ] Stream K (15 子项) + Stream L (8 子项) + Stream J.13a 收尾零债 6 条
      核验全过
- [ ] G.2 告警 + Alertmanager routing 到 on-call channel 已联调
- [ ] staging 环境部署 main 最新 commit; smoke 测试通过
- [ ] canonical agent manifest 已部署 staging 并可创建会话

打开 Gate 的 commit sha + 日期写入下面这块, Gate 期间不要改动:

```
Gate opened: 2026-MM-DD
expert_work_commit: <sha>
opened_by: <operator name>
canonical_agent_manifest: <manifest_name>:<version>
```

## 1. 30-day observation protocol

### 1.1 Cadence

| 频率 | 动作 | 责任 |
|------|------|------|
| 实时 | Alertmanager → on-call | on-call |
| 每日 | 检查 K10 大盘 8 项 SLO + 当日错误率, 把异常进 dashboard 注释 | Gate operator |
| 每周一 | 跑 `python tools/eval/run_baseline.py`, 与 main 上的 baseline diff capability 分数 | Gate operator |
| 每周一 | dogfood (可选 sanity check) 跑一轮 canonical agent 端到端用例 | Gate operator |
| 触发即响应 | P0 事故 retro + 30 天计数器重置 | on-call + tech lead |

### 1.2 Daily SLO check

每日 09:00 (本地时间) 用 K10 G.7 大盘核对 § 1.4 的 8 项 SLO. 如果任一指
标 30 天滚动窗口已超 Gate 阈值, 标红 + 写入 `gate-log.md` 当日条目, 不要
等告警自动触发.

具体命令 (假设 Prometheus 在 `http://localhost:9090`):

```sh
# 可用性 ratio (5xx 错误率) - 5 分钟窗口
curl -s 'http://localhost:9090/api/v1/query?query=Expert Work:sli:control_plane_availability:ratio5m'

# 首个图节点 P95(一期 Task 3 前名 TTFT)- 1 小时窗口
curl -s 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.95, sum by (le)(rate(expert_work_session_first_node_seconds_bucket[1h])))'

# Sandbox 冷启动 P95 - 1 小时
curl -s 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.95, sum by (le)(rate(expert_work_sandbox_cold_start_seconds_bucket[1h])))'

# Durable resume P95 - 1 小时
curl -s 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.95, sum by (le)(rate(expert_work_durable_resume_seconds_bucket[1h])))'

# End-to-end session P95 (outcome=success only) - 1 小时
curl -s 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.95, sum by (le)(rate(expert_work_session_duration_seconds_bucket{outcome="success"}[1h])))'

# SSE stream stale 率 - 1 小时
curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(expert_work_llm_stream_stale_total[1h])) / sum(rate(expert_work_llm_tokens_total[1h]))'
```

### 1.3 Weekly eval baseline drift

每周一 10:00 跑 baseline + diff:

```sh
# 1. 切到 main 最新
git checkout main && git pull

# 2. 跑 baseline 写到一次性路径 (不污染 main 上的制品)
.venv/bin/python tools/eval/run_baseline.py --out /tmp/baseline-week-N.yaml

# 3. diff capability 分数 (忽略 metadata)
diff <(yq '.capabilities' tools/eval/baselines/m0_gate_baseline.yaml) \
     <(yq '.capabilities' /tmp/baseline-week-N.yaml)
```

**判定**:
- diff 为空 → 写 `gate-log.md` "Week N baseline drift = 0, PASS"
- capability 分数有降 → 进 retro, 找根因; 不允许"分数轻微下降但继续观察"
- capability 状态从 PASS 转 FAIL → P0 升级 + 修复 PR + 重新跑确认恢复

### 1.4 八项 SLO 阈值

| SLO | Gate 阈值 | Prometheus query 核心部分 |
|-----|----------|--------------------------|
| 可用性 (control-plane 5xx) | < 0.1% in 30d | `Expert Work:sli:control_plane_availability:ratio5m` (>= 0.999) |
| 首个图节点 P95(前名 TTFT) | < 2.0s | `expert_work_session_first_node_seconds_bucket` |
| End-to-end P95 (canonical agent run) | < 30s | `expert_work_session_duration_seconds{outcome="success"}_bucket` (recording rule `Expert Work:sli:session_duration:p95_5m`) |
| SSE 流断裂率 | < 0.05% in 30d | `expert_work_llm_stream_stale_total / expert_work_llm_tokens_total` |
| Sandbox 冷启动 P95 | < 5s | `expert_work_sandbox_cold_start_seconds_bucket` |
| Durable resume P95 | < 1.0s | `expert_work_durable_resume_seconds_bucket` |
| Memory recall@5 | ≥ 0.7 | `tools/eval/memory_recall.py` against real embedder |
| P0 事故数 | = 0 in 30d | manual incident log + § 1.5 reset rule |

**所有 8 项 SLO 指标 emission 已就位** (E2E P95 在 2026-05-21 PR 接入,
labelled by `outcome` 以便过滤 error / max_steps / cancelled tails).

### 1.5 P0 incident response

P0 = 生产宕机 / 数据损坏 / cross-tenant 越权 / Gate SLO 严重 (≥ 2 倍) 越限

P0 触发即响应:

1. on-call 进战时房, 召集 tech lead + 当事 stream owner
2. 5 分钟内: incident commander 指定 + status page 更新
3. 30 分钟内: 临时 mitigation 上 (回滚 / 限流 / 切流)
4. 24 小时内: retro + 修复 PR 设计
5. 修复合并后: 30 天计数器重置 = 0

**计数器重置规则**: Gate 期间任意 P0 → 当前 30 天窗口废弃, 修复合 main
后, 从新的 commit 重新开 30 天. 不允许"绝大部分 P0 已解决, 算它过".

## 2. Eval baseline 周跑 SOP

见 § 1.3. 周跑产物归档到 `gate-log/eval-week-N.yaml` (本地 git 仓库或
GitHub release notes), 让 Gate 退出决策时有完整轨迹.

## 3. 安全验收 SOP

### 3.1 沙盒 7/7 用例

Gate 期间至少**一次** staging Linux 主机跑通 (K5 已锁定不允许"软推迟"):

```sh
cd services/sandbox-supervisor
.venv/bin/pytest tests/integration/test_gvisor_security.py -v
```

期望: 7/7 PASS, 含 `test_gvisor_cve_2019_5736_poc_fails` 和
`test_gvisor_timing_isolation`. 任一 SKIP / FAIL → Gate 不退出.

### 3.2 Cross-tenant 测试

```sh
.venv/bin/pytest services/control-plane/tests/test_sse_cross_tenant.py \
                packages/expert-work-persistence/tests/test_memory_recall_cross_tenant.py \
                services/control-plane/tests/test_artifact_cross_tenant.py -v
```

期望: 全部 reject (期望异常被正确抛出). 任何静默通过 → P0.

## 4. 数据保护演练 SOP

Gate 期间各演练一次, 写入 `gate-log/drills/`:

| 演练 | 跑法 | Runbook |
|------|------|---------|
| PG 恢复 | testcontainers 集成测试 + staging dump→restore 闭环 | [`pg-restore.md`](./pg-restore.md) |
| WORM 恢复 | round-trip integrity drill + staging 演练 1 次 | [`audit-restore.md`](./audit-restore.md) |
| KMS 轮换 | drill 测试 + staging 演练 1 次 | TODO (K13 drill 文档化) |
| Volume restore | restore drill (J.15 备份 → 新卷) | [`volume-restore.md`](./volume-restore.md) |

任一演练失败 → 回归 retro, 修复后重跑通过才能继续 Gate.

## 5. 退出决策 (Go/No-Go)

30 天到期当天召开 Go/No-Go 评审:

### 5.1 GO Criteria (全部满足)

- [ ] § 1.4 八项 SLO 30 天滚动窗口全部满足
- [ ] § 2 eval baseline 每周 drift = 0, capability 状态全部 PASS
- [ ] § 3.1 沙盒 7/7 用例 staging 跑通
- [ ] § 3.2 cross-tenant 测试全部 reject
- [ ] § 4 PG / WORM / KMS / Volume 数据保护演练全部成功
- [ ] § 1.5 30 天计数器 P0 = 0 (含任何 reset 后的新 30 天)
- [ ] Canonical agent 端到端 6 条 (STREAM-M-DESIGN § 2.2) 跑通

### 5.2 GO 流程

1. 评审会签字: tech lead + on-call + Gate operator
2. ITERATION-PLAN.md M0→M1 Gate 段全部 `[ ]` 翻 `[x]`
3. M1 入场: 切换到 STREAM-M1 design (新 stream 启动)

### 5.3 NO-GO 处理选项

| 选项 | 适用情形 | 后续 |
|------|---------|------|
| A. 再走一轮 30 天 | 1-2 个 SLO marginally 越线, 已识别明确单点 | 修复 + 30 天观察重新计时 |
| B. 暂停修复 (热修补) | 多个 SLO 越限, 但根因可在 1-2 周内解决 | 暂停 Gate, 走临时修复 sprint, 后回 Gate |
| C. 回退架构 | 系统级 SLO (首个图节点/E2E) 跑不到目标 1.5 倍内, 或安全验收红 | 架构 retro, M0→M0.5 重构 |

## 6. Failure modes — 常见处理

| 症状 | 原因 | 处理 |
|------|------|------|
| 周跑 baseline drift capability FAIL | 上游 PR 改动影响 eval | 立即 retro; baseline 不允许"暂时 FAIL 但继续观察" |
| Prometheus query 超时 | 某 SLI recording rule 计算量过大 | 缩短窗口 / 加 recording rule 缓存 |
| Alertmanager 没收到 P0 alert | severity 标签错 / routing 配置漂移 | 重新跑 G.2 路由测试 + 重启 Alertmanager |
| eval baseline 文件 metadata 之外的字段也变了 | 案例集 / 代码改动影响分数 | retro 找根因; baseline 不允许"无解释的分数漂移" |

## 7. References

- 设计源: [`docs/streams/STREAM-M-DESIGN.md`](../streams/STREAM-M-DESIGN.md)
- Exit Criteria 标准: [`docs/ITERATION-PLAN.md`](../ITERATION-PLAN.md) § "M0→M1 Gate"
- SLO 数值来源: STREAM-G-DESIGN § G.1
- Eval baseline 制品: `tools/eval/baselines/m0_gate_baseline.yaml`
- 沙盒安全用例: STREAM-K-DESIGN § K5
- 数据保护演练: STREAM-K-DESIGN § K13/K14/K15 + [`pg-restore.md`](./pg-restore.md) /
  [`audit-restore.md`](./audit-restore.md) / [`volume-restore.md`](./volume-restore.md)
