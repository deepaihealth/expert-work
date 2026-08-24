# 生产发布 runbook —— 开荒 + 发布 + 回滚

> PROD-5(2026-08-24)。读者:执行生产首发的运维/负责人。测试环境的既有惯例
> (fresh tag / 记录 PR / smoke)原样沿用,本文只写生产差异与一次性开荒。
> K8s 侧唯一事实源是 `infra/k8s/overlays/prod/`;本文是操作顺序,不是配置副本。

## 拍板记录(2026-08-24)

| 决策 | 内容 | 恢复点 |
|---|---|---|
| 单副本首发 | control-plane `replicas: 1`(`overlays/prod/replicas-patch.yaml`),分布式语义(postgres checkpointer / redis quota / sql_encrypted)保持开启 | ROADMAP PROD-1(live SSE 跨副本兜底)做完 → 删该 patch 回 base 2 副本 |
| 书面接受单层租户隔离 | 生产库只有 ORM WHERE 一层;RLS 完全惰性。**应用账号必须建成非 superuser、无 bypassrls**,给日后 FORCE RLS 留门 | 发布后第一波捞回 RLS PR B(ROADMAP PROD-3) |
| 告警走企微群机器人 | P0(+@all)/P1/P2 全进「生产告警」群,经 wecom-adapter(PROD-2) | 通道扩展(邮件/电话)按需另议 |

## 0. 资源开通清单(全部齐了才能进 §1)

