# 阿里云分布式部署方案(2026-07-28)

## Context

平台现状 = 单机 docker compose 蓝绿部署,进程内状态多处单机假设。用户拍板:上线天然多副本+弹性扩容,存储用阿里云托管,不接受线上救火。五道选型题已收口(ACS 双集群杭州 / Agent Sandbox+E2B / OSS+NAS / RDS PG16 / Redis 社区版 7.0),审计四波清单在 `docs/research/2026-07-28-multi-replica-readiness-audit.md`,选型分析在 `docs/research/2026-07-28-{agent-sandbox,storage,rds-redis}-selection.md`。

**实施铁律(用户拍板)**:先测试环境配置,所需参数逐项问用户;生产环境同构配置全部用占位符(`PROD_PLACEHOLDER_*`),测试环境验收通过后再填真值配生产。

## 一、目标形态

三环境:
- **本地开发**:全本地 compose(PG16/redis:7/MinIO 版本与云钉死),逃生舱 env 切阿里云测试实例。compose 保留,不废。
- **测试**:ACS 测试集群(杭州)+ 阿里云测试存储实例。与生产同构(同一套 K8s 清单,overlay 差异)。
- **生产**:ACS 生产集群 + 生产存储实例。测试验收后由占位符填真值。

发布 = 一键脚本:`build → push ACR → migrate Job → 滚动更新 → 冒烟`;版本 = git tag = 镜像 tag;回滚 = 一条命令切回旧 tag。

## 二、服务映射表(compose → 云)

| compose 服务 | 云上形态 | 说明 |
|---|---|---|
| control-plane-blue/green | ACS Deployment(≥2 副本)+ 滚动更新 | 蓝绿模式退役,换 K8s 原生滚动 |
| credential-proxy | ACS Deployment | 沙箱 egress 依赖;Agent Sandbox 迁移后去留在 W3 裁决 |
| sandbox-supervisor | **W3 退役**,由 AgentSandboxClient(进程内适配层)替代 | 云上无 docker.sock,旧架构不上云 |
| migrate | K8s Job(发布脚本驱动) | 直连 RDS 5432 |
| nginx + admin-ui 静态 | ACS Deployment(nginx 托静态)| 与现状同构;OSS 静态托管作后续可选 |
| searxng | ACS Deployment(单副本) | 搜索工具依赖 |
| keycloak | ACS Deployment + RDS 内独立 database | 版本钉现 compose 版本 |
| postgres + pgbouncer | **RDS PG16**(内置 PgBouncer 6432 + 直连 5432) | 双 DSN 同构照搬 |
| redis | **云 Redis 社区版 7.0** | 生产 noeviction |
| minio | **OSS**(S3 兼容 endpoint) | virtual-hosted style;本地仍 MinIO |
| otel-collector/prometheus/tempo/loki/promtail/grafana/alertmanager | 见「三、观测栈」 | |
| langfuse 全家(web/worker/pg/clickhouse/redis) | 见「三、Langfuse」 | |
| mock-upstream | 不上云(e2e 本地专用) | |

## 三、方案级技术选择(spec 内定,用户 review 时可推翻)

