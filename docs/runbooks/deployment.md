# Expert Work 部署手册

> 单一权威的部署入口:从零起本地栈、首次上线 staging / prod、滚动更新、回滚、验证、运维。
> 深度专题(蓝绿脚本、Postgres、TLS、备份)在对应 runbook,本手册串起全程并链接它们。
>
> 落实 Stream I(发布/回滚)+ Stream ACCT(首装向导)。设计见 [STREAM-I-DESIGN](../streams/STREAM-I-DESIGN.md) / [STREAM-ACCT-DESIGN](../streams/STREAM-ACCT-DESIGN.md)。

## 目录

1. [架构与拓扑](#1-架构与拓扑)
2. [前置准备](#2-前置准备)
3. [配置来源与优先级](#3-配置来源与优先级)
4. [关键环境变量](#4-关键环境变量)
5. [首次部署 — 本地 dev / dogfood](#5-首次部署--本地-dev--dogfood)
6. [首次部署 — staging / prod](#6-首次部署--staging--prod)
7. [创建第一个平台管理员(首装向导)](#7-创建第一个平台管理员首装向导)
8. [更新部署(滚动发布 / 蓝绿)](#8-更新部署滚动发布--蓝绿)
9. [回滚](#9-回滚)
10. [数据库迁移(expand-contract)](#10-数据库迁移expand-contract)
11. [部署后验证](#11-部署后验证)
12. [可观测性栈](#12-可观测性栈)
13. [备份与恢复](#13-备份与恢复)
14. [发布检查清单](#14-发布检查清单)
15. [常见坑](#15-常见坑)

---

## 1. 架构与拓扑

在线栈跑在 `docker compose` 上。control-plane 在 ADR B-6(SQL store)后**无状态**,可蓝绿两色(`control-plane-blue` / `control-plane-green`)并行连同一套 DB,发布/回滚靠切换 nginx upstream。

| 组件 | 角色 | 默认端口 |
|------|------|---------|
| `control-plane` | 后端 API(无状态,蓝绿) | blue 8000 / green 8001 |
| `sandbox-supervisor` | 每会话沙箱生命周期(Docker/gVisor) | 8001(容器内 8000) |
| `credential-proxy` | 出站凭据注入代理 | — |
| `admin-ui` | React 控制台(独立构建/部署) | dev 5173 |
| `postgres` (+`pgbouncer`) | 事件日志 / 状态单一真相源(RLS) | 5432 / 6432 |
| `redis` | 多实例限流 / 队列协调 | 6379 |
| `minio` / OSS | 对象存储(上传 / 快照 / 归档) | 9000 / 9001 |
| `keycloak` | OIDC/JWT IdP | 8080 |
| `nginx` | TLS 终结 + 蓝绿 upstream | 8080 / 8443 |

**Compose profiles**(`infra/docker-compose.yml`):

| profile | 含 | 用途 |
|---------|----|------|
| (默认) | postgres / pgbouncer / redis / minio | 数据层(集成测试、迁移、psql) |
| `full` | migrate / control-plane-blue / green / sandbox-supervisor / credential-proxy | 在线后端栈 |
| `auth` | keycloak | OIDC 登录 |
| `proxy` | nginx | TLS + 蓝绿(staging/prod 必备) |
| `observability` | otel / prometheus / tempo / loki / promtail / grafana / alertmanager / langfuse | 指标 / trace / 日志 |
| `sandbox` | credential-proxy / sandbox-supervisor | 仅沙箱子集 |
| `e2e` | mock-upstream | 端到端测试 |

## 2. 前置准备

| 项 | dev | staging / prod |
|----|-----|----------------|
| 容器运行时 | Docker Desktop / OrbStack / Lima | Linux + Docker(阿里云 ECS) |
| Postgres | compose `postgres` 容器 | 阿里云 RDS(独立实例 / 库) |
| 对象存储 | compose MinIO | 阿里云 OSS bucket |
| 密钥 | `infra/.env`(git-ignored) | 阿里云 KMS Secrets Manager |
| 镜像 | 本地 `docker build` | 阿里云 ACR |
| TLS | 自签(`tools/dev-certs/`) | 真证书 / 内部 CA([tls-certs.md](./tls-certs.md)) |
| 前端工具 | Node + pnpm(admin-ui) | 构建产物 / CDN |
| 一个真模型 key | Anthropic `sk-ant-…`(网页粘贴) | KMS 托管 |

**沙箱镜像须预构建**:`sandbox-supervisor` 运行期 `docker run` 沙箱镜像,compose 不替它构建。部署前 `docker build`(office / minimal 两镜像,build context 不同 —— 见 [sandbox.md](./sandbox.md))。

## 3. 配置来源与优先级

三处配置源,由结构化到敏感:

1. **`environments/<env>.yaml`** —— 结构化非密配置(DB host、OSS endpoint、observability、TLS 路径、`secrets.backend` 选型)。声明式,由 TLS 校验器与 SecretStore 工厂消费。
2. **`EXPERT_WORK_*` 环境变量** —— control-plane 的 pydantic `Settings` **只**读真实环境变量(不读 yaml)。compose / 部署脚本注入。
3. **阿里云 KMS Secrets Manager** —— 运行期密钥(模型 key、DB 密码、上游凭据),staging/prod 经 SecretStore(ADR-0007)拉取,永不落盘。dev 用 `local_dev` 占位 / `infra/.env`。

> **环境隔离(P0)**:三环境 DB / 密钥 / bucket 完全分离,命名带环境后缀(`expert_work_dev` / `_staging` / `_prod`)防误连;prod 凭据只存 KMS。

## 4. 关键环境变量

完整模板见 [`infra/.env.example`](../../infra/.env.example);权威定义见 `services/control-plane/src/control_plane/settings.py`(`EXPERT_WORK_` 前缀)。部署关键项:

| 变量 | 说明 | dev | staging / prod |
|------|------|-----|----------------|
| `EXPERT_WORK_DB_DSN` | Postgres DSN | pgbouncer 容器 | RDS endpoint |
| `EXPERT_WORK_STORE_BACKEND` | 存储后端 | `sql` | `sql` |
| `EXPERT_WORK_SINGLE_INSTANCE` | 是否单实例(关蓝绿多进程协调) | `true` | `false` |
| `EXPERT_WORK_QUOTA_REDIS_URL` | 多实例限流的 Redis | — | 必填(蓝绿) |
| `EXPERT_WORK_OIDC_ISSUER` | Keycloak realm issuer | localhost realm | 真 issuer |
| `EXPERT_WORK_KEYCLOAK_ENABLED` | 真 Keycloak Admin API | `true` | `true` |
| `EXPERT_WORK_SECRET_ENCRYPTION_KEY` | 平台密钥库 KEK | 固定占位 | KMS(换 KEK 旧密文永久不可解) |
| `EXPERT_WORK_SETUP_TOKEN` | **首装向导一次性 token**(见 §7) | 可选 | **必填**,首跑后清除 |
| `EXPERT_WORK_BOOTSTRAP_ADMIN_EMAIL` | 邮箱首登自动升 system_admin(可选替代向导) | — | 可选 |

## 5. 首次部署 — 本地 dev / dogfood

完整「一家公司从零用起来」闭环(建公司 → 配 key → 邀员工 → 用 agent)见 [getting-started.md](./getting-started.md)。最短路径:

```sh
# 1. 配 infra/.env(从 .env.example 复制;git-ignored)
cd infra && cp .env.example .env   # 填 Anthropic key 等

# 2. 一键起全量 dev 栈(full + auth + observability;自动迁移)
make dev-up

# 3. 前端单独起(host 上)
cd ../apps/admin-ui && pnpm install && pnpm dev   # http://localhost:5173
```

`make dev-up` 会:构建 control-plane 镜像 → 起数据层/后端/Keycloak/可观测 → 跑迁移 → 打印地址(`make dev-info`)。**与生产一致,不自动建管理员** —— 第一个 `system_admin` 走 `/setup` 向导(见 §7),dev 默认 Setup Token = `dev-setup-token`(可在 `infra/.env` 覆盖)。想跳过向导用快路径 `make dev-bootstrap-admin`(提权 dev 用户)。

**地址**:admin-ui `:5173`(SSO)· control-plane `:8000` · Keycloak `:8080`(admin/admin_dev)· Langfuse `:3001` · MinIO `:9001` · Grafana `:3000`。

常用:`make dev-ps` / `make dev-logs SVC=control-plane-blue` / `make dev-down`(留卷)/ `make dev-clean`。

## 6. 首次部署 — staging / prod

1. 备好 `environments/<env>.yaml` + 注入 `EXPERT_WORK_*` 环境变量(DSN 指向 RDS;`STORE_BACKEND=sql`;`SINGLE_INSTANCE=false` + `QUOTA_REDIS_URL`;`SETUP_TOKEN`)。
2. 从阿里云 ACR 拉 `expert-work-control-plane` / `expert-work-sandbox-supervisor` / `expert-work-credential-proxy` 镜像;预构建沙箱镜像。
3. 起数据层:staging/prod 的 Postgres 是 RDS(**不**起 compose 的 `postgres` / `pgbouncer`);redis 仍由 compose 起。
4. 跑迁移:`docker compose run --rm migrate`(= `alembic upgrade head`,纯 expand,见 §10)。
5. 蓝绿起 control-plane:首次直接 `docker compose --profile full --profile proxy up -d control-plane-blue nginx`;之后版本走 `deploy.py`(§8)。
6. 起 `sandbox-supervisor` / `credential-proxy`。
7. **Keycloak realm 前置**(两项,漏了首装向导会失败):
   - **provision admin client secret**:首装向导/邀请流建真账号时,后端要从密钥库取该 secret 才能调 KC Admin API。**漏 → /setup 502 `KEYCLOAK_ADMIN_SECRET_MISSING`**。prod 把真实 KC confidential-client secret 放进 KMS,名 `expert-work/platform/keycloak/admin-client-secret`(对应 `settings.keycloak_admin_secret_name`);也可 `python -m control_plane.seed_keycloak_secret --value <secret>` 写入当前金库。
   - **开 realm `unmanagedAttributePolicy=ENABLED`**:否则 KC 25 默认丢弃 Admin API 建用户的自定义属性 `tenant_id` → 该用户 JWT 缺 tenant_id → **登录后 /v1/me 401、前端跳回 /login**。realm import **不**还原 user-profile,须用 kcadm 后置设(dev `make dev-keycloak-config` 已封装):
     ```sh
     kcadm.sh update users/profile -r expert-work -s unmanagedAttributePolicy=ENABLED
     ```
8. **创建第一个平台管理员**:见 §7。
9. 验证:见 §11。

## 7. 创建第一个平台管理员(首装向导)

全新部署 `role_binding` 表为空 —— 没人能经 API 授第一个 `system_admin`(鸡蛋问题)。三条路径,**推荐首装向导**(运维不碰 Keycloak 控制台):

> **前置**:向导会建真 Keycloak 账号,需金库里有 KC admin client secret(§6 步骤 7)。漏了向导返 502 `KEYCLOAK_ADMIN_SECRET_MISSING`。dev `make dev-up` 已自动 seed。

### 路径 A — 首装向导(推荐,Stream ACCT)

1. 部署时设 env `EXPERT_WORK_SETUP_TOKEN=<随机串>`。生成:
   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 或 openssl rand -base64 32
   ```
2. 打开 admin-ui → 未初始化会自动进 `/setup` 向导。
3. 填:平台名 / 管理员邮箱 / 密码 / 把上面的 token 粘进 Setup Token → 提交。
4. 后端建专用「平台」租户 + Keycloak 账号(已验证 + 该密码)+ `system_admin` 绑定。
5. 跳登录,用刚设的密码登入 = 平台管理员。**首跑后清除 `SETUP_TOKEN` env**(零-admin 门控也会让端点此后永久 409)。

> 安全:`/v1/setup` 无需认证,靠 **SETUP_TOKEN(防部署窗口劫持)+ 零-admin 不变量(一次性)** 双重门控。未配 token 则端点直接拒。

### 路径 B — 邮箱首登自动升

设 `EXPERT_WORK_BOOTSTRAP_ADMIN_EMAIL=<运维邮箱>`(前提:该邮箱用户已在 Keycloak 存在且 email 已验证)。其首次登录、系统零 admin 时自动升 `system_admin`。

### 路径 C — break-glass CLI

env 不便 / 向导不可用时:在 control-plane 容器内跑

```sh
docker compose exec control-plane-blue python -m control_plane.bootstrap_admin --subject-id <keycloak-user-uuid>
```

`--subject-id` 是 Keycloak 用户的 `sub`(UUID,非 email)。幂等。详见 [bootstrap-admin.md](./bootstrap-admin.md)。

> 之后所有授权走审计化的 `POST /v1/role_bindings` 或 admin-ui「平台管理员」页;租户成员用邀请流。

## 8. 更新部署(滚动发布 / 蓝绿)

```sh
python tools/deploy/deploy.py --tag <new-image-tag>
python tools/deploy/deploy.py --tag <new-image-tag> --canary 10,50 --canary-pause 60
```

`deploy.py` 步骤:重建 idle 色(新 tag)→ 等其 `/healthz/ready` → 可选加权金丝雀步进 → 翻 nginx upstream + `nginx -s reload` → drain 旧色(**停止但保留容器**,供回滚快路径)。

**含 schema 变更**:先 `docker compose run --rm migrate`(只 expand,§10)再 `deploy.py`。

## 9. 回滚

```sh
python tools/deploy/rollback.py                  # 快路径
python tools/deploy/rollback.py --to-tag v1.2.2  # 兜底路径
```

- **快路径**:上次 `deploy.py` 把旧色容器停止保留;`rollback.py` 重启它 → 等就绪 → 切回 nginx → drain 坏色。秒级,不拉镜像。
- **兜底(`--to-tag`)**:旧色已被顶掉或要回更早版本;按 tag 跑一次完整蓝绿。

回滚安全的前提是迁移兼容纪律(§10):旧镜像必须能跑在**当前** schema 上。

## 10. 数据库迁移(expand-contract)

迁移**只向前**,不用 `alembic downgrade`:

- **expand(随发布走)**:加列(nullable/带 default)、加表、加索引(`CREATE INDEX CONCURRENTLY`)、加宽类型 —— 对旧代码恒兼容。
- **contract(单独滞后发布)**:删列、改名、加 `NOT NULL`、窄化 —— 只在「依赖旧 shape 的代码已全部下线」后的单独发布里做。
- **推论**:任一发布 v(n) 的 schema 对 v(n−1) 代码恒兼容 → 蓝绿回滚永远安全。
- 不用 `alembic downgrade`(生产降级可能丢数据、降级脚本几乎不被测)。

命令:`docker compose run --rm migrate` 或容器内 `alembic upgrade head`。alembic revision id ≤ 32 字符(超长仅真 Postgres 报错)。

**例外 —— 加索引迁移(0141/0142/0143)未用 `CONCURRENTLY`**:alembic 迁移跑在一个事务里,而 `CREATE INDEX CONCURRENTLY` 不允许在事务块内执行,所以这几条迁移都是普通(带锁的)`CREATE INDEX`,建索引期间会短暂持有写锁。多数受影响的表写少读多,窗口无感;`0143`(`sandbox_egress_audit_scan_idx`,建在 `sandbox_egress_audit` 表上)例外——这张表的写在沙箱出网请求路径上是 inline await 的(`credential-proxy` 的 `EgressProxy._record`,`services/credential-proxy/src/credential_proxy/egress_proxy.py`),建索引期间会短暂卡住沙箱出网请求。**升级到 `0143`(或更新版本)时挑低峰执行**,或提前评估当时的出网请求量。

## 11. 部署后验证

- [ ] `curl -sf http://<host>:8000/healthz/ready` 返回 `status: ready`(所有依赖探针绿)。
- [ ] 经 nginx `:8443` mTLS 可达(staging/prod)。
- [ ] 第一个 `system_admin` 能登录(§7)。
- [ ] Grafana SLO 大盘([slo.md](./slo.md))有数据;Langfuse 有 trace。
- [ ] 起一个 canonical agent 跑通最小对话(Gate 验收见 [canonical-agent-e2e-test.md](./canonical-agent-e2e-test.md))。

## 12. 可观测性栈

`--profile observability` 起:Prometheus(`:9090`)+ Grafana(`:3000`)+ Tempo(trace)+ Loki/Promtail(日志)+ Alertmanager(`:9093`)+ Langfuse(`:3001`,LLM trace)。指标真值源见 [observability A.8 备注](./slo.md);跨服务 trace 只在信任内部 hop 注入。SLO / 大盘见 [slo.md](./slo.md),Langfuse 见 [langfuse.md](./langfuse.md)。

## 13. 备份与恢复

- **Postgres**:[postgres.md](./postgres.md) / [pg-restore.md](./pg-restore.md)。
- **审计日志**:[audit-restore.md](./audit-restore.md)。
- **持久卷(用户文件 / agent 产物)**:[volume-restore.md](./volume-restore.md);落盘加密依赖宿主/云盘(见 §15 与下表)。
- **对象存储归档**:OSS SSE-KMS(同 ADR-0007 key)。

落盘加密强约束:

| 环境 | 责任方 | 强约束 |
|------|--------|--------|
| 阿里云 ECS | ECS 数据盘加密 + 同 region KMS | `DescribeDisks` 返 `Encrypted:true` + `KMSKeyId` 非空;`/var/lib/docker` 落在该加密盘 |
| 自托管 Linux | LUKS/dm-crypt | `cryptsetup status` = active |
| macOS dev | FileVault | `fdesetup status` = On |

## 14. 发布检查清单

- [ ] 新镜像对应提交 CI 全绿。
- [ ] 本次迁移**纯 expand**?含 contract 则确认依赖旧 shape 的代码已下线 ≥1 发布。
- [ ] `environments/<env>.yaml` 与目标环境已核对。
- [ ] 金丝雀阶段已观察 [SLO 大盘](./slo.md),错误率/延迟无异常再步进。
- [ ] 发布后旧色容器仍保留(`docker compose ps` 见 `exited`),回滚快路径可用。
- [ ] 回滚预案明确:快路径还是 `--to-tag`,旧 tag 已记录。
- [ ] prod:`SETUP_TOKEN` 已在首装后清除;密钥只在 KMS。

## 15. 常见坑

- **dev redis 占宿主 6379** —— 宿主已跑 redis 时集成测试用 `EXPERT_WORK_TEST_COMPOSE_OVERRIDE` 指向 `redis: {ports: !reset []}` override。
- **staging/prod 必须 `SINGLE_INSTANCE=false` + `QUOTA_REDIS_URL`** —— 否则蓝绿窗口内两色各算一份限流。
- **green 与 sandbox-supervisor 都占 8001** —— 预存 latent 冲突;`make dev-up` 排除 green。full profile 同起需改端口(并同步断言测试)。
- **改了 realm 的 loginTheme 不生效** —— `--import-realm` 仅首 boot 导入;重建 keycloak 容器才重导(改 CSS 则 start-dev 热加载)。
- **沙箱镜像没预构建** —— supervisor `docker run` 找不到镜像;部署前先 build。
- **prod 密钥落 `.env` / 进仓库** —— 禁;只走 KMS。
- **登录成功却跳回 /login(/v1/me 401)** —— 多半是 realm 没开 `unmanagedAttributePolicy=ENABLED`,Admin API 建的用户丢了 `tenant_id` 属性 → JWT 缺 tenant_id 被后端拒。见 §6 步骤 7;dev `make dev-keycloak-config` 修。(admin-ui 用 **id_token** 作 Bearer,其 aud=client_id 已在 allowlist,故 audience 一般不是病根。)
