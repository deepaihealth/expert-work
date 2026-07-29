# W2-PR1 K8s 部署地基(OSS 适配 + Kustomize 树 + 镜像工程)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** W2 第 1 批:OSS S3 兼容代码适配(W0 实测的两项)+ `infra/k8s/` Kustomize 树(base+overlays)+ admin-ui 容器化 + 镜像构建推送脚本。纯代码+清单,不真连云(真集群拉起是 PR-2)。

**Architecture:** Kustomize base+overlays(spec 拍板,零 Helm);Ingress 同域分流 `/`→admin-ui 静态、`/v1`→control-plane(避免 CORS,vite 产物零改);Keycloak 上 K8s 换 PG 后端;沙箱系(supervisor/credential-proxy)与四个 job 服务 W2 不部署。侦察依据:2026-07-29 W2 部署面侦察(本会话),关键 file:line 已嵌各 task。

**Tech Stack:** Kustomize(kubectl 内置)/ kubeconform 校验 / bash 脚本 / nginx:1.27-alpine 静态镜像。

## Global Constraints

- 分支 `deploy-w2-pr1-k8s-manifests`(已建,基 main 3d5b1bb2)。
- CI 门:`uv run ruff check .` + `uv run ruff format --check .` + `uv run mypy packages` + control-plane pytest(`-m "not integration"`)+ 涉及 storage 的测试文件单独跑。
- K8s 清单验证门:`kubectl kustomize infra/k8s/overlays/test`(以及 prod)构建通过 + `kubeconform -strict`(本机没有则 `brew install kubeconform`,装不上则 kubectl kustomize 通过即可,报告写明)。
- 镜像命名:`registry.cn-hangzhou.aliyuncs.com/expert-work/<name>:<tag>`;tag=git short sha,附 `latest`(照 sandbox-image.yml L145-146 双 tag 先例)。
- 域名:测试环境 `expert-work-test.deepaihealth.com`(已备案);清单里域名写 overlays/test 的 Ingress patch,不写 base。
- 实施者所有命令前台同步跑完;创建后台任务=任务失败。
- conventional commits,无 attribution。

---

### Task 1: OSS S3 兼容代码适配(virtual addressing + checksum)

