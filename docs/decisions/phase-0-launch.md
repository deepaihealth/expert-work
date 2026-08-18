# Phase 0 启动前决策记录

> 决策时间：2026-05-11
> 对应迭代计划：[ITERATION-PLAN.md](../ITERATION-PLAN.md) Phase 0.1

本文件记录 Phase 0.1 五项启动前决策的最终结论，作为后续 ADR / Stream 设计的输入。

---

## 决策 1：dogfood 首迁业务 — ❓ TBD

**状态**：暂未选定。

**影响范围**：
- M0 Stream H.5（dogfood 业务 manifest 编写）必须在此决策落地后才能开干
- M0→M1 Gate（平行运行 30 天对比）依赖此决策

**推迟原则**：可推迟到 Stream H 之前（约 M0 中后段）再拍板；不阻塞 Phase 0.2-0.5 与 Stream A-G 推进。

**选定标准**（决策时参照）：流量适中（1k-10k req/天）+ 工具调用简单（builtin / http / mcp）+ 合规简单（非医疗 / 非金融严格合规域）。

---

## 决策 2：Linux 部署服务器 — ✅ 可申请 / 报备

**结论**：通过公司流程申请 / 报备，预期本周内到位。

**影响范围**：
- Stream F（Sandbox / gVisor）必须在 Linux 主机跑 prod；本地 dev 用 OrbStack / Lima 占位
- Phase 0.3 三环境配置框架可以先用占位 hostname 写好

**操作清单**：
- [ ] 提申请 ticket（云 ECS 实例，规格暂定 8 vCPU / 16GB / 200GB SSD）
- [ ] 到位后写入 `environments/staging.yaml` 真实 hostname
- [ ] 装 Docker + runsc（gVisor）

---

## 决策 3：Vault / Postgres / 监控基础设施 — ✅ 混合姿态

**结论**：

| 组件 | 部署方式 | 选型 |
|------|---------|------|
| **VM 主机** | 云 | 阿里云 ECS |
| **Postgres** | 云 | 阿里云 RDS PostgreSQL |
| **加密密钥管理（KMS）** | 云 | 阿里云 KMS（at-rest 加密 + DEK 包装） |
| **对象存储** | 云 | 阿里云 OSS（uploads / snapshots / audit WORM / event_log 归档） |
| **镜像仓库** | 云 | 阿里云 ACR |
| **应用 Secret 存储（Vault）** | ❓ TBD | 候选：HashiCorp Vault 自托管 vs KMS Secrets Manager 替代；M0 内决策 |
| **可观测**（trace/log/metric） | 自托管 | Prometheus + Grafana + Loki + Tempo + Langfuse |
| **Langfuse** | 自托管 | LangSmith 在国内不可用 → 自托管 Langfuse（ADR-0005） |

**影响范围**：
- ADR-0004（对象存储）→ 阿里云 OSS
- ADR-0005（可观测）→ 自托管 Langfuse
- Stream A.5（对象存储抽象）实现层接 OSS SDK；保留 S3 兼容接口便于本地 dev 用 MinIO
- Stream A.6（Postgres 备份）→ 用阿里云 RDS 自带备份策略 + WAL 增量
- Phase 0.3 镜像 registry → 阿里云 ACR

**操作清单**：
- [ ] 申请阿里云账号 / 子账号 + 必要权限
- [ ] 申请 RDS PostgreSQL 实例（dev 1 个 / staging 1 个 / prod 1 个）
- [ ] 申请 KMS 密钥 + ACR 私有仓库
- [ ] 申请 OSS bucket（按环境隔离）
- [ ] **Vault vs KMS Secrets Manager 二次决策**（写 ADR-0007）

---

## 决策 4：项目命名 — ✅ Expert Work

**结论**：项目最终命名为 **Expert Work**。

**与现状对齐**：
| 名称位置 | 现状 | 处理 |
|---------|------|------|
| GitHub repo | `deepaihealth/expert-work` | ✅ 已对齐 |
| Python 包 | `expert_work`（`src/expert_work/__init__.py`） | ✅ 已对齐 |
| docs/README.md 标题 | "Expert Work — 自研..." | 🔄 后续 docs/branding-cleanup PR 统一为 "Expert Work" |
| 各 docs 内提及 | "Expert Work" | 🔄 同上 |

**影响范围**：仅文档措辞，零代码层影响。

**操作清单**：
- [ ] 单独 PR `docs/branding-cleanup`：把 `docs/` 下"Expert Work"的项目级提及统一为"Expert Work"（不动 `expert_work` Python 包名）

---

## 决策 5：LangGraph 培训时间窗 — ❓ 软推迟

**状态**：暂未明确，但**不阻塞**任何后续推进。

**操作建议**：
- LangGraph 文档边干边读，主要在 Stream A.2 vendor DeerFlow + Stream E.1 PostgresSaver 接入时密集补
- 若发现需要系统补课，再开 2-3 天集中时间

---

## 后续 ADR 触发 — ✅ 全部完成（2026-05-11）

| ADR | 标题 | 状态 |
|-----|------|------|
| [ADR-0002](../adr/0002-state-layer-schema.md) | 状态层 schema（event_log + audit_log 分表） | ✅ |
| [ADR-0003](../adr/0003-authentication.md) | 认证选型 — OIDC + 自建 Keycloak + JWT | ✅ |
| [ADR-0004](../adr/0004-object-storage.md) | 对象存储 — 阿里云 OSS + S3 兼容抽象层 | ✅ |
| [ADR-0005](../adr/0005-observability-stack.md) | 可观测栈 — 自托管 Langfuse + Prometheus + Loki + Tempo + Grafana | ✅ |
| [ADR-0006](../adr/0006-compliance-pluggability.md) | 合规可插拔架构（`compliance_pack` 字段语义） | ✅ |
| [ADR-0007](../adr/0007-secret-store.md) | 应用 Secret 存储 — 阿里云 KMS Secrets Manager（M0）+ M1 评估 Vault | ✅ |

---

## 决策更新记录

| 日期 | 变更 |
|------|------|
| 2026-05-11 | 初版：5 项决策落地（决策 1、5 标 TBD；决策 2-4 已定；新增 ADR-0007 待办） |
| 2026-05-11 | ADR-0002 至 ADR-0007 全部完成并合入 docs/adr/ |
