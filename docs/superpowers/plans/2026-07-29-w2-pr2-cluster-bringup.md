# W2-PR2 真集群拉起(测试环境)Runbook

> 操作型 runbook,非代码 SDD——对真集群的有序部署操作,inline 执行。
> 代码/清单改动最小化(假注解修正 + 部署脚本微调),大头是 kubectl/kcadm 操作。
> 参数档案:`~/.kube/expert-work-test-params.env`;kubeconfig:`~/.kube/expert-work-test.yaml`。

**Goal:** expert-work 平台(control-plane + admin-ui + Keycloak + searxng)在 ACS 测试集群
`expert-work-test` 上真实跑起来,经 `https://expert-work-test.deepaihealth.com` 全链可用。

## 部署时定案(侦察结论,2026-07-29)

1. **ALB 共享实例已存在**:沙箱组件安装时建的 `alb-cv3xokwot6r9bgojzx`
   (公网 Fixed,DNS `alb-cv3xokwot6r9bgojzx.cn-hangzhou.alb.aliyuncsslb.com`),
   sandbox-system 的 sandbox-manager Ingress 已挂 80。我们的 Ingress 共挂同一 ALB,host 分流。
2. **`cert-id` / `idle-timeout` Ingress 注解不存在**(查官方 ALB Ingress 配置词典定案,
   PR-1 终审预判成立)。证书与超时都是 **AlbConfig listener 字段**:
   `certificates.CertificateId=25878622-cn-hangzhou` + `idleTimeout: 60` / `requestTimeout: 180`
   (无工单上限)。期望态:`infra/k8s/cluster/albconfig-listeners-patch.yaml`
   (merge patch 全量列 listener,含沙箱的 80)。
3. **SSE 长流风险**:requestTimeout 180s 是否砍长流待冒烟实测;被砍则提工单抬监听上限(spec 3600 目标)。
4. Keycloak realm client=`expert-work-admin-ui`(public),aud mapper → `expert-work-api-internal`;
   vendored realm 回调只有 localhost——**部署后 kcadm 追加** `https://expert-work-test.deepaihealth.com/*`
   (不改 vendored json,per-env 追加)。

## Phase A — 清单/镜像

- [x] A1 假注解修正 + AlbConfig patch 文件入仓(commit 已做)
- [x] A2 镜像双推(main 树):
  - control-plane:`tools/deploy/build-push.sh --images control-plane --tag <mainsha> --push`
  - admin-ui 环境专属:`--images admin-ui --tag <mainsha>-test --push
    --oidc-issuer https://expert-work-test.deepaihealth.com/kc/realms/expert-work
    --oidc-client-id expert-work-admin-ui --oidc-audience expert-work-api-internal`
  - overlay `images:` newTag 分别指 `<mainsha>` / `<mainsha>-test`(admin-ui 与 control-plane
    tag 不同,kustomization images 两条目本来就分开)

## Phase B — 数据面前置

- [x] B1 RDS 建 `keycloak` 库(经集群内 anolisos 探针 pod psql;owner=expert_work_dev,
  测试环境不另立用户)
- [x] B2 secret 真值生成(全机器生成,用户零填):
  - `EXPERT_WORK_SECRET_ENCRYPTION_KEY` = `openssl rand -base64 32`
  - `EXPERT_WORK_APIKEY_RATE_LIMIT_HMAC_SALT` = `openssl rand -hex 32`
  - `EXPERT_WORK_SETUP_TOKEN` = `openssl rand -hex 32`
  - `KEYCLOAK_ADMIN=expert-work-admin`,`KEYCLOAK_ADMIN_PASSWORD` = `openssl rand -base64 24`
  - DSN/Redis URL 密码 = params.env 真值;OSS AK/SK = params.env(dotenv 金库文件)
  - 归档到 `~/.kube/expert-work-test-secrets.env`(600,不进 git)

## Phase C — 部署

- [x] C1 `kubectl apply` namespace(base 携带)→ 建 3 个 Secret
  (`control-plane-secrets` / `keycloak-secrets` env 型;`control-plane-secret-files`
  --from-file dotenv 金库,配方在 overlays/test/secrets.env.example)
- [x] C2 `kustomize edit set image` 双 tag → `kubectl apply -k infra/k8s/overlays/test`
- [x] C3 migrate Job 完成(Job immutable:后续重发布 delete-then-create;ConfigMap 无 hash,
  改配置须 `kubectl rollout restart`)
- [x] C4 Keycloak ready(startupProbe 5min 预算,首次 realm import 慢)

## Phase D — Keycloak 初始化

- [x] D1 kcadm:`expert-work-admin-ui` client 追加 redirectUris/webOrigins
  `https://expert-work-test.deepaihealth.com/*`;realm unmanagedAttributePolicy=ENABLED
- [x] D2 (与 F 后)setup wizard 走 SETUP_TOKEN 建首租户

## Phase E — 入口

- [x] E1 AlbConfig merge patch(443 + 证书 + 超时)
- [x] E2 Ingress 分到 ALB(status.address 出现)
- [x] E3 **用户动作**:DNS CNAME `expert-work-test` →
  `alb-cv3xokwot6r9bgojzx.cn-hangzhou.alb.aliyuncsslb.com`

## Phase F — 验证(DNS 生效前用 `curl --resolve` 提前跑)

- [x] F1 `/.well-known/openid-configuration` issuer 逐字节 ==
  `https://expert-work-test.deepaihealth.com/kc/realms/expert-work`
- [x] F2 `/v1/healthz` 200;admin-ui `/` 出 SPA;`/kc` 出 Keycloak
- [ ] F3 冒烟(需用户):OIDC 登录 → setup → 后台粘 LLM key → 建 agent 跑 run;
  SSE 长流看 ALB 是否 180s 砍(砍则工单)