背景(W0 实测 + 侦察核实):OSS 拒绝 path-style(SecondLevelDomainForbidden)且不支持 STREAMING-UNSIGNED-PAYLOAD-TRAILER。现状三缺口:①`packages/expert-work-runtime/src/expert_work/runtime/storage/factory.py:84-87` BotoConfig 的 addressing_style 只能 `"path" if config.use_path_style else "auto"`,产不出 `"virtual"`;②全仓零 checksum 配置(botocore 1.43 默认 when_supported,PutObject 带 crc32 会被 OSS 拒);③`services/control-plane/src/control_plane/runtime.py:1525-1531` 构造 `S3CompatibleConfig` 没传 `use_path_style`(恒默认 True=path),settings.py L244-251 也无该字段;`services/retention-cleanup-job` 的 main.py:60-66 同漏。

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/storage/factory.py`(`S3CompatibleConfig` 的 `use_path_style: bool` 改为 `addressing_style: Literal["path","virtual","auto"] = "path"`——保留 `use_path_style` 兼容参数或直接迁移,看调用面小就直接迁;BotoConfig 加 `request_checksum_calculation="when_required"` + `response_checksum_validation="when_required"` 无条件加,对 MinIO/AWS 无害)
- Modify: `services/control-plane/src/control_plane/settings.py`(加 `object_store_addressing_style: Literal["path","virtual","auto"] = "path"`,docstring 写明 OSS 必须 virtual/MinIO 用 path)
- Modify: `services/control-plane/src/control_plane/runtime.py:1525-1531`(`resolve_object_store_config` 传新字段)
- Modify: 其余四服务 settings+装配点对齐(sandbox-supervisor `settings.py:169-175` app.py:248 / audit-backup-worker `settings.py:38-44` main.py:42 / event-log-archive-job `settings.py:33-39` main.py:40 / retention-cleanup-job `settings.py:73-78` main.py:60-66):bool `use_path_style` 统一迁到 addressing_style 三值(env 兼容:保留旧 bool env 读入映射 path/auto,报告写明选择)
- Test: storage factory 既有测试文件加:addressing_style 三值传导进 BotoConfig 断言 + checksum 两参断言;各服务 settings 测试对齐

- [ ] Step 1(RED):新断言跑 FAIL。
- [ ] Step 2(GREEN)实现;Step 3 回归(storage+五服务 settings 相关测试+ruff+mypy packages);Step 4 Commit `fix(storage): S3 兼容层支持 virtual-hosted 寻址+checksum when_required(OSS 适配,W0 实测两缺口)`

### Task 2: admin-ui 容器化

背景(侦察核实):admin-ui 无 Dockerfile;`apps/admin-ui/package.json:9` `tsc -b && vite build` 产 `dist/`;`/v1` 代理只存在于 vite dev server(vite.config.ts:13-22),生产产物内请求走相对路径 `/v1`——K8s 用 Ingress 同域分流,静态镜像不需要反代。`.dockerignore:13` 的 `**/dist/` 会挡住产物进镜像(侦察发现)。

**Files:**
- Create: `apps/admin-ui/Dockerfile`(两阶段:`node:22-alpine` + corepack pnpm → `pnpm install --frozen-lockfile` + `pnpm build`;runtime `nginx:1.27-alpine`,COPY dist → /usr/share/nginx/html,自带 nginx.conf:listen 8080、`try_files $uri /index.html`(SPA 路由)、gzip on、健康检查路径 `/healthz` 返 200)
- Create: `apps/admin-ui/nginx.conf`(上述内容,独立文件 COPY 进镜像)
- Modify: `.dockerignore`(`**/dist/` 行改为不影响 admin-ui 构建的写法——两阶段构建在容器内 build,产物不经 context,实际只需确认 context 传入源码;若 build context 选 apps/admin-ui 则该行无碍,报告写明)
- Test: 本地 `docker build -f apps/admin-ui/Dockerfile <context>` 成功 + `docker run` 后 curl `/healthz` 200 + `/` 返回 index.html(容器起停在前台脚本内完成,结束必删容器)

- [ ] Step 1:Dockerfile+nginx.conf;Step 2 本地 build+run+curl 验证;Step 3 Commit `feat(admin-ui): 容器化(nginx 静态镜像,SPA fallback,/v1 走 Ingress 同域分流)`

### Task 3: Kustomize base

背景(侦察核实):仓内零 K8s 残留,绿地。W2 部署集合=control-plane(2 副本)+ admin-ui + Keycloak + searxng + migrate Job;不部署=sandbox-supervisor/credential-proxy(W3)、minio(→OSS)、pg/redis/pgbouncer(→云实例)、nginx(→Ingress)、观测栈+Langfuse(PR-3)、四 job 服务(不在 compose 无镜像,后置)。

**Files(全新 `infra/k8s/base/`):**
- `kustomization.yaml`(namespace `expert-work`,commonLabels)
- `namespace.yaml`
- `control-plane/deployment.yaml`:replicas 2;image `registry.cn-hangzhou.aliyuncs.com/expert-work/control-plane:latest`(overlay 改 tag);envFrom ConfigMap `control-plane-config` + Secret `control-plane-secrets`;probes 照代码真实端点(侦察核实 api/health.py:55-57):liveness `/healthz/live`、readiness `/healthz/ready`、startup `/healthz/startup`(failureThreshold 30×2s);resources requests 500m/1Gi limits 1/2Gi;lifecycle preStop sleep 5
- `control-plane/service.yaml`(8000)
- `control-plane/migrate-job.yaml`:Job,同镜像,command 覆盖 `["alembic","-c","/app/alembic/alembic.ini","upgrade","head"]`(照 compose L502 原样;镜像内路径 Dockerfile L45-46 已核),env `EXPERT_WORK_DB_URL` 从 Secret;`backoffLimit: 2`;发布脚本先 apply Job 等完成再滚 Deployment(PR-2 接线,base 里先有 Job 清单)
- `admin-ui/deployment.yaml` + `service.yaml`(8080,1 副本,requests 50m/64Mi)
- `keycloak/statefulset.yaml` + `service.yaml`:`quay.io/keycloak/keycloak:25.0`,`start --import-realm`(生产模式非 start-dev);env:`KC_DB=postgres`/`KC_DB_URL`(RDS keycloak 库,Secret)/`KC_HOSTNAME`(overlay patch)/`KC_PROXY_HEADERS=xforwarded`/`KC_HTTP_ENABLED=true`;realm json 挂 ConfigMap(`infra/keycloak/realm-expert-work.json` 由 kustomize configMapGenerator 引入);主题目录 W2 暂不挂(文件多,记 follow-up 用 initContainer 或烧镜像)
- `searxng/deployment.yaml` + `service.yaml`(`searxng/searxng:latest` 钉 digest 或具体 tag,settings.yml 走 configMapGenerator `infra/searxng/settings.yml`)
- `ingress.yaml`:ALB IngressClass `alb`;规则:`/v1` `/healthz` `/metrics`→control-plane-svc:8000,`/auth`(Keycloak 路径前缀,按 KC_HTTP_RELATIVE_PATH=/auth 配)→keycloak:8080,`/`→admin-ui:8080;注解(idle timeout 等)放 overlay
- `configmap.yaml`(control-plane 非敏感 env:照 compose x-control-plane-base L21-150 的 45 个 config 项萃取,值用 test 无关的中性默认,环境差异进 overlay;**必含第 0 波四项**:`EXPERT_WORK_SINGLE_INSTANCE=false`/`EXPERT_WORK_CHECKPOINTER_BACKEND=postgres`/`EXPERT_WORK_OBJECT_STORE_BACKEND=s3-compatible`/`EXPERT_WORK_OBJECT_STORE_ADDRESSING_STYLE=virtual`(T1 新字段);多副本三开关 `ENABLE_SCHEDULER/CURATION_WORKER/REAPER` base 默认 true 留 overlay 决策;`EXPERT_WORK_AUTH_MODE`/`OIDC_ISSUER`/`OIDC_JWKS_URI`/`OIDC_AUDIENCE` 键位留 overlay)
- Secret **不进 base**:`secrets.example.yaml`(全部 secret 键名+占位值,侦察 B.3 表萃取:DB_DSN/CHECKPOINTER_DSN/DB_URL/QUOTA_REDIS_URL(带密码算 secret)/SECRET_ENCRYPTION_KEY/APIKEY_RATE_LIMIT_HMAC_SALT/SETUP_TOKEN/LANGFUSE 两 key/KEYCLOAK admin 密码/KC_DB_URL/OBJECT_STORE AK-SK ref 的真值注入方式)+ README 说明用 `kubectl create secret` 或 SealedSecrets 后续升级

- [ ] Step 1:清单全写;Step 2 `kubectl kustomize infra/k8s/base` 构建通过;Step 3 Commit `feat(infra): K8s Kustomize base——control-plane/admin-ui/keycloak/searxng/migrate Job/Ingress`

### Task 4: overlays/test 真值 + overlays/prod 占位

**Files(`infra/k8s/overlays/test/`):**
- `kustomization.yaml`(bases: ../../base;images 段改 tag;patches)
- `configmap-patch.yaml` 真值:`EXPERT_WORK_ENV=staging`(枚举无 test,用 staging 档)/`AUTH_MODE=prod`/`KEYCLOAK_ENABLED=true`/`OIDC_ISSUER=https://expert-work-test.deepaihealth.com/auth/realms/expert-work`/`OIDC_JWKS_URI=<issuer>/protocol/openid-connect/certs`/`OIDC_AUDIENCE=expert-work-admin-ui`/`QUOTA_REDIS_URL` 键位(值在 Secret)/`OBJECT_STORE_ENDPOINT_URL=https://s3.oss-cn-hangzhou-internal.aliyuncs.com`/`OBJECT_STORE_BUCKET=expert-work-test`/`OBJECT_STORE_REGION=cn-hangzhou`/`LANGFUSE_*` 键位留空禁用(PR-3 接)/`SANDBOX_SUPERVISOR_URL` 空(W3)/`WEB_SEARCH_SEARXNG_BASE_URL=http://searxng:8080`
- `ingress-patch.yaml`:host `expert-work-test.deepaihealth.com`;ALB 注解(alb.ingress.kubernetes.io/listen-ports '[{"HTTPS":443},{"HTTP":80}]'、证书走 alb 注解引用(证书 ID PR-2 真值,此处 `TEST_PLACEHOLDER_CERT_ID`)、idle-timeout 注解键位与工单说明注释)
- `keycloak-patch.yaml`:`KC_HOSTNAME=https://expert-work-test.deepaihealth.com/auth`
- `replicas/资源`小档:control-plane 2 副本维持(多副本验收要求)
- **`secrets.env.example`**:测试环境全部 secret 的 key=占位模板(真值用户填,PR-2 注入;DSN 模板预拼好 RDS 内网地址 `pgm-bp19o30qlb16v6w1.pg.rds.aliyuncs.com:5432` 与 Redis `r-bp123nqe025r0qvka2.redis.rds.aliyuncs.com:6379`,密码位留 `FILL_ME`)
- `infra/k8s/overlays/prod/`:同结构全部 `PROD_PLACEHOLDER_*`(spec 拍板)

