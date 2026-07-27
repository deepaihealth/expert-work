# Stream M — M0→M1 Gate 重构（设计先行）

> 2026-05-20 未交付项审计开出。文档勾选时 ITERATION-PLAN.md § M0→M1 Gate 自己已标 "退出标准待重构"（line 462），按 [[complete-not-minimal]] + [[general-platform-positioning]] 是软推迟弱版口子 —— Expert Work 已经定位通用 agent 平台、不绑用户的 Dify 业务，但 Gate Exit Criteria 仍写着"token 消耗与 Dify 偏差 < ±10%"等绑定 Dify 的判定。本 Stream 把 Gate 从"相对 Dify"框架改为"绝对数值化 + canonical agent 端到端 + eval-set 驱动"。

## 1. 范围 & 边界

### 1.1 In-scope

- 重写 M0→M1 Gate Exit Criteria：从"相对 Dify"改为**绝对数值化 SLO 目标 + canonical agent 端到端跑通 + eval-set baseline**
- 删除"相对 Dify"对比框架（dogfood 仍可平行跑作 sanity check，但不作 Exit Criteria）
- 与 J.13a 逐能力 eval 场景集 + K12 memory recall eval gate 对接为 eval baseline
- 锚定 Stream G.1 SLO 文档（已勾 `[x]`），把 SLO 列表升级为 Gate Exit Criteria 强约束
- Gate 期间运营 runbook（dogfood 平行运行、对比观测、回滚流程）

### 1.2 Out-of-scope（明确推迟）

| 推迟项 | 落地 | 备注 |
|-------|------|------|
| 多 region / 跨 AZ 灰度切流 | M1+ | M0→M1 Gate 仍是单 region 单 AZ |
| Garak / HouYi prompt injection 自动套件 CI 接入 | M2 | M0→M1 Gate 手工跑一次即可 |
| 自动金丝雀流量推进（百分比自动爬升）| M1-G | K11 加权金丝雀 + I.2 deploy.py 已经支持手工渐进，Gate 用手工 |

### 1.3 Stream M 与 J.13、Stream G 的关系

- **G.1 SLO/SLI 定义文档**（已勾 `[x]`）= **Gate Exit Criteria 的数值来源**。Stream M 不重写 SLO，但把 G.1 的 SLO 升级为 Gate 强约束
- **J.13a 逐能力 eval 场景集**（M0 必交）= **Gate eval baseline 的数据源**。Stream M 把 J.13a baseline 锚定为 Gate Exit Criteria 之一
- **K10 G.7 大盘真闭环**（已合）+ **L7 trajectory recording**（已合）= **Gate 期间 30 天稳定性观察**的数据底座
- **K12 memory recall eval gate**（已合）+ **K15 PG 恢复演练**（已合）= 部分 Gate 评估手段已就位

---

## 2. 新 M0→M1 Gate Exit Criteria（绝对数值化）

> 替换原"相对 Dify"四条（token ±10% / p95 1.2x / 质量评估 / 30 天无 P0）。原条款保留在修订记录中。

### 2.1 系统级 SLO（数值化绝对目标）

| SLO | M0 Gate 阈值 | 数据源 |
|-----|------------|-------|
| **可用性**（control-plane 5xx 错误率）| < 0.1% in 30d | A.9 Prometheus + G.2 告警 |
| **TTFT P95**（首字节延迟）| < 2.0s | K10 `expert_work_session_first_node_seconds`（2026-07-27 改名,语义=首个图节点完成,原名 `..._ttft_seconds` 误导） |
| **End-to-end P95**（run 完成）| < 30s for 95% of canonical agent runs | E.14 SSE 端到端 + run_manager 状态 |
| **SSE 流断裂率**（mid-stream 中断后未恢复）| < 0.05% in 30d | L3 `expert_work_llm_stream_stale_total` + stream_bridge 重连数据 |
| **Sandbox 冷启动 P95** | < 5s（M0 范围）| K10 `expert_work_sandbox_cold_start_seconds` |
| **Durable resume P95**（checkpointer 恢复）| < 1.0s | K10 `expert_work_durable_resume_seconds` |
| **Memory recall@5**（中 / 英文混合 seed set）| ≥ 0.7 against real embedder | K12 `tools/eval/memory_recall.py` |
| **P0 事故数**（30 天观察期）| = 0 | G.2 告警 + 事故 retro 文档 |

