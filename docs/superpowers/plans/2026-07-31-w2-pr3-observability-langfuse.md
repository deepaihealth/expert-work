# W2-PR3 观测栈 + Langfuse 上测试集群 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 观测栈(otel-collector/prometheus/tempo/grafana/alertmanager)与 Langfuse 全家(web/worker/ClickHouse)以 K8s 清单进 ACS 测试集群,loki/promtail 由 SLS 替代;顺带根治 otel exporter 噪音、注入前端 Langfuse 深链 env。

**Architecture:** 全部进 `infra/k8s/base/observability/` 与 `infra/k8s/base/langfuse/`,单副本 + 云盘 PVC;配置从 `infra/observability/` 搬 ConfigMap(vendored copy,与 compose 版并存);Langfuse 有状态依赖全外置(RDS 新 database / 平台云 Redis DB 1 / OSS `langfuse/` 前缀),ClickHouse 是唯一集群内有状态件。镜像全走 ACR mirror(后台已排队)。

**Tech Stack:** Kustomize、ACS(ALB Ingress + AlbConfig 泛域名证书)、Langfuse 3.122.0、ClickHouse 24.12、OTel Collector 0.119.0、Prometheus v3.1.0、Tempo 2.7.0、Grafana 11.4.0、Alertmanager v0.28.0。

## Global Constraints

- Langfuse 镜像钉 `3.122.0`(spec 要求 ≥2025-11,Agent Graphs GA)。
- 镜像引用 base 里写 Docker Hub 原名,overlay/test `images:` 重映射 ACR mirror(照 keycloak/searxng 先例)。
- 观测组件全单副本;PVC 各 20Gi(阿里云盘最小规格),storage class 用集群默认(拓扑感知)。
- loki/promtail **不部署**(SLS 替代,ACS 集成);grafana datasources 只配 prometheus + tempo。
- Langfuse 域名 `langfuse-test.deepaihealth.com`(DNS 已加,泛域名证书已覆盖,AlbConfig 不动)。
- OSS:复用平台 bucket,`LANGFUSE_S3_EVENT_UPLOAD_PREFIX=langfuse/`。
- 云 Redis:平台 DB 0,Langfuse DB 1。
- RDS:新 database `langfuse`(runbook 一次性 psql 建库,不进清单)。
- secrets 只碰 example 文件与金库 runbook,真值不入仓。

---

### Task 1: 观测栈 K8s 清单(base/observability/)

**Files:**
- Create: `infra/k8s/base/observability/{otel-collector,prometheus,tempo,grafana,alertmanager}-{deployment,service}.yaml`、`*-pvc.yaml`(prometheus/tempo/grafana)、`kustomization.yaml`(configMapGenerator 收 vendored 配置)
- Create: vendored 配置副本(otel-collector-config/prometheus/tempo/alertmanager/grafana provisioning;scrape 目标改 `control-plane:8000`,sandbox-supervisor 目标注释留 W3;datasources 去 Loki)
- Create: rules/dashboards ConfigMap(源 `tools/observability/rules/*.yml`、`tools/observability/dashboards/*.json` vendored)
- Modify: `infra/k8s/base/kustomization.yaml`(resources 追加)、`infra/k8s/base/configmap.yaml`(`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces`)
- Modify: `infra/k8s/overlays/test/kustomization.yaml`(5 镜像 ACR 重映射)

**验证:** `kubectl kustomize infra/k8s/overlays/test` 渲染通过;镜像引用全部落 ACR。

### Task 2: Langfuse K8s 清单(base/langfuse/)

**Files:**
- Create: `infra/k8s/base/langfuse/{clickhouse-statefulset,clickhouse-service,web-deployment,web-service,worker-deployment}.yaml`
- Modify: `infra/k8s/base/ingress.yaml` + `overlays/test/ingress-patch.yaml`(`langfuse-test.deepaihealth.com` host 规则 → langfuse-web:3000)
- Modify: `infra/k8s/base/secrets.example.yaml` + `overlays/test/secrets.env.example`(SALT/ENCRYPTION_KEY/NEXTAUTH_SECRET/CLICKHOUSE_PASSWORD/INIT keys/DATABASE_URL/REDIS/OSS AK)
- Modify: `infra/k8s/base/configmap.yaml`(`EXPERT_WORK_LANGFUSE_HOST=http://langfuse-web:3000`)
- Modify: `infra/k8s/overlays/test/kustomization.yaml`(langfuse/clickhouse 镜像重映射)

**要点:** worker 与 web 共享 env(compose `&langfuse-backend-env` 同构);`NEXTAUTH_URL=https://langfuse-test.deepaihealth.com`;S3 endpoint 走 OSS 内网 + virtual-hosted style + `PREFIX=langfuse/`;`CLICKHOUSE_CLUSTER_ENABLED=false`。

**验证:** kustomize 渲染通过。

### Task 3: otel exporter 噪音根治(tracing.py)

**Files:**
- Modify: `packages/expert-work-common/src/expert_work/common/observability/tracing.py`
- Test: `packages/expert-work-common/tests/test_observability_tracing.py`

**修法:** `init_tracing` 仅当 `otlp_endpoint` 参数或 `$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 显式给出时才装 OTLP exporter;都未配置 → 不装 processor(span 仍可建,零导出零重试)。本地 compose 与 K8s 均显式配 endpoint,行为不变;CI/裸跑不再刷 `Transient error ... retrying`(#1077 flake 噪音源)。

**TDD:** 先写测试:①endpoint 未配置 → provider 无 OTLP processor;②显式传参 → 有;③env 配置 → 有。RED → 实现 → GREEN → 全文件测试过。

### Task 4: 前端深链 env + 构建接线

**Files:**
- Modify: `tools/deploy/build-push.sh`(admin-ui build args 增 `VITE_LANGFUSE_BASE_URL` 透传)
- Modify: docs 两处(admin-ui env 惯例:getting-started/oidc-keycloak 文档块)——`VITE_LANGFUSE_BASE_URL=https://langfuse-test.deepaihealth.com`
- 深链本体已存在(`buildLangfuseTraceUrl` + TurnCard),不动;graph 视图参数(`?display=graph` 类)部署后对着真 Langfuse 3.122.0 验证,可用再补小 PR

**验证:** 逃生舱构建命令带新 env 后镜像内 bundle 含该 URL。

### Task 5: 部署 runbook + 冒烟(PR 合并后运维执行,不进 PR 代码)

1. mirror 完成确认(9 镜像在 ACR)
2. RDS 建库:`CREATE DATABASE langfuse` + 专用 user(psql 经跳板/pod)
3. secrets.env 金库补真值 → `kubectl apply -k overlays/test`
4. 冒烟:langfuse-test 域名开页登录 → 跑一条 run → trace 到达 Langfuse(Agent Graphs 出图)→ prometheus targets up → grafana port-forward 看板有数
5. **记忆召回 5.7s 拆解**:抓真 trace 看 recall 子 span(resolve_mode/embed/retrieve/rerank)哪个是大头,结论回填 progress
6. admin-ui 重建(带 VITE_LANGFUSE_BASE_URL)发测试环境 + newTag PR

---

## Self-Review 结论

- spec 三.4/三.5/版本注全覆盖;SLS 项 = 不部署 loki/promtail 即完成本期承诺(ACS 侧采集配置属控制台操作,runbook 提示)
- Task 1/2 同 PR 同分支顺序做(共享 kustomization/ingress/secrets 文件,不并行)
- Task 3 独立(python),Task 4 独立(脚本+docs),可与 1/2 并行