1. **K8s 清单 = Kustomize**(base + overlays/test + overlays/prod)。理由:两环境差异纯参数化,无需 Helm 模板语言;`kubectl apply -k` 原生;一人团队心智负担最小。prod overlay 全占位符。
2. **镜像仓库 = ACR 个人版**(免费,限流对我们规模无感);不够再升企业版。
3. **发布 = 仓库内脚本** `scripts/deploy/`(bash):`release.sh <env> --tag vX.Y.Z` 与 `rollback.sh <env> <tag>`。CI 自动化(GHA→ACR 跨境慢)后置,不在本期。
4. **观测栈 = 最小自建搬迁**:prometheus + grafana + otel-collector + alertmanager 以单副本 Deployment + 云盘 PVC 进 ACS(现有 dashboards/rules JSON 直接复用);promtail/loki 由 **SLS(ACS 默认集成)替代**,tempo 保留单副本(trace 后端)。全托管 ARMS 方案作为后续可选(迁移 dashboards 成本换取零运维)。
5. **Langfuse = 测试环境先自建搬迁**(web/worker + 专属 PG(用 RDS 新 database)+ ClickHouse 单副本 PVC + 复用平台云 Redis 的独立 DB 号 + OSS bucket)。ClickHouse 是最重的有状态组件,若运行痛,后续再评估阿里云托管 ClickHouse 或 Langfuse Cloud。
   **版本要求(2026-07-31 补,轨迹可视化调研定案)**:镜像选 ≥2025-11 发行版——「Agent Graphs」该版 GA(2026-07 起 Aggregated/Expanded 双模式),LangGraph 集成自动出执行图,是调试台 Gantt(#1073)之外的 graph 短线,零自研。部署后顺手:调试台 exact 视图加「图视图」深链跳该 trace 的 Langfuse graph 页(小前端项,随 PR-3 或紧随其后)。
6. **admin-ui 入口 = ALB Ingress**(升级版实例,工单提 idle/request timeout 3600s);SandboxGateway 泛域名走同一 ALB。**域名与 ICP 备案是前置硬项**(国内公网 80/443 强制备案)——参数清单第一批问。

## 四、波次计划

依赖关系:W0 与 W1 并行;W2 依赖 W0 的集群就绪;W3 依赖 W0 的工作区定案 + W2 的集群部署面;W4 依赖 W2;W5 依赖全部。

### W0:PoC 验证包(测试集群,1 个短 PR:PoC 脚本+结论记录)
1. ACS 测试集群就绪(用户已购,核对与 RDS 测试实例**同 VPC**)。
2. 实测创建 agent-sandbox 算力类型沙箱(坐实售后"杭州支持")→ E2B SDK 打通 create/run_code/files/pause/kill。
3. e2b 注解挂 NAS PV 实测 → **工作区方案定案**(通=NAS;不通=OSS 直挂+三场景性能实测)。
4. OSS S3 兼容 PoC:现有 storage 层 5 操作(get/put/delete/list/presigned)+ virtual-hosted style 真桶打通。
5. 产出:`docs/research/` PoC 结论追记;若有意外(如规格规整、配额),回填选型文档。

### W1:多副本正确性代码波(纯本地,与云无关,3-4 个 PR,老 SDD 流程)
- 审计第 0 波的真守卫(single_instance 启动守卫)+ 第 1 波 11 项:webhook 投递 CAS / MemoryDLQWorker claim / Consolidator+SkillCurator advisory lock+env 开关 / apikey 限流 HMAC 盐共享 / Redis fail-open 策略 / PENDING run 回收 / TOCTOU×2 / OrphanSweep 守卫 / 文档上传改对象存储 / store setup 竞态重试。
- 全部有 testcontainers 集成测覆盖,不依赖云环境。

### W2:部署工程——测试环境(2-3 个 PR)
1. `infra/k8s/` Kustomize 树:base(全部 Deployment/Service/Ingress/ConfigMap/Job)+ overlays/test(真值,参数问用户)+ overlays/prod(占位符)。
2. 第 0 波配置落地:`EXPERT_WORK_SINGLE_INSTANCE=false` + `QUOTA_REDIS_URL` + `CHECKPOINTER_BACKEND=postgres`(直连 5432)+ 五服务对象存储 `s3-compatible` + secrets(K8s Secret)。
3. ACR 接入 + 全镜像构建脚本(含镜像缓存配置,Agent Sandbox 镜像加速)。
4. 发布/回滚脚本 + migrate Job + 冒烟脚本(登录/建 run/SSE 心跳)。
5. Keycloak/searxng/admin-ui/观测栈/Langfuse 按「三」的选择部署(credential-proxy 现用途=沙箱 egress 代理,沙箱 W3 才迁 → W2 暂不部署,W3 随 egress 方案裁决去留)。
6. ALB Ingress + 超时配额工单 + 域名/证书(参数问用户)。
7. **验收**:测试集群上核心链路真栈通(登录→建 Agent→run→SSE 流式输出→记忆写读),**沙箱类工具此时允许降级缺位**(W3 补)。多副本(2 副本)下冒烟不串。

### W3:沙箱迁移(2-3 个 PR)
1. `AgentSandboxClient` 实现 `SupervisorClient` Protocol(E2B SDK;exec→commands.run/run_code 语义对齐:timeout/1M chars 截断/bash 包装;seed_files→files.write;热会话→(tenant,user)→sandbox_id 映射表+休眠唤醒;并发 acquire CAS)。
2. 沙箱镜像适配(agent-runtime 兼容 + ACR + 镜像缓存)+ SandboxSet 预热池 + SandboxGateway 缩规格。
3. 工作区按 W0 定案接线(NAS subPath+PVC 配额 或 OSS);control-plane 工作区 API 与沙箱解耦(直读 NAS/OSS);归档/备份子系统退役改 OSS 生命周期规则。
4. egress:TrafficPolicy/SecurityProfile 映射现有 allowlist/denylist;credential-proxy 去留裁决。
5. supervisor 服务退役(compose 本地保留旧路径,云上不部署);spec.sandbox 死字段随手清理。
6. **验收**:exec_python/bash/文件工具/read_document/工作区 API 测试集群全通;休眠→唤醒→继续 exec 通;网络 allowlist 生效。

### W4:多副本深水区(2 个 PR)
1. SSE 跨副本:审计第 2 波 b1(轮询 durable 事件库,PR3 批写已打地基)或 b2(Redis pub/sub)——设计小节后定,倾向 b1 起步(零新基建)。
2. 供应商 RPM 全局化(Redis 令牌桶加 per-provider-key 维度)+ LLM 熔断器评估。
3. **验收**:2 副本下 run 的 `/events` 从任一副本可收完整流;RPM 压测不超配置值。

### W5:生产环境 + 上线闸(1-2 个 PR + 运维操作)
1. 生产实例购买(RDS 高可用 2c4g / Redis HA 1GB+noeviction / OSS 生产 bucket / NAS / ACS 生产集群)——参数真值填 overlays/prod。
2. 发布演练:生产集群空跑部署→回滚→再部署。
3. 上线检查清单:RLS 两道人工闸(rls-project 记忆)、dependabot 高危清理、secrets 轮换、备份策略核对(RDS PITR/Redis 备份/OSS 版本控制)、告警接通知渠道。
4. **验收**:生产核心链路冒烟 + 一键回滚演练通过。

## 五、参数收集清单(第一批,W0/W2 开工前问用户)

| # | 参数 | 用途 | 备注 |
|---|---|---|---|
| 1 | ACS 测试集群 kubeconfig(或集群 ID + 授权方式) | 部署 | 用户已购,需访问凭据 |
| 2 | 集群所在 VPC/vSwitch ID | 核对与 RDS 同 VPC | RDS 截图 VPC=vpc-bp1pcg8olq79tjxoiq53n,**必须同 VPC 或打通** |
| 3 | RDS 测试实例内网地址 + 高权限账号密码 | 双 DSN + 建库(expert_work/keycloak/langfuse) | 已购 2c2g |
| 4 | Redis 测试实例(待购:单节点 256MB~1GB 按量)地址+密码 | 限流/配额 | 购前我给购买参数 |
| 5 | NAS 测试文件系统(待购:通用容量型)+ 挂载点 | 工作区 PoC | 购前我给购买参数 |
| 6 | OSS 测试 bucket 命名 + AK/SK(或 RAM 角色) | 对象存储 + 沙箱工作区挂载 | AccessKey 为 Agent Sandbox 挂载硬要求 |
| 7 | ACR 是否已开通(个人版即可)+ 命名空间 | 镜像 | |
| 8 | **域名 + ICP 备案状态** | ALB 公网入口 + SandboxGateway 泛域名 + 证书 | 无备案域名则测试环境先纯内网/IP 方案 |
| 9 | 现有 .env secrets 平移清单(LLM keys 等) | K8s Secret | 我列 key 名清单,用户填值 |

生产同项全部 `PROD_PLACEHOLDER_*`,W5 时收第二批。

## 六、风险与兜底

- Agent Sandbox 公测风险 → 兜底普通 ACS Pod(W3 适配层留形态开关);组件版本矩阵/E2B SDK 钉版本写入依赖锁。
- NAS 挂载不可行 → OSS 直挂 + 性能实测;AgenticFS 工单结果随时并入。
- ALB 超时配额申请不下来 → SSE 心跳间隔收紧 + 前端重连兜底(已有)。
- ClickHouse/Langfuse 运行痛 → 托管 ClickHouse 或 Langfuse Cloud 评估(不阻塞主线)。
- 测试环境验收前生产零采购(占位符策略天然保证)。

## 七、不做清单(本期明确不做)

- GitHub Actions 云端 CI/CD(本地脚本先行)。
- OSS 静态托管 admin-ui / CDN(nginx 容器先行)。
- K8s HPA 自动扩缩容(先固定副本数,生产跑稳后加)。
- 多地域/容灾(单地域杭州)。
- per-run 预算家族/DelegationGate per-replica 语义变更(审计已裁"不改")。