### 2.2 Canonical Agent 端到端验收（per-user 持久 agent 形态）

参见 [memory:target-product-form](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/project_target_product_form.md)。Gate Exit Criteria 锁定 canonical agent 必须端到端跑通：

- [ ] **多轮对话跨会话保持记忆**：J.3 long-term memory 跨 thread 召回，K6/K7 CRUD/DLQ 闭环
- [ ] **持久工作区跨 run 留存**：J.15 docker named volume 在 TTL reaper 回收后仍可 restore，文件不丢
- [ ] **空闲 hibernate + 快速 restore**：J.15 热会话 TTL 过期后下次消息 P95 < 5s 冷启动挂卷
- [ ] **artifact 跨 thread 可访问**：J.9 artifact_version 列表 + 下载，per-user 隔离
- [ ] **审批门跑通**：J.8 危险操作触发 interrupt → control-plane resume → 继续执行 + audit trail
- [ ] **多模态输入**：J.6 Path A + Path B 均在 staging 跑通真实图像

### 2.3 Eval Baseline（J.13a 逐能力 eval 场景集）

- [ ] **逐能力 eval 场景集落地**：J.1 规划质量 / J.2 反思有效性 / J.3 记忆召回 / J.4 sub-agent 委派正确性 / J.5 RAG 相关度 / J.6 多模态理解 / J.8 审批门 / J.9 artifact 完整性 / J.11 路由决议 / J.14 跨用户隔离不泄漏 / J.15 工作区跨 run 持久 + Skill（J.7a 静态启用部分）
- [ ] **Baseline 分数锁定**：每个 eval 场景跑出基线分数，写入 `tools/eval/baselines/m0_gate_baseline.yaml`；M1 上线必须 ≥ baseline
- [ ] **CI 回归门挂在 Gate**：J.13c CI 回归门可以推 M1，但 Gate 期间至少手工跑一次 eval baseline，分数 ≥ 阈值

### 2.4 安全验收（沙盒 7/7）

- [ ] **gVisor 7/7 沙盒安全用例 staging Linux 全部跑通**（K5 已锁，不允许"软推迟"）：含 `test_gvisor_cve_2019_5736_poc_fails`、`test_gvisor_timing_isolation`
- [ ] **跨租户测试**：cross-tenant SSE / cross-tenant memory recall / cross-tenant artifact 全部 reject（K2 已铺 SSE 测试 + J.14 已铺租户 + user 复合 scope）

### 2.5 数据保护演练

- [ ] **PG 恢复演练**：K15 testcontainers 集成测试 CI 每跑 + 季度手工 staging 演练
- [ ] **WORM 恢复演练**：K14 round-trip integrity + staging 演练 1 次
- [ ] **KMS 轮换演练**：K13 drill 测试 CI + staging 演练 1 次

---

## 3. Gate 运营 Runbook

### 3.1 30 天观察期协议

1. **平行运行**（不阻塞决策）：可选用 dogfood 业务对比作 sanity check（不再作 Exit Criteria）
2. **数据采集**：所有 § 2.1 SLO 指标实时上 K10 G.7 大盘 + 告警接 G.2
3. **eval 周期跑**：每周跑一次 J.13a baseline，看分数漂移
4. **事故响应**：P0 触发 → 立刻进 retro + 修复 → 30 天计数器重置

### 3.2 退出决策点

Gate 期满满足全部 § 2.1-§ 2.5 → 进 M1
不满足 → 选项 A：再走一轮 30 天观察 / 选项 B：暂停修复 / 选项 C：回退架构