| 资源 | 要求 | 产出物 |
|---|---|---|
| ACS prod 集群(杭州) | 装 ack-sandbox-manager 组件(记下 adminApiKey)+ ALB Ingress controller | kubeconfig → `~/.kube/expert-work-prod.yaml` |
| RDS PG16 | 三库三账号:应用库 + `keycloak` + `langfuse`,**全部非 superuser** | host + 三组账号密码 |
| Redis 社区版 7.0 | **实例级 `maxmemory-policy=noeviction`**(部署时 `CONFIG GET` 验证);DB 0=平台 / DB 1=Langfuse | host + 密码 |
| OSS bucket | 平台文档/产物 + `langfuse/` 前缀共 bucket;RAM AK/SK | endpoint / bucket / region / AK/SK |
| 通用型 NAS | 挂载点 + 手工建 `/workspaces` 目录(mount 后 mkdir;回收站开 7 天,照 test 勘误 #1144) | 挂载点域名 |
| 域名 ×3 + 证书 | 主域名 / Langfuse 子域名 / 沙箱网关域名;泛域名证书或逐张 | DNS CNAME → prod ALB;证书上传阿里云 |
| 企微「生产告警」群 | 群机器人 webhook URL | 进 `wecom-alert-webhook` Secret(§1.6) |

## 1. 开荒(一次性,建议发布前一天完成)

### 1.1 本机接线(不进 git)

```sh
# kubeconfig
cp <下载的凭据> ~/.kube/expert-work-prod.yaml
# 域名参数(release.sh / smoke.sh prod 都从这读)
cat > ~/.kube/expert-work-prod-params.env <<'EOF'
PROD_DOMAIN=<主域名>
PROD_LANGFUSE_DOMAIN=<langfuse 子域名>
EOF
```

### 1.2 填 overlay 占位符

`infra/k8s/overlays/prod/` 里 grep `PROD_PLACEHOLDER` 逐个替换(域名 / OSS 三
元组 / NAS 挂载点 / 沙箱网关域名 / 管理员邮箱)。`newTag: PROD_PLACEHOLDER_TAG`
**不用手填** —— 首次 `release.sh prod` 自动钉。填完提交 PR(占位符替换是配置
变更,走评审)。`release.sh prod` 在构建前会拒绝任何残留占位符。

### 1.3 集群侧一次性对象

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
# AlbConfig 监听(prod 变体):照 infra/k8s/cluster/albconfig-listeners-patch.yaml
# 的头注新建 prod 文件(prod 有自己的 ALB 实例与证书 id,勿复用 test 的),
# kubectl patch albconfig alb --type merge --patch-file <prod 文件>
# SandboxSet(namespace 语义见文件头注,by hand,不进 kustomize):
kubectl apply -f infra/k8s/sandbox/sandboxset.yaml
```

### 1.4 Secrets(五连 + 企微)

按 `infra/k8s/overlays/prod/secrets.env.example` 填一份本地副本(**绝不提交**),
按其头注 grep 前缀切五个 Secret 创建;企微 URL 单独:

```sh
kubectl create secret generic wecom-alert-webhook -n expert-work \
  --from-literal=WECOM_WEBHOOK_URL='<群机器人 URL>'
```

所有随机密钥(`SECRET_ENCRYPTION_KEY` 等)生产**新铸**,绝不复用 test 值。

### 1.5 首次发布

```sh
tools/deploy/release.sh prod        # 交互确认输入 'prod'
```

= build 双镜像(admin-ui 烤 prod OIDC)→ 钉 newTag → migrate(空库全量)→
apply → rollout → smoke。smoke 的公网检查在 DNS 生效前会红,先看
`/healthz/ready` 与 pods 两项。

### 1.6 应用层 seed(顺序敏感)

1. **Keycloak realm 三件**(漏了登录/首装直接失败,见 deployment.md §6.7):
   - kcadm 给 `expert-work-admin-ui` client 加 `https://<主域名>/*` redirectUris + webOrigins(realm 文件 seed 的是 localhost);
   - kcadm `update users/profile -r expert-work -s unmanagedAttributePolicy=ENABLED`;
   - **轮换 realm 内嵌的 dev 秘密**(base realm 文件带 dev client secret 与 dev 用户密码,base/kustomization.yaml 头注的硬警告):重置 `expert-work-api-internal` client secret、删 dev 用户。
2. **金库 seed**(sql_encrypted 启动即解析 OSS ref,空库会 CrashLoop):照
   `overlays/prod/secrets.env.example` 金库节 → KC admin-client secret
   (`python -m control_plane.seed_keycloak_secret`)+ OSS AK/SK。
3. **首个平台管理员**:configmap 已设 `EXPERT_WORK_BOOTSTRAP_ADMIN_EMAIL`,
   该邮箱首登自动升(兜底走 bootstrap-admin.md break-glass)。
4. **平台技能导入**:`POST /v1/platform/skills/import`(幂等,52 包,凭证在
   金库;见 skill-packaging.md)。
5. **租户开通 + LLM key**:admin-ui 建租户 → 金库粘贴 LLM provider key。

### 1.7 金丝雀(发布合格判据)

照 `canonical-agent-e2e-test.md` 手动跑一条真 run:exec_python + write_file +
save_artifact + 产物下载,断言 `end.status=success`。**红 = 不对外开放**。
(自动化进 release.sh 是 ROADMAP PROD-7 / X-14 P1,首发手动。)

## 2. 日常发布

```sh
tools/deploy/release.sh prod            # 确认 'prod';或 --yes 走脚本
```

与 test 同惯例:fresh tag、newTag 变更提交 `chore(deploy)` 记录 PR(**记录里
写上一版 tag** —— X-14 P5,回滚一键可查)、smoke 全绿 + §1.7 金丝雀后才算完。
发布窗口:migrate 是 expand-only 约定(向后兼容一版,deployment.md §10)。

## 3. 回滚

```sh
tools/deploy/rollback.sh prod <上一版 tag>     # 秒级 set image,无确认门
```

带破坏性迁移的版本回滚需要人(rollback.sh 头注);回滚后同样补记录 PR。

## 4. 已接受风险与延后项(首发)

- 单副本:pod 挂 = 分钟级中断到重新调度;live SSE 跨副本兜底(PROD-1)做完才扩。
- 单层租户隔离(RLS 惰性):发布后第一波。
- 金丝雀未自动化(PROD-7)、触发器投递 CAS(X-3,单副本无风险)、配额维度混扣
  (B-19,给第三方配配额前必修)、供应商 RPM 进程内限流(扩容前必须全局化,PROD-12)。
- retention-cleanup-job / billing-rollup-job / event-log-archive-job /
  audit-backup-worker:**只有代码没有部署物**(infra/k8s 零 CronJob),test 也
  没跑,首发保持一致;retention job 部署前必须先修 X-15①(第二套审批超时)。