- [ ] Step 1:两 overlay 写全;Step 2 `kubectl kustomize` 两个 overlay 均构建通过 + (装得上则)kubeconform;Step 3 Commit `feat(infra): K8s overlays——test 真值(expert-work-test.deepaihealth.com)+ prod 占位`

### Task 5: 镜像构建推送脚本

背景(侦察核实):CI 只 build sandbox 镜像(sandbox-image.yml,ACR 凭据 vars/secrets 已存在);三服务镜像只本地 compose build;`EXPERT_WORK_CONTROL_PLANE_TAG` 是既有 tag 变量;environments/{staging,prod}.yaml registry endpoint 还是 TBD 占位(staging L47-51/prod L59-63)。

**Files:**
- Create: `tools/deploy/build-push.sh`:bash;参数 `--images control-plane,admin-ui`(默认两个)`--tag <tag>`(默认 `git rev-parse --short HEAD`)`--push/--no-push`;`docker build` control-plane(context 仓根,`services/control-plane/Dockerfile`)与 admin-ui(T2 Dockerfile);tag 成 `registry.cn-hangzhou.aliyuncs.com/expert-work/<name>:<tag>` + `:latest` 双 tag;push 前 `docker login` 检查(未登录给出 `docker login registry.cn-hangzhou.aliyuncs.com` 提示退出);`set -euo pipefail`
- Modify: `environments/staging.yaml` registry 段 endpoint 回填 `registry.cn-hangzhou.aliyuncs.com`(staging=测试环境用;prod 保持 TBD→改 `PROD_PLACEHOLDER`)
- Test: `bash tools/deploy/build-push.sh --no-push --images admin-ui`(admin-ui 构建快)真跑成功;control-plane 镜像 build 真跑一次(慢,允许只在报告记录耗时);shellcheck 脚本(本机有则跑)

- [ ] Step 1:脚本+回填;Step 2 真跑验证;Step 3 Commit `feat(deploy): 镜像构建推送脚本(ACR expert-work 命名空间,sha+latest 双 tag)`

---

## 验证(整 PR)

- T1 测试全绿(storage+五服务);T2/T5 镜像本地真 build 通过;T3/T4 `kubectl kustomize` 三处(base/test/prod)全过。
- CI 门全过。
- follow-up 池:Keycloak 主题挂载(initContainer/烧镜像)/四 job 服务镜像化+CronJob(W5 前)/searxng latest 钉版/SealedSecrets 或 KMS CSI(W5 生产闸)/STREAM-I §8 载体"ECS+compose"与 K8s 现实冲突待改写(PR-2 部署跑通后一并更新 deployment.md)。