### 3.3 移除"相对 Dify"对比框架

- 删除"token 消耗与 Dify 偏差 < ±10%" → § 2.1 已用绝对值替代
- 删除"p95 延迟 < Dify 的 1.2 倍" → § 2.1 TTFT / E2E P95 已用绝对值替代
- 删除"回答质量人工评估不劣于 Dify" → § 2.3 eval baseline 替代
- 删除"每日 Dify vs Expert Work 对比报表" → § 3.1 dogfood sanity check 可选

---

## 4. Mini-ADR 汇总

### M-1｜Gate Exit Criteria 改绝对值，不绑 Dify
- **背景**：Expert Work 已定位通用 agent 平台（[[general-platform-positioning]]），dogfood 用 canonical agent，与具体 Dify 业务解耦
- **备选**：(a) 保留"相对 Dify"作主 baseline；(b) 双 baseline（Dify 对比 + 绝对值）；(c) 完全切到绝对值 + canonical agent + eval baseline
- **取舍**：选 (c) —— (a) 把 Expert Work 上限锁死在 Dify；(b) 维护两套 baseline 是浪费

### M-2｜Eval baseline 不是 LLM-as-judge 的"分数"，是逐能力测试集
- **背景**：J.13b 在线采样 + LLM-as-judge 在 M0 不交付（推 M1 早期），但 Gate 需要 baseline
- **备选**：(a) 用 LLM-as-judge 评分；(b) 用规则 / 关键指标（recall@k / 完成率 / 错误率）
- **取舍**：选 (b) —— LLM-as-judge 自身不稳定，flaky 不能作 Gate；规则 + 关键指标可重复

### M-3｜30 天观察期 P0 事故 = 0，不容忍
- **背景**：原"30 天无 P0 事故"已经够强
- **取舍**：保留，但加 "P0 触发 → 30 天计数器重置" 显式规则

### M-4｜canonical agent ≠ dogfood agent，dogfood 仍可平行跑
- **背景**：上一轮决策已经把"dogfood = canonical agent"
- **取舍**：本 Stream 进一步明确 dogfood 平行跑是**可选 sanity check**，不是 Exit Criteria，移出依赖路径

---

## 5. PR 拆分

- **M.0 设计先行**（本文档）
- **M.1 ITERATION-PLAN.md § M0→M1 Gate 整段重写** + 同 PR 删除"相对 Dify"四条 Exit Criteria
- **M.2 Eval baseline 锚定**：等 J.13a 落地后跑出 baseline + 写入 `tools/eval/baselines/m0_gate_baseline.yaml`
- **M.3 Runbook**：`docs/runbooks/m0-m1-gate.md`（30 天观察期协议 + 退出决策点 + 事故响应）
- **M.4 大盘 panel + 告警**：把 § 2.1 SLO 表里所有指标在 K10 大盘统一展示 + G.2 告警接入

M.1/M.3/M.4 与 J 剩余子项并行可做；M.2 阻塞于 J.13a。

---

## 6. Verification

- ITERATION-PLAN.md § M0→M1 Gate 重写后，旧"相对 Dify"四条不再出现，新 § 2.1-§ 2.5 各条 1-1 替换
- 所有 § 2.1 SLO 数值都引用 G.1 SLO 文档行号，避免数值漂移
- M.4 大盘 panel + 告警在 K10 G.7 大盘 + G.2 告警上配置完成
- 零债 6 条核验通过（[[zero-tech-debt]]）

---

## 7. 与现有文档的关系

- 替换 `docs/ITERATION-PLAN.md` § "M0→M1 Gate（2-4 周，dogfood 平行运行）" 整段
- 引用并锚定 `docs/streams/STREAM-G-DESIGN.md` § G.1 SLO 列表
- 引用并锚定 `docs/streams/STREAM-J-DESIGN.md` § 18 J.13a baseline
- 引用并锚定 `docs/architecture/08-AGENT-CAPABILITY-ASSESSMENT.md` canonical agent 定义
